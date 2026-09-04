"""Unit tests for ``agentc._optimizer`` — the typed Python shim.

We don't test the native FFI here (that's covered in Rust); we test the
dataclass assembly + fail-open wrapping.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from agentc._optimizer import (
    PASS_THROUGH,
    Plan,
    model_catalog,
    observe_outcome,
    plan_call,
    record_divergence,
)


def test_pass_through_shape():
    assert PASS_THROUGH.kind == "pass_through"
    assert PASS_THROUGH.is_pass_through


def test_plan_call_decodes_pass_through():
    with patch("agentc._optimizer._native.optimize_plan", return_value='{"kind":"pass_through"}'):
        p = plan_call({"call_site_id": "x", "model": "m"})
    assert p.kind == "pass_through"


def test_plan_call_decodes_cached():
    with patch(
        "agentc._optimizer._native.optimize_plan",
        return_value='{"kind":"cached","value":{"output_content_id":"abc"}}',
    ):
        p = plan_call({"call_site_id": "x", "model": "m"})
    assert p.kind == "cached"
    assert p.value == {"output_content_id": "abc"}


def test_plan_call_decodes_rewritten():
    payload = (
        '{"kind":"rewritten","rule":"ModelDowngrade",'
        '"call":{"call_site_id":"x","trace_id":"00",'
        '"span_id":"00","model":"mini","messages":[]},'
        '"projected_savings_usd":0.0042}'
    )
    with patch("agentc._optimizer._native.optimize_plan", return_value=payload):
        p = plan_call({"call_site_id": "x", "model": "m"})
    assert p.kind == "rewritten"
    assert p.rule == "ModelDowngrade"
    assert p.call is not None and p.call["model"] == "mini"
    assert abs(p.projected_savings_usd - 0.0042) < 1e-6


def test_plan_call_hydrates_versioned_dispatch_metadata():
    metadata = {
        "catalog_version": "catalog-v1",
        "price_table_version": "prices-v1",
        "provider_protocol": "openai.chat.completions.v1",
        "provider_namespace": "openai",
        "target_model_id": "gpt-5.4-mini-2026-03-17",
        "target_model_version": "gpt-5.4-mini-2026-03-17",
    }
    payload = json.dumps(
        {
            "kind": "rewritten",
            "rule": "ModelDowngrade",
            "call": {
                "model": "gpt-5.4-mini-2026-03-17",
                "parameters": {"extra": {"agentc_routed_target": metadata}},
            },
        }
    )
    original = {
        "call_site_id": "x",
        "model": "gpt-5.4-2026-03-05",
        "parameters": {
            "extra": {
                "agentc_route_context": {
                    "provider_protocol": "openai.chat.completions.v1",
                    "provider_namespace": "openai",
                }
            }
        },
    }
    with patch("agentc._optimizer._native.optimize_plan", return_value=payload):
        plan = plan_call(original)

    assert plan.provider_protocol == "openai.chat.completions.v1"
    assert plan.provider_namespace == "openai"
    assert plan.target_model_id == "gpt-5.4-mini-2026-03-17"
    assert plan.target_model_version == "gpt-5.4-mini-2026-03-17"
    assert plan.catalog_version == "catalog-v1"
    assert plan.price_table_version == "prices-v1"


def test_model_catalog_decodes_native_snapshot():
    payload = '{"catalog_version":"v1","targets":[{"model_id":"m"}]}'
    with patch("agentc._optimizer._native.optimize_model_catalog", return_value=payload):
        catalog = model_catalog()
    assert catalog["catalog_version"] == "v1"
    assert catalog["targets"] == [{"model_id": "m"}]


def test_plan_call_decodes_parallel():
    payload = (
        '{"kind":"parallel","rule":"ParallelBranch",'
        '"calls":[{"model":"m1"},{"model":"m2"}],'
        '"projected_savings_usd":0.5}'
    )
    with patch("agentc._optimizer._native.optimize_plan", return_value=payload):
        p = plan_call({"call_site_id": "x", "model": "m"})
    assert p.kind == "parallel"
    assert len(p.calls) == 2
    assert p.projected_savings_usd == 0.5


def test_plan_call_passes_through_on_bad_json():
    with patch("agentc._optimizer._native.optimize_plan", return_value="not json"):
        p = plan_call({"call_site_id": "x", "model": "m"})
    assert p.is_pass_through


def test_plan_call_passes_through_on_native_panic():
    def boom(_):
        raise RuntimeError("native blew up")

    with patch("agentc._optimizer._native.optimize_plan", side_effect=boom):
        p = plan_call({"call_site_id": "x", "model": "m"})
    assert p.is_pass_through


def test_plan_call_passes_through_on_unserializable_input():
    # ``object()`` isn't JSON-serializable.
    p = plan_call({"weird": object()})
    assert p.is_pass_through


def test_observe_outcome_forwards_raw_json():
    captured = []

    def _observe(plan_json, outcome_json):
        captured.append((plan_json, outcome_json))
        return "opaque-observation-token"

    with patch("agentc._optimizer._native.optimize_observe", side_effect=_observe):
        plan = Plan(kind="pass_through", raw_json='{"kind":"pass_through"}')
        token = observe_outcome(plan, {"input_tokens": 5, "output_tokens": 3})
    assert len(captured) == 1
    assert captured[0][0] == '{"kind":"pass_through"}'
    assert '"input_tokens": 5' in captured[0][1]
    assert token == "opaque-observation-token"
    assert plan.observation_token == "opaque-observation-token"


def test_observe_outcome_suppresses_native_failure():
    def boom(_a, _b):
        raise RuntimeError("native fail")

    plan = Plan(kind="pass_through", observation_token="stale-token")
    with patch("agentc._optimizer._native.optimize_observe", side_effect=boom):
        # Must not raise.
        observe_outcome(plan, {"input_tokens": 1, "output_tokens": 1})
    assert plan.observation_token is None


def test_observe_outcome_suppresses_unserializable_outcome():
    # Should not call native, should not raise.
    call_count = {"n": 0}

    def _observe(_a, _b):
        call_count["n"] += 1

    with patch("agentc._optimizer._native.optimize_observe", side_effect=_observe):
        observe_outcome(Plan(kind="pass_through"), {"weird": object()})
    assert call_count["n"] == 0


def test_record_divergence_forwards_only_opaque_token_and_value():
    with patch("agentc._optimizer._native.optimize_record_divergence") as native:
        record_divergence("opaque-observation-token", 0.25)

    native.assert_called_once_with("opaque-observation-token", 0.25)
