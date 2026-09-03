"""No-network preflight for guard input validation.

The experiment proves that non-finite and out-of-range divergence samples do
not create guard state, and that invalid environment thresholds fall back to
the firing rule's declared accuracy budget.

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
_RULE = "OutputBudget"
_INVALID_DIVERGENCES = [
    ("nan", float("nan")),
    ("positive_infinity", float("inf")),
    ("negative_infinity", float("-inf")),
    ("negative_epsilon_before_f32_cast", -1e-300),
    ("above_one_before_f32_cast", 1.0 + 1e-12),
    ("negative_fraction", -0.1),
    ("above_one_fraction", 1.1),
]
_INVALID_THRESHOLDS = [
    "nan",
    "inf",
    "-inf",
    "-1e-300",
    "1.000000000001",
    "-0.1",
    "1.1",
]


def _git_commit() -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _guard_state(storage: Path, call_site_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(storage / "cost_model.db") as connection:
        divergence = connection.execute(
            "SELECT n_samples, divergence_mean, divergence_var, "
            "consecutive_breaches FROM rule_divergence "
            "WHERE call_site_id = ? AND rule = ?",
            (call_site_id, _RULE),
        ).fetchone()
        disabled = connection.execute(
            "SELECT reason FROM optimizer_disabled WHERE call_site_id = ? AND rule = ?",
            (call_site_id, _RULE),
        ).fetchone()
    if divergence is None:
        return None
    return {
        "n_samples": int(divergence[0]),
        "divergence_mean": float(divergence[1]),
        "divergence_var": float(divergence[2]),
        "consecutive_breaches": int(divergence[3]),
        "disable_reason": str(disabled[0]) if disabled is not None else None,
    }


def _configure(threshold: str) -> None:
    os.environ.update(
        {
            "AGENTC_ENABLED_RULES": _RULE,
            "AGENTC_OPTIMIZE": "1",
            "AGENTC_OPTIMIZE_SHADOW": "0",
            "AGENTC_SHADOW_DIVERGENCE_BUDGET": threshold,
        }
    )


def run() -> dict[str, Any]:
    """Execute invalid-divergence and invalid-threshold checks."""
    with tempfile.TemporaryDirectory(prefix="agentc-guard-inputs-") as temp:
        root = Path(temp).resolve()

        import agentc

        invalid_storage = root / "invalid-divergence"
        _configure("0.1")
        agentc.init(storage_path=str(invalid_storage))
        for _, divergence in _INVALID_DIVERGENCES:
            agentc._native.optimize_record_divergence(
                "invalid-divergence-site", _RULE, divergence
            )
        agentc.shutdown()
        invalid_divergence_state = _guard_state(
            invalid_storage, "invalid-divergence-site"
        )

        threshold_fallbacks: dict[str, dict[str, Any] | None] = {}
        for index, threshold in enumerate(_INVALID_THRESHOLDS):
            storage = root / f"invalid-threshold-{index}"
            site = f"invalid-threshold-site-{index}"
            _configure(threshold)
            agentc.init(storage_path=str(storage))
            for _ in range(5):
                agentc._native.optimize_record_divergence(site, _RULE, 0.5)
            agentc.shutdown()
            threshold_fallbacks[threshold] = _guard_state(storage, site)

    expected_fallback_state = {
        "n_samples": 5,
        "divergence_mean": 0.5,
        "divergence_var": 0.0,
        "consecutive_breaches": 0,
        "disable_reason": "shadow_divergence",
    }
    assert invalid_divergence_state is None
    assert threshold_fallbacks == {
        threshold: expected_fallback_state for threshold in _INVALID_THRESHOLDS
    }

    return {
        "schema_version": 1,
        "experiment_kind": "guard_input_validation_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "agentc_git_commit": _git_commit(),
        "paper_evidence": False,
        "network_calls": 0,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "settings": {
            "rule": _RULE,
            "valid_control_divergence": 0.5,
            "fallback_rule_budget": 0.01,
            "breach_streak": 5,
        },
        "result": {
            "rejected_divergence_labels": [label for label, _ in _INVALID_DIVERGENCES],
            "invalid_divergence_state": invalid_divergence_state,
            "invalid_threshold_fallbacks": threshold_fallbacks,
        },
        "interpretation_limits": [
            "This proves native-boundary validation for one rule and direct synthetic inputs only.",
            "No LLM, provider, semantic-quality metric, billed cost, or network transport is involved.",
            "The experiment does not evaluate guard detection delay, false disables, drift, or cumulative damage.",
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
