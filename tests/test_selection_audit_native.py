"""Actual native selection counts and evidence; no provider or patched plans."""

import json

import pytest

from agentc import _native
from agentc._patches._optimizer_glue import build_call_dict_openai


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_OPTIMIZE", "1")
    monkeypatch.setenv("AGENTC_COMPOSE", "1")
    monkeypatch.setenv("AGENTC_ENABLED_RULES", "OutputBudget")
    monkeypatch.setenv("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "50")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "0")
    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "0.02")
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "joint_guarded")
    monkeypatch.setenv("AGENTC_OPTIMIZE_SELECTION_AUDIT", "1")
    yield
    _native.optimize_reset()


def call():
    return build_call_dict_openai(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "private original input"}],
            "max_tokens": 1024,
        },
        call_site_id="selection.audit.control",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
    )


def plan():
    return json.loads(_native.optimize_plan(json.dumps(call())))


def observe(value):
    _native.optimize_observe(
        json.dumps(value),
        json.dumps(
            {
                "call_site_id": "selection.audit.control",
                "model": "gpt-4o",
                "input_tokens": 100,
                "output_tokens": 100,
                "latency_ms": 20,
                "cost_usd": 0.004,
                "output_is_short": True,
            }
        ),
    )


def test_cold_native_count_is_reference_only_and_search_is_not_complete(tmp_path):
    _native.optimize_configure(str(tmp_path))
    value = plan()
    audit = value["agentc_selection_audit"]
    assert audit["schema_version"] == 1
    assert audit["candidate_count"] == 1
    assert audit["search_status"] == "cold_call_site"
    assert not audit["enumeration_complete"]
    assert (
        audit["effective_selected_plan_id"]
        == value["agentc_observation_context"]["key"]["execution_plan_id"]
    )
    assert "private original input" not in json.dumps(audit)


def test_warm_native_count_and_cold_exact_candidate_rejection_are_measured(tmp_path):
    _native.optimize_configure(str(tmp_path))
    for _ in range(3):
        value = plan()
        observe(value)
    value = plan()
    audit = value["agentc_selection_audit"]
    assert audit["candidate_count"] == 2
    assert audit["enumeration_complete"] and audit["search_status"] == "complete"
    assert len(audit["alternatives"]) == 1
    rejection = audit["selection"]["rejections"][0]
    assert rejection["plan_id"] == audit["alternatives"][0]["id"]
    assert rejection["reason"]["reason"] == "missing_estimate"
    assert audit["policy"]["min_paired_observations"] == 20
    assert audit["policy"]["divergence_exposure_budget"] == 1
    assert value["kind"] == "pass_through"


def test_opt_in_is_frozen_per_lifecycle_and_default_envelope_is_unchanged(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENTC_OPTIMIZE_SELECTION_AUDIT", "0")
    _native.optimize_configure(str(tmp_path))
    plain = plan()
    assert "agentc_selection_audit" not in plain
    monkeypatch.setenv("AGENTC_OPTIMIZE_SELECTION_AUDIT", "1")
    assert plan() == plain
    _native.optimize_configure(str(tmp_path / "audited"))
    audited = plan()
    audited.pop("agentc_selection_audit")
    assert audited == plain


def test_disabled_selector_and_failed_identity_do_not_fabricate_warm_opportunity(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENTC_OPTIMIZE", "0")
    _native.optimize_configure(str(tmp_path))
    value = plan()
    audit = value["agentc_selection_audit"]
    assert audit["search_status"] == "disabled"
    assert not audit["enumeration_complete"]
    assert audit["candidate_count"] == 1
    invalid = json.loads(_native.optimize_plan("invalid JSON"))
    assert invalid["kind"] == "pass_through"
    assert invalid["agentc_selection_audit"]["candidate_count"] is None
    assert invalid["agentc_selection_audit"]["abstention_reason"] == "invalid_request"


def test_unknown_model_retains_pass_through_and_marks_identity_unavailable(tmp_path):
    _native.optimize_configure(str(tmp_path))
    request = call()
    request["model"] = "unregistered-fixture-model"
    value = json.loads(_native.optimize_plan(json.dumps(request)))
    assert value["kind"] == "pass_through"
    audit = value["agentc_selection_audit"]
    assert audit["candidate_count"] is None
    assert audit["effective_selected_plan_id"] is None
    assert audit["abstention_reason"] == "reference_identity_unavailable"


def test_greedy_mode_reports_missing_complete_enumeration_without_guessing_counts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "current_greedy")
    _native.optimize_configure(str(tmp_path))
    audit = plan()["agentc_selection_audit"]
    assert audit["planner_mode"] == "current_greedy"
    assert audit["candidate_count"] is None
    assert audit["abstention_reason"] == "complete_plans_not_enumerated"
