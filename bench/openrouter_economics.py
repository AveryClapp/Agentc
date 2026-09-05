"""Descriptive cache, latency, repeatability, and setup-cost diagnostics.

This evaluator never dispatches a provider call or chooses a policy. Matrix
cache warming and shared-host timing prevent causal deployment claims.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from bench.openrouter_frontier import CONTEXTS, SOURCE_MODEL, load_tasks
from bench.openrouter_frontier_analysis import calibrate, total_cost
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_mechanisms import indexed, mean_interval
from bench.openrouter_pilot import PilotError, digest, money
from bench.openrouter_replay import validate_matrix


def quantile(values, probability):
    """Nearest-rank empirical quantile; not the planner's conformal bound."""
    if not values or not 0 < probability <= 1:
        raise PilotError("nonempty samples and probability in (0,1] required")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise PilotError("finite numeric diagnostic samples required")
    return sorted(values)[math.ceil(probability * len(values)) - 1]


def cache_latency(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["phase"], row["context"], row["model"], row["arm"])].append(row)
    reports = []
    for key, group in sorted(groups.items()):
        known = []
        for row in group:
            cached = row["cached_input_tokens"]
            raw = row["usage"].get("prompt_tokens_details", {}).get("cached_tokens")
            if cached != raw or (cached is not None and (
                isinstance(cached, bool) or not isinstance(cached, int)
                or not 0 <= cached <= row["usage"]["prompt_tokens"]
            )):
                raise PilotError("cache diagnostic accounting mismatch")
            if cached is not None:
                known.append(row)
        billed = total_cost(group, "cost_usd")
        nominal = total_cost(group, "nominal_uncached_cost_usd")
        latency = [r["latency_ms"] for r in group]
        if any(v < 0 for v in latency):
            raise PilotError("negative request latency")
        known_tokens = sum(r["usage"]["prompt_tokens"] for r in known)
        reports.append({"phase": key[0], "context": key[1], "model": key[2], "arm": key[3],
            "calls": len(group), "cache_accounting_known_calls": len(known),
            "cache_accounting_missing_calls": len(group) - len(known),
            "cache_hit_calls": sum(r["cached_input_tokens"] > 0 for r in known),
            "cached_input_tokens_known": sum(r["cached_input_tokens"] for r in known),
            "known_input_tokens": known_tokens,
            "cached_fraction_among_known_input_tokens": sum(r["cached_input_tokens"] for r in known) / known_tokens if known_tokens else None,
            "billed_cost_usd": str(billed), "nominal_uncached_cost_usd": str(nominal),
            "nominal_minus_billed_usd": str(nominal - billed),
            "billed_to_nominal_ratio": float(billed / nominal) if nominal else None,
            "request_latency_ms_p50": quantile(latency, .5),
            "request_latency_ms_p95": quantile(latency, .95),
            "scope": "cache counters are provider reports; nominal-minus-billed is not attributed solely to caching; request wall time excludes optimizer and account-check overhead"})
    return reports


def paired_repeats(manifest, rows):
    cells = indexed(rows)
    reports = []
    for context in CONTEXTS:
        ids = sorted({r["task_id"] for r in rows if r["context"] == context})
        for model in sorted(manifest["endpoints"]):
            pairs = [(cells[(context, i, model, "full")], cells[(context, i, model, "compress")]) for i in ids]
            for identical in (True, False):
                selected = [(a, b) for a, b in pairs if (a["request_sha256"] == b["request_sha256"]) == identical]
                if not selected:
                    continue
                deltas = [b["latency_ms"] - a["latency_ms"] for a, b in selected]
                reports.append({"context": context, "model": model, "identical_payload": identical,
                    "pairs": len(selected), "different_exact_answer_strings": sum(a["answer"] != b["answer"] for a, b in selected),
                    "f1_losses": sum(b["f1"] < a["f1"] for a, b in selected),
                    "f1_gains": sum(b["f1"] > a["f1"] for a, b in selected),
                    "mean_f1_delta": sum(b["f1"] - a["f1"] for a, b in selected) / len(selected),
                    "mean_request_latency_delta_ms": sum(deltas) / len(deltas),
                    "request_latency_delta_paired_bootstrap_95_ms": mean_interval(deltas, digest(["request-latency-v1", context, model, identical])),
                    "billed_cost_delta_usd": str(sum((money(b["cost_usd"]) - money(a["cost_usd"]) for a, b in selected), Decimal(0))),
                    "scope": "separate provider calls; identical payloads are no-op repeat controls, not rewrite effects; latency contrasts are descriptive, not causal"})
    return reports


