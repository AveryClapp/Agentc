"""Read-only verification and descriptive analysis of complete suffix diagnostics.

No Ledger instance, provider call, native runtime, policy feedback, or heldout
evaluation is used. The frozen source study remains failed-admission evidence.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bench import openrouter_outcome_diagnostic as diagnostic
from bench.openrouter_joint_analysis import validate_row
from bench.openrouter_matrix import file_hash, score, write_json
from bench.openrouter_pilot import PilotError, digest, make_request, money
from bench.openrouter_replay import lexical_divergence, public_task

Json = dict[str, Any]
EXPECTED_MANIFEST = "8854e56cc2f2b2855254c096034f61b5dd039dc19fed0581147d6ae172c16109"
STAGES = ("filter", "synthesize", "answer")
BRANCHES = ("reference", "candidate", "repeat")


def require_complete(manifest: Json, summary: Json, expected_hash: str) -> None:
    if (digest(manifest) != expected_hash or summary["manifest_sha256"] != expected_hash
            or summary["schedule_complete"] is not True
            or summary["completed_calls"] != len(manifest["schedule"])
            or summary["scheduled_calls"] != len(manifest["schedule"])
            or summary["scheduled_pairs"] != len(manifest["pairs"])
            or summary["paper_evidence"] is not False or summary["efficacy_claim"] is not False):
        raise PilotError("complete externally frozen diagnostic is required")


def verify_archive(data: Json) -> None:
    """The new immutable manifest binds every archive byte; check pair semantics too."""
    old = data["manifest.json"]
    calls, decisions = data["calls.json"], data["decisions.json"]
    if (digest(old) != diagnostic.PREVIOUS_HASH
            or any(d["phase"] == "heldout" for d in decisions)
            or any(r["phase"] == "heldout" for r in calls)
            or data["training-gate.json"]["proceed_to_heldout"] is not False):
        raise PilotError("archive is not the closed training-only study")
    by_id = {r["id"]: r for r in calls}
    if len(by_id) != len(calls):
        raise PilotError("archive contains duplicate call IDs")
    histories: dict[tuple[str, str, str], Json] = {}
    for d in decisions:
        key = (d["phase"], d["arm"], d["task_id"])
        history = histories.setdefault(key, {})
        stage = d["workflow_stage"]
        if set(history) != set(STAGES[:STAGES.index(stage)]):
            raise PilotError("archive own-history order changed")
        signature = d["semantic_plan"]
        primary = by_id[d["primary_id"]]
        if d["phase"] == "training" and signature["candidate"] is not None:
            ids = d["incurred_ids"]
            if len(ids) != 2 or ids[0] != d["primary_id"]:
                raise PilotError("archive candidate is not source-paired")
            candidate = by_id[ids[1]]
            if primary["scope"] != "primary" or candidate["scope"] != "exploration":
                raise PilotError("archive candidate scope changed")
            if any(row[k] != d[k] for row in (primary, candidate)
                   for k in ("phase", "task_id", "arm", "workflow_stage")):
                raise PilotError("archive candidate crosses workflow boundaries")
            if (d["divergence_feedback"] is None or isinstance(d["divergence_feedback"], bool)
                    or d["divergence_feedback"] != lexical_divergence(primary["answer"], candidate["answer"])):
                raise PilotError("archive lexical feedback changed")
            messages = signature["primary"]["messages"]
            if stage != "filter" and messages[1]["content"] != history["filter"]:
                raise PilotError("archive source filter history changed")
            if stage == "answer" and messages[2]["content"] != history["synthesize"]:
                raise PilotError("archive source synthesis history changed")
        history[stage] = primary["answer"]


def load_selected_tasks(fixture: Path, manifest: Json, old_manifest: Json) -> tuple[Json, dict[str, str]]:
    wanted = {p["task_id"] for p in manifest["pairs"]}
    phases = defaultdict(set)
    for row in old_manifest["schedule"]:
        phases[row["task_id"]].add(row["phase"])
    if any(phases[task] != {"training"} for task in wanted):
        raise PilotError("diagnostic pair is not exclusively a training question")
    if file_hash(fixture) != manifest["fixture_sha256"]:
        raise PilotError("fixture differs from frozen manifest")
    tasks, gold = {}, {}
    for row in json.loads(fixture.read_text()):
        task = row["task_id"]
        if task in wanted:
            if task in tasks or not isinstance(row["expected"], str):
                raise PilotError("duplicate or invalid selected training task")
            tasks[task], gold[task] = public_task(row), row["expected"]
    if set(tasks) != wanted:
        raise PilotError("selected training task is missing")
    return tasks, gold


def source_payload(item: Json, pair: Json, history: Json, task: Json, manifest: Json) -> Json:
    """Independently project source suffix messages; do not call acquire/request_for."""
    stage = item["workflow_stage"]
    if set(history) != set(STAGES[:STAGES.index(stage)]):
        raise PilotError("suffix request lacks this branch's exact preceding stages")
    if item["branch"] == "repeat" and stage == pair["workflow_stage"]:
        return deepcopy(pair["reference_payload"])
    if stage not in ("synthesize", "answer"):
        raise PilotError("unexpected generated filter request")
    system = manifest["prompts"]["SYNTH_SYSTEM" if stage == "synthesize" else "ANSWER_SYSTEM"]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": history["filter"]}]
    if stage == "answer":
        messages.append({"role": "user", "content": history["synthesize"]})
    messages.append({"role": "user", "content": "Question: " + task["prompt"]})
    endpoint = manifest["endpoints"][manifest["source_model"]]
    return make_request(manifest["source_model"], [endpoint["tag"]], messages, max_tokens=512)


def reconstruct(manifest: Json, calls: list[Json], tasks: Json, gold: dict[str, str]) -> list[Json]:
    if len(calls) != len(manifest["schedule"]):
        raise PilotError("diagnostic call journal is incomplete or overlong")
    pairs = {p["pair_id"]: p for p in manifest["pairs"]}
    if len(pairs) != len(manifest["pairs"]):
        raise PilotError("duplicate diagnostic pair")
    histories = {}
    for pair in pairs.values():
        for branch in BRANCHES:
            history = deepcopy(pair["history"])
            if branch != "repeat":
                history[pair["workflow_stage"]] = pair[branch + "_output"]
            histories[(pair["pair_id"], branch)] = history
    stage_id = "outcome-diagnostic-v1-" + digest(manifest)[:20]
    generations: set[str] = set()
    ids: set[str] = set()
    endpoint = manifest["endpoints"][manifest["source_model"]]
    for item, row in zip(manifest["schedule"], calls):
        pair = pairs[item["pair_id"]]
        if item["task_id"] != pair["task_id"] or item["branch"] not in BRANCHES:
            raise PilotError("scheduled branch task changed")
        history = histories[(pair["pair_id"], item["branch"])]
        payload = source_payload(item, pair, history, tasks[item["task_id"]], manifest)
        meta = {**item, "phase": "development", "manifest_sha256": digest(manifest),
                "dispatch_contract": {"provider_name": endpoint["provider_name"],
                                      "endpoint_model": endpoint["name"].split(" | ", 1)[1]}}
        expected_id = stage_id + "-" + digest(item)[:24]
        if (any(row[k] != v for k, v in item.items()) or row["id"] != expected_id
                or row["id"] in ids or row["stage"] != stage_id
                or row["request_sha256"] != digest(payload) or row.get("event") != "result"
                or payload["model"] != manifest["source_model"]
                or row.get("finish_reason") not in ("stop", "length")):
            raise PilotError("call is not the successful scheduled source-only request")
        validate_row(row, payload, meta, manifest)  # type: ignore[no-untyped-call]
        generation = row.get("generation_id")
        if not isinstance(generation, str) or not generation or generation in generations:
            raise PilotError("missing or duplicate provider generation")
        generations.add(generation)
        ids.add(row["id"])
        history[item["workflow_stage"]] = row["answer"]
    comparisons = []
    for pair in manifest["pairs"]:
        outputs = {branch: histories[(pair["pair_id"], branch)]["answer"] for branch in BRANCHES}
        comparisons.append({
            **{k: pair[k] for k in ("pair_id", "task_id", "arm", "workflow_stage", "candidate_rules", "candidate_identity")},
            "callsite_lexical_divergence": pair["lexical_divergence"],
            "candidate_endpoint_disagreement": 1-score(outputs["candidate"], outputs["reference"])["f1"],
            "repeat_endpoint_disagreement": 1-score(outputs["repeat"], outputs["reference"])["f1"],
            "scores": {b: score(text, gold[pair["task_id"]]) for b, text in outputs.items()},
            "repeat_callsite_lexical_divergence": lexical_divergence(
                histories[(pair["pair_id"], "repeat")][pair["workflow_stage"]], pair["reference_output"]),
        })
    return comparisons


def branch_summary(rows: list[Json], branch: str) -> Json:
    result: Json = {"n": len(rows), "distinct_questions": len({p["task_id"] for p in rows})}
    if not rows:
        return result
    for metric in ("f1", "em"):
        reference = [p["scores"]["reference"][metric] for p in rows]
        candidate = [p["scores"][branch][metric] for p in rows]
        result[metric] = {"reference_mean": sum(reference)/len(rows), "branch_mean": sum(candidate)/len(rows),
            "mean_paired_delta": sum(c-r for r, c in zip(reference, candidate))/len(rows),
            "losses": sum(c < r for r, c in zip(reference, candidate)),
            "gains": sum(c > r for r, c in zip(reference, candidate)),
            "ties": sum(c == r for r, c in zip(reference, candidate)),
            "both_zero": sum(c == r == 0 for r, c in zip(reference, candidate))}
    return result


def group_summary(rows: list[Json]) -> Json:
    return {"candidate": branch_summary(rows, "candidate"), "repeat": branch_summary(rows, "repeat"),
            "original_lexical_breaches": sum(p["callsite_lexical_divergence"] > p["candidate_identity"]["divergence_threshold"] for p in rows),
            "original_zero_divergence": sum(p["callsite_lexical_divergence"] == 0 for p in rows),
            "repeat_callsite_zero_divergence": sum(p["repeat_callsite_lexical_divergence"] == 0 for p in rows),
            "repeat_callsite_breaches_at_candidate_threshold": sum(p["repeat_callsite_lexical_divergence"] > p["candidate_identity"]["divergence_threshold"] for p in rows)}


def analyze(manifest: Json, summary: Json, calls: list[Json], data: Json, args: Any) -> Json:
    require_complete(manifest, summary, args.manifest_sha256)
    if summary["calls_sha256"] != digest(calls):
        raise PilotError("summary does not bind the call journal")
    # This helper is entirely read-only. Never invoke diagnostic.previous(),
    # acquire(), retry_call(), Ledger.call(), or a native runtime here.
    diagnostic.validate_manifest(manifest, args, data)  # type: ignore[no-untyped-call]
    verify_archive(data)
    tasks, gold = load_selected_tasks(args.fixture, manifest, data["manifest.json"])
    comparisons = reconstruct(manifest, calls, tasks, gold)
    if comparisons != summary["paired_comparisons"]:
        raise PilotError("summary endpoints or scores differ from reconstructed branches")
    artifact_paid = sum((money(r["cost_usd"]) for r in calls), Decimal(0))
    if artifact_paid != money(summary["incremental_artifact_paid_usd"]):
        raise PilotError("summary artifact usage total changed")
    groups, exact, questions = defaultdict(list), defaultdict(list), defaultdict(list)
    for p in comparisons:
        groups[(p["workflow_stage"], tuple(p["candidate_rules"]))].append(p)
        exact[(p["arm"], p["workflow_stage"], digest(p["candidate_identity"]))].append(p)
        questions[p["task_id"]].append(p)
    breaches = [p for p in comparisons if p["callsite_lexical_divergence"] > p["candidate_identity"]["divergence_threshold"]]
    nonbreaches = [p for p in comparisons if p not in breaches]
    return {
        "kind": "independent_complete_training_suffix_diagnostic_analysis_v1", "paper_evidence": False,
        "efficacy_claim": False, "provider_calls_made_by_analysis": 0, "ledger_accessed": False,
        "financial_completeness_certified": False, "heldout_questions_evaluated": 0,
        "manifest_sha256": digest(manifest), "calls_sha256": digest(calls), "summary_sha256": digest(summary),
        "complete_calls": len(calls), "complete_pairs": len(comparisons), "distinct_questions": len(questions),
        "incremental_artifact_paid_usd": str(artifact_paid),
        "all_pairs": group_summary(comparisons),
        "by_site_rule": [{"workflow_stage": k[0], "rules": list(k[1]), **group_summary(rows)} for k, rows in sorted(groups.items())],
        "by_exact_arm_plan": [{"arm": k[0], "workflow_stage": k[1], "identity_sha256": k[2],
                               "candidate_identity": rows[0]["candidate_identity"], "rules": rows[0]["candidate_rules"],
                               **group_summary(rows)} for k, rows in sorted(exact.items())],
        "lexical_breach_contingency": {"breach": group_summary(breaches), "no_breach": group_summary(nonbreaches)},
        "by_question": [{"task_id": task, **group_summary(rows)} for task, rows in sorted(questions.items())],
        "pairs": comparisons,
        "verification": ["Externally supplied immutable diagnostic manifest and all frozen source/archive/fixture hashes match.",
            "All original training exploration pairs, including zeros, are present in deterministic frozen schedule.",
            "Each new request is independently reconstructed from its branch's own history; stage repeats use exact captured source payloads.",
            "All new requested/returned models and selected provider endpoints match the frozen source contract; request hashes, fingerprints and unique generations checked.",
            "All final branch outputs and unchanged raw-answer EM/F1 scores independently reproduce the stored summary."],
        "limitations": [
            "The 69 adaptively selected training pairs share 25 questions; pooled pair means mix interventions and are not independent-question treatment-effect estimates.",
            "Early-stage comparisons condition on archived reference/candidate outputs and replay a new source-only suffix; they are not on-policy joint executions.",
            "Answer-stage reference/candidate endpoints are the original captured answers, not new replications of those contrasts.",
            "Repeat-versus-reference captures source-call, suffix, backend and temporal variation; one draw per suffix branch cannot isolate pure semantic noise or provide a noise correction.",
            "A lexical breach with no observed score loss is not a proven false positive; EM/F1 ties can be jointly wrong and unobserved failure modes remain.",
            "No heldout labels, native feedback, threshold relaxation, policy retraining or old failed-gate reinterpretation. This diagnostic does not establish efficacy, safety or a 2pp non-inferiority bound.",
            "Provider usage fields and summary totals are internally checked, but no ledger/account snapshot was read; financial completeness and later adjustments are not certified.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "previous", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--manifest-sha256", default=EXPECTED_MANIFEST)
    args = parser.parse_args()
    try:
        paths = {"diagnostic/" + name: args.artifacts/name for name in ("manifest.json", "summary.json")}
        raw = {name: path.read_bytes() for name, path in paths.items()}
        manifest, summary = (json.loads(raw["diagnostic/" + name]) for name in ("manifest.json", "summary.json"))
        require_complete(manifest, summary, args.manifest_sha256)  # Before opening calls or gold.
        paths["diagnostic/calls.json"] = args.artifacts/"calls.json"
        paths.update({"previous/" + name: args.previous/name for name in diagnostic.FILES})
        raw.update({name: path.read_bytes() for name, path in paths.items() if name not in raw})
        calls = json.loads(raw["diagnostic/calls.json"])
        data = {name: json.loads(raw["previous/" + name]) for name in diagnostic.FILES}
        result = analyze(manifest, summary, calls, data, args)
        if any(path.read_bytes() != raw[name] for name, path in paths.items()):
            raise PilotError("artifact bytes changed during analysis")
        result["file_sha256"] = {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()}
        result["analysis_source_sha256"] = file_hash(Path(__file__))
        write_json(args.output, result, immutable=True)
        print(json.dumps({k: result[k] for k in ("complete_calls", "complete_pairs", "distinct_questions", "all_pairs", "by_site_rule", "lexical_breach_contingency")}))
        return 0
    except (PilotError, OSError, KeyError, ValueError, TypeError) as exc:
        print(f"Outcome analysis stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
