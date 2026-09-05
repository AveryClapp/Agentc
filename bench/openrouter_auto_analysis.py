"""Validated paired comparison of bounded Auto and calibration-locked controls."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from bench.openrouter_auto import request_for, validate_dispatch
from bench.openrouter_frontier import CONTEXTS, SOURCE_MODEL, load_tasks
from bench.openrouter_frontier_analysis import calibrate, pair_summary, total_cost
from bench.openrouter_matrix import file_hash, score, write_json
from bench.openrouter_pilot import PilotError, digest, money
from bench.openrouter_replay import validate_matrix


def validate_auto(manifest, rows, tasks):
    if len(rows) != len(manifest["schedule"]):
        raise PilotError("Auto baseline is incomplete")
    stage = "auto-default-v1-" + digest(manifest)[:20]
    generations = set()
    for i, (item, row) in enumerate(zip(manifest["schedule"], rows)):
        if (any(row.get(k) != v for k, v in item.items()) or row.get("id") != stage + f"-{i:05d}"
                or row.get("stage") != stage or row.get("paper_evidence") is not False
                or row.get("optimizer") != "none" or row.get("arm") != "full"
                or row.get("requested_model") != "openrouter/auto"):
            raise PilotError("Auto row differs from frozen schedule/scope")
        if not row.get("generation_id") or row["generation_id"] in generations:
            raise PilotError("missing or duplicated Auto generation")
        generations.add(row["generation_id"])
        e = validate_dispatch(row, manifest)
        if row["service_tier_reported"] not in (None, "default") or row["provider_tag_requested_for_model"] != e["tag"]:
            raise PilotError("Auto endpoint controls differ")
        task = tasks[item["context"]][item["task_id"]]
        payload = request_for(manifest, task)
        if row["request_sha256"] != digest(payload):
            raise PilotError("Auto recorded request changed")
        metadata = {**item, "manifest_sha256": digest(manifest), "purpose": "bounded_default_auto_service"}
        if row["fingerprint"] != digest({"payload": payload, "metadata": metadata, "stage": stage}):
            raise PilotError("Auto ledger request fingerprint differs")
        if row["expected"] != task["expected"] or any(row[k] != v for k, v in score(row["answer"], task["expected"]).items()):
            raise PilotError("Auto gold or score changed")
        nominal = money(e["pricing"]["prompt"]) * row["usage"]["prompt_tokens"] + money(e["pricing"]["completion"]) * row["usage"]["completion_tokens"]
        if money(row["nominal_uncached_cost_usd"]) != nominal:
            raise PilotError("Auto nominal repricing differs")
    return rows


def analyze(manifest, auto_rows, frontier, rows, lock, tasks):
    if manifest["frontier_manifest_sha256"] != digest(frontier) or calibrate(frontier, rows) != lock:
        raise PilotError("Auto source frontier or static calibration lock changed")
    reports = []
    opening_groups = Counter()
    for r in auto_rows:
        payload = request_for(manifest, tasks[r["context"]][r["task_id"]])
        msgs = payload["messages"]
        # An observable grouping, not a claimed reconstruction of private server hashes.
        opening_groups[(r["model"], digest([msgs[0], msgs[1]]))] += 1
    for context in CONTEXTS:
        all_auto = [r for r in auto_rows if r["context"] == context]
        # Adapter for the shared paired evaluator; explicitly no native plan ran.
        treatment = [{**r, "native_plan": {"kind": "not_applicable_auto_service"}}
                     for r in all_auto if r["phase"] == "holdout"]
        controls = [{"name": "source_only", "selected": {"model": SOURCE_MODEL, "arm": "full"},
                     "candidates": [{"model": SOURCE_MODEL, "arm": "full"}]}]
        controls += [c for c in lock["controls"] if c["context"] == context]
        for control in controls:
            selected = control["selected"]
            reference = [r for r in rows if r["context"] == context and r["phase"] == "holdout"
                         and (r["model"], r["arm"]) == (selected["model"], selected["arm"])]
            candidates = {(c["model"], c["arm"]) for c in control["candidates"]}
            setup = [r for r in rows if r["context"] == context and (
                (r["phase"] == "warmup" and (r["model"], r["arm"]) == (SOURCE_MODEL, "full")) or
                (r["phase"] == "calibration" and (r["model"], r["arm"]) in candidates))]
            reports.append({"context": context, "reference": control["name"], "reference_selected": selected,
                "reference_setup_calls": len(setup), "auto_total_calls": len(all_auto),
                "reference_total_nominal_uncached_cost_usd": str(total_cost(setup + reference, "nominal_uncached_cost_usd")),
                "auto_total_nominal_uncached_cost_usd": str(total_cost(all_auto, "nominal_uncached_cost_usd")),
                "reference_total_billed_noncausal_usd": str(total_cost(setup + reference, "cost_usd")),
                "auto_total_billed_noncausal_usd": str(total_cost(all_auto, "cost_usd")),
                "auto_heldout_selected_models": dict(Counter(r["model"] for r in treatment)),
                **pair_summary(reference, treatment, digest(["auto-default-v1", context, control["name"]]))})
    return {"paper_evidence": False, "manifest_sha256": digest(manifest), "results_sha256": digest(auto_rows),
        "frontier_manifest_sha256": digest(frontier), "frontier_results_sha256": digest(rows),
        "calibration_lock_sha256": digest(lock), "analysis_source_sha256": file_hash(Path(__file__)),
        "comparisons": reports,
        "selected_model_and_opening_groups": len(opening_groups),
        "calls_in_repeated_selected_model_opening_groups": sum(n for n in opening_groups.values() if n > 1),
        "max_selected_model_opening_group_size": max(opening_groups.values(), default=0),
        "limitations": manifest["limitations"] + [
            "Opening groups are observable prompt-prefix/model groups, not verified server session identities.",
            "All paired intervals are descriptive and unadjusted; service time/cache histories differ.",
            "Static controls pay every calibration candidate and use calibration gold; Auto uses opaque service-level market data.",
            "The paired evaluator's full_* fields denote the named reference, which may itself use compression."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "frontier", "natural", "extended", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads((args.artifacts / "manifest.json").read_text())
        frontier = json.loads((args.frontier / "manifest.json").read_text())
        hashes = {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}
        if manifest["fixtures"] != hashes or frontier["fixtures"] != hashes:
            raise PilotError("Auto analysis fixture hashes differ")
        tasks = load_tasks(args.natural, args.extended)
        auto_rows = validate_auto(manifest, json.loads((args.artifacts / "results.json").read_text()), tasks)
        rows = validate_matrix(frontier, json.loads((args.frontier / "results.json").read_text()), tasks, calibration_only=False)
        lock = json.loads((args.frontier / "static_calibration_lock.json").read_text())
        report = analyze(manifest, auto_rows, frontier, rows, lock, tasks)
        write_json(args.output, report, immutable=True)
        print(json.dumps({"output": str(args.output), "comparisons": len(report["comparisons"]), "paper_evidence": False}))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Auto analysis stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
