"""Evaluator-only diagnostics of interactions, evidence retention, and guards.

These descriptive analyses are not inputs to native policy selection. They
cannot certify semantic safety, causal live savings, or a best heldout policy.
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bench.openrouter_contract import messages
from bench.openrouter_frontier import CONTEXTS, SOURCE_MODEL, load_tasks
from bench.openrouter_frontier_analysis import total_cost
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_pilot import PilotError, digest, money
from bench.openrouter_replay import lexical_divergence, validate_matrix


def indexed(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    result = {}
    for r in rows:
        key = (r["context"], r["task_id"], r["model"], r["arm"])
        if key in result:
            raise PilotError("duplicate mechanism matrix cell")
        result[key] = r
    return result


def mean_interval(values: list[float], seed: str, draws: int = 2000) -> list[float]:
    if not values or draws < 40:
        raise PilotError("nonempty pairs and at least 40 bootstrap draws required")
    rng = random.Random(seed)
    samples = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws))
    return [samples[int(.025 * (draws - 1))], samples[int(.975 * (draws - 1))]]


def interactions(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Model-specific rewrite effect minus the source-model rewrite effect."""
    cells = indexed(rows)
    reports = []
    for context in CONTEXTS:
        ids = sorted({r["task_id"] for r in rows if r["context"] == context})
        for model in sorted(manifest["endpoints"]):
            if model == SOURCE_MODEL:
                continue
            values, source_deltas, model_deltas = [], [], []
            for task_id in ids:
                def delta(target):
                    return cells[(context, task_id, target, "compress")]["f1"] - cells[(context, task_id, target, "full")]["f1"]
                source_deltas.append(delta(SOURCE_MODEL))
                model_deltas.append(delta(model))
                values.append(model_deltas[-1] - source_deltas[-1])
            reports.append({"context": context, "model": model, "source_model": SOURCE_MODEL,
                "questions": len(ids), "source_rewrite_f1_delta": sum(source_deltas) / len(ids),
                "model_rewrite_f1_delta": sum(model_deltas) / len(ids),
                "difference_in_differences": sum(values) / len(values),
                "paired_bootstrap_95": mean_interval(values, digest(["interaction-v1", context, model])),
                "scope": "descriptive four-outcome question-paired interaction; unadjusted across model/context comparisons"})
    return reports


def support_retention(rows: list[dict[str, Any]], tasks: dict[str, Any], contract: str) -> list[dict[str, Any]]:
    cells = indexed(rows)
    groups = defaultdict(list)
    for row in rows:
        if row["arm"] != "compress":
            continue
        task = tasks[row["context"]][row["task_id"]]
        original = messages(task, contract)
        selected = row["native_plan"].get("call", {}).get("messages", original)
        retained = Counter(m["content"] for m in selected[1:-1])
        all_passages = Counter(m["content"] for m in original[1:-1])
        if selected[0] != original[0] or selected[-1] != original[-1] or retained - all_passages:
            raise PilotError("retention analysis requires whole-paragraph deletion with protected instructions")
        support = Counter(p["title"] + "\n" + " ".join(p["sentences"])
                          for p in task["meta"]["paragraphs"] if p["supporting"])
        removed = list((support - retained).elements())
        full = cells[(row["context"], row["task_id"], row["model"], "full")]
        groups[(row["context"], row["model"])].append({"task_id": row["task_id"],
            "support_paragraphs": sum(support.values()), "removed_support_paragraphs": len(removed),
            "removed_support_titles": [p.split("\n", 1)[0] for p in removed],
            "f1_delta": row["f1"] - full["f1"], "f1_loss": row["f1"] < full["f1"],
            "rewritten": row["native_plan"]["kind"] != "pass_through"})
    reports = []
    for (context, model), pairs in sorted(groups.items()):
        categories = []
        for removed in (False, True):
            group = [p for p in pairs if bool(p["removed_support_paragraphs"]) == removed]
            categories.append({"removed_any_support": removed, "questions": len(group),
                "f1_loss_count": sum(p["f1_loss"] for p in group),
                "mean_f1_delta": sum(p["f1_delta"] for p in group) / len(group) if group else None})
        reports.append({"context": context, "model": model, "questions": len(pairs),
            "rewritten": sum(p["rewritten"] for p in pairs),
            "questions_losing_support_paragraphs": sum(p["removed_support_paragraphs"] > 0 for p in pairs),
            "support_removed_total": sum(p["removed_support_paragraphs"] for p in pairs),
            "categories": categories, "pairs": pairs,
            "scope": "dataset supporting-paragraph labels used only after outcomes; retention does not prove answer correctness"})
    return reports


