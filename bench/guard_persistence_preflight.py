"""No-network preflight for durable shadow-guard evidence.

The experiment warms one OutputBudget call site, records four consecutive
over-budget divergence samples, restarts Agentc, and records the fifth sample.
It passes only when the pre-restart samples are already durable, the fifth
sample disables the rule, and that disabled decision survives another restart.

This is Stage E0 engineering evidence, not a provider or paper result.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parent.parent
_CALL_SITE = "guard-persistence-preflight"
_RULE = "OutputBudget"
_DIVERGENCE = 0.5
_BUDGET = 0.1
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
    return {
        "call_site_id": _CALL_SITE,
        "trace_id": "0" * 32,
        "span_id": "0" * 16,
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "synthetic guard preflight"}],
        "parameters": {"max_output_tokens": 256},
        "tools": [],
        "input_deps": [],
        "occurrence_ix": 0,
    }


def _outcome() -> dict[str, Any]:
    return {
        "input_tokens": 10,
        "output_tokens": 20,
        "latency_ms": 1.0,
        "cost_usd": 0.001,
        "output_is_structured": False,
        "output_is_short": True,
        "call_site_id": _CALL_SITE,
    }


def _query_row(db_path: Path, query: str) -> list[Any] | None:
    """Read through a separate process to avoid mixed SQLite-library caches."""
    completed = subprocess.run(
        [sys.executable, "-c", _SQLITE_QUERY, str(db_path), query, _CALL_SITE, _RULE],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _divergence_row(db_path: Path) -> dict[str, int | float] | None:
    row = _query_row(
        db_path,
        "SELECT n_samples, divergence_mean, divergence_var, "
        "consecutive_breaches FROM rule_divergence "
        "WHERE call_site_id = ? AND rule = ?",
    )
    if row is None:
        return None
    return {
        "n_samples": int(row[0]),
        "divergence_mean": float(row[1]),
        "divergence_var": float(row[2]),
        "consecutive_breaches": int(row[3]),
    }


def _disabled_row(db_path: Path) -> dict[str, int | str] | None:
    row = _query_row(
        db_path,
        "SELECT reason, disabled_at, reenable_at "
        "FROM optimizer_disabled "
        "WHERE call_site_id = ? AND rule = ?",
    )
    if row is None:
        return None
    return {
        "reason": str(row[0]),
        "disabled_at_us": int(row[1]),
        "reenable_at_us": int(row[2]),
    }


def run() -> dict[str, Any]:
    """Execute the two-restart guard-persistence experiment."""
    with tempfile.TemporaryDirectory(prefix="agentc-guard-persistence-") as temp:
        storage = Path(temp).resolve()
        db_path = storage / "cost_model.db"
        os.environ.update(
            {
                "AGENTC_ENABLED_RULES": _RULE,
                "AGENTC_OPTIMIZE": "1",
                "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3",
                "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
                "AGENTC_OPTIMIZE_SHADOW": "0",
                "AGENTC_SHADOW_DIVERGENCE_BUDGET": str(_BUDGET),
            }
        )

        import agentc
        from agentc._optimizer import observe_outcome, plan_call, record_divergence

        agentc.init(storage_path=str(storage))
        first_native_storage = agentc._native.optimize_storage_path()
        warmup_plans: list[str] = []
        for _ in range(3):
            plan = plan_call(_call())
            warmup_plans.append(plan.kind)
            observe_outcome(plan, _outcome())
        hot_plan_before_guard = plan_call(_call())
        for _ in range(4):
            record_divergence(_CALL_SITE, _RULE, _DIVERGENCE)
        divergence_before_restart = _divergence_row(db_path)
        disabled_before_restart = _disabled_row(db_path)
        agentc.shutdown()

        agentc.init(storage_path=str(storage))
        second_native_storage = agentc._native.optimize_storage_path()
        plan_before_fifth_breach = plan_call(_call())
        record_divergence(_CALL_SITE, _RULE, _DIVERGENCE)
        plan_after_fifth_breach = plan_call(_call())
        divergence_after_disable = _divergence_row(db_path)
        disabled_after_disable = _disabled_row(db_path)
        agentc.shutdown()

        agentc.init(storage_path=str(storage))
        third_native_storage = agentc._native.optimize_storage_path()
        plan_after_second_restart = plan_call(_call())
        agentc.shutdown()

        result = {
            "native_storage_match": all(
                path == str(storage)
                for path in (
                    first_native_storage,
                    second_native_storage,
                    third_native_storage,
                )
            ),
            "warmup_plan_kinds": warmup_plans,
            "hot_plan_before_guard": {
                "kind": hot_plan_before_guard.kind,
                "rule": hot_plan_before_guard.rule,
            },
            "divergence_before_restart": divergence_before_restart,
            "disabled_before_restart": disabled_before_restart,
            "plan_before_fifth_breach": {
                "kind": plan_before_fifth_breach.kind,
                "rule": plan_before_fifth_breach.rule,
            },
            "plan_after_fifth_breach": {
                "kind": plan_after_fifth_breach.kind,
                "rule": plan_after_fifth_breach.rule,
            },
            "divergence_after_disable": divergence_after_disable,
            "disabled_after_disable": disabled_after_disable,
            "plan_after_second_restart": {
                "kind": plan_after_second_restart.kind,
                "rule": plan_after_second_restart.rule,
            },
        }

    assert result["warmup_plan_kinds"] == ["pass_through"] * 3
    assert result["native_storage_match"]
    assert result["hot_plan_before_guard"] == {
        "kind": "rewritten",
        "rule": _RULE,
    }
    assert result["divergence_before_restart"] == {
        "n_samples": 4,
        "divergence_mean": _DIVERGENCE,
        "divergence_var": 0.0,
        "consecutive_breaches": 4,
    }
    assert result["disabled_before_restart"] is None
    assert result["plan_before_fifth_breach"] == {
        "kind": "rewritten",
        "rule": _RULE,
    }
    assert result["plan_after_fifth_breach"] == {
        "kind": "pass_through",
        "rule": None,
    }
    assert result["divergence_after_disable"] == {
        "n_samples": 5,
        "divergence_mean": _DIVERGENCE,
        "divergence_var": 0.0,
        "consecutive_breaches": 0,
    }
    assert result["disabled_after_disable"] is not None
    assert result["disabled_after_disable"]["reason"] == "shadow_divergence"
    assert (
        result["disabled_after_disable"]["reenable_at_us"]
        > result["disabled_after_disable"]["disabled_at_us"]
    )
    assert result["plan_after_second_restart"] == {
        "kind": "pass_through",
        "rule": None,
    }

    return {
        "schema_version": 1,
        "experiment_kind": "guard_persistence_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "agentc_git_commit": _git_commit(),
        "paper_evidence": False,
        "network_calls": 0,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "settings": {
            "enabled_rules": [_RULE],
            "hot_threshold": 3,
            "max_planning_overhead_ms": 1000,
            "divergence_budget": _BUDGET,
            "divergence_sample": _DIVERGENCE,
            "breach_streak": 5,
            "process_reinitializations": 2,
            "sqlite_probe_process": "separate child process",
        },
        "result": result,
        "interpretation_limits": [
            "This proves persistence and restart behavior for one rule and one call site only.",
            "Calls, outcomes, and divergences are deterministic synthetic optimizer inputs; no LLM or provider is invoked.",
            "The raised planning ceiling is an activation control, not the production setting.",
            "This does not measure guard precision, recall, semantic quality, or cumulative damage.",
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
