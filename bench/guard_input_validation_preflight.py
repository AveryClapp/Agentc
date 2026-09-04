"""No-network preflight for complete-plan guard input validation.

The experiment proves that non-finite and out-of-range divergence samples do
not create complete-plan guard state, and that invalid environment thresholds
fall back to the minimum declared accuracy budget of the composed plan.

This is Stage E0 engineering evidence, not a provider or paper result.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


_REPO_ROOT = Path(__file__).resolve().parent.parent
_RULES = ("ContextCompress", "OutputBudget")
_VALID_DIVERGENCE = 0.5
_FALLBACK_THRESHOLD = 0.01
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


def _call(call_site_id: str) -> dict[str, Any]:
    big_dead_context = "historical context " * 700
    return {
        "call_site_id": call_site_id,
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


def _outcome(call_site_id: str) -> dict[str, Any]:
    return {
        "input_tokens": 5_000,
        "output_tokens": 200,
        "latency_ms": 1_000.0,
        "cost_usd": 0.05,
        "output_is_structured": False,
        "output_is_short": False,
        "call_site_id": call_site_id,
    }


def _settings(threshold: str) -> dict[str, str]:
    return {
        "AGENTC_ENABLED_RULES": ",".join(_RULES),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_SHADOW": "0.02",
        "AGENTC_SHADOW_DIVERGENCE_BUDGET": threshold,
    }


def _plan_summary(plan: Any) -> dict[str, Any]:
    return {
        "kind": plan.kind,
        "rules": sorted(plan.rules or ([plan.rule] if plan.rule else [])),
    }


def _warm_call_site(plan_call: Any, observe_outcome: Any, call_site_id: str) -> None:
    call = _call(call_site_id)
    outcome = _outcome(call_site_id)
    for _ in range(3):
        plan = plan_call(call)
        assert plan.kind == "pass_through"
        observe_outcome(plan, outcome)


def _guard_state(storage: Path, call_site_id: str) -> dict[str, Any]:
    with sqlite3.connect(storage / "cost_model.db") as connection:
        guard = connection.execute(
            "SELECT divergence_threshold, divergence_exposure, window_samples "
            "FROM execution_plan_guard"
        ).fetchone()
        disabled = connection.execute(
            "SELECT reason, exposure FROM execution_plan_disabled"
        ).fetchone()
        paired = connection.execute(
            "SELECT COALESCE(SUM(n_paired_observations), 0), "
            "COALESCE(SUM(paired_observations), 0) "
            "FROM execution_plan_profile"
        ).fetchone()
        legacy_divergence = connection.execute(
            "SELECT COUNT(*) FROM rule_divergence WHERE call_site_id = ?",
            (call_site_id,),
        ).fetchone()
        legacy_disabled = connection.execute(
            "SELECT COUNT(*) FROM optimizer_disabled WHERE call_site_id = ?",
            (call_site_id,),
        ).fetchone()
    assert paired is not None
    assert legacy_divergence is not None
    assert legacy_disabled is not None
    return {
        "plan_guard": (
            {
                "divergence_threshold": float(guard[0]),
                "divergence_exposure": float(guard[1]),
                "window_samples": int(guard[2]),
            }
            if guard is not None
            else None
        ),
        "plan_disabled": (
            {"reason": str(disabled[0]), "exposure": float(disabled[1])}
            if disabled is not None
            else None
        ),
        "lifetime_paired_observations": int(paired[0]),
        "retained_paired_observations": int(paired[1]),
        "legacy_rule_divergence_rows": int(legacy_divergence[0]),
        "legacy_rule_disabled_rows": int(legacy_disabled[0]),
    }


def _assert_composed(summary: dict[str, Any]) -> None:
    assert summary == {"kind": "composed", "rules": sorted(_RULES)}


def run() -> dict[str, Any]:
    """Execute invalid-divergence and invalid-threshold checks."""
    with tempfile.TemporaryDirectory(prefix="agentc-guard-inputs-") as temp:
        root = Path(temp).resolve()

        import agentc
        from agentc._optimizer import observe_outcome, plan_call, record_divergence

        invalid_storage = root / "invalid-divergence"
        invalid_site = "invalid-complete-plan-divergence"
        invalid_plan_kinds: list[dict[str, Any]] = []
        with patch.dict(os.environ, _settings("0.1")):
            agentc.init(storage_path=str(invalid_storage))
            try:
                _warm_call_site(plan_call, observe_outcome, invalid_site)
                for _, divergence in _INVALID_DIVERGENCES:
                    plan = plan_call(_call(invalid_site))
                    invalid_plan_kinds.append(_plan_summary(plan))
                    token = observe_outcome(plan, _outcome(invalid_site))
                    assert token is not None
                    record_divergence(token, divergence)
                plan_after_invalid_divergences = _plan_summary(
                    plan_call(_call(invalid_site))
                )
            finally:
                agentc.shutdown()
        invalid_divergence_state = _guard_state(invalid_storage, invalid_site)

        threshold_fallbacks: dict[str, dict[str, Any]] = {}
        for index, threshold in enumerate(_INVALID_THRESHOLDS):
            storage = root / f"invalid-threshold-{index}"
            site = f"invalid-complete-plan-threshold-{index}"
            sampled_plans: list[dict[str, Any]] = []
            with patch.dict(os.environ, _settings(threshold)):
                agentc.init(storage_path=str(storage))
                try:
                    _warm_call_site(plan_call, observe_outcome, site)
                    for _ in range(3):
                        plan = plan_call(_call(site))
                        sampled_plans.append(_plan_summary(plan))
                        token = observe_outcome(plan, _outcome(site))
                        assert token is not None
                        record_divergence(token, _VALID_DIVERGENCE)
                    plan_after_budget = _plan_summary(plan_call(_call(site)))
                finally:
                    agentc.shutdown()
            threshold_fallbacks[threshold] = {
                "sampled_plans": sampled_plans,
                "plan_after_budget": plan_after_budget,
                "state": _guard_state(storage, site),
            }

    for summary in invalid_plan_kinds:
        _assert_composed(summary)
    _assert_composed(plan_after_invalid_divergences)
    assert invalid_divergence_state == {
        "plan_guard": None,
        "plan_disabled": None,
        "lifetime_paired_observations": 0,
        "retained_paired_observations": 0,
        "legacy_rule_divergence_rows": 0,
        "legacy_rule_disabled_rows": 0,
    }
    expected_exposure = 3 * (_VALID_DIVERGENCE - _FALLBACK_THRESHOLD)
    for threshold, fallback in threshold_fallbacks.items():
        for summary in fallback["sampled_plans"]:
            _assert_composed(summary)
        assert fallback["plan_after_budget"] == {"kind": "pass_through", "rules": []}
        state = fallback["state"]
        guard = state["plan_guard"]
        disabled = state["plan_disabled"]
        assert guard is not None, threshold
        assert disabled is not None, threshold
        assert math.isclose(
            guard["divergence_threshold"],
            _FALLBACK_THRESHOLD,
            rel_tol=1e-6,
            abs_tol=1e-9,
        )
        assert math.isclose(
            guard["divergence_exposure"],
            expected_exposure,
            rel_tol=1e-6,
            abs_tol=1e-9,
        )
        assert guard["window_samples"] == 3
        assert disabled["reason"] == "divergence_exposure"
        assert math.isclose(
            disabled["exposure"],
            expected_exposure,
            rel_tol=1e-6,
            abs_tol=1e-9,
        )
        assert state["lifetime_paired_observations"] == 3
        assert state["retained_paired_observations"] == 3
        assert state["legacy_rule_divergence_rows"] == 0
        assert state["legacy_rule_disabled_rows"] == 0

    return {
        "schema_version": 2,
        "experiment_kind": "complete_plan_guard_input_validation_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "agentc_git_commit": _git_commit(),
        "paper_evidence": False,
        "network_calls": 0,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "settings": {
            "rules": list(_RULES),
            "valid_control_divergence": _VALID_DIVERGENCE,
            "fallback_plan_threshold": _FALLBACK_THRESHOLD,
            "plan_exposure_budget": 1.0,
            "samples_to_disable": 3,
        },
        "result": {
            "rejected_divergence_labels": [label for label, _ in _INVALID_DIVERGENCES],
            "invalid_divergence_sampled_plans": invalid_plan_kinds,
            "plan_after_invalid_divergences": plan_after_invalid_divergences,
            "invalid_divergence_state": invalid_divergence_state,
            "invalid_threshold_fallbacks": threshold_fallbacks,
        },
        "interpretation_limits": [
            "This proves native-boundary validation for one canonical composed plan and synthetic inputs only.",
            "No LLM, provider, semantic-quality metric, billed cost, or network transport is involved.",
            "The experiment does not evaluate detection delay, false disables, drift, or task-equivalent damage.",
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
