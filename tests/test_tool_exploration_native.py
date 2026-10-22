"""Tool comparison declarations gate actual native lease acquisition."""

from copy import deepcopy
import json

import pytest

from agentc import _native
from agentc._patches._optimizer_glue import build_call_dict_openai

PROTOCOL = "openai.chat.completions.v1"
VERSION = "typed-actions-v1"
SETTINGS = "tool-settings-v1:" + "01" * 32


def request():
    return {
        "model": "gpt-4o-2024-11-20",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Synthetic tool control."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "fixture_action",
                    "parameters": {"type": "object"},
                },
            }
        ],
    }


def call():
    value = build_call_dict_openai(
        request(),
        call_site_id="tool-exploration-control",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
    )
    # Direct FFI fixture explicitly declares its comparison contract. Actual
    # production SDK emission is checked independently below and by the Tau
    # provider preflight; no observation or lease tokens are constructed here.
    value["parameters"]["extra"]["agentc_route_context"].update(
        tool_action_comparator_version=VERSION,
        tool_settings_version=SETTINGS,
    )
    return value


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    _native.optimize_reset()
    for name, value in {
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_COMPOSE": "1",
        "AGENTC_OPTIMIZE_EXPLORATION": "1",
        "AGENTC_ENABLED_RULES": "OutputBudget",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "50",
    }.items():
        monkeypatch.setenv(name, value)
    _native.optimize_configure(str(tmp_path))
    yield
    _native.optimize_reset()


def warm_and_plan(value):
    for _ in range(3):
        planned = _native.optimize_plan(json.dumps(value))
        _native.optimize_observe(
            planned,
            json.dumps(
                {
                    "call_site_id": value["call_site_id"],
                    "input_tokens": 100,
                    "output_tokens": 100,
                    "latency_ms": 100,
                    "cost_usd": 0.001,
                }
            ),
        )
    return json.loads(_native.optimize_plan(json.dumps(value)))


@pytest.mark.parametrize("opaque_history", [False, True])
def test_versioned_client_tools_receive_a_real_native_lease(opaque_history):
    value = call()
    if opaque_history:
        value["parameters"]["extra"]["agentc_native_messages_opaque"] = True
    planned = warm_and_plan(value)
    assert planned["kind"] == "pass_through"
    exploration = planned["agentc_exploration_context"]
    candidate = exploration["candidate_plan"]
    assert candidate["kind"] == "rewritten" and candidate["rule"] == "OutputBudget"
    assert candidate["call"]["tools"] == value["tools"]
    assert candidate["call"]["messages"] == value["messages"]
    assert candidate["agentc_observation_context"]["key"]["execution_plan_id"]
    assert _native.optimize_fail_exploration(exploration["lease_token"])


@pytest.mark.parametrize(
    "changed",
    [
        "legacy",
        "unknown_version",
        "missing_settings",
        "bad_settings",
        "stream",
        "inconsistent_tools",
        "unsupported_protocol",
    ],
)
def test_unsupported_tool_contracts_cannot_reserve_a_counterfactual(changed):
    value = call()
    context = value["parameters"]["extra"]["agentc_route_context"]
    if changed == "legacy":
        context.pop("tool_action_comparator_version")
    elif changed == "unknown_version":
        context["tool_action_comparator_version"] = "unknown"
    elif changed == "missing_settings":
        context.pop("tool_settings_version")
    elif changed == "bad_settings":
        context["tool_settings_version"] = "not-a-cohort"
    elif changed == "stream":
        context["streaming"] = True
    elif changed == "inconsistent_tools":
        context["tool_calling"] = False
    else:
        context["provider_protocol"] = "unsupported"
    assert "agentc_exploration_context" not in warm_and_plan(value)


@pytest.mark.parametrize(
    "field", ["tool_action_comparator_version", "tool_settings_version"]
)
def test_comparator_and_tool_settings_are_bound_to_native_profile_identity(
    field, monkeypatch
):
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "0")
    before = call()
    after = deepcopy(before)
    after["parameters"]["extra"]["agentc_route_context"][field] += "-different"
    keys = [
        json.loads(_native.optimize_plan(json.dumps(value)))[
            "agentc_observation_context"
        ]["key"]
        for value in (before, after)
    ]
    assert keys[0]["call_site_version"] != keys[1]["call_site_version"]


def test_sdk_declares_tool_comparison_and_versions_tool_choice():
    value = request()
    contexts = []
    for choice in (None, "auto", "none", "required"):
        value["tool_choice"] = choice
        contexts.append(
            build_call_dict_openai(
                value,
                call_site_id="SDK-contract",
                trace_id_hex="00" * 16,
                span_id_hex="01" * 8,
            )["parameters"]["extra"]["agentc_route_context"]
        )
    assert all(
        context["tool_action_comparator_version"] == VERSION for context in contexts
    )
    assert contexts[0]["tool_settings_version"] == contexts[1]["tool_settings_version"]
    assert len({context["tool_settings_version"] for context in contexts}) == 3


