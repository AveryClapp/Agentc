"""Synthetic source-suffix evidence; no Ledger.call, provider, or native calls."""

from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bench import openrouter_outcome_analysis as analysis
from bench import openrouter_outcome_diagnostic as diagnostic
from bench.openrouter_matrix import file_hash, score, write_json
from bench.openrouter_pilot import PilotError, digest, make_request
from bench.openrouter_replay import lexical_divergence


@pytest.fixture
def example(tmp_path, monkeypatch):
    task = {"task_id": "training", "prompt": "Public question?", "expected": "Paris",
            "meta": {"paragraphs": [{"title": "Public", "sentences": ["Public fact"]}]}}
    source, target = diagnostic.live.SOURCE_MODEL, diagnostic.live.TARGET_MODEL
    endpoint = {"tag": "anthropic", "provider_name": "Anthropic", "name": "Anthropic | endpoint-source",
                "pricing": {"prompt": "0.000003", "completion": "0.000015"}}
    old_manifest = {"schedule": [{"task_id": "training", "phase": "training"},
                                  {"task_id": "heldout", "phase": "heldout"}]}
    monkeypatch.setattr(diagnostic, "PREVIOUS_HASH", digest(old_manifest))
    data = {"manifest.json": old_manifest, "calls.json": [], "decisions.json": [], "intents.json": [],
            "training-gate.json": {"proceed_to_heldout": False}, "static-selection.json": {}, "summary.json": {}}
    history = {}
    for stage in analysis.STAGES:
        item = {"phase": "training", "task_id": "training", "arm": "joint", "workflow_stage": stage}
        messages = [{"role": "system", "content": "Public system"}]
        messages += [{"role": "user", "content": history[k]} for k in analysis.STAGES[:analysis.STAGES.index(stage)]]
        messages += [{"role": "user", "content": "Question: " + task["prompt"]}]
        payload = make_request(source, ["anthropic"], messages, max_tokens=512)
        reference_text = "Paris" if stage == "answer" else "old-source-" + stage
        candidate_text = reference_text if stage == "answer" else "old-candidate-" + stage
        identity = {"key": {"call_site_version": stage, "execution_plan_id": "model-plan"}, "divergence_threshold": .03}
        signature = {"kind": "pass_through", "primary": payload, "candidate": {**payload, "model": target},
                     "candidate_identity": identity, "candidate_rules": ["ModelDowngrade"]}
        data["calls.json"].extend([
            {**item, "id": "reference-" + stage, "scope": "primary", "answer": reference_text},
            {**item, "id": "candidate-" + stage, "scope": "exploration", "answer": candidate_text},
        ])
        data["decisions.json"].append({**item, "semantic_plan": signature,
            "primary_id": "reference-" + stage, "incurred_ids": ["reference-" + stage, "candidate-" + stage],
            "divergence_feedback": lexical_divergence(reference_text, candidate_text)})
        history[stage] = reference_text
    args = SimpleNamespace(artifacts=tmp_path/"new", previous=tmp_path/"old", fixture=tmp_path/"fixture.json",
                           output=tmp_path/"analysis.json")
    # Missing heldout expected field proves the analyzer does not access it.
    write_json(args.fixture, [task, {"task_id": "heldout"}])
    for name in diagnostic.FILES:
        write_json(args.previous/name, data[name])
    pairs = diagnostic.pairs_for(data)
    manifest = {"kind": "paired_source_suffix_development_v1", "paper_evidence": False,
        "source_model": source, "endpoints": {source: endpoint}, "stage_cap_usd": diagnostic.CAP,
        "source_files": diagnostic.sources(), "previous_manifest_sha256": diagnostic.PREVIOUS_HASH,
        "previous_files": {name: file_hash(args.previous/name) for name in diagnostic.FILES},
        "fixture_sha256": file_hash(args.fixture), "pairs": pairs, "schedule": diagnostic.schedule_for(pairs),
        "prompts": diagnostic.live.prompt_constants()}
    args.manifest_sha256 = digest(manifest)
    stage_id = "outcome-diagnostic-v1-" + digest(manifest)[:20]
    attention = Mock()
    attention.compute_attention_scores.side_effect = lambda messages, _: ([.5]*len(messages), [])
    pairs_by_id = {p["pair_id"]: p for p in pairs}
    histories = {}
    for p in pairs:
        for branch in analysis.BRANCHES:
            h = deepcopy(p["history"])
            if branch != "repeat": h[p["workflow_stage"]] = p[branch + "_output"]
            histories[(p["pair_id"], branch)] = h
    calls = []
    for index, item in enumerate(manifest["schedule"]):
        pair = pairs_by_id[item["pair_id"]]
        h = histories[(item["pair_id"], item["branch"])]
        # The original acquisition helper generates fixture requests. The new
        # analyzer must independently reconstruct the same provider payloads.
        payload = diagnostic.request_for(item, pair, h, task, attention, manifest)
        assert "expected" not in json.dumps(payload) and "heldout" not in json.dumps(payload)
        meta = {**item, "phase": "development", "manifest_sha256": digest(manifest),
                "dispatch_contract": {"provider_name": "Anthropic", "endpoint_model": "endpoint-source"}}
        text = ("Rome" if item["branch"] == "candidate" else "Paris") if item["workflow_stage"] == "answer" else "new-" + item["branch"] + "-" + item["workflow_stage"]
        calls.append({**item, "event": "result", "id": stage_id + "-" + digest(item)[:24], "stage": stage_id,
            "request_sha256": digest(payload), "fingerprint": digest({"payload": payload, "metadata": meta, "stage": stage_id}),
            "model": source, "provider": "Anthropic", "answer": text, "finish_reason": "stop", "generation_id": "g-" + str(index),
            "latency_ms": 10, "cost_usd": "0.00021", "cached_input_tokens": 0, "nominal_uncached_cost_usd": "0.00021",
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "cost": "0.00021", "is_byok": False,
                      "prompt_tokens_details": {"cached_tokens": 0}},
            "router_metadata": {"requested": source, "attempt": 1, "is_byok": False,
                "endpoints": {"available": [{"selected": True, "provider": "Anthropic", "model": "endpoint-source"}]}},
        })
        h[item["workflow_stage"]] = text
    comparisons = []
    for p in pairs:
        outputs = {b: histories[(p["pair_id"], b)]["answer"] for b in analysis.BRANCHES}
        comparisons.append({**{k: p[k] for k in ("pair_id", "task_id", "arm", "workflow_stage", "candidate_rules", "candidate_identity")},
            "callsite_lexical_divergence": p["lexical_divergence"],
            "candidate_endpoint_disagreement": 1-score(outputs["candidate"], outputs["reference"])["f1"],
            "repeat_endpoint_disagreement": 1-score(outputs["repeat"], outputs["reference"])["f1"],
            "scores": {b: score(text, task["expected"]) for b, text in outputs.items()},
            "repeat_callsite_lexical_divergence": lexical_divergence(histories[(p["pair_id"], "repeat")][p["workflow_stage"]], p["reference_output"])})
    summary = {"manifest_sha256": digest(manifest), "calls_sha256": digest(calls), "schedule_complete": True,
               "completed_calls": len(calls), "scheduled_calls": len(calls), "scheduled_pairs": len(pairs),
               "paired_comparisons": comparisons, "paper_evidence": False, "efficacy_claim": False,
               "incremental_artifact_paid_usd": str(Decimal("0.00021")*len(calls))}
    for name, value in (("manifest.json", manifest), ("calls.json", calls), ("summary.json", summary)):
        write_json(args.artifacts/name, value)
    return args, manifest, summary, calls, data


