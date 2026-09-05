import json
from copy import deepcopy
from decimal import Decimal

import pytest

from bench.openrouter_matrix import file_hash
from bench.openrouter_pilot import PilotError, digest
from bench.openrouter_rules_comparison import compare, load_tasks
from bench.openrouter_rules_live import ARMS, semantic_plan
from bench.openrouter_rules_protocol import STAGES


def artifact():
    schedule = [{"arm": "calibration/source", "phase": "calibration", "task_id": "c"}]
    schedule += [{"arm": arm, "phase": phase, "task_id": task}
                 for phase, task in (("warmup", "w"), ("development", "q1"), ("development", "q2")) for arm in ARMS]
    manifest = {"schedule": schedule, "endpoints": {"source": {"tag": "provider"}}, "shadow_seed": "test",
                "policies": {arm: {"AGENTC_OPTIMIZE_SHADOW": "0"} for arm in ARMS}}
    paid_stage = "rules-live-dev-v1-" + digest(manifest)[:20]
    decisions, calls, intents = [], [], []
    for workflow in schedule:
        for stage in STAGES:
            item = {**workflow, "workflow_stage": stage}
            original = {"model": "source", "parameters": {"max_output_tokens": 512, "temperature": 0},
                        "messages": [{"role": "user", "content": "Question"}], "tools": []}
            plan = {"kind": "pass_through"}
            if workflow["arm"] == "joint" and stage == "answer":
                candidate = deepcopy(original)
                candidate["parameters"]["max_output_tokens"] = 64
                plan["agentc_exploration_context"] = {"candidate_plan": {"kind": "rewritten", "rule": "OutputBudget", "call": candidate}}
            signature = semantic_plan(original, plan, manifest)
            intents.append({**item, "original_call": original, "native_plan": plan, "semantic_plan": signature})
            scopes = [("calibration" if workflow["phase"] == "calibration" else "primary", signature["primary"])]
            if signature["candidate"] is not None:
                scopes.append(("exploration", signature["candidate"]))
            ids = []
            for scope, payload in scopes:
                call_id = paid_stage + "-" + digest([item, scope])[:24]
                ids.append(call_id)
                calls.append({**item, "id": call_id, "stage": paid_stage, "generation_id": "gen-" + call_id,
                    "scope": scope, "finish_reason": "stop", "answer": "wrong" if workflow["arm"] == "joint" and scope == "primary" else "Entity",
                    "request_sha256": digest(payload), "decision_sha256": digest(signature),
                    "cost_usd": ".01", "nominal_uncached_cost_usd": ".02"})
            decisions.append({**item, "semantic_plan": signature, "primary_id": ids[0], "incurred_ids": ids})
    return manifest, decisions, calls, intents, {task: {"expected": "Entity"} for task in ("c", "w", "q1", "q2")}


def prefix(values, decision_count, pending_primary=False):
    manifest, decisions, calls, intents, tasks = values
    count = sum(len(d["incurred_ids"]) for d in decisions[:decision_count])
    return manifest, decisions[:decision_count], calls[:count+int(pending_primary)], intents[:decision_count+int(pending_primary)], tasks


def test_complete_matched_costs_include_probes_but_scores_use_primary():
    report = compare(*artifact())
    assert report["matched_task_ids"] == ["q1", "q2"]
    joint = next(r for r in report["reports"] if r["arm"] == "joint")
    assert joint["matched_mean_f1"] == 0 and joint["matched_mean_f1_delta_vs_original"] == -1
    assert Decimal(joint["matched_billed_usd"]) == Decimal(".08")
    assert Decimal(joint["matched_primary_billed_usd"]) == Decimal(".06")
    assert Decimal(joint["matched_probe_shadow_billed_usd"]) == Decimal(".02")
    assert Decimal(joint["matched_nominal_uncached_usd"]) == Decimal(".16")
    assert report["paper_evidence"] is False


def test_unbalanced_prefix_and_unrecorded_paid_primary_are_not_matched():
    # calibration3 + warmup18 + complete q1 across arms18 + q2 first arm3;
    # next arm has one paid filter call but no completed decision yet.
    report = compare(*prefix(artifact(), 42, pending_primary=True))
    assert report["matched_task_ids"] == ["q1"]
    reports = {r["arm"]: r for r in report["reports"]}
    assert reports["original"]["completed_development_questions"] == 2
    assert reports["historical_rules"]["completed_development_questions"] == 1
    assert Decimal(reports["original"]["unmatched_or_partial_development_billed_usd"]) == Decimal(".03")
    assert Decimal(reports["historical_rules"]["unmatched_or_partial_development_billed_usd"]) == Decimal(".01")
    for row in reports.values():
        assert Decimal(row["all_artifact_billed_usd"]) == sum(Decimal(row[k]) for k in
            ("setup_billed_usd", "matched_billed_usd", "unmatched_or_partial_development_billed_usd"))


def test_no_shared_complete_workflows_emits_null_scores():
    report = compare(*prefix(artifact(), 24))
    assert report["comparison_available"] is False and report["paired_questions"] == []
    assert all(r["matched_mean_f1"] is None for r in report["reports"])


def test_calibration_is_charged_once_only_to_sequential_setup():
    report = compare(*artifact())
    reports = {r["arm"]: r for r in report["reports"]}
    assert Decimal(reports["sequential"]["setup_billed_usd"]) == Decimal(".06")
    assert Decimal(reports["original"]["setup_billed_usd"]) == Decimal(".03")
    assert sum(Decimal(r["all_artifact_billed_usd"]) for r in report["reports"]) == Decimal(".60")


def test_comparison_validates_journal_prefix():
    values = artifact()
    values[2][-1]["request_sha256"] = "corrupt"
    with pytest.raises(PilotError, match="intent prefix"):
        compare(*values)


@pytest.mark.parametrize("finish", ["error", "content_filter", "tool_calls", None])
def test_failed_provider_completion_suppresses_all_quality_not_accounting(finish):
    values = artifact()
    values[2][0]["finish_reason"] = finish
    report = compare(*values)
    assert report["analysis_eligible"] is False and report["comparison_available"] is False
    assert report["structurally_complete_common_task_ids"] == ["q1", "q2"]
    assert report["matched_task_ids"] == report["paired_questions"] == []
    assert all(row["matched_mean_f1"] is None for row in report["reports"])
    assert sum(Decimal(row["all_artifact_billed_usd"]) for row in report["reports"]) == Decimal(".60")
    assert report["failed_provider_calls"][0]["id"] == values[2][0]["id"]


def test_length_stops_are_retained_not_selectively_removed():
    values = artifact()
    values[2][-1]["finish_reason"] = "length"
    assert compare(*values)["analysis_eligible"] is True


def test_fixture_is_bound_and_unique(tmp_path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps([{"task_id": "q", "expected": "answer"}]))
    manifest = {"fixture_sha256": file_hash(fixture)}
    assert load_tasks(manifest, fixture)["q"]["expected"] == "answer"
    fixture.write_text("[]")
    with pytest.raises(PilotError, match="differs"):
        load_tasks(manifest, fixture)
    fixture.write_text(json.dumps([{"task_id": "q"}, {"task_id": "q"}]))
    with pytest.raises(PilotError, match="duplicate"):
        load_tasks({"fixture_sha256": file_hash(fixture)}, fixture)