def amortization(rows, lock):
    reports = []
    for control in lock["controls"]:
        context = control["context"]
        selected = control["selected"]
        candidates = {(c["model"], c["arm"]) for c in control["candidates"]}
        group = [r for r in rows if r["context"] == context]
        reference = [r for r in group if (r["model"], r["arm"]) == (SOURCE_MODEL, "full")]
        ref_setup = [r for r in reference if r["phase"] in ("warmup", "calibration")]
        ref_heldout = [r for r in reference if r["phase"] == "holdout"]
        setup = [r for r in group if (r["phase"] == "warmup" and (r["model"], r["arm"]) == (SOURCE_MODEL, "full"))
            or (r["phase"] == "calibration" and (r["model"], r["arm"]) in candidates)]
        treatment = [r for r in group if r["phase"] == "holdout" and (r["model"], r["arm"]) == (selected["model"], selected["arm"])]
        if not ref_heldout or sorted(r["task_id"] for r in ref_heldout) != sorted(r["task_id"] for r in treatment):
            raise PilotError("amortization requires paired heldout tasks")
        field = "nominal_uncached_cost_usd"
        ref_total = total_cost(ref_setup + ref_heldout, field)
        target_total = total_cost(setup + treatment, field)
        excess_setup = total_cost(setup, field) - total_cost(ref_setup, field)
        per_task = (total_cost(ref_heldout, field) - total_cost(treatment, field)) / len(ref_heldout)
        break_even = int((max(Decimal(0), excess_setup) / per_task).to_integral_value(rounding=ROUND_CEILING)) if per_task > 0 else None
        reports.append({"name": control["name"], "context": context, "selected": selected,
            "heldout_tasks": len(ref_heldout), "baseline_setup_calls": len(ref_setup), "policy_setup_calls": len(setup),
            "baseline_total_nominal_usd": str(ref_total), "policy_total_nominal_usd": str(target_total),
            "setup_inclusive_nominal_cost_reduction": float(1 - target_total / ref_total) if ref_total else None,
            "incremental_setup_nominal_usd": str(excess_setup), "heldout_mean_marginal_saving_nominal_usd": str(per_task),
            "projected_break_even_post_calibration_tasks": break_even,
            "scope": "projection assumes stationary heldout mean token costs, excludes implicit cache effects, and charges every candidate calibration call; null means no positive marginal saving"})
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "natural", "extended", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads((args.artifacts / "manifest.json").read_text())
        if manifest["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}:
            raise PilotError("economics fixtures differ from manifest")
        rows = validate_matrix(manifest, json.loads((args.artifacts / "results.json").read_text()),
            load_tasks(args.natural, args.extended), calibration_only=False)
        lock = json.loads((args.artifacts / "static_calibration_lock.json").read_text())
        if calibrate(manifest, rows) != lock:
            raise PilotError("economics static calibration lock changed")
        report = {"paper_evidence": False, "manifest_sha256": digest(manifest), "results_sha256": digest(rows),
            "analysis_source_sha256": file_hash(Path(__file__)), "calibration_lock_sha256": digest(lock),
            "cache_latency": cache_latency(rows),
            "paired_repeats": paired_repeats(manifest, [r for r in rows if r["phase"] == "holdout"]),
            "amortization": amortization(rows, lock), "limitations": manifest["limitations"] + [
                "Descriptive diagnostics added after calibration; neither acquisition nor selection is changed.",
                "Implicit cache warming, output length, and shared-host timing confound latency/cost contrasts.",
                "Intervals are unadjusted across groups; projections are not measured future savings."]}
        write_json(args.output, report, immutable=True)
        print(json.dumps({"output": str(args.output), "paper_evidence": False}))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Economics analysis stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
