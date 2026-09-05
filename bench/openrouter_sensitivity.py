"""Offline guard-tolerance sweep; no API calls or best-heldout selection."""
from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from bench.openrouter_frontier import CONTEXTS, ROOT, load_tasks
from bench.openrouter_matrix import file_hash, load_module, write_json
from bench.openrouter_pilot import PilotError, digest
from bench.openrouter_replay import evaluate, replay_policy, validate_matrix

THRESHOLDS = ("0", "0.05", "0.15", "0.5", "1")


def grid(manifest):
    result = []
    for threshold in THRESHOLDS:
        for policy in manifest["policy_replay"]["specs"]:
            policy = deepcopy(policy)
            policy["primary_policy_name"] = policy["name"]
            policy["name"] += "/lexical_" + threshold
            policy["settings"]["AGENTC_SHADOW_DIVERGENCE_BUDGET"] = threshold
            policy["lexical_threshold"] = threshold
            result.append(policy)
    return result


def sources(manifest):
    return {**manifest["source_files"], **{p: file_hash(ROOT / p) for p in (
        "bench/openrouter_replay.py", "bench/openrouter_sensitivity.py")}}


def comparable(trajectory):
    """Behavior/cost identity excluding timestamps and opaque native tokens."""
    return [{k: v for k, v in d.items() if k != "native_plan"} for d in trajectory["decisions"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    for name in ("artifacts", "natural", "extended", "native", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--calibration-only", action="store_true")
    parser.add_argument("--restart-after-calibration", action="store_true")
    args = parser.parse_args()
    try:
        manifest = json.loads((args.artifacts / "manifest.json").read_text())
        if (manifest["native_sha256"] != file_hash(args.native)
                or manifest["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}
                or os.environ.get("PYTHONHASHSEED") != manifest["pythonhashseed"]):
            raise PilotError("sensitivity native, fixtures, or hash seed changed")
        if any(file_hash(ROOT / p) != h for p, h in manifest["source_files"].items()):
            raise PilotError("acquisition source changed")
        specification = args.output / "manifest.json"
        if args.command == "prepare":
            frozen = {"paper_evidence": False, "created_at": datetime.now(timezone.utc).isoformat(),
                "frontier_manifest_sha256": digest(manifest), "source_files": sources(manifest),
                "grid": grid(manifest), "contexts": list(CONTEXTS),
                "scope": "descriptive sensitivity, not replacement for frozen primary policies or best-heldout configuration",
                "limitations": ["Tolerance1 intentionally accepts any lexical disagreement; it is not a safety contract.",
                    "Tolerance0 uses exact whitespace-token-set agreement; neither endpoint certifies correctness.",
                    "All configurations use the same measured questions; they are not independent replications.",
                    "No new provider calls; selected-feedback replay charges reused measurements counterfactually.",
                    "All gold is evaluator-only; no policy is selected by heldout scores."]}
            write_json(specification, frozen, immutable=True)
            print(json.dumps({"configurations_per_context": len(frozen["grid"]), "manifest_sha256": digest(frozen)}))
            return 0
        frozen = json.loads(specification.read_text())
        if frozen["frontier_manifest_sha256"] != digest(manifest) or frozen["source_files"] != sources(manifest) or frozen["grid"] != grid(manifest):
            raise PilotError("sensitivity specification or source changed")
        tasks = load_tasks(args.natural, args.extended)
        rows = validate_matrix(manifest, json.loads((args.artifacts / "results.json").read_text()), tasks,
                               calibration_only=args.calibration_only)
        phase = "calibration" if args.calibration_only else "complete"
        prefix = phase + ("-restart" if args.restart_after_calibration else "")
        output = args.output / (prefix + ".json")
        if output.exists():
            raise PilotError("sensitivity result already exists")
        native = load_module("_native", args.native, native=True)
        attention = load_module("sensitivity_attention", ROOT / "python/agentc/_attention.py")
        trajectories, reports = [], []
        for policy in frozen["grid"]:
            for context in CONTEXTS:
                trajectory = replay_policy(native, attention, manifest, rows, tasks, policy, context,
                    restart_after_calibration=args.restart_after_calibration)
                trajectory["lexical_threshold"] = policy["lexical_threshold"]
                trajectory["primary_policy_name"] = policy["primary_policy_name"]
                report = {**evaluate(trajectory, rows), "lexical_threshold": policy["lexical_threshold"]}
                trajectories.append(trajectory)
                reports.append(report)
                print(json.dumps({"policy": policy["name"], "context": context,
                                  "revealed_calls": trajectory["revealed_calls"]}), flush=True)
        write_json(output, {"paper_evidence": False, "evaluation_kind": "offline_selected_feedback_sensitivity",
            "manifest_sha256": digest(manifest), "sensitivity_manifest_sha256": digest(frozen),
            "consumed_rows_sha256": digest(rows), "calibration_only": args.calibration_only,
            "restart_after_calibration": args.restart_after_calibration,
            "reports": reports, "trajectories": trajectories,
            "limitations": manifest["limitations"] + frozen["limitations"]}, immutable=True)
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Sensitivity stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