def test_complete_independent_reconstruction_matches_original_helper(example, monkeypatch):
    args, manifest, summary, calls, data = example
    for name in ("previous", "acquire", "retry_call"):
        monkeypatch.setattr(diagnostic, name, lambda *a, **k: pytest.fail("paid seam invoked"))
    monkeypatch.setattr(diagnostic.Ledger, "call", lambda *a, **k: pytest.fail("Ledger.call invoked"))
    result = analysis.analyze(manifest, summary, calls, data, args)
    assert result["complete_calls"] == 12 and result["complete_pairs"] == 3
    assert result["distinct_questions"] == 1
    assert result["all_pairs"]["candidate"]["f1"]["losses"] == 2
    assert result["lexical_breach_contingency"]["no_breach"]["candidate"]["f1"]["ties"] == 1
    assert result["pairs"] == summary["paired_comparisons"]
    assert not result["ledger_accessed"] and not result["efficacy_claim"]


@pytest.mark.parametrize("field", ["request_sha256", "fingerprint", "model", "provider", "branch", "stage", "id", "event", "finish_reason"])
def test_changed_request_or_result_binding_rejected(example, field):
    args, manifest, summary, calls, data = example
    calls = deepcopy(calls)
    calls[0][field] = "wrong"
    summary = {**summary, "calls_sha256": digest(calls)}
    with pytest.raises((PilotError, KeyError)):
        analysis.analyze(manifest, summary, calls, data, args)


def test_prior_branch_output_change_invalidates_later_request(example):
    args, manifest, summary, calls, data = example
    calls = deepcopy(calls)
    changed = next(row for row in calls if row["workflow_stage"] != "answer")
    changed["answer"] = "cross-branch contamination"
    with pytest.raises(PilotError):
        analysis.analyze(manifest, {**summary, "calls_sha256": digest(calls)}, calls, data, args)


