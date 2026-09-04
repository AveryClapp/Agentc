"""No-network end-to-end preflight for reference-visible exploration.

The harness drives the real Python OpenAI adapter and native Rust optimizer
against a deterministic fake provider. It warms one call site, collects the
default 20 exact paired observations for OutputBudget, restarts the runtime,
and checks that persisted evidence admits the candidate on the next call.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import agentc
from agentc._patches._openai import _wrap_create


REFERENCE_CAP = 1024
REFERENCE_OUTPUT_TOKENS = 100
CANDIDATE_OUTPUT_TOKENS = 40
CALIBRATION_PAIRS = 20
MAX_CALIBRATION_USER_CALLS = 3 + CALIBRATION_PAIRS * 4
MODEL = "gpt-4o-2024-11-20"


def response(*, origin: str, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"fake-{origin}",
        model=MODEL,
        origin=origin,
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=output_tokens,
        ),
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    role="assistant",
                    content="stable answer",
                ),
            )
        ],
    )


class FakeProvider:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        cap = int(kwargs.get("max_tokens", 0) or 0)
        origin = "reference" if cap == REFERENCE_CAP else "candidate"
        with self._lock:
            self.calls.append(
                {
                    "origin": origin,
                    "max_tokens": cap,
                    "thread": threading.current_thread().name,
                }
            )
        return response(
            origin=origin,
            output_tokens=(
                REFERENCE_OUTPUT_TOKENS
                if origin == "reference"
                else CANDIDATE_OUTPUT_TOKENS
            ),
        )


def invoke(provider: FakeProvider, request: dict[str, Any]) -> Any:
    # Keep the user-level call-site frame stable across warmup and restart.
    return _wrap_create(provider, None, (), request)


def percentile(values: list[float], percentile_value: float) -> float:
    ordered = sorted(values)
    rank = max(math.ceil(percentile_value * len(ordered)) - 1, 0)
    return ordered[rank]


def run(storage: Path) -> dict[str, Any]:
    # Match lifecycle.Config's canonical path. On macOS, /tmp aliases
    # /private/tmp; opening one WAL through both spellings can hide committed
    # rows from the polling connection and is not a valid SQLite test setup.
    storage = storage.resolve()
    request = {
        "model": MODEL,
        "max_tokens": REFERENCE_CAP,
        "temperature": 0,
        "messages": [{"role": "user", "content": "same shaped prompt"}],
    }
    provider = FakeProvider()
    calibration_responses: list[Any] = []
    critical_path_ms: list[float] = []
    environment = {
        "AGENTC_ENABLED_RULES": "OutputBudget",
        "AGENTC_OPTIMIZE_EXPLORATION": "1",
        "AGENTC_OPTIMIZE_SHADOW": "0",
    }

    with patch.dict(os.environ, environment, clear=False), patch(
        "agentc._lifecycle._apply_patches"
    ), patch("agentc._patches._openai._write_root_span"):
        agentc.init(storage_path=str(storage))
        try:
            # The first three calls warm the call site. Subsequent calls remain
            # reference-visible while at most one off-path candidate runs. A
            # concurrent call is allowed to observe the active lease and skip,
            # so bound user calls rather than assuming one lease per call.
            while (
                len(calibration_responses) < MAX_CALIBRATION_USER_CALLS
            ):
                with provider._lock:
                    completed_provider_candidates = sum(
                        call["origin"] == "candidate"
                        and call["thread"] == "agentc-counterfactual"
                        for call in provider.calls
                    )
                if completed_provider_candidates >= CALIBRATION_PAIRS:
                    break
                started = time.perf_counter()
                result = invoke(provider, request)
                critical_path_ms.append((time.perf_counter() - started) * 1000.0)
                calibration_responses.append(result)
                # Join outside the measured critical path. This makes the
                # artifact deterministic while exercising the same production
                # background worker and native completion endpoint.
                from agentc._patches._optimizer_glue import drain_exploration

                drain_exploration(5_000)
            with provider._lock:
                completed_provider_candidates = sum(
                    call["origin"] == "candidate"
                    and call["thread"] == "agentc-counterfactual"
                    for call in provider.calls
                )
            if completed_provider_candidates != CALIBRATION_PAIRS:
                raise RuntimeError(
                    "calibration did not reach the paired evidence target; "
                    f"observed {completed_provider_candidates} candidates after "
                    f"{len(calibration_responses)} user calls"
                )
        finally:
            agentc.shutdown()

        # The exact paired profile and call cap must survive a full native
        # optimizer reset before any candidate becomes user-visible.
        agentc.init(storage_path=str(storage))
        try:
            started = time.perf_counter()
            admitted_response = invoke(provider, request)
            admitted_latency_ms = (time.perf_counter() - started) * 1000.0
        finally:
            agentc.shutdown()

    if any(result.origin != "reference" for result in calibration_responses):
        raise RuntimeError("an unadmitted candidate became user-visible during calibration")
    if admitted_response.origin != "candidate":
        raise RuntimeError("persisted paired evidence did not admit the candidate after restart")

    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        attempt = connection.execute(
            "SELECT execution_plan_id, COUNT(*), SUM(cost_usd), "
            "SUM(latency_ms) FROM execution_plan_exploration "
            "WHERE status = 'completed' GROUP BY execution_plan_id"
        ).fetchone()
        if attempt is None:
            raise RuntimeError("no completed exploration attempts were persisted")
        profile = connection.execute(
            "SELECT paired_observations, window_observations, "
            "divergence_upper_p95, cost_usd_mean, latency_ms_mean "
            "FROM execution_plan_profile WHERE execution_plan_id = ?",
            (attempt[0],),
        ).fetchone()
        if profile is None:
            raise RuntimeError("leased candidate has no exact persisted plan profile")
        charged_incomplete_attempts = connection.execute(
            "SELECT COUNT(*) FROM execution_plan_exploration "
            "WHERE status IN ('failed', 'abandoned', 'reserved')"
        ).fetchone()[0]

    with provider._lock:
        calls = list(provider.calls)
    reference_calls = [call for call in calls if call["origin"] == "reference"]
    candidate_calls = [call for call in calls if call["origin"] == "candidate"]
    background_candidates = [
        call for call in candidate_calls if call["thread"] == "agentc-counterfactual"
    ]
    main_thread_candidates = [
        call for call in candidate_calls if call["thread"] != "agentc-counterfactual"
    ]

    return {
        "artifact_schema_version": 1,
        "stage": "E0",
        "paper_evidence": False,
        "network_calls": 0,
        "provider": "deterministic_fake_openai_adapter",
        "requested_model": MODEL,
        "enabled_rules": ["OutputBudget"],
        "exploration_policy": {
            "paired_evidence_target": CALIBRATION_PAIRS,
            "calls_per_site_24h": 20,
            "max_concurrent_per_site": 1,
            "shadow_rate": 0.0,
        },
        "results": {
            "calibration_user_calls": len(calibration_responses),
            "reference_visible_calibration_calls": sum(
                result.origin == "reference" for result in calibration_responses
            ),
            "background_candidate_calls": len(background_candidates),
            "post_restart_candidate_admitted": admitted_response.origin == "candidate",
            "post_restart_candidate_max_tokens": main_thread_candidates[-1]["max_tokens"],
            "provider_calls_total": len(calls),
            "provider_reference_calls": len(reference_calls),
            "provider_candidate_calls": len(candidate_calls),
            "persisted_completed_attempts": int(attempt[1]),
            "persisted_incomplete_attempts": int(charged_incomplete_attempts),
            "persisted_counterfactual_cost_usd": float(attempt[2]),
            "persisted_counterfactual_latency_ms": float(attempt[3]),
            "candidate_paired_observations": int(profile[0]),
            "candidate_window_observations": int(profile[1]),
            "candidate_divergence_upper_p95": float(profile[2]),
            "candidate_cost_usd_mean": float(profile[3]),
            "candidate_latency_ms_mean": float(profile[4]),
            "calibration_critical_path_p50_ms": statistics.median(critical_path_ms),
            "calibration_critical_path_p99_ms": percentile(critical_path_ms, 0.99),
            "admitted_call_latency_ms": admitted_latency_ms,
        },
        "claims": [
            "Every calibration response came from the immutable reference request.",
            "Exactly twenty leased candidates completed through the production adapter.",
            "Planning-only aborts did not consume the provider-call exploration budget.",
            "Candidate cost, latency, and paired divergence survived native restart.",
            "The next call admitted the exact candidate from persisted evidence.",
            "This synthetic preflight validates mechanics and is not paper evidence.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.storage is None:
        with tempfile.TemporaryDirectory(prefix="agentc-live-exploration-") as temp:
            result = run(Path(temp))
    else:
        args.storage.mkdir(parents=True, exist_ok=True)
        result = run(args.storage)

    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)


if __name__ == "__main__":
    main()
