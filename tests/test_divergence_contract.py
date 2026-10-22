"""Contradiction controls and native identity for the normalized metric."""

from unittest.mock import patch

import pytest

from agentc._patches._optimizer_glue import _text_divergence


@pytest.mark.parametrize("mode", ["normalized", "embedding"])
@pytest.mark.parametrize(
    "left,right",
    [
        ("answer is Paris", "answer is not Paris"),
        ("allow the transfer", "do not allow the transfer"),
        ("delete account", "never delete account"),
        ("total is 10", "total is 10 or 100"),
    ],
)
def test_added_negation_or_alternative_is_not_exact_agreement(
    monkeypatch, mode, left, right
):
    monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", mode)
    with patch("agentc._native.embed_text_bytes", return_value=None):
        assert _text_divergence(left, right) > 0
        assert _text_divergence(left, right) == _text_divergence(right, left)


@pytest.mark.parametrize(
    "left,right",
    [("The PARIS!", "Paris"), ("an answer.", "answer"), ("A red ball", "red ball")],
)
def test_normalized_formatting_controls_preserve_zero(monkeypatch, left, right):
    monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "normalized")
    assert _text_divergence(left, right) == 0


def test_sdk_rejects_a_mismatched_normalized_native_contract(monkeypatch):
    from agentc._patches._optimizer_glue import build_call_dict_openai

    monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "normalized")
    with patch("agentc._native.optimize_divergence_metric_version", return_value="1"):
        with pytest.raises(RuntimeError, match="divergence metric identity"):
            build_call_dict_openai(
                {"model": "gpt-4o", "messages": [{"role": "user", "content": "task"}]},
                call_site_id="metric.contract",
                trace_id_hex="00" * 16,
                span_id_hex="01" * 8,
            )


@pytest.mark.parametrize(
    "mode,version",
    [
        ("lexical", "1"),
        ("normalized", "normalized-jaccard-v2"),
        ("embedding", "embedding-cosine-normalized-jaccard-v2"),
        (" NORMALIZED ", "normalized-jaccard-v2"),
        ("unknown", "1"),
    ],
)
def test_native_metric_versions_include_fallback_and_preserve_lexical(mode, version):
    from agentc import _native

    assert _native.optimize_divergence_metric_version(mode) == version


@pytest.mark.parametrize("mode", ["lexical", "normalized", "embedding"])
def test_real_native_planning_remains_available_under_versioned_metrics(
    mode, tmp_path, monkeypatch
):
    import json
    from agentc import _native
    from agentc._patches._optimizer_glue import build_call_dict_openai

    for name, value in {
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_COMPOSE": "1",
        "AGENTC_ENABLED_RULES": "OutputBudget",
        "AGENTC_EVAL_PLANNER_MODE": "current_greedy",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_MODEL_CATALOG_OFFLINE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_SHADOW_DIVERGENCE_MODE": mode,
    }.items():
        monkeypatch.setenv(name, value)
    _native.optimize_reset()
    _native.optimize_configure(str(tmp_path))
    try:
        call = build_call_dict_openai(
            {
                "model": "gpt-4o-2024-11-20",
                "messages": [{"role": "user", "content": "private task"}],
                "max_tokens": 1024,
            },
            call_site_id="metric.native.policy",
            trace_id_hex="00" * 16,
            span_id_hex="01" * 8,
        )
        outcome = {
            "call_site_id": call["call_site_id"],
            "input_tokens": 100,
            "output_tokens": 100,
            "latency_ms": 100,
            "cost_usd": 0.01,
        }
        for _ in range(3):
            plan = _native.optimize_plan(json.dumps(call))
            assert json.loads(plan)["kind"] == "pass_through"
            assert _native.optimize_observe(plan, json.dumps(outcome))
        result = json.loads(_native.optimize_plan(json.dumps(call)))
        assert result["kind"] == "rewritten"
        assert result["agentc_observation_context"]["key"]["execution_plan_id"]
        assert _native.optimize_divergence_metric_version(mode) == (
            "1"
            if mode == "lexical"
            else "normalized-jaccard-v2"
            if mode == "normalized"
            else "embedding-cosine-normalized-jaccard-v2"
        )
    finally:
        _native.optimize_reset()
