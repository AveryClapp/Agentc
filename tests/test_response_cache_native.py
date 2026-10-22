"""Full SDK request keys and real native cache lookup/seeding round trips."""

import copy
import json
import pickle
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentc import _native
from agentc._optimizer import observe_outcome, plan_call
from agentc._patches._optimizer_glue import (
    build_call_dict_anthropic,
    build_call_dict_openai,
    decode_cached_response,
)
from agentc._response_cache import VERSION, request_declaration, seed_response

OPENAI = "openai.chat.completions.v1"
ANTHROPIC = "anthropic.messages.v1"
LITELLM = "litellm.completion.v1"


def request(**changes):
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": " task "}],
        "temperature": 0,
        "max_tokens": 128,
        "seed": 1,
        **changes,
    }


def call(kwargs=None, protocol=OPENAI):
    builder = (
        build_call_dict_anthropic if protocol == ANTHROPIC else build_call_dict_openai
    )
    options = {} if protocol == ANTHROPIC else {"provider_protocol": protocol}
    return builder(
        request() if kwargs is None else kwargs,
        call_site_id="sdk.cache.key",
        trace_id_hex="00" * 16,
        span_id_hex="00" * 8,
        **options,
    )


def key(value):
    encoded = _native.optimize_sdk_response_cache_key(json.dumps(value))
    return None if encoded is None else json.loads(encoded)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_tokens": 2048},
        {"seed": 2},
        {"temperature": 0.5},
        {"top_p": 0.8},
        {"reasoning_effort": "high"},
        {"stop": ["END"]},
        {"presence_penalty": 0.2},
        {"service_tier": "priority"},
        {"model": "gpt-4o-2024-11-20"},
        {"messages": [{"role": "user", "content": "task"}]},
        {"response_format": {"type": "json_object"}},
        {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "f",
                        "description": "a",
                        "parameters": {"type": "object"},
                    },
                }
            ]
        },
    ],
)
def test_supported_request_changes_cannot_hit_the_same_sdk_entry(changes):
    left, right = key(call()), key(call(request(**changes)))
    assert left is not None and right is not None
    assert (left["prompt_hash"], left["parameters_hash"], left["model"]) != (
        right["prompt_hash"],
        right["parameters_hash"],
        right["model"],
    )


@pytest.mark.parametrize("protocol", [OPENAI, ANTHROPIC, LITELLM])
def test_keys_ignore_trace_provenance_and_bind_provider_namespace(protocol):
    kwargs = (
        request()
        if protocol != ANTHROPIC
        else request(model="claude-sonnet-4-5-20250929", seed=None)
    )
    if protocol == ANTHROPIC:
        kwargs.pop("seed", None)
    left = call(kwargs, protocol)
    right = copy.deepcopy(left)
    right["trace_id"] = "01" * 16
    right["span_id"] = "02" * 8
    right["parameters"]["extra"]["attention_scores"] = [0.2]
    assert key(left) == key(right)
    right["parameters"]["extra"]["agentc_route_context"]["provider_namespace"] = (
        "other-provider"
    )
    assert key(left)["parameters_hash"] != key(right)["parameters_hash"]


def test_original_tools_schemas_and_anthropic_system_text_are_bound():
    tools = [
        {"name": "f", "description": "original", "input_schema": {"type": "object"}}
    ]
    first = request(model="claude-sonnet-4-5-20250929", system="policy", tools=tools)
    first.pop("seed")
    changed = copy.deepcopy(first)
    changed["tools"][0]["description"] = "different"
    assert (
        key(call(first, ANTHROPIC))["parameters_hash"]
        != key(call(changed, ANTHROPIC))["parameters_hash"]
    )
    changed = {**first, "system": "different policy"}
    assert (
        key(call(first, ANTHROPIC))["prompt_hash"]
        != key(call(changed, ANTHROPIC))["prompt_hash"]
    )


@pytest.mark.parametrize(
    "mutation",
    ["missing", "legacy", "unknown", "malformed", "stream", "image", "opaque"],
)
def test_incomplete_or_unsupported_requests_have_no_sdk_key(mutation):
    value = call()
    context = value["parameters"]["extra"]["agentc_route_context"]
    if mutation == "missing":
        context.pop("sdk_response_cache")
    elif mutation == "legacy":
        context.pop("provider_configuration")
    elif mutation == "unknown":
        context["provider_configuration"]["supported"] = False
    elif mutation == "malformed":
        context["sdk_response_cache"]["message_sha256"] = "broken"
    elif mutation == "stream":
        context["streaming"] = True
    elif mutation == "image":
        context["image_input"] = True
    else:
        context["sdk_response_cache"] = request_declaration({"messages": object()})
    assert key(value) is None


@pytest.fixture
def cache_runtime(tmp_path, monkeypatch):
    import agentc

    agentc.shutdown()
    _native.optimize_reset()
    for name, value in {
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_MODEL_CATALOG_OFFLINE": "1",
        "AGENTC_EVAL_PLANNER_MODE": "current_greedy",
        "AGENTC_ENABLED_RULES": "CacheHit",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AGENTC_STORAGE_PATH", str(tmp_path))
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(tmp_path), capture_embeddings=False)
    try:
        yield agentc, tmp_path
    finally:
        agentc.shutdown()
        _native.optimize_reset()


