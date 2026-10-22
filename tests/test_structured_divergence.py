"""Tool outcomes must not collapse into identical empty assistant text."""

from types import SimpleNamespace
from copy import deepcopy
import json

import pytest

from agentc._patches._optimizer_glue import (
    _response_output_text,
    _text_divergence,
    build_outcome_openai,
    estimate_cost_usd,
    _structured_output_schema_version,
)


def chat(name="cancel", arguments='{"order":"a"}', call_id="id", refusal=None):
    message = SimpleNamespace(
        content=None,
        refusal=refusal,
        tool_calls=[]
        if refusal
        else [
            SimpleNamespace(
                id=call_id,
                type="function",
                function=SimpleNamespace(name=name, arguments=arguments),
            )
        ],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.parametrize("mode", ["lexical", "normalized", "embedding"])
@pytest.mark.parametrize(
    "changed",
    [
        chat(name="refund"),
        chat(arguments='{"order":"b"}'),
        chat(refusal="cannot act"),
        "",
    ],
)
def test_different_actions_are_maximally_divergent_in_every_meter(
    mode, changed, monkeypatch
):
    monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", mode)
    reference = _response_output_text(chat())
    candidate = _response_output_text(changed)
    assert reference is not None and candidate is not None
    assert _text_divergence(reference, candidate) == 1.0


def test_volatile_tool_ids_and_argument_key_order_do_not_change_actions():
    a = _response_output_text(chat(arguments='{"x":1,"y":2}', call_id="a"))
    b = _response_output_text(chat(arguments=' {"y":2, "x":1} ', call_id="b"))
    assert _text_divergence(a, b) == 0


def test_openai_and_anthropic_same_action_have_identical_semantic_projection():
    openai = _response_output_text(chat())
    anthropic = _response_output_text(
        SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "different",
                    "name": "cancel",
                    "input": {"order": "a"},
                }
            ]
        )
    )
    assert _text_divergence(openai, anthropic) == 0
    different = _response_output_text(
        SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "different",
                    "name": "cancel",
                    "input": {"order": "b"},
                }
            ]
        )
    )
    assert _text_divergence(openai, different) == 1


def test_duplicate_argument_keys_are_not_normalized_into_a_valid_action():
    invalid = _response_output_text(chat(arguments='{"order":"wrong","order":"a"}'))
    assert _text_divergence(invalid, _response_output_text(chat())) == 1


def test_malformed_tool_envelope_is_unknown_rather_than_empty_agreement():
    response = chat()
    response.choices[0].message.tool_calls[0].function.arguments = None
    assert _response_output_text(response) is None


@pytest.mark.parametrize("cost", [0, "0.0042", 0.003])
def test_non_byok_provider_charge_warms_the_cost_model_with_actual_billing(cost):
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=20, completion_tokens=5, cost=cost, is_byok=False
        ),
        choices=[],
    )
    outcome = build_outcome_openai(
        response, elapsed_s=0.1, model="gpt-4o-mini", call_site_id="site"
    )
    assert outcome["cost_usd"] == float(cost)


@pytest.mark.parametrize(
    "cost", [None, True, -1, float("nan"), float("inf"), "invalid"]
)
def test_invalid_reported_charge_does_not_inject_nonfinite_or_negative_profiles(cost):
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=20, completion_tokens=5, cost=cost, is_byok=False
        ),
        choices=[],
    )
    outcome = build_outcome_openai(
        response, elapsed_s=0.1, model="gpt-4o-mini", call_site_id="site"
    )
    assert outcome["cost_usd"] == estimate_cost_usd("gpt-4o-mini", 20, 5)


def test_byok_router_cost_is_not_substituted_for_unknown_provider_billing():
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=20, completion_tokens=5, cost=0, is_byok=True
        ),
        choices=[],
    )
    outcome = build_outcome_openai(
        response, elapsed_s=0.1, model="gpt-4o-mini", call_site_id="site"
    )
    assert outcome["cost_usd"] == estimate_cost_usd("gpt-4o-mini", 20, 5)


def test_tool_guard_upgrade_versions_profiles_without_changing_plain_text_identity():
    assert _structured_output_schema_version({}) is None
    tools = [
        {
            "type": "function",
            "function": {"name": "fixture", "parameters": {"type": "object"}},
        }
    ]
    assert _structured_output_schema_version({"tools": tools}) is not None
    response_format = {"type": "json_object"}
    ordinary = _structured_output_schema_version({"response_format": response_format})
    assert ordinary != _structured_output_schema_version(
        {"response_format": response_format, "tools": tools}
    )


def test_tool_guard_marker_changes_the_real_native_call_site_version(
    tmp_path, monkeypatch
):
    from agentc import _native
    from agentc._patches._optimizer_glue import build_call_dict_openai

    monkeypatch.setenv("AGENTC_OPTIMIZE", "1")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "0")
    monkeypatch.setenv("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "50")
    request = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "fixture"}],
        "max_tokens": 128,
        "tools": [
            {
                "type": "function",
                "function": {"name": "fixture", "parameters": {"type": "object"}},
            }
        ],
    }
    call = build_call_dict_openai(
        request,
        call_site_id="tool-guard-version",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
    )
    previous = deepcopy(call)
    for value in previous["parameters"]["extra"].values():
        if isinstance(value, dict):
            value.pop("structured_output_schema_version", None)
    _native.optimize_reset()
    _native.optimize_configure(str(tmp_path))
    try:
        old = json.loads(_native.optimize_plan(json.dumps(previous)))
        new = json.loads(_native.optimize_plan(json.dumps(call)))
        old_key = old["agentc_observation_context"]["key"]
        new_key = new["agentc_observation_context"]["key"]
        assert old_key["call_site_version"] != new_key["call_site_version"]
    finally:
        _native.optimize_reset()
