"""Actual frozen-policy execution, stage order, identities and fail-safe guards."""

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from agentc import _native
from agentc._evaluation_policy import (
    FrozenEvaluationPolicy,
    evaluation_policy_scope,
    current_evaluation_policy,
)
from agentc._patches._optimizer_glue import build_call_dict_openai

STRONG = "openai/gpt-4o-2024-11-20"
CHEAP = "openai/gpt-4o-mini-2024-07-18"
MODES = ("best_static_joint", "route_then_rewrite", "rewrite_then_route")


def configuration(mode):
    cfg = {
        "strong_model": STRONG,
        "cheap_model": CHEAP,
        "divergence_threshold": 0.05,
        "static_plan": None,
        "router": None,
        "rewrite_by_target": {},
    }
    if mode == "best_static_joint":
        cfg["static_plan"] = {
            "target_model": CHEAP,
            "rewrites": [{"rule": "OutputBudget", "max_output_tokens": 128}],
        }
    else:
        cfg["router"] = {
            "feature": "prompt_utf8_bytes_v1",
            "cheap_max_prompt_bytes": 400,
        }
        cfg["rewrite_by_target"] = {
            STRONG: [
                {"rule": "StateDrop"},
                {"rule": "OutputBudget", "max_output_tokens": 384},
            ],
            CHEAP: [{"rule": "OutputBudget", "max_output_tokens": 128}],
        }
    return cfg


def policy(mode, *, lock="a" * 64, cfg=None):
    return FrozenEvaluationPolicy.from_configuration(
        mode=mode,
        calibration_lock_sha256=lock,
        configuration=cfg or configuration(mode),
    )


def native_call(mode):
    messages = [
        {
            "role": role,
            "content": "old state " * 100 if i == 2 else f"preserve {i}",
        }
        for i, role in enumerate(
            ["system", "user", "tool", "assistant", "tool", "user"]
        )
    ]
    with evaluation_policy_scope(policy(mode)):
        call = build_call_dict_openai(
            {
                "model": STRONG,
                "messages": messages,
                "max_tokens": 1024,
                "temperature": 0,
            },
            call_site_id="frozen.policy.control",
            trace_id_hex="00" * 16,
            span_id_hex="01" * 8,
            provider_protocol="litellm.completion.v1",
        )
    call["parameters"]["extra"]["message_deps"] = [
        {"kind": "state", "key": "unused"} if i == 2 else {"kind": "literal"}
        for i in range(6)
    ]
    call["parameters"]["extra"]["window_state_reads"] = []
    return call


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_OPTIMIZE", "1")
    monkeypatch.setenv("AGENTC_OPTIMIZE_SELECTION_AUDIT", "1")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
    monkeypatch.setenv("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "50")
    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "0.02")
    yield
    _native.optimize_reset()


