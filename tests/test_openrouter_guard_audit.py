"""Training-only synthetic audit tests: no provider/native/ledger access."""

from copy import deepcopy
import json

import pytest

from bench import openrouter_guard_audit as audit
from bench.openrouter_pilot import PilotError, digest


def pair(task="t", reference="Paris", candidate="Paris", stage="answer", scope="exploration"):
    item = {"task_id": task, "phase": "training", "arm": "joint", "workflow_stage": stage}
    identity = {"key": {"call_site_version": stage, "execution_plan_id": "plan"}, "divergence_threshold": .03}
    sig = {"kind": "pass_through" if scope == "exploration" else "rewritten",
           "primary": {"model": "source" if scope == "exploration" else "target"},
           "candidate": {"model": "target"} if scope == "exploration" else None,
           "candidate_identity": identity, "primary_identity": identity,
           "candidate_rules": ["ModelDowngrade"], "primary_rules": ["ModelDowngrade"]}
    texts = [reference, candidate] if scope == "exploration" else [candidate, reference]
    calls = [{**item, "id": task + str(i), "answer": text, "scope": "primary" if i == 0 else scope,
              "model": "source" if (scope == "exploration") == (i == 0) else "target"}
             for i, text in enumerate(texts)]
    d = {**item, "primary_id": calls[0]["id"], "incurred_ids": [c["id"] for c in calls],
         "semantic_plan": sig, "divergence_feedback": audit.lexical_divergence(reference, candidate)}
    decisions = [d]
    if stage != "answer":
        endpoint = {**calls[0], "id": task + "end", "workflow_stage": "answer", "answer": "Paris"}
        calls.append(endpoint)
        decisions.append({**d, "workflow_stage": "answer", "primary_id": endpoint["id"],
                          "incurred_ids": [endpoint["id"]], "divergence_feedback": None})
    return decisions, calls, {task: "Paris"}


def test_zero_feedback_is_not_missing_and_both_wrong_is_not_correct():
    ds, cs, gold = pair(reference="Rome", candidate="Rome")
    rows = audit.paired_records(ds, cs, gold)
    report = audit.summarize_pairs(rows)
    assert report["n"] == report["zero_divergence"] == 1
    assert report["answer_quality"]["zero_divergence"]["em"]["both_zero"] == 1


def test_zero_token_set_divergence_does_not_imply_equal_f1():
    ds, cs, gold = pair(reference="Paris Paris", candidate="Paris")
    result = audit.summarize_pairs(audit.paired_records(ds, cs, gold))
    assert result["zero_divergence"] == 1
    assert result["answer_quality"]["all"]["f1"]["gains"] == 1


def test_positive_divergence_can_be_equal_normalized_quality():
    ds, cs, gold = pair(reference="The Paris", candidate="Paris")
    row = audit.paired_records(ds, cs, gold)[0]
    assert row["over_threshold"]
    assert row["reference_score"] == row["candidate_score"] == {"f1": 1., "em": 1.}


def test_shadow_pair_is_oriented_candidate_minus_reference():
    ds, cs, gold = pair(reference="Paris", candidate="Rome", scope="shadow")
    row = audit.paired_records(ds, cs, gold)[0]
    assert row["reference_id"] == "t1" and row["candidate_id"] == "t0"
    result = audit.quality_summary([row])
    assert result["f1"]["mean_paired_delta"] == -1


def test_early_stage_factual_endpoint_is_not_candidate_quality():
    ds, cs, gold = pair(reference="Long source paragraph", candidate="Missing detail", stage="filter")
    row = audit.paired_records(ds, cs, gold)[0]
    assert row["factual_workflow_endpoint_score"]["f1"] == 1
    assert row["candidate_suffix_quality"] is None
    assert row["reference_score"] is None and row["candidate_score"] is None
    assert audit.summarize_pairs([row])["answer_quality"]["all"] == {"n": 0}


@pytest.mark.parametrize("feedback", [True, float("nan"), float("inf"), -1, 1.1, .5])
def test_invalid_or_mismatched_feedback_rejected(feedback):
    ds, cs, gold = pair()
    ds[0]["divergence_feedback"] = feedback
    with pytest.raises(PilotError):
        audit.paired_records(ds, cs, gold)


@pytest.mark.parametrize("change", ["scope", "task", "duplicate", "reused", "identity", "endpoint", "heldout", "model"])
def test_invalid_pair_binding_rejected(change):
    ds, cs, gold = pair()
    if change == "scope": cs[1]["scope"] = "primary"
    if change == "task": cs[1]["task_id"] = "other"
    if change == "duplicate": cs.append(deepcopy(cs[0]))
    if change == "reused": ds.append(deepcopy(ds[0]))
    if change == "identity": ds[0]["semantic_plan"]["candidate_identity"] = None
    if change == "endpoint": ds[0]["workflow_stage"] = cs[0]["workflow_stage"] = cs[1]["workflow_stage"] = "filter"
    if change == "heldout": cs[1]["phase"] = "heldout"
    if change == "model": cs[1]["model"] = "wrong-model"
    with pytest.raises(PilotError):
        audit.paired_records(ds, cs, gold)


