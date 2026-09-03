"""No-network preflight for Agentc lifecycle storage ownership.

The experiment warms the native optimizer in store A, shuts Agentc down, and
reinitializes the same Python process with store B.  A decoy
``AGENTC_STORAGE_PATH`` is present before each programmatic init.  The run
passes only when Python tracing, native optimizer state, audit databases, and
environment restoration all agree and store B begins cold.

This is Stage E0 engineering evidence, not a provider or paper result.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parent.parent
_CALL_SITE = "storage-isolation-preflight"


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
        "messages": [{"role": "user", "content": "synthetic warmup"}],
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


def _table_count(path: Path, table: str) -> int:
    if not path.exists():
        return 0
    with sqlite3.connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def run() -> dict[str, Any]:
    """Execute the A-to-B storage isolation experiment."""
    with tempfile.TemporaryDirectory(prefix="agentc-storage-isolation-") as temp:
        root = Path(temp)
        first = (root / "store-a").resolve()
        second = (root / "store-b").resolve()
        decoy = (root / "decoy-from-environment").resolve()
        os.environ.update(
            {
                "AGENTC_STORAGE_PATH": str(decoy),
                "AGENTC_ENABLED_RULES": "OutputBudget",
                "AGENTC_OPTIMIZE": "1",
                "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3",
                "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
                "AGENTC_OPTIMIZE_SHADOW": "0",
            }
        )

        import agentc
        from agentc._optimizer import observe_outcome, plan_call

        first_plans: list[str] = []
        agentc.init(storage_path=str(first))
        first_python_path = str(agentc._lifecycle.get_config().storage_path)
        first_native_path = agentc._native.optimize_storage_path()
        first_active_env = os.environ["AGENTC_STORAGE_PATH"]
        for _ in range(3):
            plan = plan_call(_call())
            first_plans.append(plan.kind)
            observe_outcome(plan, _outcome())
        first_hot_plan = plan_call(_call())
        first_plans.append(first_hot_plan.kind)
        agentc.shutdown()
        env_after_first_shutdown = os.environ["AGENTC_STORAGE_PATH"]

        agentc.init(storage_path=str(second))
        second_python_path = str(agentc._lifecycle.get_config().storage_path)
        second_native_path = agentc._native.optimize_storage_path()
        second_active_env = os.environ["AGENTC_STORAGE_PATH"]
        second_first_plan = plan_call(_call())
        agentc.shutdown()
        env_after_second_shutdown = os.environ["AGENTC_STORAGE_PATH"]

        result = {
            "first_store": {
                "python_native_match": first_python_path
                == first_native_path
                == str(first),
                "active_environment_match": first_active_env == str(first),
                "plan_sequence": first_plans,
                "cost_profile_rows": _table_count(
                    first / "cost_model.db", "call_site_profile"
                ),
                "audit_rows": _table_count(first / "optimizer_audit.db", "plan_audit"),
            },
            "second_store": {
                "python_native_match": (
                    second_python_path == second_native_path == str(second)
                ),
                "active_environment_match": second_active_env == str(second),
                "first_plan_kind": second_first_plan.kind,
                "cost_profile_rows": _table_count(
                    second / "cost_model.db", "call_site_profile"
                ),
                "audit_rows": _table_count(second / "optimizer_audit.db", "plan_audit"),
            },
            "environment_restored_after_first": env_after_first_shutdown == str(decoy),
            "environment_restored_after_second": env_after_second_shutdown
            == str(decoy),
            "decoy_store_created": decoy.exists(),
        }

    assert result["first_store"]["python_native_match"]
    assert result["first_store"]["active_environment_match"]
    assert result["first_store"]["plan_sequence"] == [
        "pass_through",
        "pass_through",
        "pass_through",
        "rewritten",
    ]
    assert result["first_store"]["cost_profile_rows"] == 1
    assert result["first_store"]["audit_rows"] == 4
    assert result["second_store"]["python_native_match"]
    assert result["second_store"]["active_environment_match"]
    assert result["second_store"]["first_plan_kind"] == "pass_through"
    assert result["second_store"]["cost_profile_rows"] == 0
    assert result["second_store"]["audit_rows"] == 1
    assert result["environment_restored_after_first"]
    assert result["environment_restored_after_second"]
    assert result["decoy_store_created"] is False

    return {
        "schema_version": 1,
        "experiment_kind": "same_process_storage_isolation_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "agentc_git_commit": _git_commit(),
        "paper_evidence": False,
        "network_calls": 0,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "settings": {
            "enabled_rules": ["OutputBudget"],
            "hot_threshold": 3,
            "max_planning_overhead_ms": 1000,
            "warm_observations_in_first_store": 3,
        },
        "result": result,
        "interpretation_limits": [
            "This proves same-process lifecycle isolation and path ownership only.",
            "Calls and outcomes are direct synthetic optimizer inputs; no LLM or provider is invoked.",
            "The raised planning ceiling is an activation control, not the production setting.",
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
