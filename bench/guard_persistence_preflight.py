"""No-network preflight for durable complete-plan guard evidence.

The experiment warms one canonical ContextCompress+OutputBudget plan, records
two above-threshold comparisons (0.8 cumulative exposure), restarts Agentc,
and records a third comparison (1.2 cumulative exposure). It passes only when
the pre-restart exposure is already durable, the third comparison disables the
exact composed plan, no constituent legacy rule is disabled, and the exact-plan
decision survives another restart.

This is Stage E0 engineering evidence, not a provider or paper result.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


_REPO_ROOT = Path(__file__).resolve().parent.parent
_CALL_SITE = "complete-plan-guard-persistence-preflight"
_RULES = ("ContextCompress", "OutputBudget")
_DIVERGENCE = 0.5
_THRESHOLD = 0.1
_EXCESS_PER_SAMPLE = _DIVERGENCE - _THRESHOLD
_EXPOSURE_BUDGET = 1.0
_SQLITE_QUERY = """\
import json
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    row = connection.execute(sys.argv[2], sys.argv[3:]).fetchone()
print(json.dumps(row))
"""


def _git_commit() -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


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


def _query_row(db_path: Path, query: str, *parameters: str) -> list[Any] | None:
    """Read through a separate process to avoid mixed SQLite-library caches."""
    completed = subprocess.run(
        [sys.executable, "-c", _SQLITE_QUERY, str(db_path), query, *parameters],
        check=True,
        capture_output=True,
        text=True,
    )
    decoded: object = json.loads(completed.stdout)
    if decoded is None:
        return None
    if not isinstance(decoded, list):
        raise TypeError("SQLite probe returned a non-row JSON value")
    return decoded


def _plan_guard_row(db_path: Path) -> dict[str, int | float | str] | None:
    row = _query_row(
        db_path,
        "SELECT call_site_version, execution_plan_id, divergence_threshold, "
        "divergence_exposure, window_samples, provider_protocol, "
        "target_model_id, target_model_version, price_table_version "
        "FROM execution_plan_guard",
    )
    if row is None:
        return None
    return {
        "call_site_version": str(row[0]),
        "execution_plan_id": str(row[1]),
        "divergence_threshold": float(row[2]),
        "divergence_exposure": float(row[3]),
        "window_samples": int(row[4]),
        "provider_protocol": str(row[5]),
        "target_model_id": str(row[6]),
        "target_model_version": str(row[7]),
        "price_table_version": str(row[8]),
    }


def _plan_disabled_row(db_path: Path) -> dict[str, int | float | str] | None:
    row = _query_row(
        db_path,
        "SELECT call_site_version, execution_plan_id, reason, exposure, "
        "disabled_at, reenable_at FROM execution_plan_disabled",
    )
    if row is None:
        return None
    return {
        "call_site_version": str(row[0]),
        "execution_plan_id": str(row[1]),
        "reason": str(row[2]),
        "exposure": float(row[3]),
        "disabled_at_us": int(row[4]),
        "reenable_at_us": int(row[5]),
    }


def _legacy_row_counts(db_path: Path) -> dict[str, int]:
    divergence = _query_row(
        db_path,
        "SELECT COUNT(*) FROM rule_divergence WHERE call_site_id = ?",
        _CALL_SITE,
    )
    disabled = _query_row(
        db_path,
        "SELECT COUNT(*) FROM optimizer_disabled WHERE call_site_id = ?",
        _CALL_SITE,
    )
    if divergence is None or disabled is None:
        raise RuntimeError("legacy guard count query returned no row")
    return {
        "rule_divergence_rows": int(divergence[0]),
        "rule_disabled_rows": int(disabled[0]),
    }


def _plan_summary(plan: Any) -> dict[str, Any]:
    return {
        "kind": plan.kind,
        "rules": sorted(plan.rules or ([plan.rule] if plan.rule else [])),
    }


def _record_sample(plan_call: Any, observe_outcome: Any, record_divergence: Any) -> Any:
    selected = plan_call(_call())
    token = observe_outcome(selected, _outcome())
    assert token is not None
    record_divergence(token, _DIVERGENCE)
    return selected


def _assert_composed(summary: dict[str, Any]) -> None:
    assert summary == {"kind": "composed", "rules": sorted(_RULES)}


def _assert_guard_state(
    state: dict[str, int | float | str] | None,
    *,
    exposure: float,
    samples: int,
) -> None:
    assert state is not None
    assert math.isclose(float(state["divergence_threshold"]), _THRESHOLD)
    assert math.isclose(float(state["divergence_exposure"]), exposure)
    assert state["window_samples"] == samples
    assert state["provider_protocol"] == "openai.chat.completions.v1"
    assert state["target_model_id"] == "gpt-4o"


def run() -> dict[str, Any]:
    """Execute the two-restart complete-plan persistence experiment."""
    settings = {
        "AGENTC_ENABLED_RULES": ",".join(_RULES),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_SHADOW": "0.02",
        "AGENTC_SHADOW_DIVERGENCE_BUDGET": str(_THRESHOLD),
    }
    with tempfile.TemporaryDirectory(prefix="agentc-guard-persistence-") as temp:
        storage = Path(temp).resolve()
        db_path = storage / "cost_model.db"

        import agentc
        from agentc._optimizer import observe_outcome, plan_call, record_divergence

        with patch.dict(os.environ, settings):
            agentc.init(storage_path=str(storage))
            try:
                first_native_storage = agentc._native.optimize_storage_path()
                warmup_plans: list[str] = []
                for _ in range(3):
                    plan = plan_call(_call())
                    warmup_plans.append(plan.kind)
                    observe_outcome(plan, _outcome())
                pre_restart_plans = [
                    _plan_summary(
                        _record_sample(plan_call, observe_outcome, record_divergence)
                    )
                    for _ in range(2)
                ]
                guard_before_restart = _plan_guard_row(db_path)
                disabled_before_restart = _plan_disabled_row(db_path)
                legacy_before_restart = _legacy_row_counts(db_path)
            finally:
                agentc.shutdown()

            agentc.init(storage_path=str(storage))
            try:
                second_native_storage = agentc._native.optimize_storage_path()
                plan_before_budget_crossing = plan_call(_call())
                crossing_token = observe_outcome(plan_before_budget_crossing, _outcome())
                assert crossing_token is not None
                record_divergence(crossing_token, _DIVERGENCE)
                plan_after_budget_crossing = plan_call(_call())
                guard_after_disable = _plan_guard_row(db_path)
                disabled_after_disable = _plan_disabled_row(db_path)
                legacy_after_disable = _legacy_row_counts(db_path)
            finally:
                agentc.shutdown()

            agentc.init(storage_path=str(storage))
            try:
                third_native_storage = agentc._native.optimize_storage_path()
                plan_after_second_restart = plan_call(_call())
            finally:
                agentc.shutdown()

        result: dict[str, Any] = {
            "native_storage_match": all(
                path == str(storage)
                for path in (
                    first_native_storage,
                    second_native_storage,
                    third_native_storage,
                )
            ),
            "warmup_plan_kinds": warmup_plans,
            "pre_restart_plans": pre_restart_plans,
            "guard_before_restart": guard_before_restart,
            "disabled_before_restart": disabled_before_restart,
            "legacy_before_restart": legacy_before_restart,
            "plan_before_budget_crossing": _plan_summary(plan_before_budget_crossing),
            "plan_after_budget_crossing": _plan_summary(plan_after_budget_crossing),
            "guard_after_disable": guard_after_disable,
            "disabled_after_disable": disabled_after_disable,
            "legacy_after_disable": legacy_after_disable,
            "plan_after_second_restart": _plan_summary(plan_after_second_restart),
        }

    assert result["warmup_plan_kinds"] == ["pass_through"] * 3
    assert result["native_storage_match"]
    for summary in result["pre_restart_plans"]:
        _assert_composed(summary)
    _assert_guard_state(
        result["guard_before_restart"],
        exposure=2 * _EXCESS_PER_SAMPLE,
        samples=2,
    )
    assert result["disabled_before_restart"] is None
    assert result["legacy_before_restart"] == {
        "rule_divergence_rows": 0,
        "rule_disabled_rows": 0,
    }
    _assert_composed(result["plan_before_budget_crossing"])
    assert result["plan_after_budget_crossing"] == {
        "kind": "pass_through",
        "rules": [],
    }
    _assert_guard_state(
        result["guard_after_disable"],
        exposure=3 * _EXCESS_PER_SAMPLE,
        samples=3,
    )
    disabled = result["disabled_after_disable"]
    assert disabled is not None
    assert disabled["reason"] == "divergence_exposure"
    assert math.isclose(float(disabled["exposure"]), 3 * _EXCESS_PER_SAMPLE)
    assert disabled["call_site_version"] == result["guard_after_disable"][
        "call_site_version"
    ]
    assert disabled["execution_plan_id"] == result["guard_after_disable"][
        "execution_plan_id"
    ]
    assert disabled["reenable_at_us"] > disabled["disabled_at_us"]
    assert result["legacy_after_disable"] == {
        "rule_divergence_rows": 0,
        "rule_disabled_rows": 0,
    }
    assert result["plan_after_second_restart"] == {
        "kind": "pass_through",
        "rules": [],
    }

    return {
        "schema_version": 2,
        "experiment_kind": "complete_plan_guard_persistence_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "agentc_git_commit": _git_commit(),
        "paper_evidence": False,
        "network_calls": 0,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "settings": {
            "enabled_rules": list(_RULES),
            "hot_threshold": 3,
            "max_planning_overhead_ms": 1000,
            "divergence_threshold": _THRESHOLD,
            "divergence_sample": _DIVERGENCE,
            "excess_per_sample": _EXCESS_PER_SAMPLE,
            "plan_exposure_budget": _EXPOSURE_BUDGET,
            "pre_restart_samples": 2,
            "post_restart_samples": 1,
            "process_reinitializations": 2,
            "sqlite_probe_process": "separate child process",
        },
        "result": result,
        "interpretation_limits": [
            "This proves exposure persistence and restart behavior for one exact composed plan only.",
            "Calls, outcomes, and divergences are deterministic synthetic optimizer inputs; no LLM or provider is invoked.",
            "The raised planning ceiling is an activation control, not the production setting.",
            "This does not measure guard precision, recall, semantic quality, or task-equivalent damage.",
            "SQLite rows are read in a child process to avoid same-process cache interactions between Python SQLite and rusqlite's bundled SQLite.",
        ],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = json.dumps(run(), indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
