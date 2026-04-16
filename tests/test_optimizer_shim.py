"""Unit tests for ``agentc._optimizer`` — the typed Python shim.

We don't test the native FFI here (that's covered in Rust); we test the
dataclass assembly + fail-open wrapping.
"""

from __future__ import annotations

from unittest.mock import patch

from agentc._optimizer import PASS_THROUGH, Plan, observe_outcome, plan_call


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

    with patch("agentc._optimizer._native.optimize_observe", side_effect=_observe):
        plan = Plan(kind="pass_through", raw_json='{"kind":"pass_through"}')
        observe_outcome(plan, {"input_tokens": 5, "output_tokens": 3})
    assert len(captured) == 1
    assert captured[0][0] == '{"kind":"pass_through"}'
    assert '"input_tokens": 5' in captured[0][1]


def test_observe_outcome_suppresses_native_failure():
    def boom(_a, _b):
        raise RuntimeError("native fail")

    with patch("agentc._optimizer._native.optimize_observe", side_effect=boom):
        # Must not raise.
        observe_outcome(Plan(kind="pass_through"), {"input_tokens": 1, "output_tokens": 1})


def test_observe_outcome_suppresses_unserializable_outcome():
    # Should not call native, should not raise.
    call_count = {"n": 0}

    def _observe(_a, _b):
        call_count["n"] += 1

    with patch("agentc._optimizer._native.optimize_observe", side_effect=_observe):
        observe_outcome(Plan(kind="pass_through"), {"weird": object()})
    assert call_count["n"] == 0
