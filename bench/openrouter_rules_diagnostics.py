"""Read-only exact-plan diagnostics for the development workflow; no API calls.

Observed proxy disagreement is neither task damage nor a guard false-positive
label. This report does not reconstruct native disable state from aggregates.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_pilot import PilotError, digest, money
from bench.openrouter_rules_validity import provider_failures


def repeated_source_requests(decisions, by_id):
    """Disjoint adjacent repeats of exact warmup payloads, not all-pairs n²."""
    from bench.openrouter_replay import lexical_divergence
    groups = defaultdict(list)
    for d in decisions:
        if d["phase"] != "warmup" or d["native_plan"]["kind"] != "pass_through":
            continue
        row = by_id[d["primary_id"]]
        key = (d["task_id"], d["workflow_stage"], row["model"], row["provider"], row["request_sha256"])
        groups[key].append(row)
    pairs = []
    for (task_id, stage, model, provider, request_hash), rows in sorted(groups.items()):
        for left, right in zip(rows[::2], rows[1::2]):
            pairs.append({"task_id": task_id, "workflow_stage": stage, "model": model, "provider": provider,
                "request_sha256": request_hash, "row_ids": [left["id"], right["id"]],
                "lexical_divergence": lexical_divergence(left["answer"], right["answer"]),
                "output_token_difference": right["usage"]["completion_tokens"] - left["usage"]["completion_tokens"]})
    reports = []
    for stage in sorted({p["workflow_stage"] for p in pairs}):
        values = [p["lexical_divergence"] for p in pairs if p["workflow_stage"] == stage]
        reports.append({"workflow_stage": stage, "disjoint_pairs": len(values),
            "mean_lexical_divergence": sum(values)/len(values),
            "pairs_exceeding_0_01": sum(v > .01 for v in values),
            "pairs_exceeding_0_02": sum(v > .02 for v in values),
            "pairs_exceeding_0_03": sum(v > .03 for v in values)})
    return {"pairing": "adjacent disjoint pairs in acquisition order within exact task/stage/model/provider/payload warmup groups",
        "pairs": pairs, "by_stage": reports, "limitations": [
            "Post-hoc development diagnostic; no guard setting is selected from these values.",
            "Several pairs share a question; pair count is not independent question count.",
            "Output variation without optimization is not proof of semantic equivalence or guard false positives."]}


def analyze(manifest, decisions, calls, intents):
    # Reuse the frozen acquisition's side-effect-free journal validator. Its
    # required state is explicit; no constructor, credential, ledger, native
    # runtime or dispatch is invoked by this read-only report.
    from bench.openrouter_rules_live import Acquisition
    state = SimpleNamespace(manifest=manifest, decisions=decisions, calls=calls, intents=intents,
                            stage="rules-live-dev-v1-" + digest(manifest)[:20])
    Acquisition.validate_journals(state)
    by_id = {row["id"]: row for row in calls}
    if len(by_id) != len(calls):
        raise PilotError("duplicate paid call identity")
    groups = defaultdict(list)
    used_feedback = set()
    for decision in decisions:
        feedback = decision["divergence_feedback"]
        if feedback is None:
            continue
        if not math.isfinite(feedback) or not 0 <= feedback <= 1:
            raise PilotError("invalid divergence feedback")
        signature = decision["semantic_plan"]
        is_candidate = signature["candidate"] is not None
        identity = signature["candidate_identity" if is_candidate else "primary_identity"]
        if not identity or set(identity.get("key", {})) != {"call_site_version", "execution_plan_id"}:
            raise PilotError("paired outcome lacks a complete native plan identity")
        ids = decision["incurred_ids"]
        if len(ids) != 2 or ids[0] != decision["primary_id"]:
            raise PilotError("paired decision needs exactly primary and comparison")
        if any(row_id in used_feedback for row_id in ids):
            raise PilotError("feedback call reused across decisions")
        used_feedback.update(ids)
        primary, comparison = (by_id[row_id] for row_id in ids)
        expected_scope = "exploration" if is_candidate else "shadow"
        if primary["scope"] != "primary" or comparison["scope"] != expected_scope:
            raise PilotError("paired outcome scope mismatch")
        from bench.openrouter_replay import lexical_divergence
        if feedback != lexical_divergence(primary["answer"], comparison["answer"]):
            raise PilotError("divergence differs from observed response texts")
        selected, reference = (comparison, primary) if is_candidate else (primary, comparison)
        payload = signature["candidate" if is_candidate else "primary"]
        rules = signature["candidate_rules" if is_candidate else "primary_rules"]
        key = (decision["arm"], decision["workflow_stage"], digest(identity))
        groups[key].append({"identity": identity, "task_id": decision["task_id"], "divergence": feedback,
            "scope": expected_scope, "model": payload["model"], "max_tokens": payload["max_tokens"], "rules": rules,
            "input_token_difference": selected["usage"]["prompt_tokens"] - reference["usage"]["prompt_tokens"],
            "output_token_difference": selected["usage"]["completion_tokens"] - reference["usage"]["completion_tokens"],
            "nominal_cost_difference_usd": str(money(selected["nominal_uncached_cost_usd"]) - money(reference["nominal_uncached_cost_usd"]))})
    reports = []
    for (arm, stage, _), values in sorted(groups.items()):
        first = values[0]
        threshold = first["identity"]["divergence_threshold"]
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise PilotError("invalid exact-plan threshold")
        if any(any(v[k] != first[k] for k in ("identity", "model", "max_tokens", "rules")) for v in values):
            raise PilotError("one exact plan has inconsistent dispatch identity")
        ds = sorted(v["divergence"] for v in values)
        n = len(ds)
        required = int(manifest["policies"][arm]["AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE"])
        p95 = ds[math.ceil(.95*n)-1]
        reports.append({"arm": arm, "workflow_stage": stage, **{k: first[k] for k in ("identity", "model", "max_tokens", "rules")},
            "observed_pairs": n, "minimum_required_pairs": required, "below_minimum_pair_count": n < required,
            "identical_whitespace_token_sets": sum(d == 0 for d in ds), "mean_divergence": sum(ds)/n,
            "descriptive_nearest_rank_p95": p95, "observed_p95_over_threshold": p95 > threshold,
            "observed_positive_excess_sum": sum(max(0, d-threshold) for d in ds),
            "mean_input_token_difference": sum(v["input_token_difference"] for v in values)/n,
            "mean_output_token_difference": sum(v["output_token_difference"] for v in values)/n,
            "observations": values})
    failures = provider_failures(calls)
    return {"paper_evidence": False, "kind": "development_exact_plan_feedback_diagnostics",
        "analysis_eligible": not failures, "failed_provider_calls": failures,
        "manifest_sha256": digest(manifest), "decisions_sha256": digest(decisions), "calls_sha256": digest(calls),
        "exact_plans_with_feedback": len(reports), "paired_decisions": sum(r["observed_pairs"] for r in reports),
        "intents_sha256": digest(intents),
        "repeated_source_requests": repeated_source_requests(decisions, by_id),
        "plans": reports, "limitations": [
            "If analysis_eligible is false, feedback includes potentially contaminated controller state and is only a raw incident diagnostic, not valid evidence.",
            "This is observed feedback, not native eligibility/disable state or a quality guarantee.",
            "Descriptive p95 uses nearest rank over all recorded pairs, not native rolling-window interpolation.",
            "Positive excess is an observed sum, not reconstructed time-window guard state.",
            "Token differences compare paid stage outputs; they do not measure downstream task damage.",
            "Rule-only activation counts can hide distinct cap/model/call-site-version plan identities."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        values = [json.loads((args.artifacts / name).read_text()) for name in ("manifest.json", "decisions.json", "calls.json", "intents.json")]
        result = analyze(*values)
        result["analysis_source_sha256"] = file_hash(Path(__file__))
        write_json(args.output, result, immutable=True)
        print(json.dumps({k: result[k] for k in ("exact_plans_with_feedback", "paired_decisions")}))
        return 0
    except (PilotError, OSError, KeyError, ValueError, TypeError) as exc:
        print(f"Workflow diagnostics stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
