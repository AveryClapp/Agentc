"""Paired natural/extended-context model × native rewrite measurements.

Acquisition is a frozen factorial, not a deployed policy evaluation. The
predeclared native policy configurations are for separately labeled sequential
offline replay with selected-only feedback. Never expose gold to native state.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench.openrouter_contract import endpoints, messages as contract_messages
from bench.openrouter_matrix import (
    MODELS, PROTOCOL, ROOT, SETTINGS, file_hash, load_module, native_call,
    preserve_replayed_row, score, source_hashes, write_json,
)
from bench.openrouter_pilot import Ledger, PilotError, canonical, digest, load_key, make_request, money

CONTEXTS = ("natural", "extended")
SOURCE_MODEL = MODELS[0][0]
CAP = 512
POLICY_SETTINGS = {
    "AGENTC_OPTIMIZE": "1", "AGENTC_EVAL_PLANNER_MODE": "joint_guarded", "AGENTC_COMPOSE": "1",
    "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3", "AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE": "20",
    "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW": "50", "AGENTC_OPTIMIZE_COST_MODEL_WINDOW": "50",
    "AGENTC_OPTIMIZE_DIVERGENCE_WINDOW": "50", "AGENTC_OPTIMIZE_PLAN_PROFILE_FRESHNESS_HOURS": "24",
    "AGENTC_OPTIMIZE_EXPLORATION": "1", "AGENTC_OPTIMIZE_MAX_CONCURRENT_COUNTERFACTUALS": "1",
    "AGENTC_OPTIMIZE_DIVERGENCE_EXPOSURE_BUDGET": "1", "AGENTC_SHADOW_DIVERGENCE_BUDGET": "0.05",
    "AGENTC_OPTIMIZE_SHADOW": "0.02", "AGENTC_SHADOW_DIVERGENCE_MODE": "lexical",
    "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "100", "AGENTC_OPTIMIZE_OBJECTIVE": "cost",
}


def sources() -> dict[str, str]:
    return {**source_hashes(), **{p: file_hash(ROOT / p) for p in
             ("bench/openrouter_frontier.py", "bench/openrouter_contract.py",
              "python/agentc/_patches/_optimizer_glue.py")}}


def load_tasks(natural: Path, extended: Path) -> dict[str, dict[str, dict[str, Any]]]:
    result = {}
    for context, path in zip(CONTEXTS, (natural, extended)):
        rows = json.loads(path.read_text())
        result[context] = {t["task_id"]: t for t in rows}
        if len(result[context]) != len(rows):
            raise PilotError("duplicate fixture question")
    if set(result["natural"]) != set(result["extended"]):
        raise PilotError("context conditions have different question universes")
    for task_id, original in result["natural"].items():
        extended_task = result["extended"][task_id]
        if any(original[k] != extended_task[k] for k in ("prompt", "expected")):
            raise PilotError("matched context conditions change question or gold")
        project = lambda p: canonical({"title": p["title"], "sentences": p["sentences"]})
        before = Counter(project(p) for p in original["meta"]["paragraphs"])
        after = Counter(project(p) for p in extended_task["meta"]["paragraphs"])
        if before - after or sum(after.values()) <= sum(before.values()):
            raise PilotError("extended context must retain every original passage and add distractors")
    return result


def schedule_for(ids: list[str], excluded: set[str], calibration: int, holdout: int) -> list[dict[str, Any]]:
    eligible = sorted(set(ids) - excluded, key=lambda t: digest(["frontier-v2-question-order-20260904", t]))
    if len(ids) != len(set(ids)) or calibration < 1 or holdout < 1 or len(eligible) < 3 + calibration + holdout:
        raise PilotError("not enough unique unexposed questions")
    schedule = []
    for i, task_id in enumerate(eligible[:3 + calibration + holdout]):
        phase = "warmup" if i < 3 else "calibration" if i < 3 + calibration else "holdout"
        block = [{"task_id": task_id, "phase": phase, "context": context, "model": model,
                  "provider_tag": tag, "arm": arm}
                 for context in CONTEXTS for model, tag in MODELS
                 for arm in (["full"] if phase == "warmup" else ["full", "compress"])]
        block.sort(key=lambda row: digest(["frontier-v2-cell-order", row]))
        schedule.extend(block)
    return schedule


def policy_specs() -> list[dict[str, Any]]:
    return [{"name": name + suffix, "settings": {**POLICY_SETTINGS,
             "AGENTC_ENABLED_RULES": rules, "AGENTC_OPTIMIZE_EXPLORATION_CALLS_PER_SITE_24H": str(cap)}}
            for name, rules in (("routing", "ModelDowngrade"), ("rewrite", "ContextCompress"),
                                ("joint", "ModelDowngrade,ContextCompress"))
            for suffix, cap in (("_default_budget", 20), ("_expanded_budget", 160))]


def prepare(args: argparse.Namespace, key: str) -> dict[str, Any]:
    if (args.output / "manifest.json").exists():
        raise PilotError("manifest already frozen; run to resume")
    previous = json.loads(args.previous.read_text())
    contract_manifest = json.loads((args.contract / "manifest.json").read_text())
    contract_rows = json.loads((args.contract / "results.json").read_text())
    contract_stage = "answer-contract-v1-" + digest(contract_manifest)[:20]
    if (contract_manifest.get("paper_evidence") is not False
            or contract_manifest.get("previous_manifest_sha256") != digest(previous)
            or len(contract_rows) != len(contract_manifest["schedule"])):
        raise PilotError("development artifact provenance differs")
    for index, row in enumerate(contract_rows):
        if (any(row.get(k) != v for k, v in contract_manifest["schedule"][index].items())
                or row.get("id") != contract_stage + f"-{index:04d}"):
            raise PilotError("development results differ from their frozen schedule")
    from bench.openrouter_contract import summarize
    contract_summary = summarize(contract_rows, contract_manifest)
    if contract_summary["completed_calls"] != 96 or contract_summary["scheduled_calls"] != 96:
        raise PilotError("development contract stage is incomplete")
    screened = [r for r in contract_summary["cells"] if r["contract"] == "reinforced" and r["max_tokens"] == CAP]
    if (len(screened) != len(MODELS) or any(r["n"] != 6 or r["truncated"] or r["max_answer_words"] > 20 for r in screened)):
        raise PilotError("development short-answer screen failed; do not scale")
    tasks = load_tasks(args.natural, args.extended)
    excluded = {r["task_id"] for r in previous["schedule"]}
    excluded.update(r["task_id"] for r in contract_rows)
    schedule = schedule_for(list(tasks["natural"]), excluded, args.calibration, args.holdout)
    observed = datetime.now(timezone.utc).isoformat()
    selected_endpoints = endpoints(key)
    version = "openrouter-frontier-v2-" + digest(selected_endpoints)[:20]
    catalog = json.loads(json.dumps(previous["catalog"]))
    catalog.update(catalog_version=version, price_table_version=version, observed_at_utc=observed)
    for target in catalog["targets"]:
        model = target["model_id"]
        e = selected_endpoints[model]
        url = "https://openrouter.ai/api/v1/models/" + model + "/endpoints"
        target.update(model_version=model + "@" + version + "/" + e["tag"],
                      max_output_tokens=CAP, context_window_tokens=min(65536, e["context_length"]))
        target["price"] = {"input_per_million_tokens_usd": float(money(e["pricing"]["prompt"]) * 1_000_000),
                           "output_per_million_tokens_usd": float(money(e["pricing"]["completion"]) * 1_000_000),
                           "table_version": version, "source_url": url, "observed_at_utc": observed}
        target["provenance"] = {"catalog_version": version, "source_url": url, "observed_at_utc": observed}
    manifest = {
        "schema_version": 2, "kind": "matched_context_factorial", "paper_evidence": False,
        "created_at": observed, "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_files": sources(), "native_sha256": file_hash(args.native),
        "fixtures": {"natural": file_hash(args.natural), "extended": file_hash(args.extended)},
        "previous_manifest_sha256": digest(previous), "contract_manifest_sha256": digest(contract_manifest),
        "contract_results_sha256": digest(contract_rows), "excluded_question_ids": sorted(excluded),
        "contract": "reinforced", "max_tokens": CAP, "epsilon": 0.15, "pythonhashseed": "0",
        "warmup_tasks": 3, "calibration_tasks": args.calibration, "holdout_tasks": args.holdout,
        "schedule": schedule, "settings": SETTINGS, "catalog": catalog, "endpoints": selected_endpoints,
        "stage_cap_usd": "20", "primary_quality_metric": "normalized_token_f1", "secondary_quality_metric": "normalized_exact_match",
        "policy_replay": {"source_model": SOURCE_MODEL, "specs": policy_specs(), "shadow_seed": "frontier-shadow-v1",
            "divergence": "production_lexical_jaccard", "feedback": "selected_primary_then_leased_candidate_or_sampled_reference_only",
            "static_selection": "per_context_cheapest_calibration_mean_nominal_uncached_cost_with_mean_F1_at_least_reference_minus_0.02",
            "static_tie_break": "cost_then_model_then_arm", "risk_margin": 0.02,
            "cost_accounting": ["actual_acquisition_billed", "replay_observed_billed_noncausal", "replay_nominal_uncached_token_price_estimate"]},
        "limitations": ["One public QA dataset, not multiple agent workloads or paper evidence.",
            "Holdout is disjoint from this campaign development; historical repository exposure is not ruled out.",
            "Both context conditions share question identities; statistical resampling must cluster by question.",
            "Official-style EM/F1 are imperfect semantic proxies; do not edit gold labels or accept substrings.",
            "Shared-host wall latency is diagnostic only.",
            "Implicit provider caches are not disabled; record cached tokens and separate nominal uncached cost estimates.",
            "Off-policy cache warming and synchronous feedback prevent a causal live-policy cost or latency claim.",
            "Natural-context compression may abstain; identical-payload repeated calls measure provider variability, not a rewrite effect.",
            "Offline replay uses only selected outcomes, never heldout gold or unsampled counterfactual outputs.",
            "Runtime clock is current wall time; no historical drift or detection-delay claim."]}
    # Reject oversized reference requests before freezing/charging any inference.
    for context in CONTEXTS:
        for task_id in {r["task_id"] for r in schedule}:
            make_request(MODELS[0][0], [MODELS[0][1]], contract_messages(tasks[context][task_id], "reinforced"), max_tokens=CAP)
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {"scheduled_calls": len(schedule), "manifest_sha256": digest(manifest), "stage_cap_usd": "20"}


def make_call(task: dict[str, Any], item: dict[str, Any], attention: Any, *, source_model: str | None = None) -> dict[str, Any]:
    """Same provider-visible request for acquisition and policy replay."""
    call = native_call(task, item, attention)
    call["model"] = source_model or item["model"]
    call["call_site_id"] = "openrouter-frontier-v2/" + item["context"] + "/" + call["model"]
    call["messages"] = contract_messages(task, "reinforced")
    scores, follow = attention.compute_attention_scores(call["messages"], None)
    call["parameters"]["max_output_tokens"] = CAP
    extra = call["parameters"]["extra"]
    extra.update(attention_scores=scores if item["arm"] == "compress" else [], follow_on_tokens=follow)
    extra["agentc_route_context"]["input_tokens_upper_bound"] = len(canonical(call["messages"])) + 1024 + 64 * len(call["messages"])
    return call


def outcome(row: dict[str, Any], call_site_id: str, *, nominal_cost: bool = False) -> dict[str, Any]:
    return {"input_tokens": row["usage"]["prompt_tokens"], "output_tokens": row["usage"]["completion_tokens"],
            "latency_ms": row["latency_ms"], "cost_usd": float(money(row["nominal_uncached_cost_usd"] if nominal_cost else row["cost_usd"])),
            "call_site_id": call_site_id, "executed_model_id": row["model"],
            "output_is_structured": False, "output_is_short": row["usage"]["completion_tokens"] <= 128}


def summarize(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["phase"], row["context"], row["model"], row["arm"])].append(row)
    aggregates = []
    for (phase, context, model, arm), values in sorted(groups.items()):
        aggregates.append({"phase": phase, "context": context, "model": model, "arm": arm, "n": len(values),
            "em": sum(r["em"] for r in values) / len(values), "f1": sum(r["f1"] for r in values) / len(values),
            "cost_usd": str(sum((money(r["cost_usd"]) for r in values), Decimal(0))),
            "nominal_uncached_cost_usd": str(sum((money(r["nominal_uncached_cost_usd"]) for r in values), Decimal(0))),
            "input_tokens": sum(r["usage"]["prompt_tokens"] for r in values),
            "cached_input_tokens": sum(r["cached_input_tokens"] or 0 for r in values),
            "cache_accounting_missing_calls": sum(r["cached_input_tokens"] is None for r in values),
            "output_tokens": sum(r["usage"]["completion_tokens"] for r in values),
            "rewritten": sum(r["native_plan"]["kind"] == "rewritten" for r in values),
            "truncated": sum(r["finish_reason"] == "length" for r in values),
            "answers_over_20_words": sum(len(r["answer"].split()) > 20 for r in values)})
    return {"paper_evidence": False, "manifest_sha256": digest(manifest), "results_sha256": digest(rows),
            "completed_calls": len(rows), "scheduled_calls": len(manifest["schedule"]),
            "cost_usd": str(sum((money(r["cost_usd"]) for r in rows), Decimal(0))),
            "aggregates": aggregates, "limitations": manifest["limitations"]}


def run(args: argparse.Namespace, key: str) -> dict[str, Any]:
    manifest = json.loads((args.output / "manifest.json").read_text())
    if (manifest["source_files"] != sources() or manifest["native_sha256"] != file_hash(args.native)
            or manifest["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}
            or os.environ.get("PYTHONHASHSEED") != manifest["pythonhashseed"]):
        raise PilotError("frozen source, native, fixture, or hash seed changed")
    tasks = load_tasks(args.natural, args.extended)
    stage = "frontier-v2-" + digest(manifest)[:20]
    path = args.output / "results.json"
    existing = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(existing, list) or len(existing) > len(manifest["schedule"]):
        raise PilotError("invalid saved result count")
    for index, row in enumerate(existing):
        if (any(row.get(k) != v for k, v in manifest["schedule"][index].items())
                or row.get("id") != stage + f"-{index:05d}" or row.get("stage") != stage):
            raise PilotError("saved results differ from schedule")
    native = load_module("_native", args.native, native=True)
    attention = load_module("frontier_attention", ROOT / "python/agentc/_attention.py")
    saved_env = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    for k in saved_env:
        os.environ.pop(k)
    os.environ.update(manifest["settings"])
    ledger, rows = Ledger(args.ledger, key), []
    try:
        with ExitStack() as stack:
            storage = stack.enter_context(tempfile.TemporaryDirectory(prefix="agentc-frontier-v2-"))
            stack.callback(native.optimize_reset)
            native.optimize_configure(storage, catalog_json=json.dumps(manifest["catalog"]))
            if json.loads(native.optimize_model_catalog()) != manifest["catalog"]:
                raise PilotError("native catalog does not match frozen catalog")
            for index, item in enumerate(manifest["schedule"]):
                if args.max_calls is not None and index >= args.max_calls:
                    break
                task = tasks[item["context"]][item["task_id"]]
                call = make_call(task, item, attention)
                plan = json.loads(native.optimize_plan(json.dumps(call)))
                if plan.get("kind") not in {"rewritten", "pass_through"} or not plan.get("agentc_observation_context"):
                    raise PilotError("native plan cannot be attributed")
                selected = plan.get("call", call)
                if selected["model"] != item["model"] or (item["arm"] == "full" and selected["messages"] != call["messages"]):
                    raise PilotError("factorial arm changed model or full reference messages")
                if selected["messages"][0] != call["messages"][0] or selected["messages"][-1] != call["messages"][-1]:
                    raise PilotError("rewrite changed protected instruction or question")
                payload = make_request(item["model"], [item["provider_tag"]], selected["messages"], max_tokens=CAP)
                e = manifest["endpoints"][item["model"]]
                metadata = {**item, "manifest_sha256": digest(manifest), "dispatch_contract": {
                    "provider_name": e["provider_name"], "endpoint_model": e["name"].split(" | ", 1)[1]}}
                result = ledger.call(key, stage + f"-{index:05d}", stage, money(manifest["stage_cap_usd"]), payload, metadata)
                nominal = money(e["pricing"]["prompt"]) * result["usage"]["prompt_tokens"] + money(e["pricing"]["completion"]) * result["usage"]["completion_tokens"]
                cache_tokens = result["usage"].get("prompt_tokens_details", {}).get("cached_tokens")
                if cache_tokens is not None and (type(cache_tokens) is not int or not 0 <= cache_tokens <= result["usage"]["prompt_tokens"]):
                    raise PilotError("invalid cached-input accounting")
                row = {**item, **{k: v for k, v in result.items() if k not in {"key_id", "metadata", "at"}},
                    "request_sha256": digest(payload), "expected": task["expected"], **score(result["answer"], task["expected"]),
                    "native_plan": plan, "nominal_uncached_cost_usd": str(nominal), "cached_input_tokens": cache_tokens,
                    "original_message_count": len(call["messages"]), "selected_message_count": len(selected["messages"])}
                token = native.optimize_observe(json.dumps(plan), json.dumps(outcome(row, call["call_site_id"])))
                if not token:
                    raise PilotError("native outcome lacks plan attribution")
                if index < len(existing):
                    row = preserve_replayed_row(existing[index], row)
                rows.append(row)
                if len(rows) > len(existing):
                    write_json(path, rows)
                if len(rows) >= len(existing):
                    write_json(args.output / "summary.json", summarize(rows, manifest))
                print(json.dumps({"completed": len(rows), "total": len(manifest["schedule"]), "phase": item["phase"],
                    "context": item["context"], "model": item["model"], "arm": item["arm"], "plan": plan["kind"],
                    "em": row["em"], "f1": row["f1"], "cost_usd": row["cost_usd"]}), flush=True)
    finally:
        native.optimize_reset()
        for k in list(os.environ):
            if k.startswith("AGENTC_"):
                os.environ.pop(k)
        os.environ.update(saved_env)
    return {**summarize(rows, manifest), "ledger": ledger.summary()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    for name in ("env-file", "ledger", "natural", "extended", "native", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--previous", type=Path, default=ROOT / "bench/repro/openrouter-pilot-2026-09-04/manifest.json")
    parser.add_argument("--contract", type=Path, default=ROOT / "bench/repro/openrouter-contract-2026-09-04")
    parser.add_argument("--calibration", type=int, default=20)
    parser.add_argument("--holdout", type=int, default=160)
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    try:
        key = load_key(args.env_file)
        result = prepare(args, key) if args.command == "prepare" else run(args, key)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Frontier experiment stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
