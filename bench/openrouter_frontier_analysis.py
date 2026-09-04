"""Calibration-locked static controls and question-paired frontier diagnostics."""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench.openrouter_analysis import wilson
from bench.openrouter_frontier import CONTEXTS, SOURCE_MODEL, load_tasks
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_pilot import PilotError, digest, money
from bench.openrouter_replay import validate_matrix


def total_cost(rows: list[dict[str, Any]], field: str) -> Decimal:
    return sum((money(r[field]) for r in rows), Decimal(0))


def calibrate(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Learn static controls from calibration labels only; lock before holdout."""
    calibration = [r for r in rows if r["phase"] == "calibration"]
    controls = []
    margin = manifest["policy_replay"]["risk_margin"]
    for context in CONTEXTS:
        groups = defaultdict(list)
        for row in calibration:
            if row["context"] == context:
                groups[(row["model"], row["arm"])].append(row)
        if len(groups) != len(manifest["endpoints"]) * 2 or any(len(v) != manifest["calibration_tasks"] for v in groups.values()):
            raise PilotError("static selection needs complete calibration cells")
        ref_f1 = sum(r["f1"] for r in groups[(SOURCE_MODEL, "full")]) / manifest["calibration_tasks"]
        for name, allow_rewrite in (("calibrated_fixed_model", False), ("calibrated_static_joint", True)):
            eligible = []
            candidates = []
            for (model, arm), values in sorted(groups.items()):
                if not allow_rewrite and arm != "full":
                    continue
                f1 = sum(r["f1"] for r in values) / len(values)
                cost = total_cost(values, "nominal_uncached_cost_usd")
                candidate = {"model": model, "arm": arm, "calibration_f1": f1, "calibration_nominal_cost_usd": str(cost)}
                candidates.append(candidate)
                if f1 >= ref_f1 - margin:
                    eligible.append((cost, model, arm, candidate))
            if not eligible:
                raise PilotError("even the reference failed its own static selection constraint")
            selected = min(eligible, key=lambda x: x[:3])[3]
            controls.append({"name": name, "context": context, "reference_calibration_f1": ref_f1,
                             "margin": margin, "selected": selected, "candidates": candidates})
    return {"paper_evidence": False, "manifest_sha256": digest(manifest),
            "calibration_rows_sha256": digest(calibration), "controls": controls,
            "selection_scope": "calibration labels only; empirical mean constraint, not safety-certified",
            "cost_scope": "all candidate calibration calls plus source warmup and selected heldout calls"}


def paired_interval(full: list[dict[str, Any]], treatment: list[dict[str, Any]], *, seed: str, draws: int = 2000) -> dict[str, Any]:
    """Descriptive paired bootstrap conditional on a fixed treatment/selection."""
    if not full or len(full) != len(treatment) or any(a["task_id"] != b["task_id"] for a, b in zip(full, treatment)):
        raise PilotError("bootstrap requires question-matched pairs")
    n = len(full)
    delta = [b["f1"] - a["f1"] for a, b in zip(full, treatment)]
    base = [float(money(r["nominal_uncached_cost_usd"])) for r in full]
    target = [float(money(r["nominal_uncached_cost_usd"])) for r in treatment]
    if sum(base) <= 0:
        raise PilotError("positive reference cost required")
    rng = random.Random(seed)
    quality, savings = [], []
    for _ in range(draws):
        indices = [rng.randrange(n) for _ in range(n)]
        quality.append(sum(delta[i] for i in indices) / n)
        denom = sum(base[i] for i in indices)
        savings.append(1 - sum(target[i] for i in indices) / denom if denom else 0)
    def interval(values):
        values.sort()
        return [values[int(.025 * (len(values) - 1))], values[int(.975 * (len(values) - 1))]]
    return {"draws": draws, "quality_f1_delta_95": interval(quality),
            "nominal_uncached_cost_reduction_95": interval(savings),
            "scope": "unadjusted descriptive question-paired bootstrap; conditional on frozen selection, not joint familywise evidence"}


def pair_summary(full: list[dict[str, Any]], treatment: list[dict[str, Any]], seed: str) -> dict[str, Any]:
    full = sorted(full, key=lambda r: r["task_id"])
    treatment = sorted(treatment, key=lambda r: r["task_id"])
    intervals = paired_interval(full, treatment, seed=seed)
    n = len(full)
    full_f1 = sum(r["f1"] for r in full) / n
    treatment_f1 = sum(r["f1"] for r in treatment) / n
    losses = [a["task_id"] for a, b in zip(full, treatment) if b["f1"] < a["f1"]]
    em_losses = [a["task_id"] for a, b in zip(full, treatment) if a["em"] == 1 and b["em"] == 0]
    em_gains = [a["task_id"] for a, b in zip(full, treatment) if a["em"] == 0 and b["em"] == 1]
    original_cost = total_cost(full, "cost_usd")
    nominal = total_cost(full, "nominal_uncached_cost_usd")
    return {"questions": n, "full_f1": full_f1, "treatment_f1": treatment_f1,
        "f1_delta": treatment_f1 - full_f1, "full_exact_matches": sum(int(r["em"]) for r in full),
        "treatment_exact_matches": sum(int(r["em"]) for r in treatment),
        "any_f1_loss_count": len(losses), "f1_loss_task_ids": losses,
        "mean_positive_f1_loss": sum(max(0, a["f1"] - b["f1"]) for a, b in zip(full, treatment)) / n,
        "any_f1_loss_wilson_95": wilson(len(losses), n),
        "strict_em_losses": len(em_losses), "strict_em_gains": len(em_gains),
        "em_loss_task_ids": em_losses, "em_gain_task_ids": em_gains,
        "full_billed_cost_usd": str(original_cost), "treatment_billed_cost_usd": str(total_cost(treatment, "cost_usd")),
        "billed_cost_reduction": float(1 - total_cost(treatment, "cost_usd") / original_cost) if original_cost else None,
        "full_nominal_uncached_cost_usd": str(nominal),
        "treatment_nominal_uncached_cost_usd": str(total_cost(treatment, "nominal_uncached_cost_usd")),
        "nominal_uncached_cost_reduction": float(1 - total_cost(treatment, "nominal_uncached_cost_usd") / nominal),
        "full_input_tokens": sum(r["usage"]["prompt_tokens"] for r in full),
        "treatment_input_tokens": sum(r["usage"]["prompt_tokens"] for r in treatment),
        "treatment_rewrites": sum(r["native_plan"]["kind"] == "rewritten" for r in treatment),
        "identical_payload_pairs": sum(a["request_sha256"] == b["request_sha256"] for a, b in zip(full, treatment)),
        "intervals": intervals}


def analyze(manifest: dict[str, Any], rows: list[dict[str, Any]], lock: dict[str, Any]) -> dict[str, Any]:
    if calibrate(manifest, rows) != lock:
        raise PilotError("static lock changed or no longer matches calibration")
    heldout = [r for r in rows if r["phase"] == "holdout"]
    compression, controls = [], []
    for context in CONTEXTS:
        for model in sorted(manifest["endpoints"]):
            full = [r for r in heldout if (r["context"], r["model"], r["arm"]) == (context, model, "full")]
            treatment = [r for r in heldout if (r["context"], r["model"], r["arm"]) == (context, model, "compress")]
            compression.append({"context": context, "model": model,
                                **pair_summary(full, treatment, digest(["compression-v2", context, model]))})
        for control in [c for c in lock["controls"] if c["context"] == context]:
            selected = control["selected"]
            full = [r for r in heldout if (r["context"], r["model"], r["arm"]) == (context, SOURCE_MODEL, "full")]
            treatment = [r for r in heldout if (r["context"], r["model"], r["arm"]) == (context, selected["model"], selected["arm"])]
            paid_candidates = {(c["model"], c["arm"]) for c in control["candidates"]}
            setup = [r for r in rows if r["context"] == context and (
                (r["phase"] == "warmup" and r["model"] == SOURCE_MODEL and r["arm"] == "full") or
                (r["phase"] == "calibration" and (r["model"], r["arm"]) in paid_candidates))]
            controls.append({"name": control["name"], "context": context, "selected": selected,
                "setup_calls": len(setup), "setup_billed_cost_usd": str(total_cost(setup, "cost_usd")),
                "total_billed_cost_noncausal_usd": str(total_cost(setup + treatment, "cost_usd")),
                "total_nominal_uncached_cost_estimate_usd": str(total_cost(setup + treatment, "nominal_uncached_cost_usd")),
                **pair_summary(full, treatment, digest(["static-v2", context, control["name"]]))})
    return {"paper_evidence": False, "manifest_sha256": digest(manifest), "results_sha256": digest(rows),
        "calibration_lock_sha256": digest(lock), "acquisition_cost_usd": str(total_cost(rows, "cost_usd")),
        "compression": compression, "calibration_selected_controls": controls,
        "limitations": manifest["limitations"] + [
            "Within-context intervals pair by question; do not treat the two contexts as independent datasets or double sample size.",
            "Bootstrap intervals are descriptive/unadjusted; calibration uncertainty and provider replication uncertainty are not estimated.",
            "A 0.02 mean F1 margin is distinct from probability of any task harm.",
            "Static controls receive calibration gold labels and pay every candidate calibration call; native policies receive no gold."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("calibrate", "analyze"))
    for name in ("artifacts", "natural", "extended"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads((args.artifacts / "manifest.json").read_text())
        if manifest["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}:
            raise PilotError("analysis fixtures differ from manifest")
        rows = validate_matrix(manifest, json.loads((args.artifacts / "results.json").read_text()),
                               load_tasks(args.natural, args.extended), calibration_only=args.command == "calibrate")
        lock_path = args.artifacts / "static_calibration_lock.json"
        if args.command == "calibrate":
            report = calibrate(manifest, rows)
            path = lock_path
        else:
            report = analyze(manifest, rows, json.loads(lock_path.read_text()))
            path = args.artifacts / "frontier_analysis.json"
        write_json(path, report, immutable=True)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Frontier analysis stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
