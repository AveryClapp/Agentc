"""Verify that campaign arms select distinct native planner implementations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from agentc import _native
from agentc._patches._optimizer_glue import build_call_dict_openai


@pytest.fixture(autouse=True)
def isolated_optimizer(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_OPTIMIZE", "1")
    monkeypatch.setenv("AGENTC_COMPOSE", "1")
    monkeypatch.setenv("AGENTC_ENABLED_RULES", "OutputBudget")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
    monkeypatch.setenv("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "50")
    monkeypatch.delenv("AGENTC_EVAL_PLANNER_MODE", raising=False)
    yield
    _native.optimize_reset()


def native_call() -> dict:
    return build_call_dict_openai(
        {
            "model": "gpt-4o-2024-11-20",
            "messages": [{"role": "user", "content": "original user instruction"}],
            "max_tokens": 1024,
            "temperature": 0,
        },
        call_site_id="campaign.native.control",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
    )


def warm_and_plan(storage: Path) -> dict:
    _native.optimize_configure(str(storage.resolve()))
    call = native_call()
    for _ in range(3):
        plan = _native.optimize_plan(json.dumps(call))
        assert json.loads(plan)["kind"] == "pass_through"
        _native.optimize_observe(
            plan,
            json.dumps(
                {
                    "call_site_id": call["call_site_id"],
                    "model": call["model"],
                    "input_tokens": 100,
                    "output_tokens": 100,
                    "latency_ms": 100.0,
                    "cost_usd": 0.01,
                }
            ),
        )
    return json.loads(_native.optimize_plan(json.dumps(call)))


def test_greedy_composes_without_joint_evidence_or_exploration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "current_greedy")
    plan = warm_and_plan(tmp_path / "greedy")
    assert _native.optimize_evaluation_planner_mode() == "current_greedy"
    assert plan["kind"] == "rewritten"
    assert plan["rule"] == "OutputBudget"
    assert plan["call"]["parameters"]["max_output_tokens"] < 1024
    assert "agentc_observation_context" in plan
    assert "agentc_exploration_context" not in plan


@pytest.mark.parametrize("compose", ["0", "1"])
def test_greedy_cost_reduction_precedes_parallel_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compose: str,
) -> None:
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "current_greedy")
    monkeypatch.setenv("AGENTC_COMPOSE", compose)
    monkeypatch.setenv("AGENTC_ENABLED_RULES", "ParallelBranch,OutputBudget")
    call = native_call()
    call["input_deps"] = [{"kind": "user_input", "span_id": "02" * 8}]
    call["parameters"]["extra"]["parallel_peer"] = {
        "input_deps": [{"kind": "user_input", "span_id": "03" * 8}],
    }
    monkeypatch.setattr("tests.test_joint_campaign_native.native_call", lambda: call)
    plan = warm_and_plan(tmp_path / "cost-priority")
    assert plan["kind"] == "rewritten"
    assert plan["rule"] == "OutputBudget"
    assert plan["call"]["parameters"]["max_output_tokens"] < 1024
    assert 0 < plan["projected_savings_usd"] < 0.01


def test_joint_remains_reference_visible_without_paired_evidence(
    tmp_path: Path,
) -> None:
    plan = warm_and_plan(tmp_path / "joint")
    assert _native.optimize_evaluation_planner_mode() == "joint_guarded"
    assert plan["kind"] == "pass_through"
    assert "agentc_exploration_context" in plan


def test_greedy_retains_complete_plan_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "current_greedy")
    plan = warm_and_plan(tmp_path / "guarded-greedy")
    assert plan["kind"] == "rewritten"
    call = native_call()
    for _ in range(2):
        token = _native.optimize_observe(
            json.dumps(plan),
            json.dumps(
                {
                    "call_site_id": call["call_site_id"],
                    "model": call["model"],
                    "input_tokens": 100,
                    "output_tokens": 100,
                    "latency_ms": 100.0,
                    "cost_usd": 0.01,
                }
            ),
        )
        assert token
        _native.optimize_record_divergence(token, 1.0)
    after_feedback = json.loads(_native.optimize_plan(json.dumps(call)))
    assert after_feedback["kind"] == "pass_through"
    assert "agentc_exploration_context" not in after_feedback


def test_invalid_mode_passes_through_without_exploration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "misspelled-control")
    plan = warm_and_plan(tmp_path / "invalid")
    assert _native.optimize_evaluation_planner_mode() == "invalid"
    assert plan["kind"] == "pass_through"
    assert "agentc_exploration_context" not in plan


def test_mode_is_frozen_until_explicit_reconfiguration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _native.optimize_configure(str(tmp_path / "joint"))
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "current_greedy")
    assert _native.optimize_evaluation_planner_mode() == "joint_guarded"
    _native.optimize_configure(str(tmp_path / "greedy"))
    assert _native.optimize_evaluation_planner_mode() == "current_greedy"
