"""Bounded default OpenRouter Auto service baseline, with no native optimizer.

The allowed pool is an upper bound: the provider's current default cost band
and sticky-session behavior remain part of this named service baseline.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bench.openrouter_contract import endpoints, messages
from bench.openrouter_frontier import CAP, ROOT, SOURCE_MODEL, load_tasks
from bench.openrouter_frontier_analysis import total_cost
from bench.openrouter_matrix import file_hash, score, write_json
from bench.openrouter_pilot import Ledger, PilotError, digest, load_key, make_request, money


def sources():
    return {p: file_hash(ROOT / p) for p in ("bench/openrouter_auto.py", "bench/openrouter_pilot.py",
        "bench/openrouter_matrix.py", "bench/openrouter_contract.py", "bench/openrouter_frontier.py",
        "bench/openrouter_frontier_analysis.py")}


def request_for(manifest, task):
    return make_request("openrouter/auto", manifest["provider_only"], messages(task, manifest["contract"]),
                        max_tokens=CAP, allowed_models=manifest["allowed_models"])


def validate_dispatch(result, manifest):
    model = result["model"]
    if model not in manifest["endpoints"]:
        raise PilotError("Auto returned a model outside the bounded pool")
    endpoint = manifest["endpoints"][model]
    router = result.get("router_metadata") or {}
    selected = [e for e in router.get("endpoints", {}).get("available", []) if e.get("selected") is True]
    if (result["provider"] != endpoint["provider_name"] or len(selected) != 1
            or selected[0].get("provider") != endpoint["provider_name"]
            or selected[0].get("model") != endpoint["name"].split(" | ", 1)[1]
            or router.get("requested") != "openrouter/auto" or router.get("strategy") != "auto"
            or router.get("attempt") != 1 or router.get("is_byok") is not False):
        raise PilotError("Auto response failed selected endpoint/router attribution")
    if result["usage"].get("is_byok") or money(result["usage"]["cost"]) != money(result["cost_usd"]):
        raise PilotError("Auto response has inconsistent billing")
    return endpoint


def reported_service_tier(ledger, call_id):
    with ledger.locked() as handle:
        raw = [e["response"] for e in ledger.read(handle) if e["event"] == "response" and e["id"] == call_id]
    if len(raw) != 1:
        raise PilotError("Auto lacks exactly one durable raw response")
    tier = raw[0].get("service_tier")
    if tier not in (None, "default"):
        raise PilotError("Auto reported a non-default service tier")
    return tier


def prepare(args, key):
    if (args.output / "manifest.json").exists():
        raise PilotError("Auto manifest already frozen")
    frontier = json.loads((args.frontier / "manifest.json").read_text())
    tasks = load_tasks(args.natural, args.extended)
    if frontier["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}:
        raise PilotError("Auto fixtures differ from frontier")
    current = endpoints(key)
    if set(current) != set(frontier["endpoints"]):
        raise PilotError("Auto model pool differs from frontier")
    for model, endpoint in current.items():
        old = frontier["endpoints"][model]
        if any(endpoint[k] != old[k] for k in ("tag", "provider_name", "name", "pricing")):
            raise PilotError("Auto endpoint identity or pricing changed; require a separate protocol")
    schedule = [{k: row[k] for k in ("task_id", "phase", "context")}
                for row in frontier["schedule"] if row["model"] == SOURCE_MODEL and row["arm"] == "full"]
    manifest = {"paper_evidence": False, "kind": "bounded_default_auto_service", "created_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_files": sources(), "frontier_manifest_sha256": digest(frontier), "fixtures": frontier["fixtures"],
        "stage_cap_usd": "5", "contract": frontier["contract"], "endpoints": current,
        "allowed_models": sorted(current), "provider_only": sorted({e["tag"] for e in current.values()}),
        "schedule": schedule, "scoring": {"primary": frontier["primary_quality_metric"],
                                           "secondary": frontier["secondary_quality_metric"]}, "optimizer": "none",
        "cost_setting": "service_default_omitted", "session_id": "omitted_service_inferred_stickiness",
        "limitations": frontier["limitations"] + [
            "Auto is a bounded default-service baseline, not an optimizer over all four pool members.",
            "Default cost-band filtering and inferred session stickiness remain enabled; selected models may be a strict subset.",
            "The allowed model/provider lists constrain dispatch; response metadata does not independently prove quantization tags.",
            "Separate acquisition time and provider-cache histories prevent a causal router latency or cost comparison.",
            "No model or prompt is selected using this baseline's heldout outcomes.",
            "Public service routing behavior can change; the returned model and metadata are recorded on every call."]}
    for row in schedule:
        request_for(manifest, tasks[row["context"]][row["task_id"]])
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {"scheduled_calls": len(schedule), "stage_cap_usd": "5", "manifest_sha256": digest(manifest)}


def summarize(rows, manifest):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["phase"], row["context"])].append(row)
    return {"paper_evidence": False, "manifest_sha256": digest(manifest), "results_sha256": digest(rows),
        "completed_calls": len(rows), "scheduled_calls": len(manifest["schedule"]),
        "cost_usd": str(total_cost(rows, "cost_usd")), "aggregates": [
            {"phase": phase, "context": context, "n": len(group),
             "f1": sum(r["f1"] for r in group) / len(group), "em": sum(r["em"] for r in group) / len(group),
             "selected_models": dict(Counter(r["model"] for r in group)),
             "cost_usd": str(total_cost(group, "cost_usd")),
             "nominal_uncached_cost_usd": str(total_cost(group, "nominal_uncached_cost_usd")),
             "truncated": sum(r["finish_reason"] == "length" for r in group),
             "answers_over_20_words": sum(len(r["answer"].split()) > 20 for r in group),
             "cached_input_tokens": sum(r["cached_input_tokens"] or 0 for r in group),
             "cache_accounting_missing_calls": sum(r["cached_input_tokens"] is None for r in group)}
            for (phase, context), group in sorted(groups.items())], "limitations": manifest["limitations"]}


def run(args, key):
    manifest = json.loads((args.output / "manifest.json").read_text())
    frontier = json.loads((args.frontier / "manifest.json").read_text())
    if (manifest["source_files"] != sources() or manifest["frontier_manifest_sha256"] != digest(frontier)
            or manifest["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}):
        raise PilotError("Auto frozen source or fixture changed")
    tasks = load_tasks(args.natural, args.extended)
    ledger = Ledger(args.ledger, key)
    stage = "auto-default-v1-" + digest(manifest)[:20]
    path = args.output / "results.json"
    existing = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(existing, list) or len(existing) > len(manifest["schedule"]):
        raise PilotError("invalid saved Auto result count")
    rows = []
    for i, item in enumerate(manifest["schedule"]):
        if args.max_calls is not None and i >= args.max_calls:
            break
        task = tasks[item["context"]][item["task_id"]]
        payload = request_for(manifest, task)
        result = ledger.call(key, stage + f"-{i:05d}", stage, money(manifest["stage_cap_usd"]), payload,
                             {**item, "manifest_sha256": digest(manifest), "purpose": "bounded_default_auto_service"})
        e = validate_dispatch(result, manifest)
        tier = reported_service_tier(ledger, result["id"])
        usage = result["usage"]
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens")
        if cached is not None and (type(cached) is not int or not 0 <= cached <= usage["prompt_tokens"]):
            raise PilotError("invalid Auto cached-token accounting")
        row = {**item, **{k: v for k, v in result.items() if k not in {"at", "key_id", "metadata"}},
            "requested_model": "openrouter/auto", "arm": "full", "optimizer": "none", "provider_tag_requested_for_model": e["tag"],
            "request_sha256": digest(payload), "expected": task["expected"], **score(result["answer"], task["expected"]),
            "service_tier_reported": tier,
            "nominal_uncached_cost_usd": str(money(e["pricing"]["prompt"]) * usage["prompt_tokens"] + money(e["pricing"]["completion"]) * usage["completion_tokens"]),
            "cached_input_tokens": cached}
        if i < len(existing) and row != existing[i]:
            raise PilotError("saved Auto outcome differs from immutable ledger")
        rows.append(row)
        if len(rows) > len(existing):
            write_json(path, rows)
        if len(rows) >= len(existing):
            write_json(args.output / "summary.json", summarize(rows, manifest))
        print(json.dumps({"completed": len(rows), "total": len(manifest["schedule"]), "phase": item["phase"],
                          "context": item["context"], "selected_model": row["model"], "cost_usd": row["cost_usd"]}), flush=True)
    return {**summarize(rows, manifest), "ledger": ledger.summary()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    for name in ("env-file", "ledger", "frontier", "natural", "extended", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    try:
        key = load_key(args.env_file)
        print(json.dumps(prepare(args, key) if args.command == "prepare" else run(args, key), indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Auto baseline stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