@pytest.mark.parametrize("protocol", [OPENAI, ANTHROPIC, LITELLM])
def test_real_native_lookup_matches_seeded_requested_alias_after_merge(
    cache_runtime, protocol
):
    agentc, storage = cache_runtime
    kwargs = request()
    if protocol == ANTHROPIC:
        kwargs.update(model="claude-sonnet-4-5-20250929")
        kwargs.pop("seed")
    elif protocol == LITELLM:
        kwargs.update(model="openai/gpt-4o")
    value = call(kwargs, protocol)
    for _ in range(3):
        baseline = plan_call(value)
        assert baseline.kind == "pass_through"
        observe_outcome(
            baseline,
            {
                "call_site_id": value["call_site_id"],
                "input_tokens": 100,
                "output_tokens": 300,
                "cost_usd": 0.05,
                "latency_ms": 100,
            },
        )
    response = SimpleNamespace(
        model="provider-expanded-revision",
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=300,
            input_tokens=100,
            output_tokens=300,
        ),
        choices=[SimpleNamespace(message=SimpleNamespace(content="cached response"))],
        content=[SimpleNamespace(text="cached response")],
    )
    from agentc._writer import flush_blocking

    seed_response(baseline, response)
    assert flush_blocking()
    agentc.shutdown()
    _native.merge_all_pending()
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(storage), capture_embeddings=False)
    cached = plan_call(value)
    assert cached.kind == "cached"
    assert cached.value["sdk_response_cache_version"] == VERSION
    assert (
        decode_cached_response(cached.value, provider_protocol=protocol).model
        == "provider-expanded-revision"
    )
    different = call({**kwargs, "max_tokens": 2048}, protocol)
    assert plan_call(different).kind == "pass_through"
    different = call(
        {**kwargs, "messages": [{"role": "user", "content": "different task"}]},
        protocol,
    )
    assert plan_call(different).kind == "pass_through"


@pytest.mark.parametrize(
    "payload_kind", ["generic", "legacy", "protocol", "namespace", "shape", "version"]
)
def test_decoder_rejects_legacy_and_mismatched_protocol_payloads(payload_kind):
    value = {
        "sdk_response_cache_version": VERSION,
        "provider_protocol": OPENAI,
        "provider_namespace": "openai",
        "output_content_id": "fixture",
    }
    response = SimpleNamespace(
        choices=[], usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0)
    )
    envelope = {
        "version": VERSION,
        "provider_protocol": OPENAI,
        "provider_namespace": "openai",
        "response": response,
    }
    if payload_kind == "generic":
        envelope = response
    elif payload_kind == "legacy":
        value.pop("sdk_response_cache_version")
    elif payload_kind == "protocol":
        envelope["provider_protocol"] = ANTHROPIC
    elif payload_kind == "namespace":
        envelope["provider_namespace"] = "other"
    elif payload_kind == "shape":
        envelope["response"] = {"answer": "not an SDK response"}
    else:
        envelope["version"] = "old"
    with patch(
        "agentc._native.output_content_load", return_value=pickle.dumps(envelope)
    ):
        assert decode_cached_response(value, provider_protocol=OPENAI) is None
    assert decode_cached_response(value, provider_protocol=ANTHROPIC) is None


def test_legacy_function_entry_survives_without_becoming_an_sdk_hit(cache_runtime):
    agentc, storage = cache_runtime
    from agentc._canonicalize import parameters_hash, prompt_hash

    value = call()
    for _ in range(3):
        baseline = plan_call(value)
        observe_outcome(
            baseline,
            {
                "call_site_id": value["call_site_id"],
                "input_tokens": 100,
                "output_tokens": 300,
                "cost_usd": 0.05,
                "latency_ms": 100,
            },
        )
    old_prompt = prompt_hash(value["messages"], "openai")
    old_params = parameters_hash(value["parameters"])
    legacy = pickle.dumps({"function_result": "legacy"})
    _native.cache_insert(
        old_prompt,
        value["model"],
        old_params,
        value["call_site_id"],
        legacy,
        100,
        300,
        0.05,
        3600,
    )
    hit = _native.cache_lookup(
        old_prompt, value["model"], old_params, value["call_site_id"]
    )
    assert hit is not None
    content_id = hit["output_content_id"]
    agentc.shutdown()
    _native.merge_all_pending()
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(storage), capture_embeddings=False)
    assert plan_call(value).kind == "pass_through"
    assert _native.output_content_load(content_id) == legacy


def test_shape_declaration_contains_only_digests_and_rejects_opaque_content():
    original = request(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "private-tool",
                    "description": "private-description",
                    "parameters": {},
                },
            }
        ]
    )
    declaration = request_declaration(original)
    encoded = json.dumps(declaration)
    assert declaration["supported"]
    assert (
        "private-tool" not in encoded
        and "private-description" not in encoded
        and "task" not in encoded
    )
    assert not request_declaration({"messages": object()})["supported"]
    assert not request_declaration({"messages": [float("nan")]})["supported"]
