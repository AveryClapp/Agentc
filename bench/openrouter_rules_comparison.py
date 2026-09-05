"""Read-only matched complete workflow comparison, not confirmatory evidence."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from bench.openrouter_matrix import file_hash, score, write_json
from bench.openrouter_pilot import PilotError, digest, money
from bench.openrouter_rules_live import ARMS, Acquisition
from bench.openrouter_rules_protocol import STAGES


def load_tasks(manifest, fixture):
    if file_hash(fixture) != manifest["fixture_sha256"]:
        raise PilotError("comparison fixture differs from frozen acquisition")
    rows = json.loads(fixture.read_text())
    tasks = {row["task_id"]: row for row in rows}
    if len(tasks) != len(rows):
        raise PilotError("duplicate fixture task identity")
    return tasks


def compare(manifest, decisions, calls, intents, tasks):
    Acquisition.validate_journals(SimpleNamespace(manifest=manifest, decisions=decisions, calls=calls,
        intents=intents, stage="rules-live-dev-v1-" + digest(manifest)[:20]))
    by_id = {row["id"]: row for row in calls}
    grouped = {arm: {} for arm in ARMS}
    for decision in decisions:
        if decision["phase"] == "development":
            grouped[decision["arm"]].setdefault(decision["task_id"], {})[decision["workflow_stage"]] = decision
    complete = {arm: {task for task, stages in groups.items() if set(stages) == set(STAGES)}
                for arm, groups in grouped.items()}
    common = sorted(set.intersection(*complete.values()))
    paired, reports = [], []
    for task in common:
        scores = {arm: score(by_id[grouped[arm][task]["answer"]["primary_id"]]["answer"],
                             tasks[task]["expected"])["f1"] for arm in ARMS}
        paired.append({"task_id": task, "final_f1": scores,
                       "f1_delta_vs_original": {arm: value-scores["original"] for arm, value in scores.items()}})
    for arm in ARMS:
        matched_ids = {row_id for task in common for decision in grouped[arm][task].values()
                       for row_id in decision["incurred_ids"]}
        charged = [row for row in calls if row["arm"] == arm
                   or (arm == "sequential" and row["phase"] == "calibration")]
        setup = [row for row in charged if row["phase"] in {"calibration", "warmup"}]
        matched = [row for row in charged if row["id"] in matched_ids]
        unmatched = [row for row in charged if row["phase"] == "development" and row["id"] not in matched_ids]
        if len(set(row["id"] for row in setup + matched + unmatched)) != len(charged):
            raise PilotError("comparison cost partition does not cover each charged call exactly once")
        def cost(rows, field="cost_usd"):
            return str(sum((money(row[field]) for row in rows), Decimal(0)))
        reports.append({"arm": arm, "completed_development_questions": len(complete[arm]),
            "matched_questions": len(common), "matched_mean_f1": sum(p["final_f1"][arm] for p in paired)/len(paired) if paired else None,
            "matched_mean_f1_delta_vs_original": sum(p["f1_delta_vs_original"][arm] for p in paired)/len(paired) if paired else None,
            "all_artifact_billed_usd": cost(charged), "setup_billed_usd": cost(setup),
            "matched_billed_usd": cost(matched), "matched_nominal_uncached_usd": cost(matched, "nominal_uncached_cost_usd"),
            "matched_primary_billed_usd": cost([row for row in matched if row["scope"] == "primary"]),
            "matched_probe_shadow_billed_usd": cost([row for row in matched if row["scope"] != "primary"]),
            "unmatched_or_partial_development_billed_usd": cost(unmatched), "matched_call_ids": sorted(matched_ids)})
    return {"paper_evidence": False, "kind": "matched_complete_development_workflows", "manifest_sha256": digest(manifest),
        "calls_sha256": digest(calls), "decisions_sha256": digest(decisions), "intents_sha256": digest(intents),
        "comparison_available": bool(common), "matched_task_ids": common, "paired_questions": paired, "reports": reports,
        "limitations": ["Post-hoc development intersection, not a holdout or an unbiased estimate under cost-dependent stopping.",
            "No significance, non-inferiority, safety, or joint-system-benefit claim follows from this small screen.",
            "Known artifact costs only: authoritative ledger-only charges and unresolved failure allowances must be reported separately.",
            "Setup includes warmup and, for sequential, all global-router calibration calls; do not treat matched costs as setup-inclusive.",
            "Probe answers do not enter downstream histories or final task scores."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        values = [json.loads((args.artifacts / name).read_text()) for name in ("manifest.json", "decisions.json", "calls.json", "intents.json")]
        result = compare(*values, load_tasks(values[0], args.fixture))
        result["analysis_source_sha256"] = file_hash(Path(__file__))
        write_json(args.output, result, immutable=True)
        print(json.dumps({"comparison_available": result["comparison_available"], "matched_questions": len(result["matched_task_ids"])}))
        return 0
    except (PilotError, OSError, KeyError, ValueError, TypeError) as exc:
        print(f"Workflow comparison stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