def test_cached_text_blocks_receive_a_real_native_tool_lease_without_flattening():
    value = request()
    value["messages"][0]["content"] = [
        {
            "type": "text",
            "text": "Synthetic tool control.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    before = deepcopy(value)
    built = build_call_dict_openai(
        value,
        call_site_id="cached-tool-contract",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
    )
    assert value == before
    context = built["parameters"]["extra"]["agentc_route_context"]
    assert context["tool_action_comparator_version"] == VERSION
    planned = warm_and_plan(built)
    assert (
        planned["agentc_exploration_context"]["candidate_plan"]["call"]["tools"]
        == built["tools"]
    )
    assert _native.optimize_fail_exploration(
        planned["agentc_exploration_context"]["lease_token"]
    )


def test_cache_layout_changes_identity_but_prompt_text_does_not(monkeypatch):
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "0")
    value = request()
    keys = []
    settings = []
    for content in [
        "plain",
        [{"type": "text", "text": "first"}],
        [{"type": "text", "text": "second"}],
        [{"type": "text", "text": "second", "cache_control": {"type": "ephemeral"}}],
    ]:
        value["messages"][0]["content"] = content
        built = build_call_dict_openai(
            value,
            call_site_id="cached-tool-contract",
            trace_id_hex="00" * 16,
            span_id_hex="01" * 8,
        )
        settings.append(
            built["parameters"]["extra"]["agentc_route_context"][
                "tool_settings_version"
            ]
        )
        keys.append(
            json.loads(_native.optimize_plan(json.dumps(built)))[
                "agentc_observation_context"
            ]["key"]
        )
    assert settings[1] == settings[2] and keys[1] == keys[2]
    assert len(set(settings)) == 3
    assert keys[0] != keys[1] != keys[3]


@pytest.mark.parametrize(
    "changed", ["ttl", "unknown", "envelope_role", "empty", "null_text"]
)
def test_unfrozen_cached_shapes_do_not_advertise_comparison(changed):
    value = request()
    block = {"type": "text", "text": "fixture", "cache_control": {"type": "ephemeral"}}
    value["messages"][0]["content"] = [block]
    if changed == "ttl":
        block["cache_control"]["ttl"] = "1h"
    elif changed == "unknown":
        block["future_feature"] = True
    elif changed == "envelope_role":
        value["messages"][0]["cache_control"] = {"type": "ephemeral"}
    elif changed == "empty":
        value["messages"][0]["content"] = []
    else:
        block["text"] = None
    context = build_call_dict_openai(
        value,
        call_site_id="unsupported-cached-tool-contract",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
    )["parameters"]["extra"]["agentc_route_context"]
    assert "tool_action_comparator_version" not in context


@pytest.mark.parametrize(
    "changed", ["parallel", "named_choice", "description", "strict"]
)
def test_complete_tool_settings_change_native_profile_identity(changed, monkeypatch):
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "0")
    before = request()
    after = deepcopy(before)
    if changed == "parallel":
        after["parallel_tool_calls"] = False
    elif changed == "named_choice":
        after["tool_choice"] = {
            "type": "function",
            "function": {"name": "fixture_action"},
        }
    elif changed == "description":
        after["tools"][0]["function"]["description"] = "Different tool contract."
    else:
        after["tools"][0]["function"]["strict"] = True
    keys = []
    for value in (before, after):
        built = build_call_dict_openai(
            value,
            call_site_id="SDK-contract",
            trace_id_hex="00" * 16,
            span_id_hex="01" * 8,
        )
        planned = json.loads(_native.optimize_plan(json.dumps(built)))
        keys.append(planned["agentc_observation_context"]["key"]["call_site_version"])
    assert keys[0] != keys[1]


@pytest.mark.parametrize(
    "changed", ["hosted", "image", "opaque", "stream", "multi", "bool_count"]
)
def test_sdk_does_not_advertise_a_comparator_for_unsupported_shapes(changed):
    value = request()
    if changed == "hosted":
        value["tools"] = [{"type": "web_search"}]
    elif changed == "image":
        value["messages"][0]["content"] = [
            {"type": "image_url", "image_url": {"url": "fixture"}}
        ]
    elif changed == "opaque":
        value["messages"][0]["audio"] = "opaque"
    elif changed == "stream":
        value["stream"] = True
    else:
        value["n"] = True if changed == "bool_count" else 2
    context = build_call_dict_openai(
        value,
        call_site_id="SDK-contract",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
    )["parameters"]["extra"]["agentc_route_context"]
    assert "tool_action_comparator_version" not in context