def configure(mode, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", mode)
    _native.optimize_configure(str(tmp_path / mode))
    assert _native.optimize_evaluation_planner_mode() == mode


@pytest.mark.parametrize("mode", MODES)
def test_frozen_controls_execute_without_live_profiles_or_exploration(
    mode, tmp_path, monkeypatch
):
    configure(mode, tmp_path, monkeypatch)
    call = native_call(mode)
    original = deepcopy(call)
    plan = json.loads(_native.optimize_plan(json.dumps(call)))
    assert call == original
    assert "agentc_exploration_context" not in plan
    assert len(plan["agentc_observation_context"]["key"]["execution_plan_id"]) == 64
    audit = plan["agentc_selection_audit"]
    assert audit["planner_mode"] == mode and audit["candidate_count"] is None
    assert audit["enumeration_complete"] is False
    assert audit["policy"]["interaction_profiles_consulted"] is False
    assert audit["policy"]["cost_estimate_available"] is False
    expected = (
        (STRONG, 384)
        if mode == "route_then_rewrite"
        else (CHEAP, 384 if mode == "rewrite_then_route" else 128)
    )
    assert (
        plan["call"]["model"],
        plan["call"]["parameters"]["max_output_tokens"],
    ) == expected
    if mode != "best_static_joint":
        assert (audit["policy"]["router_prompt_bytes"] <= 400) == (
            mode == "rewrite_then_route"
        )


@pytest.mark.parametrize(
    "mutation", ["missing", "configuration_digest", "mode", "target", "unknown_field"]
)
def test_invalid_declarations_return_reference_without_exploration(
    mutation, tmp_path, monkeypatch
):
    mode = "best_static_joint"
    configure(mode, tmp_path, monkeypatch)
    call = native_call(mode)
    context = call["parameters"]["extra"]["agentc_route_context"]
    declaration = context["frozen_request_policy"]
    if mutation == "missing":
        context.pop("frozen_request_policy")
    elif mutation == "configuration_digest":
        declaration["configuration_sha256"] = "b" * 64
    elif mutation == "mode":
        declaration["mode"] = "rewrite_then_route"
    elif mutation == "unknown_field":
        declaration["unfrozen"] = True
    else:
        cfg = configuration(mode)
        cfg["static_plan"]["target_model"] = "unknown"
        context["frozen_request_policy"] = policy(mode, cfg=cfg).declaration()
    plan = json.loads(_native.optimize_plan(json.dumps(call)))
    assert plan["kind"] == "pass_through" and "agentc_exploration_context" not in plan
    assert plan["agentc_selection_audit"]["abstention_reason"]


def test_lock_changes_native_cohort_identity(tmp_path, monkeypatch):
    mode = "best_static_joint"
    configure(mode, tmp_path, monkeypatch)
    call = native_call(mode)
    first = json.loads(_native.optimize_plan(json.dumps(call)))
    call["parameters"]["extra"]["agentc_route_context"]["frozen_request_policy"] = (
        policy(mode, lock="b" * 64).declaration()
    )
    second = json.loads(_native.optimize_plan(json.dumps(call)))
    assert (
        first["agentc_observation_context"]["key"]["call_site_version"]
        != second["agentc_observation_context"]["key"]["call_site_version"]
    )


def test_policy_guard_rolls_back_the_complete_plan(tmp_path, monkeypatch):
    mode = "best_static_joint"
    configure(mode, tmp_path, monkeypatch)
    call = native_call(mode)
    raw = _native.optimize_plan(json.dumps(call))
    for _ in range(2):
        token = _native.optimize_observe(
            raw,
            json.dumps(
                {
                    "call_site_id": call["call_site_id"],
                    "model": CHEAP,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "latency_ms": 2.0,
                    "cost_usd": 0.0001,
                }
            ),
        )
        assert token
        _native.optimize_record_divergence(token, 1.0)
    plan = json.loads(_native.optimize_plan(json.dumps(call)))
    assert plan["kind"] == "pass_through"
    assert plan["agentc_selection_audit"]["abstention_reason"] == "complete_plan_guard"
    assert (
        plan["agentc_selection_audit"]["effective_selected_plan_id"]
        == plan["agentc_selection_audit"]["reference"]["id"]
    )


def test_scope_freezes_configuration_and_restores_nested_state():
    cfg = configuration("best_static_joint")
    frozen = policy("best_static_joint", cfg=cfg)
    cfg["static_plan"]["target_model"] = "mutated"
    assert json.loads(frozen.configuration_json)["static_plan"]["target_model"] == CHEAP
    assert (
        frozen.declaration()["configuration_sha256"]
        == sha256(frozen.configuration_json.encode()).hexdigest()
    )
    assert current_evaluation_policy() is None
    with evaluation_policy_scope(frozen):
        with evaluation_policy_scope(policy("route_then_rewrite")):
            assert current_evaluation_policy().mode == "route_then_rewrite"
        assert current_evaluation_policy() is frozen
    assert current_evaluation_policy() is None
