from copy import deepcopy

import pytest

from bench.openrouter_pilot import PilotError
from bench.openrouter_replay import lexical_divergence
from bench.openrouter_rules_diagnostics import analyze


def pair(index=0, cap=64, plan_id="plan", primary="Entity", candidate="entity"):
    identity = {"key": {"call_site_version": "site", "execution_plan_id": plan_id}, "divergence_threshold": .01}
    calls = [{"id": str(index) + scope, "scope": scope, "answer": answer,
        "usage": {"prompt_tokens": 10, "completion_tokens": tokens}, "nominal_uncached_cost_usd": cost}
        for scope, answer, tokens, cost in (("primary", primary, 20, ".01"), ("exploration", candidate, 10, ".005"))]
    decision = {"arm": "joint", "workflow_stage": "answer", "task_id": str(index),
        "divergence_feedback": lexical_divergence(primary, candidate), "primary_id": calls[0]["id"],
        "incurred_ids": [r["id"] for r in calls], "semantic_plan": {"candidate": {"model": "cheap", "max_tokens": cap},
            "candidate_identity": identity, "candidate_rules": ["ModelDowngrade", "OutputBudget"]}}
    manifest = {"policies": {"joint": {"AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE": "20"}}}
    return manifest, decision, calls


def test_exact_cap_identities_are_not_pooled():
    manifest, a, rows_a = pair()
    _, b, rows_b = pair(1, 96, "other-plan")
    report = analyze(manifest, [a, b], rows_a + rows_b)
    assert report["exact_plans_with_feedback"] == 2 and report["paired_decisions"] == 2
    assert {p["max_tokens"] for p in report["plans"]} == {64, 96}
    assert all(p["observed_pairs"] == 1 and p["below_minimum_pair_count"] for p in report["plans"])


def test_feedback_is_proxy_not_damage_or_native_guard_state():
    manifest, decision, calls = pair()
    report = analyze(manifest, [decision], calls)
    plan = report["plans"][0]
    assert plan["mean_divergence"] == 1
    assert plan["observed_positive_excess_sum"] == .99
    assert plan["mean_output_token_difference"] == -10
    assert "damage" not in plan and "disabled" not in plan and "false_positive" not in plan
    assert report["paper_evidence"] is False


def test_inconsistent_exact_plan_dispatch_rejected():
    manifest, a, rows_a = pair()
    _, b, rows_b = pair(1, 96)
    with pytest.raises(PilotError, match="inconsistent"):
        analyze(manifest, [a, b], rows_a + rows_b)


@pytest.mark.parametrize("mutation", ["duplicate", "feedback", "identity", "scope"])
def test_invalid_attribution_rejected(mutation):
    manifest, decision, calls = pair()
    if mutation == "duplicate":
        calls.append(deepcopy(calls[0]))
    elif mutation == "feedback":
        decision["divergence_feedback"] = 0
    elif mutation == "identity":
        decision["semantic_plan"]["candidate_identity"] = {}
    else:
        calls[1]["scope"] = "shadow"
    with pytest.raises(PilotError):
        analyze(manifest, [decision], calls)
