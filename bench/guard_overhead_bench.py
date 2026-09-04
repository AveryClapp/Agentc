"""Microbenchmark the synchronous complete-plan feedback path.

For each *fresh* sampled execution this benchmark measures:

  1. ``_text_divergence(a, b)`` -- the Python output-divergence metric.
  2. ``record_divergence(token, d)`` -- opaque-token validation, the paired
     complete-plan profile update, the exact-plan exposure guard, and the
     synchronous SQLite durability boundary used by production.

Every measured token is issued by ``optimize_observe`` for a canonical
ContextCompress+OutputBudget plan. Replaying one token would measure the
idempotence fast path, not the cost of accepting a new shadow sample.

The shadow provider call itself is deliberately excluded. This is a no-network
Stage E0 engineering benchmark, not paper evidence. Run with:

    python -m bench.guard_overhead_bench

Set ``AGENTC_GUARD_BENCH_ITERS`` to shorten or lengthen the fresh-token run.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import agentc
from agentc import _native
from agentc._optimizer import observe_outcome, plan_call, record_divergence
from agentc._patches._optimizer_glue import _text_divergence


_CALL_SITE = "complete-plan-guard-overhead"
_DIVERGENCE = 0.05
_THRESHOLD = 0.10
_DEFAULT_FRESH_TOKENS = 2_000
_DEFAULT_METRIC_ITERS = 50_000
_DEFAULT_WARMUP_TOKENS = 100

# Representative outputs: a ~60-token answer and a benign elaboration of it.
BASE = (
    "The Kansas Song, also titled We're From Kansas, was adopted as the official "
    "state song. It was written in the early twentieth century and is performed at "
    "state ceremonies and university events across the region to this day."
)
ELAB = BASE + " The melody is widely recognized throughout the midwestern United States."


def _call() -> dict[str, Any]:
    big_dead_context = "historical context " * 700
    return {
        "call_site_id": _CALL_SITE,
        "trace_id": "0" * 32,
        "span_id": "0" * 16,
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "Answer the user's question."},
            {"role": "user", "content": "What is the answer?"},
            {"role": "user", "content": big_dead_context},
        ],
        "parameters": {
            "max_output_tokens": 512,
            "extra": {
                "attention_scores": [1.0, 1.0, 0.0],
                "message_deps": [
                    {"kind": "literal"},
                    {"kind": "user_input", "span_id": "0102030405060708"},
                    {"kind": "literal"},
                ],
                "follow_on_tokens": [],
                "dead_attention_epsilon": 0.10,
                "agentc_route_context": {
                    "provider_protocol": "openai.chat.completions.v1",
                    "provider_namespace": "openai",
                    "input_tokens_upper_bound": 5_000,
                    "image_input": False,
                    "tool_calling": False,
                    "structured_outputs": False,
                    "streaming": False,
                },
            },
        },
        "tools": [],
        "input_deps": [],
        "occurrence_ix": 0,
    }


def _outcome() -> dict[str, Any]:
    return {
        "input_tokens": 5_000,
        "output_tokens": 200,
        "latency_ms": 1_000.0,
        "cost_usd": 0.05,
        "output_is_structured": False,
        "output_is_short": False,
        "call_site_id": _CALL_SITE,
    }


def _git_commit() -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parent.parent), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _composed_plan_json() -> str:
    call = _call()
    outcome = _outcome()
    for _ in range(3):
        observe_outcome(plan_call(call), outcome)
    plan = plan_call(call)
    if plan.kind != "composed" or set(plan.rules or []) != {
        "ContextCompress",
        "OutputBudget",
    }:
        raise RuntimeError(
            "benchmark fixture did not produce the required canonical composed plan"
        )
    return plan.raw_json


def _fresh_tokens(plan_json: str, count: int) -> list[str]:
    outcome_json = json.dumps(_outcome())
    tokens = [
        str(_native.optimize_observe(plan_json, outcome_json)) for _ in range(count)
    ]
    if any(not token for token in tokens):
        raise RuntimeError("failed to issue a fresh optimizer observation token")
    return tokens


def _bench(fn: Callable[[], object], iters: int) -> dict[str, float]:
    for _ in range(2_000):
        fn()
    samples: list[int] = []
    for _ in range(iters):
        t0 = time.perf_counter_ns()
        fn()
        samples.append(time.perf_counter_ns() - t0)
    return _summarize(samples)


def _bench_fresh_tokens(tokens: Sequence[str]) -> dict[str, float]:
    samples: list[int] = []
    for token in tokens:
        t0 = time.perf_counter_ns()
        record_divergence(token, _DIVERGENCE)
        samples.append(time.perf_counter_ns() - t0)
    return _summarize(samples)


def _summarize(samples: list[int]) -> dict[str, float]:
    samples.sort()
    return {
        "mean_us": statistics.mean(samples) / 1e3,
        "p50_us": samples[len(samples) // 2] / 1e3,
        "p99_us": samples[min(len(samples) - 1, int(len(samples) * 0.99))] / 1e3,
    }


def _verify_complete_plan_guard(storage: Path) -> dict[str, int | float]:
    _native.optimize_flush()
    with sqlite3.connect(storage / "cost_model.db") as connection:
        guard = connection.execute(
            "SELECT COUNT(*), MIN(divergence_threshold), "
            "MAX(divergence_exposure), SUM(window_samples) "
            "FROM execution_plan_guard"
        ).fetchone()
        legacy_rows = connection.execute(
            "SELECT COUNT(*) FROM rule_divergence WHERE call_site_id = ?",
            (_CALL_SITE,),
        ).fetchone()
    if guard is None or legacy_rows is None:
        raise RuntimeError("guard verification query returned no row")
    result = {
        "plan_guard_rows": int(guard[0]),
        "divergence_threshold": float(guard[1]),
        "divergence_exposure": float(guard[2]),
        "positive_exposure_events": int(guard[3]),
        "legacy_rule_rows": int(legacy_rows[0]),
    }
    if result != {
        "plan_guard_rows": 1,
        "divergence_threshold": _THRESHOLD,
        "divergence_exposure": 0.0,
        "positive_exposure_events": 0,
        "legacy_rule_rows": 0,
    }:
        raise RuntimeError(f"benchmark did not isolate the complete-plan guard: {result}")
    return result


def run(*, fresh_tokens: int, metric_iters: int, warmup_tokens: int) -> dict[str, Any]:
    """Run the isolated metric and fresh-feedback measurements."""
    if min(fresh_tokens, metric_iters, warmup_tokens) <= 0:
        raise ValueError("iteration and warmup counts must be positive")
    settings = {
        "AGENTC_ENABLED_RULES": "ContextCompress,OutputBudget",
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_SHADOW": "0.02",
        "AGENTC_SHADOW_DIVERGENCE_BUDGET": str(_THRESHOLD),
        "AGENTC_SHADOW_DIVERGENCE_MODE": "normalized",
    }

    metric = _bench(lambda: _text_divergence(BASE, ELAB), metric_iters)
    with tempfile.TemporaryDirectory(prefix="agentc-guard-overhead-") as temp:
        storage = Path(temp).resolve()
        with patch.dict(os.environ, settings):
            agentc.init(storage_path=str(storage))
            try:
                plan_json = _composed_plan_json()
                for token in _fresh_tokens(plan_json, warmup_tokens):
                    record_divergence(token, _DIVERGENCE)
                feedback = _bench_fresh_tokens(_fresh_tokens(plan_json, fresh_tokens))
                guard_state = _verify_complete_plan_guard(storage)
            finally:
                agentc.shutdown()

    return {
        "schema_version": 1,
        "experiment_kind": "complete_plan_guard_overhead_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "agentc_git_commit": _git_commit(),
        "paper_evidence": False,
        "network_calls": 0,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "settings": {
            "enabled_rules": ["ContextCompress", "OutputBudget"],
            "plan_kind": "composed",
            "divergence_metric": "normalized_containment",
            "divergence_sample": _DIVERGENCE,
            "divergence_threshold": _THRESHOLD,
            "metric_iterations": metric_iters,
            "fresh_feedback_tokens": fresh_tokens,
            "feedback_warmup_tokens": warmup_tokens,
        },
        "measurements_us": {
            "divergence_metric": metric,
            "fresh_complete_plan_feedback": feedback,
            "estimated_combined_mean": metric["mean_us"] + feedback["mean_us"],
        },
        "validated_state": guard_state,
        "timed_scope": [
            "Python normalized-containment divergence metric",
            "opaque observation-token validation",
            "exact complete-plan paired-profile update",
            "exact complete-plan exposure-guard update",
            "synchronous SQLite durability for accepted feedback",
        ],
        "excluded_scope": [
            "primary provider call",
            "shadow provider call",
            "plan selection and request dispatch",
            "network latency and billed tokens",
        ],
        "interpretation_limits": [
            "This is a single-machine Stage E0 engineering measurement, not paper evidence.",
            "The benchmark uses deterministic synthetic optimizer inputs and performs no network calls.",
            "The combined mean sums separately timed metric and feedback paths; it is not an end-to-end request latency.",
            "Provider counterfactual latency and cost dominate sampled requests and must be measured separately.",
        ],
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fresh-tokens",
        type=_positive_int,
        default=int(
            os.environ.get("AGENTC_GUARD_BENCH_ITERS", _DEFAULT_FRESH_TOKENS)
        ),
    )
    parser.add_argument(
        "--metric-iters", type=_positive_int, default=_DEFAULT_METRIC_ITERS
    )
    parser.add_argument(
        "--warmup-tokens", type=_positive_int, default=_DEFAULT_WARMUP_TOKENS
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = json.dumps(
        run(
            fresh_tokens=args.fresh_tokens,
            metric_iters=args.metric_iters,
            warmup_tokens=args.warmup_tokens,
        ),
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