def guard_proxy(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare the lexical signal with measured F1 loss, not semantic truth."""
    cells = indexed(rows)
    reports = []
    for context in CONTEXTS:
        ids = sorted({r["task_id"] for r in rows if r["context"] == context})
        for model in sorted(manifest["endpoints"]):
            for arm in ("full", "compress"):
                if (model, arm) == (SOURCE_MODEL, "full"):
                    continue
                counts = Counter()
                examples = []
                for task_id in ids:
                    ref = cells[(context, task_id, SOURCE_MODEL, "full")]
                    target = cells[(context, task_id, model, arm)]
                    # Match the replay's answer-independent designated full sample
                    # when the native compression arm emits an identical payload.
                    designated = cells[(context, task_id, model, "full")]
                    if target["request_sha256"] == designated["request_sha256"]:
                        target = designated
                    divergence = lexical_divergence(ref["answer"], target["answer"])
                    flagged, loss = divergence > .05, target["f1"] < ref["f1"]
                    counts[f"flagged_{str(flagged).lower()}_f1_loss_{str(loss).lower()}"] += 1
                    if flagged and ref["em"] == target["em"] == 1:
                        counts["flagged_both_exact_correct"] += 1
                        if len(examples) < 5:
                            examples.append({"task_id": task_id, "reference_answer": ref["answer"],
                                "target_answer": target["answer"], "lexical_divergence": divergence})
                reports.append({"context": context, "model": model, "arm": arm, "questions": len(ids),
                    "lexical_threshold": .05, "counts": dict(counts), "examples": examples,
                    "scope": "all measured counterfactuals, evaluator only; F1 harm is an imperfect semantic proxy, not guard false-positive ground truth"})
    return reports


def planner_diagnostics(replay: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs = {(r["context"], r["task_id"]): r for r in rows if (r["model"], r["arm"]) == (SOURCE_MODEL, "full")}
    reports = []
    for trajectory in replay["trajectories"]:
        decisions = trajectory["decisions"]
        by_plan = {}
        reasons = Counter()
        for d in decisions:
            diagnostic = d["native_plan"].get("agentc_planner_diagnostics", {})
            for c in diagnostic.get("candidates", []):
                if c.get("rejection_reason"):
                    reasons[c["rejection_reason"].split(":", 1)[0]] += 1
                by_plan[c["plan_id"]] = {k: c.get(k) for k in ("plan_id", "target_model_id", "ordered_rewrites",
                    "admissible", "selected", "rejection_reason", "divergence_exposure", "estimate")}
        reference_rows = [refs[(d["context"], d["task_id"])] for d in decisions]
        baseline = total_cost(reference_rows, "nominal_uncached_cost_usd")
        total = sum((money(d["nominal_uncached_cost_estimate_usd"]) for d in decisions), start=money("0"))
        reports.append({"policy": trajectory["policy"], "context": trajectory["context"],
            "questions": len(decisions), "first_nonreference_primary_task": next((d["task_id"] for d in decisions if d["native_plan"]["kind"] != "pass_through"), None),
            "nonreference_primary_count": sum(d["native_plan"]["kind"] != "pass_through" for d in decisions),
            "candidate_rejection_events": dict(reasons), "latest_seen_candidate_states": list(by_plan.values()),
            "source_only_nominal_cost_usd": str(baseline), "policy_nominal_cost_with_exploration_usd": str(total),
            "nominal_cost_reduction_with_setup": float(1 - total / baseline) if baseline else None,
            "scope": "candidate states are latest pre-decision snapshots, not a final post-observation database dump"})
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "natural", "extended", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    try:
        manifest = json.loads((args.artifacts / "manifest.json").read_text())
        if manifest["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}:
            raise PilotError("mechanism fixtures differ from manifest")
        tasks = load_tasks(args.natural, args.extended)
        rows = validate_matrix(manifest, json.loads((args.artifacts / "results.json").read_text()), tasks, calibration_only=False)
        heldout = [r for r in rows if r["phase"] == "holdout"]
        report = {"paper_evidence": False, "manifest_sha256": digest(manifest), "results_sha256": digest(rows),
            "analysis_source_sha256": file_hash(Path(__file__)), "interactions": interactions(manifest, heldout),
            "support_retention": support_retention(heldout, tasks, manifest["contract"]),
            "guard_proxy": guard_proxy(manifest, heldout), "limitations": manifest["limitations"] + [
                "Mechanism diagnostics designed after calibration; descriptive, not a new heldout policy selection.",
                "All gold/support labels are evaluator-only; no guard threshold or acquisition policy is retuned."]}
        if args.replay:
            replay = json.loads(args.replay.read_text())
            if replay["manifest_sha256"] != digest(manifest) or replay["consumed_rows_sha256"] != digest(rows):
                raise PilotError("replay provenance differs from complete matrix")
            report["replay_sha256"] = digest(replay)
            report["planner_diagnostics"] = planner_diagnostics(replay, rows)
        write_json(args.output, report, immutable=True)
        print(json.dumps({"output": str(args.output), "questions_per_context": len(heldout) // 16,
                          "paper_evidence": False}))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Mechanism analysis stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
