"""Provider controls isolate evidence without copying private request data."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from agentc._provider_config import VERSION, declaration

PROTOCOL = "openai.chat.completions.v1"


@pytest.mark.parametrize(
    "control",
    [
        {"seed": 19},
        {"reasoning_effort": "high"},
        {"tool_choice": "required"},
        {"tool_choice": {"type": "function", "function": {"name": "lookup"}}},
        {"parallel_tool_calls": False},
        {"frequency_penalty": 0.5},
        {"presence_penalty": 0.5},
        {"logit_bias": {"17": -10}},
        {"top_logprobs": 5},
        {"logprobs": True},
        {"n": 2},
        {"verbosity": "high"},
        {"service_tier": "priority"},
        {"top_k": 20},
        {"stop_sequences": ["END"]},
        {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        {"output_config": {"effort": "high"}},
        {"betas": ["known-feature-v1"]},
        {"max_tokens": 256},
        {"max_completion_tokens": 256},
        {"extra_body": {"reasoning": {"effort": "high", "exclude": True}}},
        {"extra_headers": {"Anthropic-Beta": "feature-v1"}},
    ],
)
def test_generation_and_queue_controls_change_declared_cohort(control):
    baseline = declaration({}, provider_protocol=PROTOCOL)
    result = declaration(control, provider_protocol=PROTOCOL)
    assert result["supported"] is True
    assert result["version"] == VERSION
    assert result["sha256"] != baseline["sha256"]
    assert set(result) == {"version", "supported", "sha256"}


def test_protocols_and_present_null_have_distinct_settings_cohorts():
    openai = declaration({}, provider_protocol=PROTOCOL)
    anthropic = declaration({}, provider_protocol="anthropic.messages.v1")
    litellm = declaration({}, provider_protocol="litellm.completion.v1")
    assert len({r["sha256"] for r in (openai, anthropic, litellm)}) == 3
    assert declaration({"seed": None}, provider_protocol=PROTOCOL) != openai


def test_nested_object_order_is_canonical_and_inputs_are_unchanged():
    first = {
        "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        "logit_bias": {"17": -1, "42": 1},
    }
    second = {
        "logit_bias": {"42": 1, "17": -1},
        "extra_body": {"thinking": {"budget_tokens": 2048, "type": "enabled"}},
    }
    before = deepcopy(first)
    assert declaration(first, provider_protocol=PROTOCOL) == declaration(
        second, provider_protocol=PROTOCOL
    )
    assert first == before


def test_request_content_credentials_and_user_annotations_are_not_inspected():
    class Sensitive:
        def __repr__(self):
            raise AssertionError("private data must not be rendered")

    request = {
        name: Sensitive()
        for name in (
            "messages",
            "system",
            "model",
            "tools",
            "api_key",
            "client",
            "metadata",
            "user",
            "safety_identifier",
            "api_base",
            "timeout",
        )
    }
    assert declaration(request, provider_protocol=PROTOCOL) == declaration(
        {}, provider_protocol=PROTOCOL
    )


def test_credentials_in_headers_are_not_part_of_the_provider_configuration():
    a = {"extra_headers": {"Authorization": "secret-A", "x-api-key": "key-A"}}
    b = {"extra_headers": {"authorization": "secret-B", "X-API-Key": "key-B"}}
    assert declaration(a, provider_protocol=PROTOCOL) == declaration(
        b, provider_protocol=PROTOCOL
    )
    assert "secret" not in json.dumps(declaration(a, provider_protocol=PROTOCOL))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"unknown_future_control": True},
        {"extra_body": {"unknown": "private-value"}},
        {"extra_body": {"messages": [{"content": "private"}]}},
        {"prediction": {"type": "content", "content": "private"}},
        {"audio": {"voice": "alloy", "format": "wav"}},
        {"web_search_options": {"user_location": "private"}},
        {"context_management": {"edits": ["private"]}},
        {"thinking": {"type": "enabled", "budget_tokens": 2048, "unknown": True}},
        {"thinking": {"type": "enabled", "budget_tokens": float("nan")}},
        {"frequency_penalty": float("inf")},
        {"seed": object()},
        {"extra_headers": {"x-unknown-generation-mode": "private"}},
        {"extra_headers": {"Anthropic-Beta": "a", "anthropic-beta": "b"}},
        {"extra_query": {"custom": "private"}},
        {"functions": [{"name": "legacy-function", "parameters": {}}]},
        {
            "extra_body": {
                "output_config": {"format": {"type": "json_schema", "schema": {}}}
            }
        },
    ],
)
def test_unprojected_or_opaque_controls_abstain_without_serializing_their_values(
    kwargs,
):
    result = declaration(kwargs, provider_protocol=PROTOCOL)
    assert result == {"version": VERSION, "supported": False, "sha256": None}
    assert "private" not in json.dumps(result)


def test_output_format_is_left_to_its_separate_schema_identity():
    settings = {"output_config": {"effort": "high"}}
    with_format = {
        "output_config": {
            "effort": "high",
            "format": {"schema": {"properties": {"input": {}}}},
        }
    }
    assert declaration(settings, provider_protocol=PROTOCOL) == declaration(
        with_format, provider_protocol=PROTOCOL
    )


def test_deep_or_cyclic_settings_cannot_enter_a_profile():
    value = {}
    value["cycle"] = value
    result = declaration({"logit_bias": value}, provider_protocol=PROTOCOL)
    assert result["supported"] is False and result["sha256"] is None


def test_provider_omission_sentinels_have_the_same_config_as_absence():
    from openai import NOT_GIVEN

    assert declaration({"seed": NOT_GIVEN}, provider_protocol=PROTOCOL) == declaration(
        {}, provider_protocol=PROTOCOL
    )


def test_frozen_swe_agent_default_completion_settings_are_supported():
    kwargs = {
        "model": "openai/gpt-4o-2024-11-20",
        "messages": [],
        "temperature": 0.0,
        "top_p": 1.0,
        "api_version": None,
        "api_key": "private",
        "fallbacks": None,
        "n": None,
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
    }
    assert (
        declaration(kwargs, provider_protocol="litellm.completion.v1")["supported"]
        is True
    )
    kwargs["fallbacks"] = ["other-provider/model"]
    assert (
        declaration(kwargs, provider_protocol="litellm.completion.v1")["supported"]
        is False
    )