def test_gold_loader_only_accesses_training_expected_fields(tmp_path):
    fixture = tmp_path / "fixture.json"
    # Heldout deliberately has no expected field: the loader must not ask for it.
    fixture.write_text(json.dumps([{"task_id": "train", "expected": "Paris"}, {"task_id": "heldout"}]))
    manifest = {"fixture_sha256": audit.file_hash(fixture), "schedule": [
        {"task_id": "train", "phase": "training"}, {"task_id": "heldout", "phase": "heldout"}]}
    assert audit.load_training_gold(fixture, manifest) == {"train": "Paris"}
    manifest["schedule"].append({"task_id": "train", "phase": "heldout"})
    with pytest.raises(PilotError, match="shared"):
        audit.load_training_gold(fixture, manifest)


def test_gold_fixture_change_rejected(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_text("[]")
    with pytest.raises(PilotError, match="fixture"):
        audit.load_training_gold(path, {"fixture_sha256": "bad"})


def test_source_repeats_are_disjoint_and_exact_payload_only():
    calls, decisions = [], []
    for i in range(7):
        row = {"id": str(i), "phase": "training", "task_id": "t", "workflow_stage": "filter",
               "model": "source", "provider": "provider", "request_sha256": "same" if i < 6 else "different",
               "answer": "same" if i % 2 else "changed"}
        calls.append(row)
        decisions.append({**row, "primary_id": row["id"], "semantic_plan": {"kind": "pass_through"}})
    result = audit.repeated_references(decisions, calls, "source")
    assert len(result["pairs"]) == 3
    assert len({i for p in result["pairs"] for i in p["row_ids"]}) == 6
    assert result["by_stage"][0]["distinct_questions"] == 1


def test_proposal_selection_ignores_outcomes_and_is_order_invariant():
    rows = []
    for stage in ("filter", "synthesize", "answer"):
        for i in range(3):
            ds, cs, gold = pair(task=stage + str(i), stage=stage)
            rows += audit.paired_records(ds, cs, gold)
    expected = audit.diagnostic_proposal(rows, "manifest")
    changed = deepcopy(list(reversed(rows)))
    for row in changed:
        row.update(divergence=.999, reference_score={"f1": 0, "em": 0}, candidate_score={"f1": 1, "em": 1})
    assert audit.diagnostic_proposal(changed, "manifest") == expected
    assert len(expected["cases"]) == 3
    assert expected["maximum_new_calls_without_retries"] == 7 + 4 + 1


def test_training_gold_scope_and_incomplete_journals_rejected_before_validation():
    manifest = {"schedule": [{"task_id": "train", "phase": "training"}]}
    with pytest.raises(PilotError, match="exactly"):
        audit.analyze(manifest, [], [], [], {"heldout": "secret"})
    with pytest.raises(PilotError, match="complete"):
        audit.analyze(manifest, [], [], [], {"train": "Paris"})


def test_main_refuses_heldout_before_opening_gold(tmp_path, monkeypatch):
    for name, value in {"manifest": {}, "decisions": [{"phase": "heldout"}], "calls": [], "intents": []}.items():
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    monkeypatch.setattr(audit, "load_training_gold", lambda *args: pytest.fail("gold must stay unopened"))
    monkeypatch.setattr("sys.argv", ["audit", "--artifacts", str(tmp_path), "--fixture", "never-open", "--output", str(tmp_path / "out.json")])
    assert audit.main() == 1
    assert not (tmp_path / "out.json").exists()


def test_main_rejects_changing_snapshot(tmp_path, monkeypatch):
    for name, value in {"manifest": {}, "decisions": [], "calls": [], "intents": []}.items():
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    def changed(*args):
        (tmp_path / "calls.json").write_text("[{}]")
        return {}
    monkeypatch.setattr(audit, "load_training_gold", lambda *args: {})
    monkeypatch.setattr(audit, "analyze", changed)
    monkeypatch.setattr("sys.argv", ["audit", "--artifacts", str(tmp_path), "--fixture", "unused", "--output", str(tmp_path / "out.json")])
    assert audit.main() == 1
    assert not (tmp_path / "out.json").exists()


def complete_synthetic_journals():
    from bench import openrouter_rules_live as live
    manifest = {"source_model": "source", "schedule": [{"task_id": "t", "phase": "training", "arm": "joint"}],
                "policies": {"joint": {"AGENTC_OPTIMIZE_SHADOW": "0"}}, "shadow_seed": "synthetic",
                "endpoints": {model: {"tag": "anthropic"} for model in ("source", "target")}}
    decisions, calls, intents = [], [], []
    stage_id = "rules-live-dev-v1-" + digest(manifest)[:20]
    for stage in ("filter", "synthesize", "answer"):
        item = dict(manifest["schedule"][0], workflow_stage=stage)
        original = {"model": "source", "tools": [], "messages": [{"role": "user", "content": "Public question"}],
                    "parameters": {"max_output_tokens": 512, "temperature": 0}}
        identity = {"key": {"call_site_version": stage, "execution_plan_id": "source"}, "divergence_threshold": .03}
        plan = {"kind": "pass_through", "agentc_observation_context": identity}
        if stage == "answer":
            candidate = {"kind": "rewritten", "rule": "ModelDowngrade", "call": {**original, "model": "target"},
                         "agentc_observation_context": {**identity, "key": {"call_site_version": stage, "execution_plan_id": "target"}}}
            plan["agentc_exploration_context"] = {"candidate_plan": candidate, "lease_token": "synthetic"}
        signature = live.semantic_plan(original, plan, manifest)
        intents.append({**item, "original_call": original, "native_plan": plan, "semantic_plan": signature})
        ids = []
        scopes = [("primary", signature["primary"])]
        if stage == "answer": scopes.append(("exploration", signature["candidate"]))
        for scope, payload in scopes:
            call_id = stage_id + "-" + digest([item, scope])[:24]
            ids.append(call_id)
            calls.append({**item, "id": call_id, "scope": scope, "stage": stage_id,
                          "request_sha256": digest(payload), "decision_sha256": digest(signature),
                          "generation_id": "g-" + call_id, "model": payload["model"], "provider": "Anthropic",
                          "answer": "Paris", "finish_reason": "stop"})
        decisions.append({**item, "native_plan": plan, "semantic_plan": signature,
                          "primary_id": ids[0], "incurred_ids": ids, "divergence_feedback": 0. if len(ids) == 2 else None})
    return manifest, decisions, calls, intents, {"t": "Paris"}


def test_complete_real_journal_validation_and_analysis():
    report = audit.analyze(*complete_synthetic_journals())
    assert report["counts"] == {"calls": 4, "decisions": 3, "training_questions": 1, "paired_scopes": {"exploration": 1}}
    assert report["exploration_summary"]["answer_quality"]["all"]["em"]["ties"] == 1
    assert not report["financial_completeness_certified"]


@pytest.mark.parametrize("change", ["request_hash", "decision_plan", "model", "provider_error", "duplicate_generation"])
def test_full_audit_rejects_changed_attribution(change):
    data = complete_synthetic_journals()
    _, decisions, calls, _, _ = data
    if change == "request_hash": calls[-1]["request_sha256"] = "wrong"
    if change == "decision_plan":
        decisions[-1] = deepcopy(decisions[-1])
        decisions[-1]["native_plan"]["agentc_exploration_context"]["candidate_plan"]["call"]["model"] = "source"
    if change == "model": calls[-1]["model"] = "other"
    if change == "provider_error": calls[-1]["finish_reason"] = "error"
    if change == "duplicate_generation": calls[-1]["generation_id"] = calls[0]["generation_id"]
    with pytest.raises(PilotError):
        audit.analyze(*data)


def test_source_answer_repeat_can_change_f1_without_optimization():
    data = complete_synthetic_journals()
    _, ds, cs, _, gold = data
    original = cs[-2]
    duplicate = {**original, "id": "repeat", "answer": "Paris extra"}
    repeat_decision = {**ds[-1], "primary_id": "repeat"}
    report = audit.repeated_references([ds[-1], repeat_decision], [original, duplicate], "source", gold)
    summary = report["by_stage"][0]["repeat_answer_quality"]
    assert summary["f1"]["losses"] == summary["em"]["losses"] == 1


def test_recorded_guard_requires_both_call_site_and_plan_identity():
    ds, cs, gold = pair()
    record = audit.paired_records(ds, cs, gold)[0]
    diagnostics = {"call_site_version": "other-site", "candidates": [{"plan_id": "plan",
        "divergence_threshold": .03, "divergence_exposure": 1.1, "rejection_reason": "disabled",
        "estimate": {"paired_observations": 2}}]}
    ds[0]["native_plan"] = {"agentc_planner_diagnostics": diagnostics}
    assert audit.recorded_guard_state(record, ds)["first_recorded_disabled"] is None
    diagnostics["call_site_version"] = "answer"
    result = audit.recorded_guard_state(record, ds)
    assert result["first_recorded_disabled"]["paired_observations"] == 2
    assert result["last_recorded_candidate_state"]["rejection_reason"] == "disabled"
    diagnostics["candidates"][0]["divergence_threshold"] = .02
    with pytest.raises(PilotError, match="threshold"):
        audit.recorded_guard_state(record, ds)
