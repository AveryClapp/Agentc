from copy import deepcopy

import pytest

from bench.openrouter_pilot import PilotError, digest
from bench.openrouter_replay import lexical_divergence
from bench.openrouter_rules_diagnostics import analyze, repeated_source_requests
from bench.openrouter_rules_live import semantic_plan
from bench.openrouter_rules_protocol import STAGES


def artifact(specs=((64, "plan"),)):
    schedule = [{"arm": "joint", "task_id": str(i), "phase": "development"} for i in range(len(specs))]
    manifest = {"schedule": schedule, "endpoints": {m: {"tag": "provider"} for m in ("source", "cheap")},
        "shadow_seed": "test", "policies": {"joint": {"AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE": "20", "AGENTC_OPTIMIZE_SHADOW": "0"}}}
    paid_stage = "rules-live-dev-v1-" + digest(manifest)[:20]
    decisions, calls, intents = [], [], []
    for workflow, (cap, plan_id) in zip(schedule, specs):
        for stage in STAGES:
            item = {**workflow, "workflow_stage": stage}
            original = {"model": "source", "parameters": {"max_output_tokens": 512, "temperature": 0},
                "messages": [{"role": "user", "content": "Question"}], "tools": []}
            plan = {"kind": "pass_through"}
            if stage == "answer":
                candidate = deepcopy(original)
                candidate["model"] = "cheap"
                candidate["parameters"]["max_output_tokens"] = cap
                plan["agentc_exploration_context"] = {"candidate_plan": {"kind": "rewritten", "rule": "OutputBudget", "call": candidate,
                    "agentc_observation_context": {"key": {"call_site_version": "site", "execution_plan_id": plan_id}, "divergence_threshold": .01}}}
            signature = semantic_plan(original, plan, manifest)
            intents.append({**item, "original_call": original, "native_plan": plan, "semantic_plan": signature})
            scopes = [("primary", signature["primary"], "Entity", 20, ".01")]
            if stage == "answer":
                scopes.append(("exploration", signature["candidate"], "entity", 10, ".005"))
            incurred = []
            for scope, payload, answer, tokens, cost in scopes:
                call_id = paid_stage + "-" + digest([item, scope])[:24]
                incurred.append(call_id)
                calls.append({**item, "id": call_id, "stage": paid_stage, "generation_id": "gen-" + call_id,
                    "scope": scope, "finish_reason": "stop", "answer": answer, "request_sha256": digest(payload), "decision_sha256": digest(signature),
                    "usage": {"prompt_tokens": 10, "completion_tokens": tokens}, "nominal_uncached_cost_usd": cost})
            decisions.append({**item, "native_plan": plan, "semantic_plan": signature,
                "primary_id": incurred[0], "incurred_ids": incurred,
                "divergence_feedback": lexical_divergence("Entity", "entity") if stage == "answer" else None})
    return manifest, decisions, calls, intents


def test_exact_cap_identities_are_not_pooled():
    report = analyze(*artifact(((64, "plan"), (96, "other-plan"))))
    assert report["exact_plans_with_feedback"] == 2 and report["paired_decisions"] == 2
    assert {p["max_tokens"] for p in report["plans"]} == {64, 96}
    assert all(p["observed_pairs"] == 1 and p["below_minimum_pair_count"] for p in report["plans"])


def test_feedback_is_proxy_not_damage_or_native_guard_state():
    report = analyze(*artifact())
    plan = report["plans"][0]
    assert plan["mean_divergence"] == 1
    assert plan["observed_positive_excess_sum"] == .99
    assert plan["mean_output_token_difference"] == -10
    assert "damage" not in plan and "disabled" not in plan and "false_positive" not in plan
    assert report["paper_evidence"] is False


def test_provider_error_marks_all_diagnostics_as_incident_only():
    values = artifact()
    values[2][0]["finish_reason"] = "error"
    report = analyze(*values)
    assert report["analysis_eligible"] is False
    assert report["failed_provider_calls"][0]["id"] == values[2][0]["id"]
    assert report["paired_decisions"] == 1  # raw trace retained, not certified


def test_inconsistent_exact_plan_dispatch_rejected():
    with pytest.raises(PilotError, match="inconsistent"):
        analyze(*artifact(((64, "plan"), (96, "plan"))))


@pytest.mark.parametrize("mutation", ["duplicate", "feedback", "identity", "scope", "request", "task", "stage", "decisions"])
def test_invalid_attribution_rejected(mutation):
    manifest, decisions, calls, intents = artifact()
    if mutation == "duplicate":
        calls.append(deepcopy(calls[0]))
    elif mutation == "feedback":
        decisions[-1]["divergence_feedback"] = 0
    elif mutation == "identity":
        decisions[-1]["semantic_plan"]["candidate_identity"] = {}
    elif mutation == "scope":
        calls[-1]["scope"] = "shadow"
    elif mutation == "request":
        calls[-1]["request_sha256"] = "different"
    elif mutation == "task":
        calls[-1]["task_id"] = "different"
    elif mutation == "stage":
        calls[-1]["workflow_stage"] = "filter"
    else:
        decisions *= 20
    with pytest.raises(PilotError):
        analyze(manifest, decisions, calls, intents)


def test_warmup_self_consistency_uses_disjoint_exact_payload_pairs():
    decisions, rows = [], {}
    for i in range(5):
        row_id = str(i)
        decisions.append({"phase": "warmup", "native_plan": {"kind": "pass_through"}, "task_id": "q",
            "workflow_stage": "filter", "primary_id": row_id})
        rows[row_id] = {"id": row_id, "model": "source", "provider": "provider", "request_sha256": "same",
            "answer": "Entity" if i % 2 else "entity", "usage": {"completion_tokens": 10}}
    report = repeated_source_requests(decisions, rows)
    assert len(report["pairs"]) == 2  # not ten dependent all-pairs comparisons
    assert len({r for p in report["pairs"] for r in p["row_ids"]}) == 4
    assert report["by_stage"][0]["pairs_exceeding_0_03"] == 2
    rows["1"]["request_sha256"] = "different"
    assert len(repeated_source_requests(decisions, rows)["pairs"]) == 2
    rows["3"]["request_sha256"] = "third"
    assert len(repeated_source_requests(decisions, rows)["pairs"]) == 1
