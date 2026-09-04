"""End-to-end native persistence tests for complete-plan diagnostics."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentc import _native


@pytest.fixture(autouse=True)
def _reset_optimizer() -> Any:
    _native.optimize_reset()
    yield
    _native.optimize_reset()


def _call(site: str) -> dict[str, Any]:
    return {
        "call_site_id": site,
        "trace_id": "00" * 16,
        "span_id": "00" * 8,
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "diagnostic input"}],
        "parameters": {
            "max_output_tokens": 512,
            "extra": {
                "agentc_route_context": {
                    "provider_protocol": "openai.chat.completions.v1",
                    "provider_namespace": "openai",
                    "input_tokens_upper_bound": 64,
                    "input_tokens_upper_bound_basis": "json_utf8_bytes_v1",
                    "image_input": False,
                    "tool_calling": False,
                    "structured_outputs": False,
                    "streaming": False,
                }
            },
        },
        "tools": [],
        "input_deps": [{"kind": "literal"}],
        "occurrence_ix": 0,
    }


def _outcome(site: str) -> dict[str, Any]:
    return {
        "input_tokens": 64,
        "output_tokens": 16,
        "latency_ms": 40.0,
        "cost_usd": 0.002,
        "output_is_structured": False,
        "output_is_short": True,
        "call_site_id": site,
    }


def test_native_audit_persists_candidate_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "0")
    monkeypatch.setenv("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "1000")
    monkeypatch.setenv("AGENTC_OPTIMIZE_OBJECTIVE", "latency")
    storage = tmp_path / "agentc"
    _native.optimize_configure(str(storage))
    site = "tests.planner:diagnostics"
    call_json = json.dumps(_call(site))

    for _ in range(3):
        plan_json = _native.optimize_plan(call_json)
        _native.optimize_observe(plan_json, json.dumps(_outcome(site)))
    plan_json = _native.optimize_plan(call_json)
    plan = json.loads(plan_json)
    diagnostics = plan["agentc_planner_diagnostics"]

    assert plan["kind"] == "pass_through"
    assert diagnostics["risk"]["objective"] == "latency"
    assert diagnostics["selected_reference"] is True
    assert diagnostics["fallback_reason"] == "no_admissible_alternative"
    assert diagnostics["candidates"]
    assert all(
        candidate["rejection_reason"] == "missing_estimate"
        for candidate in diagnostics["candidates"]
    )
    assert all(
        candidate["evidence_confidence"] == 0.0
        for candidate in diagnostics["candidates"]
    )
    assert "diagnostic input" not in json.dumps(diagnostics)

    with sqlite3.connect(storage / "optimizer_audit.db") as connection:
        persisted = connection.execute(
            "SELECT planner_diagnostics_json FROM plan_audit "
            "WHERE call_site_id = ? ORDER BY audit_id DESC LIMIT 1",
            (site,),
        ).fetchone()
    assert persisted is not None
    assert json.loads(persisted[0]) == diagnostics


def test_malformed_objective_disables_optimization_and_exploration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTC_OPTIMIZE_OBJECTIVE", "fastest-ish")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
    storage = tmp_path / "agentc"
    _native.optimize_configure(str(storage))

    plan = json.loads(_native.optimize_plan(json.dumps(_call("tests.planner:invalid"))))
    diagnostics = plan["agentc_planner_diagnostics"]
    assert plan["kind"] == "pass_through"
    assert "agentc_exploration_context" not in plan
    assert diagnostics["selected_reference"] is True
    assert diagnostics["fallback_reason"] == "invalid_configuration"
    assert "AGENTC_OPTIMIZE_OBJECTIVE" in diagnostics["risk"]["configuration_error"]
    assert diagnostics["risk"]["exploration_enabled"] is False


def test_invalid_global_divergence_override_fails_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_BUDGET", "1.5")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
    storage = tmp_path / "agentc"
    _native.optimize_configure(str(storage))

    plan = json.loads(_native.optimize_plan(json.dumps(_call("tests.planner:threshold"))))
    diagnostics = plan["agentc_planner_diagnostics"]
    assert plan["kind"] == "pass_through"
    assert "agentc_exploration_context" not in plan
    assert diagnostics["fallback_reason"] == "invalid_configuration"
    assert "global_divergence_threshold" in diagnostics["risk"]["configuration_error"]
    assert diagnostics["risk"]["exploration_enabled"] is False
