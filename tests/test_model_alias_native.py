"""Provider alias resolution remains usable and version-safe after restart."""

from __future__ import annotations

import json

import pytest

from agentc import _native
from agentc._patches._optimizer_glue import build_call_dict_openai


@pytest.fixture(autouse=True)
def optimizer(tmp_path, monkeypatch):
    _native.optimize_reset()
    for key, value in {
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_COMPOSE": "1",
        "AGENTC_EVAL_PLANNER_MODE": "current_greedy",
        "AGENTC_ENABLED_RULES": "OutputBudget",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "100",
        "AGENTC_MODEL_CATALOG_OFFLINE": "1",
    }.items():
        monkeypatch.setenv(key, value)
    storage = tmp_path / "state"
    _native.optimize_configure(str(storage))
    yield storage
    _native.optimize_reset()


def call(protocol):
    model = "openai/gpt-4o" if protocol == "litellm.completion.v1" else "gpt-4o"
    return build_call_dict_openai(
        {
            "model": model,
            "messages": [{"role": "user", "content": "original task"}],
            "max_tokens": 1024,
            "temperature": 0,
        },
        provider_protocol=protocol,
        call_site_id="alias.native",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
    )


def observe(plan, model, *, cost, paired=False):
    token = _native.optimize_observe(
        plan,
        json.dumps(
            {
                "call_site_id": "alias.native",
                "executed_model_id": model,
                "input_tokens": 100,
                "output_tokens": 20,
                "latency_ms": 100,
                "cost_usd": cost,
                "cost_source": "provider_reported",
            }
        ),
    )
    assert json.loads(token).get("plan_observation") is not None
    if paired:
        _native.optimize_record_divergence(token, 0.0)


def learn(protocol):
    request = call(protocol)
    for _ in range(3):
        plan = _native.optimize_plan(json.dumps(request))
        assert json.loads(plan)["kind"] == "pass_through"
        observe(plan, "gpt-4o-2024-11-20", cost=0.03)
    candidate = _native.optimize_plan(json.dumps(request))
    parsed = json.loads(candidate)
    assert parsed["kind"] == "rewritten" and parsed["rule"] == "OutputBudget"
    assert parsed["call"]["model"] == request["model"]
    assert parsed["agentc_observation_context"]["provider_alias"] is True
    for _ in range(20):
        observe(candidate, "gpt-4o-2024-11-20", cost=0.005, paired=True)
    return request


@pytest.mark.parametrize(
    "protocol", ["openai.chat.completions.v1", "litellm.completion.v1"]
)
def test_resolved_alias_can_select_its_evidenced_rewrite_after_restart(
    optimizer, monkeypatch, protocol
):
    request = learn(protocol)
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "joint_guarded")
    _native.optimize_configure(str(optimizer))
    result = json.loads(_native.optimize_plan(json.dumps(request)))
    assert result["kind"] == "rewritten", result
    assert result["rule"] == "OutputBudget"
    assert result["call"]["model"] == request["model"]


@pytest.mark.parametrize("response_model", ["gpt-4o-next-revision", "gpt-4o"])
def test_revision_change_or_opaque_response_cannot_reuse_old_alias_evidence(
    optimizer, monkeypatch, response_model
):
    request = learn("openai.chat.completions.v1")
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "joint_guarded")
    _native.optimize_configure(str(optimizer))
    assert json.loads(_native.optimize_plan(json.dumps(request)))["kind"] == "rewritten"
    # Disabling optimization yields the actual reference context for this same
    # request, preserving its identity while a new provider revision is observed.
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_OPTIMIZE", "0")
    _native.optimize_configure(str(optimizer))
    reference_plan = _native.optimize_plan(json.dumps(request))
    assert json.loads(reference_plan)["kind"] == "pass_through"
    observe(reference_plan, response_model, cost=0.03)
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_OPTIMIZE", "1")
    _native.optimize_configure(str(optimizer))
    assert (
        json.loads(_native.optimize_plan(json.dumps(request)))["kind"] == "pass_through"
    )
