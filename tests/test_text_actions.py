from __future__ import annotations

from types import SimpleNamespace
import json

import pytest

from agentc._text_actions import (
    TextActionContract,
    current_text_action_contract,
    text_action_scope,
)
from agentc._patches._optimizer_glue import (
    _response_output_text,
    _text_divergence,
    build_call_dict_openai,
)

CONTRACT = TextActionContract("text-action-settings-v1:" + "a" * 64)


def response(command="printf '%s\\n' reference", thought="THOUGHT: first"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=thought + "\n```bash\n" + command + "\n```",
                    refusal=None,
                    tool_calls=None,
                ),
            )
        ]
    )


@pytest.mark.parametrize("mode", ["lexical", "normalized", "embedding"])
@pytest.mark.parametrize(
    "command",
    [
        "printf '%s\\n' candidate",
        "PRINTF '%s\\n' reference",
        "printf '%s\\n' reference > file",
    ],
)
def test_shell_actions_compare_exactly_in_every_prose_meter(mode, command, monkeypatch):
    monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", mode)
    with text_action_scope(CONTRACT):
        a, b = (
            _response_output_text(response()),
            _response_output_text(response(command)),
        )
    assert _text_divergence(a, b) == 1


def test_prose_and_outer_whitespace_do_not_change_the_executed_action():
    with text_action_scope(CONTRACT):
        a = _response_output_text(response())
        b = _response_output_text(
            response("  printf '%s\\n' reference  ", "THOUGHT: changed prose")
        )
    assert _text_divergence(a, b) == 0
    assert current_text_action_contract() is None


@pytest.mark.parametrize(
    "text",
    [
        "missing fence",
        "```python\npass\n```",
        "```bash\nx\n```\n```bash\ny\n```",
        "```bash\r\nx\r\n```",
    ],
)
def test_malformed_actions_never_supply_agreement_even_with_each_other(text):
    with text_action_scope(CONTRACT):
        invalid = _response_output_text(text)
        valid = _response_output_text(response())
    assert invalid is not None and _text_divergence(invalid, invalid) == 1
    assert _text_divergence(valid, invalid) == 1


@pytest.mark.parametrize(
    "change",
    ["length", "empty_choices", "multi_choice", "refusal", "tool", "missing_content"],
)
def test_unavailable_response_envelopes_remain_unknown(change):
    value = response()
    if change == "length":
        value.choices[0].finish_reason = "length"
    elif change == "empty_choices":
        value.choices = []
    elif change == "multi_choice":
        value.choices *= 2
    elif change == "refusal":
        value.choices[0].message.refusal = "no"
    elif change == "tool":
        value.choices[0].message.tool_calls = [object()]
    else:
        value.choices[0].message.content = None
    with text_action_scope(CONTRACT):
        assert _response_output_text(value) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"n": 2},
        {"n": True},
        {"stream": True},
        {"stream": 0},
        {"tools": [{}]},
        {"response_format": {"type": "json_object"}},
        {"tool_choice": "auto"},
    ],
)
def test_unsupported_request_declarations_cannot_fall_into_ordinary_text_identity(
    extra,
):
    request = {
        "model": "openai/gpt-4o-2024-11-20",
        "messages": [{"role": "user", "content": "fixture"}],
        **extra,
    }
    with text_action_scope(CONTRACT):
        call = build_call_dict_openai(
            request,
            call_site_id="fixture",
            trace_id_hex="0" * 32,
            span_id_hex="1" * 16,
            provider_protocol="litellm.completion.v1",
        )
    declaration = call["parameters"]["extra"]["agentc_route_context"]
    assert declaration["text_action_comparator_version"] == "mini-swe-bash-v1"
    assert declaration["text_action_contract_supported"] is False


def test_nested_settings_restore_and_no_contract_preserves_ordinary_text():
    other = TextActionContract("text-action-settings-v1:" + "b" * 64)
    with text_action_scope(CONTRACT):
        with text_action_scope(other):
            assert current_text_action_contract() == other
        assert current_text_action_contract() == CONTRACT
    assert current_text_action_contract() is None
    assert _response_output_text("ordinary text") == "ordinary text"


@pytest.mark.parametrize(
    "field",
    [
        "text_action_settings_version",
        "text_action_comparator_version",
        "text_action_contract_supported",
    ],
)
def test_action_declarations_change_actual_native_cohort_identity(field, tmp_path):
    from agentc import _native

    _native.optimize_reset()
    _native.optimize_configure(str(tmp_path))
    request = {
        "model": "gpt-4o-2024-11-20",
        "messages": [{"role": "user", "content": "fixture"}],
    }
    try:
        legacy = build_call_dict_openai(
            request, call_site_id="fixture", trace_id_hex="0" * 32, span_id_hex="1" * 16
        )
        with text_action_scope(CONTRACT):
            declared = build_call_dict_openai(
                request,
                call_site_id="fixture",
                trace_id_hex="0" * 32,
                span_id_hex="1" * 16,
            )

        def version(call):
            plan = json.loads(_native.optimize_plan(json.dumps(call)))
            return plan["agentc_observation_context"]["key"]["call_site_version"]

        declared_version = version(declared)
        assert version(legacy) != declared_version
        declared["parameters"]["extra"]["agentc_route_context"][field] = "changed"
        assert version(declared) != declared_version
    finally:
        _native.optimize_reset()
