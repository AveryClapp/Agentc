"""Qualified OpenAI LiteLLM requests need actual native profile identities."""

import json

import pytest

from agentc import _native
from agentc._patches._optimizer_glue import (
    LITELLM_COMPLETION_PROTOCOL,
    build_call_dict_openai,
)


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-4o",
        "openai/gpt-4o-2024-11-20",
        "openai/gpt-5.4",
        "openai/gpt-5.4-2026-03-05",
    ],
)
def test_qualified_litellm_openai_call_has_native_plan_profile_key(
    model, tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENTC_OPTIMIZE", "1")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "0")
    monkeypatch.setenv("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "50")
    call = build_call_dict_openai(
        {
            "model": model,
            "messages": [{"role": "user", "content": "fixture"}],
            "max_tokens": 1024,
        },
        call_site_id="litellm-qualified-openai",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
        provider_protocol=LITELLM_COMPLETION_PROTOCOL,
    )
    _native.optimize_reset()
    _native.optimize_configure(str(tmp_path))
    try:
        plan = json.loads(_native.optimize_plan(json.dumps(call)))
        context = plan["agentc_observation_context"]
        assert context["key"]["call_site_version"]
        assert context["key"]["execution_plan_id"]
        assert context["runtime_version"]["target_model_id"] == model
    finally:
        _native.optimize_reset()


def test_litellm_openai_targets_preserve_pinned_provider_facts():
    catalog = json.loads(_native.optimize_model_catalog())
    direct = [
        target
        for target in catalog["targets"]
        if target["adapter_protocol"] == "openai.chat.completions.v1"
        and target["provider_namespace"] == "openai"
    ]
    adapted = [
        target
        for target in catalog["targets"]
        if target["adapter_protocol"] == LITELLM_COMPLETION_PROTOCOL
        and target["provider_namespace"] == "openai"
    ]
    assert len(adapted) == len(direct) == 4
    for source in direct:
        target = next(
            target
            for target in adapted
            if target["model_id"] == "openai/" + source["model_id"]
        )
        for field in (
            "price",
            "capabilities",
            "routing_group",
            "context_window_tokens",
            "max_output_tokens",
            "output_token_parameter",
            "revision_kind",
            "provenance",
        ):
            assert target[field] == source[field]
        assert target["aliases"] == ["openai/" + alias for alias in source["aliases"]]
    assert catalog["catalog_version"] != "agentc-model-catalog-2026-09-03-r1"
    assert catalog["price_table_version"] == "agentc-price-table-2026-10-12-r2"
