"""Exercise actual SDK declarations through native complete-plan identities."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from agentc import _native
from agentc._patches._optimizer_glue import (
    build_call_dict_anthropic,
    build_call_dict_openai,
)
from agentc._provider_config import CONTEXT_KEY, VERSION


@pytest.fixture(autouse=True)
def optimizer(tmp_path, monkeypatch):
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_OPTIMIZE", "1")
    monkeypatch.setenv("AGENTC_COMPOSE", "1")
    monkeypatch.setenv("AGENTC_ENABLED_RULES", "OutputBudget")
    monkeypatch.setenv("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "100")
    monkeypatch.setenv("AGENTC_MODEL_CATALOG_OFFLINE", "1")
    monkeypatch.delenv("AGENTC_EVAL_PLANNER_MODE", raising=False)
    _native.optimize_configure(str(tmp_path / "state"))
    yield
    _native.optimize_reset()


def request(protocol, changes=None):
    kwargs = {
        "model": "gpt-4o-2024-11-20",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": "a task-specific input"}],
    }
    kwargs.update(changes or {})
    ids = {
        "call_site_id": "provider.config.native",
        "trace_id_hex": "00" * 16,
        "span_id_hex": "01" * 8,
    }
    if protocol == "anthropic.messages.v1":
        kwargs["model"] = "claude-sonnet-4-5-20250929"
        return build_call_dict_anthropic(kwargs, **ids)
    if protocol == "litellm.completion.v1":
        kwargs["model"] = "openai/gpt-4o-2024-11-20"
    return build_call_dict_openai(kwargs, provider_protocol=protocol, **ids)


def native_key(call):
    result = json.loads(_native.optimize_plan(json.dumps(call)))
    assert result["kind"] == "pass_through"
    return result["agentc_observation_context"]["key"]


@pytest.mark.parametrize(
    "protocol",
    [
        "openai.chat.completions.v1",
        "anthropic.messages.v1",
        "litellm.completion.v1",
    ],
)
@pytest.mark.parametrize(
    "controls",
    [
        {"seed": 7},
        {"reasoning_effort": "high"},
        {"frequency_penalty": 0.5},
        {"tool_choice": "none"},
        {"parallel_tool_calls": False},
        {"thinking": {"type": "enabled", "budget_tokens": 2048}},
    ],
)
def test_provider_settings_change_actual_profile_key(protocol, controls):
    before = native_key(request(protocol))
    after = native_key(request(protocol, controls))
    assert before["call_site_version"] != after["call_site_version"]
    assert before["execution_plan_id"] == after["execution_plan_id"]


@pytest.mark.parametrize(
    "protocol",
    [
        "openai.chat.completions.v1",
        "anthropic.messages.v1",
        "litellm.completion.v1",
    ],
)
def test_private_task_values_and_credentials_do_not_change_the_settings_key(protocol):
    first = request(protocol)
    second = request(
        protocol,
        {
            "api_key": "PRIVATE-CREDENTIAL",
            "metadata": {"private_user": "PRIVATE-USER"},
            "messages": [{"role": "user", "content": "a different private task"}],
        },
    )
    assert native_key(first) == native_key(second)
    assert "PRIVATE" not in json.dumps(
        second["parameters"]["extra"]["agentc_route_context"][CONTEXT_KEY]
    )


@pytest.mark.parametrize("compose", ["0", "1"])
def test_unknown_controls_never_issue_a_complete_plan_observation(
    compose, tmp_path, monkeypatch
):
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_COMPOSE", compose)
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "current_greedy")
    _native.optimize_configure(str(tmp_path / "unknown"))
    call = request(
        "openai.chat.completions.v1", {"extra_body": {"unknown": "private input"}}
    )
    for _ in range(5):
        result = _native.optimize_plan(json.dumps(call))
        plan = json.loads(result)
        assert plan["kind"] == "pass_through"
        assert "agentc_observation_context" not in plan
        token = json.loads(
            _native.optimize_observe(
                result,
                json.dumps(
                    {
                        "call_site_id": call["call_site_id"],
                        "model": call["model"],
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cost_usd": 0.01,
                        "latency_ms": 100.0,
                    }
                ),
            )
        )
        assert token.get("plan_observation") is None


def test_projection_version_cannot_be_ignored_by_an_older_native_build(monkeypatch):
    monkeypatch.setattr(
        _native, "optimize_provider_configuration_version", lambda: "older-v0"
    )
    with pytest.raises(RuntimeError, match="provider-configuration"):
        request("openai.chat.completions.v1")


def test_malformed_declarations_abstain_before_native_identity_is_created():
    call = request("openai.chat.completions.v1")
    for declaration in (
        {"version": VERSION, "supported": True, "sha256": "bad"},
        {"version": VERSION, "supported": True, "sha256": "a" * 64, "extra": True},
        {"version": "future", "supported": True, "sha256": "a" * 64},
    ):
        altered = deepcopy(call)
        altered["parameters"]["extra"]["agentc_route_context"][CONTEXT_KEY] = (
            declaration
        )
        plan = json.loads(_native.optimize_plan(json.dumps(altered)))
        assert plan["kind"] == "pass_through"
        assert "agentc_observation_context" not in plan
