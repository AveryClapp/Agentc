"""Mechanism analyses use paired evaluator data without hiding negative effects."""
from copy import deepcopy

import pytest

from bench.openrouter_contract import messages
from bench.openrouter_frontier import CONTEXTS, SOURCE_MODEL
from bench.openrouter_mechanisms import guard_proxy, indexed, interactions, mean_interval, planner_diagnostics, support_retention
from bench.openrouter_pilot import PilotError


def matrix():
    tasks = {context: {} for context in CONTEXTS}
    rows = []
    manifest = {"endpoints": {SOURCE_MODEL: {}, "cheap": {}}}
    for context in CONTEXTS:
        for i in range(2):
            task = {"task_id": str(i), "prompt": "Who?", "expected": "Ada", "meta": {"paragraphs": [
                {"title": "Evidence", "sentences": ["Ada is here."], "supporting": True},
                {"title": "Distractor", "sentences": ["Other."], "supporting": False}]}}
            tasks[context][str(i)] = task
            for model in manifest["endpoints"]:
                for arm in ("full", "compress"):
                    original = messages(task, "reinforced")
                    rows.append({"id": f"{context}/{i}/{model}/{arm}", "context": context, "task_id": str(i),
                        "phase": "holdout", "model": model, "arm": arm, "f1": 1., "em": 1., "answer": "Ada",
                        "request_sha256": f"{context}/{i}/{model}/{arm}", "nominal_uncached_cost_usd": "1",
                        "native_plan": {"kind": "rewritten", "call": {"messages": [original[0], original[1], original[-1]]}}
                        if arm == "compress" else {"kind": "pass_through"}})
    return manifest, rows, tasks


def test_interaction_uses_four_outcomes_for_same_question():
    manifest, rows, _ = matrix()
    for row in rows:
        if row["model"] == SOURCE_MODEL and row["arm"] == "full":
            row["f1"] = .5
        if row["model"] == "cheap" and row["arm"] == "compress":
            row["f1"] = .25
    report = interactions(manifest, rows)
    assert len(report) == 2
    for result in report:
        assert result["source_rewrite_f1_delta"] == .5
        assert result["model_rewrite_f1_delta"] == -.75
        assert result["difference_in_differences"] == -1.25
        assert result["paired_bootstrap_95"] == [-1.25, -1.25]


def test_support_retention_distinguishes_context_removal_from_answer_harm():
    _, rows, tasks = matrix()
    kept = support_retention(rows, tasks, "reinforced")
    assert all(r["support_removed_total"] == 0 for r in kept)
    for row in rows:
        if row["arm"] == "compress":
            msgs = row["native_plan"]["call"]["messages"]
            row["native_plan"]["call"]["messages"] = [msgs[0], msgs[-1]]
    removed = support_retention(rows, tasks, "reinforced")
    assert all(r["support_removed_total"] == 2 for r in removed)
    assert all(r["categories"][1]["f1_loss_count"] == 0 for r in removed)
    assert all(r["pairs"][0]["removed_support_titles"] == ["Evidence"] for r in removed)


def test_support_retention_rejects_edits_or_unprotected_question():
    _, rows, tasks = matrix()
    row = next(r for r in rows if r["arm"] == "compress")
    row["native_plan"]["call"]["messages"][1]["content"] = "Edited evidence"
    with pytest.raises(PilotError, match="whole-paragraph"):
        support_retention(rows, tasks, "reinforced")


def test_guard_disagreement_is_not_automatically_task_damage():
    manifest, rows, _ = matrix()
    for row in rows:
        if row["model"] == "cheap":
            row["answer"] = "Ada."
    report = guard_proxy(manifest, rows)
    cheap = [r for r in report if r["model"] == "cheap"]
    assert all(r["counts"]["flagged_true_f1_loss_false"] == 2 for r in cheap)
    assert all(r["counts"]["flagged_both_exact_correct"] == 2 for r in cheap)
    assert cheap[0]["examples"][0]["lexical_divergence"] == 1


def test_noop_guard_comparison_uses_designated_full_not_lucky_repeat():
    manifest, rows, _ = matrix()
    cells = indexed(rows)
    for row in rows:
        if row["arm"] == "compress":
            row.update(answer="wrong", f1=0., em=0., request_sha256=cells[(row["context"], row["task_id"], row["model"], "full")]["request_sha256"])
    assert all(r["counts"] == {"flagged_false_f1_loss_false": 2} for r in guard_proxy(manifest, rows))


def test_planner_charges_exploration_and_reports_rejection_snapshots():
    _, rows, _ = matrix()
    ref = next(r for r in rows if r["model"] == SOURCE_MODEL and r["arm"] == "full")
    d = {"context": ref["context"], "task_id": ref["task_id"], "nominal_uncached_cost_estimate_usd": "1.25",
        "native_plan": {"kind": "pass_through", "agentc_planner_diagnostics": {"candidates": [
            {"plan_id": "p", "target_model_id": "cheap", "rejection_reason": "insufficient_evidence:3/20",
             "estimate": {"paired_observations": 3}}]}}}
    replay = {"trajectories": [{"policy": "joint", "context": ref["context"], "decisions": [d]}]}
    result = planner_diagnostics(replay, rows)[0]
    assert result["candidate_rejection_events"] == {"insufficient_evidence": 1}
    assert result["nominal_cost_reduction_with_setup"] == -.25
    assert result["first_nonreference_primary_task"] is None
    assert result["latest_seen_candidate_states"][0]["estimate"]["paired_observations"] == 3


def test_duplicate_cells_and_invalid_bootstrap_rejected():
    _, rows, _ = matrix()
    with pytest.raises(PilotError, match="duplicate"):
        indexed(rows + [deepcopy(rows[0])])
    with pytest.raises(PilotError):
        mean_interval([], "x")
    with pytest.raises(PilotError):
        mean_interval([1.], "x", draws=0)
