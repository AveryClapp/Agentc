"""Separately frozen evidence-refresh ablation of measured provider outcomes.

The acquisition's native library is never replaced. This post-calibration
engineering comparison is not a new provider experiment or confirmatory claim.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from bench.openrouter_frontier import CONTEXTS, ROOT, load_tasks
from bench.openrouter_matrix import file_hash, load_module, write_json
from bench.openrouter_pilot import PilotError, digest, money
from bench.openrouter_replay import evaluate, replay_policy, validate_matrix
from bench.openrouter_sensitivity import comparable

REVIEWED_COMMIT = "355f099c5db8ca08b9df3fa44f3bb3db90479df8"
PATCH_FILES = frozenset({
    "crates/agentc-optimizer/src/execution_plan.rs",
    "crates/agentc-optimizer/src/exploration.rs",
    "crates/agentc-optimizer/src/ffi.rs",
    "crates/agentc-optimizer/examples/exploration_preflight.rs",
})


def validate_patch(original, patched):
    if set(original) != set(patched):
        raise PilotError("patched runtime source coverage differs")
    changed = {p for p in original if original[p] != patched[p]}
    if changed != PATCH_FILES:
        raise PilotError("runtime differs outside the reviewed evidence-refresh patch")


def source_snapshot(manifest, runtime_source):
    original = manifest["source_files"]
    if any(file_hash(ROOT / p) != h for p, h in original.items()):
        raise PilotError("original acquisition source changed")
    patched = {p: file_hash(runtime_source / p) for p in original}
    validate_patch(original, patched)
    # Verify the build checkout against the independently reviewed commit,
    # including dirty tracked source, rather than accepting arbitrary FFI edits.
    subprocess.run(["git", "-C", str(runtime_source), "diff", "--exit-code", "--quiet",
        REVIEWED_COMMIT, "--", *original], check=True)
    commit = subprocess.check_output(["git", "-C", str(runtime_source), "rev-parse", REVIEWED_COMMIT], text=True).strip()
    analysis = {p: file_hash(ROOT / p) for p in (
        "bench/openrouter_replay.py", "bench/openrouter_sensitivity.py", "bench/openrouter_refresh.py")}
    return {"runtime_commit": commit, "runtime_source_files": patched, "analysis_source_files": analysis}


def compare(baseline, patched):
    keys = lambda t: (t["policy"], t["context"])
    old = {keys(t): t for t in baseline}
    new = {keys(t): t for t in patched}
    if len(old) != len(baseline) or len(new) != len(patched) or old.keys() != new.keys():
        raise PilotError("refresh comparison needs identical unique policy/context cells")
    reports = []
    for key in sorted(old):
        a, b = old[key], new[key]
        if a["settings"] != b["settings"]:
            raise PilotError("refresh ablation changed a policy setting")
        left, right = comparable(a), comparable(b)
        if [(d["task_id"], d["phase"]) for d in left] != [(d["task_id"], d["phase"]) for d in right]:
            raise PilotError("refresh ablation changed task chronology")
        reports.append({"policy": key[0], "context": key[1], "decisions": len(left),
            "different_behavior_or_cost_decisions": sum(x != y for x, y in zip(left, right)),
            "different_primary_outcome_decisions": sum(x["primary_row_id"] != y["primary_row_id"] for x, y in zip(left, right)),
            "original_revealed_calls": a["revealed_calls"], "patched_revealed_calls": b["revealed_calls"],
            "patched_minus_original_nominal_cost_usd": str(sum((money(y["nominal_uncached_cost_estimate_usd"]) - money(x["nominal_uncached_cost_estimate_usd"])
                for x, y in zip(left, right)), money("0")))})
    return reports


def validate_baseline(baseline, frozen, manifest, rows, calibration_only, restart):
    if (baseline.get("manifest_sha256") != digest(manifest)
            or baseline.get("consumed_rows_sha256") != digest(rows)
            or baseline.get("calibration_only") is not calibration_only
            or baseline.get("restart_after_calibration") is not restart
            or baseline.get("paper_evidence") is not False
            or baseline.get("evaluation_kind") != "offline_selected_feedback_replay"
            or baseline.get("replay_source_sha256") != frozen["analysis_source_files"]["bench/openrouter_replay.py"]):
        raise PilotError("refresh original-runtime baseline provenance differs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    for name in ("artifacts", "natural", "extended", "native", "runtime-source", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--calibration-only", action="store_true")
    parser.add_argument("--restart-after-calibration", action="store_true")
    args = parser.parse_args()
    try:
        manifest = json.loads((args.artifacts / "manifest.json").read_text())
        if (manifest["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}
                or os.environ.get("PYTHONHASHSEED") != manifest["pythonhashseed"]):
            raise PilotError("refresh fixtures or hash seed changed")
        snapshot = source_snapshot(manifest, args.runtime_source)
        native_hash = file_hash(args.native)
        if native_hash == manifest["native_sha256"]:
            raise PilotError("refresh ablation requires the separate patched native artifact")
        specification = args.output / "manifest.json"
        identity = {"frontier_manifest_sha256": digest(manifest), "patched_native_sha256": native_hash,
            **snapshot, "policies": manifest["policy_replay"]["specs"], "contexts": list(CONTEXTS)}
        if args.command == "prepare":
            write_json(specification, {"paper_evidence": False, "created_at": datetime.now(timezone.utc).isoformat(),
                **identity, "scope": "post-calibration engineering ablation; same measured requests, original policies, separate reviewed runtime",
                "limitations": ["No new provider calls or causal live-policy estimates.",
                    "This does not replace the original frozen-runtime results.",
                    "The same per-site caps may exhaust before refresh is possible; recovery is not guaranteed.",
                    "No provider/workload drift is injected; unit-test recovery is not measured deployment adaptation."]}, immutable=True)
            print(json.dumps({"output": str(specification), "native_sha256": native_hash}))
            return 0
        frozen = json.loads(specification.read_text())
        if any(frozen.get(k) != v for k, v in identity.items()):
            raise PilotError("refresh specification, source, policies, or native changed")
        if args.baseline is None:
            raise PilotError("original-runtime baseline replay is required")
        baseline = json.loads(args.baseline.read_text())
        tasks = load_tasks(args.natural, args.extended)
        rows = validate_matrix(manifest, json.loads((args.artifacts / "results.json").read_text()), tasks,
            calibration_only=args.calibration_only)
        validate_baseline(baseline, frozen, manifest, rows, args.calibration_only, args.restart_after_calibration)
        phase = "calibration" if args.calibration_only else "complete"
        path = args.output / (phase + ("-restart" if args.restart_after_calibration else "") + ".json")
        if path.exists():
            raise PilotError("refresh result already exists")
        native = load_module("_native", args.native, native=True)
        attention = load_module("refresh_attention", ROOT / "python/agentc/_attention.py")
        trajectories, reports = [], []
        for policy in frozen["policies"]:
            for context in CONTEXTS:
                trajectory = replay_policy(native, attention, manifest, rows, tasks, policy, context,
                    restart_after_calibration=args.restart_after_calibration)
                trajectories.append(trajectory)
                reports.append(evaluate(trajectory, rows))
                print(json.dumps({"policy": policy["name"], "context": context, "revealed_calls": trajectory["revealed_calls"]}), flush=True)
        write_json(path, {"paper_evidence": False, "evaluation_kind": "offline_selected_feedback_runtime_fix_ablation",
            "manifest_sha256": digest(manifest), "refresh_manifest_sha256": digest(frozen),
            "baseline_sha256": digest(baseline), "consumed_rows_sha256": digest(rows),
            "calibration_only": args.calibration_only, "restart_after_calibration": args.restart_after_calibration,
            "reports": reports, "trajectories": trajectories, "comparison": compare(baseline["trajectories"], trajectories),
            "limitations": manifest["limitations"] + frozen["limitations"]}, immutable=True)
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"Refresh ablation stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
