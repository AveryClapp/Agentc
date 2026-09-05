"""Training-only lexical-guard diagnosis; never dispatch or evaluate heldout.

These are descriptive artifact comparisons, not a financial reconciliation,
guard false-positive classifier, or a reinterpretation of the failed study.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from bench.openrouter_matrix import file_hash, score, write_json
from bench.openrouter_pilot import PilotError, digest
from bench.openrouter_replay import lexical_divergence

ITEM_KEYS = ("phase", "task_id", "arm", "workflow_stage")
DIAGNOSTIC_SEED = "training-guard-repeat-suffix-v1"
Json = dict[str, Any]


def training_ids(manifest: Json) -> set[str]:
    phases = defaultdict(set)
    for row in manifest["schedule"]:
        phases[row["task_id"]].add(row["phase"])
    if any(len(values) != 1 for values in phases.values()):
        raise PilotError("question is shared between study phases")
    return {task for task, values in phases.items() if values == {"training"}}


def reject_heldout(*journals: list[Json]) -> None:
    if any(row["phase"] == "heldout" for journal in journals for row in journal):
        raise PilotError("training audit refuses journals containing heldout")


def load_training_gold(path: Path, manifest: Json) -> dict[str, str]:
    """Access expected-answer fields only for manifested training records."""
    if file_hash(path) != manifest["fixture_sha256"]:
        raise PilotError("gold fixture differs from frozen manifest")
    allowed = training_ids(manifest)
    result = {}
    for row in json.loads(Path(path).read_text()):
        task = row["task_id"]
        if task in allowed:
            if task in result or not isinstance(row["expected"], str):
                raise PilotError("duplicate or invalid training gold")
            result[task] = row["expected"]
    if set(result) != allowed:
        raise PilotError("training gold is incomplete")
    return result


def paired_records(decisions: list[Json], calls: list[Json], gold: dict[str, str]) -> list[Json]:
    reject_heldout(decisions, calls)
    by_id = {row["id"]: row for row in calls}
    if len(by_id) != len(calls):
        raise PilotError("duplicate call identity")
    endpoints = {
        (d["task_id"], d["arm"]): by_id[d["primary_id"]]
        for d in decisions
        if d["phase"] == "training" and d["workflow_stage"] == "answer"
    }
    pairs: list[Json] = []
    used: set[str] = set()
    for index, decision in enumerate(decisions):
        feedback = decision["divergence_feedback"]
        if decision["phase"] != "training" or feedback is None:
            continue  # Zero is measured feedback, not missing feedback.
        if isinstance(feedback, bool) or not math.isfinite(feedback) or not 0 <= feedback <= 1:
            raise PilotError("invalid feedback")
        signature = decision["semantic_plan"]
        exploration = signature["candidate"] is not None
        which = "candidate" if exploration else "primary"
        identity = signature[which + "_identity"]
        if not identity or set(identity.get("key", {})) != {"call_site_version", "execution_plan_id"}:
            raise PilotError("missing exact-plan identity")
        threshold = identity["divergence_threshold"]
        if isinstance(threshold, bool) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise PilotError("invalid exact-plan threshold")
        ids = decision["incurred_ids"]
        if len(ids) != 2 or len(set(ids)) != 2 or ids[0] != decision["primary_id"] or used.intersection(ids):
            raise PilotError("invalid or reused feedback pair")
        used.update(ids)
        primary, second = (by_id[row_id] for row_id in ids)
        scope = "exploration" if exploration else "shadow"
        if primary["scope"] != "primary" or second["scope"] != scope:
            raise PilotError("feedback scope mismatch")
        if any(row[k] != decision[k] for row in (primary, second) for k in ITEM_KEYS):
            raise PilotError("feedback pair crosses workflow boundaries")
        reference, candidate = (primary, second) if exploration else (second, primary)
        if candidate["model"] != signature[which]["model"]:
            raise PilotError("candidate model differs from requested payload")
        if feedback != lexical_divergence(reference["answer"], candidate["answer"]):
            raise PilotError("feedback differs from response text")
        task = decision["task_id"]
        if task not in gold:
            raise PilotError("feedback lacks training-only gold binding")
        endpoint = endpoints.get((task, decision["arm"]))
        if endpoint is None:
            raise PilotError("paired workflow has no completed factual endpoint")
        answer_stage = decision["workflow_stage"] == "answer"
        pairs.append({
            **{k: decision[k] for k in ITEM_KEYS}, "decision_index": index,
            "scope": scope, "identity": identity, "identity_sha256": digest(identity),
            "rules": signature[which + "_rules"], "model": candidate["model"],
            "reference_id": reference["id"], "candidate_id": candidate["id"],
            "divergence": feedback, "threshold": threshold, "over_threshold": feedback > threshold,
            "reference_score": score(reference["answer"], gold[task]) if answer_stage else None,
            "candidate_score": score(candidate["answer"], gold[task]) if answer_stage else None,
            "factual_workflow_endpoint_score": score(endpoint["answer"], gold[task]),
            "candidate_suffix_quality": None if not answer_stage else "direct_answer_observed",
        })
    return pairs


def quality_summary(pairs: list[Json]) -> Json:
    if not pairs:
        return {"n": 0}
    result: Json = {"n": len(pairs), "distinct_questions": len({p["task_id"] for p in pairs})}
    for metric in ("f1", "em"):
        reference = [p["reference_score"][metric] for p in pairs]
        candidate = [p["candidate_score"][metric] for p in pairs]
        result[metric] = {
            "reference_mean": sum(reference) / len(pairs),
            "candidate_mean": sum(candidate) / len(pairs),
            "mean_paired_delta": sum(c-r for r, c in zip(reference, candidate)) / len(pairs),
            "losses": sum(c < r for r, c in zip(reference, candidate)),
            "gains": sum(c > r for r, c in zip(reference, candidate)),
            "ties": sum(c == r for r, c in zip(reference, candidate)),
            "both_zero": sum(c == r == 0 for r, c in zip(reference, candidate)),
        }
    return result


def summarize_pairs(pairs: list[Json]) -> Json:
    result: Json = {
        "n": len(pairs), "zero_divergence": sum(p["divergence"] == 0 for p in pairs),
        "over_threshold": sum(p["over_threshold"] for p in pairs),
        "mean_divergence": sum(p["divergence"] for p in pairs) / len(pairs) if pairs else None,
    }
    answers = [p for p in pairs if p["workflow_stage"] == "answer"]
    result["answer_quality"] = {
        "all": quality_summary(answers),
        "zero_divergence": quality_summary([p for p in answers if p["divergence"] == 0]),
        "positive_divergence": quality_summary([p for p in answers if p["divergence"] > 0]),
        "over_threshold": quality_summary([p for p in answers if p["over_threshold"]]),
    }
    return result


def recorded_guard_state(pair: Json, decisions: list[Json]) -> Json:
    """Report captured native decisions, never infer disable from a raw sum."""
    matching = []
    key = pair["identity"]["key"]
    for index, d in enumerate(decisions):
        if (d["phase"], d["arm"], d["workflow_stage"]) != ("training", pair["arm"], pair["workflow_stage"]):
            continue
        diagnostics = d["native_plan"].get("agentc_planner_diagnostics", {})
        if diagnostics.get("call_site_version") != key["call_site_version"]:
            continue
        for candidate in diagnostics.get("candidates", []):
            if candidate["plan_id"] != key["execution_plan_id"]:
                continue
            if candidate["divergence_threshold"] != pair["threshold"]:
                raise PilotError("diagnostic threshold differs from exact identity")
            matching.append({"decision_index": index, "task_id": d["task_id"],
                "rejection_reason": candidate.get("rejection_reason"),
                "divergence_exposure": candidate["divergence_exposure"],
                "paired_observations": (candidate.get("estimate") or {}).get("paired_observations")})
    disabled = [row for row in matching if row["rejection_reason"] == "disabled"]
    return {"first_recorded_disabled": disabled[0] if disabled else None,
            "last_recorded_candidate_state": matching[-1] if matching else None}


def repeated_references(decisions: list[Json], calls: list[Json], source_model: str,
                        gold: dict[str, str] | None = None) -> Json:
    """Disjoint exact-payload repeats; no n-squared all-pairs inflation."""
    by_id = {row["id"]: row for row in calls}
    groups = defaultdict(list)
    for d in decisions:
        if d["phase"] not in {"warmup", "training"} or d["semantic_plan"]["kind"] != "pass_through":
            continue
        row = by_id[d["primary_id"]]
        if row["model"] == source_model:
            key = tuple(row[k] for k in ("phase", "task_id", "workflow_stage", "model", "provider", "request_sha256"))
            groups[key].append(row)
    pairs = []
    for key, rows in sorted(groups.items()):
        for left, right in zip(rows[::2], rows[1::2]):
            pair = {"phase": key[0], "task_id": key[1], "workflow_stage": key[2],
                          "row_ids": [left["id"], right["id"]], "request_sha256": key[5],
                          "divergence": lexical_divergence(left["answer"], right["answer"])}
            if key[0] == "training" and key[2] == "answer" and gold is not None:
                pair.update(reference_score=score(left["answer"], gold[key[1]]),
                            candidate_score=score(right["answer"], gold[key[1]]))
            pairs.append(pair)
    summaries = []
    for phase, stage in sorted({(p["phase"], p["workflow_stage"]) for p in pairs}):
        subset = [p for p in pairs if (p["phase"], p["workflow_stage"]) == (phase, stage)]
        summary = {"phase": phase, "workflow_stage": stage, "n": len(subset),
                          "distinct_questions": len({p["task_id"] for p in subset}),
                          "zero_divergence": sum(p["divergence"] == 0 for p in subset),
                          "mean_divergence": sum(p["divergence"] for p in subset)/len(subset),
                          "over_0_01": sum(p["divergence"] > .01 for p in subset),
                          "over_0_03": sum(p["divergence"] > .03 for p in subset)}
        scored = [p for p in subset if "reference_score" in p]
        if scored:
            summary["repeat_answer_quality"] = quality_summary(scored)
            summary["positive_divergence_repeat_answer_quality"] = quality_summary([p for p in scored if p["divergence"] > 0])
        summaries.append(summary)
    return {"pairing": "adjacent disjoint source-primary repeats within exact phase/task/stage/model/provider/payload groups; acquisition order", "by_stage": summaries, "pairs": pairs}


def diagnostic_proposal(pairs: list[Json], manifest_sha256: str) -> Json:
    groups = defaultdict(list)
    for pair in pairs:
        if pair["scope"] == "exploration":
            groups[(pair["arm"], pair["workflow_stage"], pair["identity_sha256"])].append(pair)
    cases = []
    for group, rows in sorted(groups.items()):
        # Do not select by divergence, correctness, response text, or realized cost.
        chosen = min(rows, key=lambda p: digest([DIAGNOSTIC_SEED, manifest_sha256, group,
                                                p["task_id"], p["reference_id"], p["candidate_id"]]))
        remaining = {"filter": 2, "synthesize": 1, "answer": 0}[chosen["workflow_stage"]]
        cases.append({k: chosen[k] for k in ("arm", "workflow_stage", "identity_sha256", "task_id", "reference_id", "candidate_id")}
                     | {"maximum_new_calls": 1 + 3 * remaining})
    return {
        "status": "proposal_only_not_dispatch_authority", "seed": DIAGNOSTIC_SEED,
        "selection": "one metadata-hash-minimum exploration pair per arm/stage/exact-plan stratum; no outcome-dependent selection",
        "cases": cases, "maximum_new_calls_without_retries": sum(c["maximum_new_calls"] for c in cases),
        "protocol": [
            "Freeze selected IDs, exact captured payloads, branch order seed, source endpoint, scorer and monetary ceiling before dispatch.",
            "Repeat the exact original stage payload once on the original source model: R1. Compare R1 against captured reference R0 as a same-request variation control.",
            "For filter/synthesize, separately continue captured R0, captured candidate C, and repeated source R1 through every remaining stage using only the original source model and fixed original workflow; retain each branch's own history.",
            "The R0-versus-C endpoint contrast diagnoses the captured candidate perturbation conditional on its captured prefix; R0-versus-R1 diagnoses reference-repeat variation propagated through the same suffix.",
            "For answer-stage pairs, no suffix is needed: score captured R0/C and the fresh repeated R1 directly with unchanged EM/F1.",
            "No optimizer, guard updates, automatic provider fallback, threshold tuning, or heldout inputs. Preserve all outcomes including errors, truncation, zeros, and losses; reviewed shared-budget transport must enforce the separately approved cap.",
        ],
        "limitations": [
            "A single suffix draw per branch does not isolate all downstream generation noise; repeated complete suffixes would be a separate preregistered extension.",
            "Captured and fresh outputs are noncontemporaneous; backend/cache variation remains. These are adaptive-training-selected cases, not representative or independent confirmatory samples.",
            "This cannot turn the failed admission-gated study into a successful heldout evaluation or certify a two-percentage-point damage bound.",
        ],
    }


def analyze(manifest: Json, decisions: list[Json], calls: list[Json], intents: list[Json],
            gold: dict[str, str]) -> Json:
    reject_heldout(decisions, calls, intents)
    allowed = training_ids(manifest)
    if set(gold) != allowed:
        raise PilotError("gold must contain exactly the training question set")
    expected = [dict(row, workflow_stage=stage) for row in manifest["schedule"]
                if row["phase"] != "heldout" for stage in ("filter", "synthesize", "answer")]
    if len(decisions) != len(expected) or len(intents) != len(expected):
        raise PilotError("complete pre-heldout journals are required")
    from bench.openrouter_rules_live import Acquisition, semantic_plan
    from bench.openrouter_rules_validity import provider_failures
    Acquisition.validate_journals(cast(Any, SimpleNamespace(manifest=manifest, decisions=decisions, calls=calls,  # type: ignore[no-untyped-call]
        intents=intents, stage="rules-live-dev-v1-" + digest(manifest)[:20])))
    if sum(len(d["incurred_ids"]) for d in decisions) != len(calls):
        raise PilotError("unconsumed call suffix")
    if provider_failures(calls):  # type: ignore[no-untyped-call]
        raise PilotError("provider failures prevent valid quality diagnosis")
    for d, intent in zip(decisions, intents):
        if semantic_plan(intent["original_call"], d["native_plan"], manifest) != d["semantic_plan"]:  # type: ignore[no-untyped-call]
            raise PilotError("native decision differs from semantic signature")
    by_id = {row["id"]: row for row in calls}
    for d, intent in zip(decisions, intents):
        signature = d["semantic_plan"]
        for call_id in d["incurred_ids"]:
            row = by_id[call_id]
            requested = (signature["candidate"]["model"] if row["scope"] == "exploration"
                         else intent["original_call"]["model"] if row["scope"] == "shadow"
                         else signature["primary"]["model"])
            if row["model"] != requested:
                raise PilotError("call model differs from frozen payload")
    pairs = paired_records(decisions, calls, gold)
    groups = defaultdict(list)
    for p in pairs:
        groups[(p["arm"], p["workflow_stage"], p["scope"], p["identity_sha256"])].append(p)
    return {
        "kind": "training_only_guard_diagnosis_v1", "paper_evidence": False,
        "provider_calls_made_by_audit": 0, "financial_completeness_certified": False,
        "heldout_rows_inspected": 0, "gold_scope": "manifest training IDs only",
        "manifest_sha256": digest(manifest), "calls_sha256": digest(calls),
        "decisions_sha256": digest(decisions), "intents_sha256": digest(intents),
        "counts": {"calls": len(calls), "decisions": len(decisions), "training_questions": len(allowed),
                   "paired_scopes": dict(Counter(p["scope"] for p in pairs))},
        "exploration_summary": summarize_pairs([p for p in pairs if p["scope"] == "exploration"]),
        "shadow_summary": summarize_pairs([p for p in pairs if p["scope"] == "shadow"]),
        "groups": [{"arm": key[0], "workflow_stage": key[1], "scope": key[2],
                    "identity_sha256": key[3], "identity": rows[0]["identity"], "rules": rows[0]["rules"],
                    **summarize_pairs(rows), "recorded_guard": recorded_guard_state(rows[0], decisions)}
                   for key, rows in sorted(groups.items())],
        "pairs": pairs, "reference_repeats": repeated_references(decisions, calls, manifest["source_model"], gold),
        "diagnostic_proposal": diagnostic_proposal(pairs, digest(manifest)),
        "limitations": [
            "All comparisons are descriptive post-hoc training diagnostics. Adaptive stopping/disable selects which pairs are observed; no unbiased efficacy or non-inferiority claim.",
            "Zero whitespace-token-set divergence does not guarantee correctness, equal token multiplicity, or semantic equivalence. EM/F1 ties may mean both answers are wrong.",
            "Positive lexical divergence can accompany loss, gain, or unchanged task score; it is not by itself a false-positive or task-damage label.",
            "Early-stage candidate suffix quality is unobserved. Factual workflow endpoint scores belong to the actually executed primary history, not the discarded candidate branch.",
            "Exact-payload source repeats measure observed variation across different times/arms, not semantic equivalence or a causal optimization effect.",
            "No ledger/account snapshot was opened. Artifact hashes and internal journal checks do not certify billing completeness or permit spending.",
            "No production guard, failed admission gate, or original study conclusion is changed.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    names = ("manifest", "decisions", "calls", "intents")
    try:
        raw = {name: (args.artifacts / (name + ".json")).read_bytes() for name in names}
        manifest, decisions, calls, intents = [json.loads(raw[name]) for name in names]
        reject_heldout(decisions, calls, intents)
        report = analyze(manifest, decisions, calls, intents, load_training_gold(args.fixture, manifest))
        if any((args.artifacts / (name + ".json")).read_bytes() != raw[name] for name in names):
            raise PilotError("artifact snapshot changed during audit")
        report["artifact_file_sha256"] = {name + ".json": hashlib.sha256(data).hexdigest() for name, data in raw.items()}
        report["analysis_source_sha256"] = file_hash(Path(__file__))
        write_json(args.output, report, immutable=True)
        print(json.dumps({"counts": report["counts"], "exploration_summary": report["exploration_summary"],
                          "reference_repeats": report["reference_repeats"]["by_stage"],
                          "proposed_maximum_calls": report["diagnostic_proposal"]["maximum_new_calls_without_retries"]}))
        return 0
    except (PilotError, OSError, KeyError, ValueError, TypeError) as exc:
        print(f"Training guard audit stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