@pytest.mark.parametrize("field", ["attempt", "is_byok", "selected_endpoint"])
def test_provider_endpoint_contract_rejected(example, field):
    args, manifest, summary, calls, data = example
    calls = deepcopy(calls)
    router = calls[0]["router_metadata"]
    if field == "attempt": router["attempt"] = 2
    if field == "is_byok": router["is_byok"] = True
    if field == "selected_endpoint": router["endpoints"]["available"][0]["model"] = "different"
    with pytest.raises(PilotError):
        analysis.analyze(manifest, {**summary, "calls_sha256": digest(calls)}, calls, data, args)


def test_duplicate_provider_generation_rejected(example):
    args, manifest, summary, calls, data = example
    calls = deepcopy(calls)
    calls[1]["generation_id"] = calls[0]["generation_id"]
    with pytest.raises(PilotError, match="generation"):
        analysis.analyze(manifest, {**summary, "calls_sha256": digest(calls)}, calls, data, args)


def test_summary_score_tamper_rejected(example):
    args, manifest, summary, calls, data = example
    summary = deepcopy(summary)
    summary["paired_comparisons"][0]["scores"]["candidate"]["f1"] = .123
    with pytest.raises(PilotError, match="scores"):
        analysis.analyze(manifest, summary, calls, data, args)


@pytest.mark.parametrize("change", ["drop", "extra", "partial", "wrong_hash"])
def test_only_complete_bound_journals_are_analyzed(example, change):
    args, manifest, summary, calls, data = example
    summary, calls = deepcopy(summary), deepcopy(calls)
    if change == "drop": calls.pop()
    if change == "extra": calls.append(deepcopy(calls[-1]))
    if change == "partial": summary["schedule_complete"] = False
    if change == "wrong_hash": summary["manifest_sha256"] = "wrong"
    summary["calls_sha256"] = digest(calls)
    with pytest.raises(PilotError):
        analysis.analyze(manifest, summary, calls, data, args)


def test_archive_changed_zero_feedback_is_rejected(example):
    args, manifest, summary, calls, data = example
    data = deepcopy(data)
    data["decisions.json"][-1]["divergence_feedback"] = None
    with pytest.raises(PilotError):
        analysis.analyze(manifest, summary, calls, data, args)


def test_gold_loader_accesses_only_training_pair_labels(example):
    args, manifest, _, _, data = example
    tasks, gold = analysis.load_selected_tasks(args.fixture, manifest, data["manifest.json"])
    assert gold == {"training": "Paris"} and set(tasks) == {"training"}
    assert "expected" not in tasks["training"]
    old = deepcopy(data["manifest.json"])
    old["schedule"].append({"task_id": "training", "phase": "heldout"})
    with pytest.raises(PilotError, match="exclusively"):
        analysis.load_selected_tasks(args.fixture, manifest, old)


def test_main_complete_writes_verified_report(example, monkeypatch):
    args, _, _, _, _ = example
    monkeypatch.setattr("sys.argv", ["analyze", "--artifacts", str(args.artifacts), "--previous", str(args.previous),
        "--fixture", str(args.fixture), "--output", str(args.output), "--manifest-sha256", args.manifest_sha256])
    assert analysis.main() == 0
    result = json.loads(args.output.read_text())
    assert result["complete_pairs"] == 3 and len(result["file_sha256"]) == 10


def test_partial_cli_does_not_open_calls_or_gold(example, monkeypatch):
    args, _, summary, _, _ = example
    write_json(args.artifacts/"summary.json", {**summary, "schedule_complete": False})
    monkeypatch.setattr(analysis, "load_selected_tasks", lambda *a, **k: pytest.fail("gold opened for partial"))
    original = analysis.Path.read_bytes
    def checked(path):
        if path.name == "calls.json": pytest.fail("calls opened for partial")
        return original(path)
    monkeypatch.setattr(analysis.Path, "read_bytes", checked)
    monkeypatch.setattr("sys.argv", ["analyze", "--artifacts", str(args.artifacts), "--previous", str(args.previous),
        "--fixture", str(args.fixture), "--output", str(args.output), "--manifest-sha256", args.manifest_sha256])
    assert analysis.main() == 1
    assert not args.output.exists()


def test_snapshot_change_during_analysis_rejected(example, monkeypatch):
    args, _, _, _, _ = example
    original = analysis.analyze
    def changed(*a):
        result = original(*a)
        write_json(args.artifacts/"calls.json", [])
        return result
    monkeypatch.setattr(analysis, "analyze", changed)
    monkeypatch.setattr("sys.argv", ["analyze", "--artifacts", str(args.artifacts), "--previous", str(args.previous),
        "--fixture", str(args.fixture), "--output", str(args.output), "--manifest-sha256", args.manifest_sha256])
    assert analysis.main() == 1
    assert not args.output.exists()
