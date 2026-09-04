"""Frozen development-only answer-contract experiment; no native optimization.

Compare instruction placement and output caps before defining a new held-out
protocol. All questions come from the already-exposed v1 calibration split.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench.openrouter_matrix import MODELS, ROOT, file_hash, messages_for, score, write_json
from bench.openrouter_pilot import Ledger, PilotError, digest, load_key, make_request, money, request_json

PREFIX = ("Answer format: Return only the shortest answer phrase or entity name. "
          "Do not explain, restate the question, add a sentence, or use Markdown. "
          "If the passages do not provide the answer, return unknown.\n")


def messages(task: dict[str, Any], contract: str) -> list[dict[str, str]]:
    result = messages_for(task)
    if contract == "reinforced":
        result[-1]["content"] = PREFIX + result[-1]["content"]
    elif contract != "legacy":
        raise PilotError("unknown answer contract")
    return result


def sources() -> dict[str, str]:
    paths = ["bench/openrouter_contract.py", "bench/openrouter_matrix.py", "bench/openrouter_pilot.py"]
    return {p: file_hash(ROOT / p) for p in paths}


def endpoints(key: str) -> dict[str, Any]:
    result = {}
    for model, tag in MODELS:
        available = request_json("/models/" + model + "/endpoints", key)["data"]["endpoints"]
        selected = [e for e in available if e["tag"] == tag]
        if len(selected) != 1:
            raise PilotError("provider endpoint not uniquely available")
        e = selected[0]
        if not {"max_tokens", "temperature"}.issubset(e["supported_parameters"]):
            raise PilotError("endpoint lacks controlled parameters")
        if money(e["pricing"]["prompt"]) > Decimal("0.000006") or money(e["pricing"]["completion"]) > Decimal("0.000030"):
            raise PilotError("endpoint exceeds frozen price ceilings")
        result[model] = e
    return result


def prepare(args: argparse.Namespace, key: str) -> dict[str, Any]:
    if (args.output / "manifest.json").exists():
        raise PilotError("manifest already frozen; run to resume")
    previous = json.loads(args.previous.read_text())
    development = sorted({r["task_id"] for r in previous["schedule"] if r["phase"] == "calibration"},
                         key=lambda t: digest(["answer-contract-development-v1", t]))[:6]
    tasks = {t["task_id"]: t for t in json.loads(args.fixture.read_text())}
    if len(development) != 6 or not set(development).issubset(tasks):
        raise PilotError("need six already-exposed calibration questions")
    schedule = []
    for task_id in development:
        block = [{"task_id": task_id, "model": model, "provider_tag": tag,
                  "contract": contract, "max_tokens": cap}
                 for model, tag in MODELS for contract in ("legacy", "reinforced") for cap in (128, 512)]
        block.sort(key=lambda r: digest(["contract-order-v1", r]))
        schedule.extend(block)
    manifest = {"schema_version": 1, "kind": "development_answer_contract", "paper_evidence": False,
                "created_at": datetime.now(timezone.utc).isoformat(), "source_files": sources(),
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "fixture_sha256": file_hash(args.fixture), "previous_manifest_sha256": digest(previous),
                "stage_cap_usd": "3", "schedule": schedule, "endpoints": endpoints(key),
                "reinforcement_prefix": PREFIX, "scoring": "unchanged_normalized_raw_answer_EM_F1",
                "limitations": ["Development questions only; no confirmatory claim.",
                    "Six questions per cell and one sample per prompt; format suitability screen, not model ranking.",
                    "No rewriting or native planner in this isolated prompt-control experiment.",
                    "Latency is diagnostic only on the shared host."]}
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {"scheduled_calls": len(schedule), "stage_cap_usd": "3", "manifest_sha256": digest(manifest)}


def summarize(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["model"], row["contract"], row["max_tokens"])].append(row)
    cells = []
    for (model, contract, cap), values in sorted(groups.items()):
        cells.append({"model": model, "contract": contract, "max_tokens": cap, "n": len(values),
                      "exact_matches": sum(int(r["em"]) for r in values),
                      "mean_f1": sum(r["f1"] for r in values) / len(values),
                      "truncated": sum(r["finish_reason"] == "length" for r in values),
                      "max_answer_words": max(len(r["answer"].split()) for r in values),
                      "output_tokens": sum(r["usage"]["completion_tokens"] for r in values),
                      "cost_usd": str(sum((money(r["cost_usd"]) for r in values), Decimal(0)))})
    return {"paper_evidence": False, "manifest_sha256": digest(manifest), "results_sha256": digest(rows),
            "completed_calls": len(rows), "scheduled_calls": len(manifest["schedule"]), "cells": cells,
            "cost_usd": str(sum((money(r["cost_usd"]) for r in rows), Decimal(0))),
            "limitations": manifest["limitations"]}


def run(args: argparse.Namespace, key: str) -> dict[str, Any]:
    manifest = json.loads((args.output / "manifest.json").read_text())
    if manifest["source_files"] != sources() or manifest["fixture_sha256"] != file_hash(args.fixture):
        raise PilotError("frozen source or dataset changed")
    tasks = {t["task_id"]: t for t in json.loads(args.fixture.read_text())}
    stage = "answer-contract-v1-" + digest(manifest)[:20]
    ledger, rows = Ledger(args.ledger, key), []
    existing_path = args.output / "results.json"
    existing = json.loads(existing_path.read_text()) if existing_path.exists() else []
    if not isinstance(existing, list) or len(existing) > len(manifest["schedule"]):
        raise PilotError("invalid saved result length")
    for index, row in enumerate(existing):
        item = manifest["schedule"][index]
        if any(row.get(k) != v for k, v in item.items()) or row.get("id") != stage + f"-{index:04d}":
            raise PilotError("saved results differ from frozen schedule")
    for index, item in enumerate(manifest["schedule"]):
        if args.max_calls is not None and index >= args.max_calls:
            break
        task = tasks[item["task_id"]]
        payload = make_request(item["model"], [item["provider_tag"]], messages(task, item["contract"]), max_tokens=item["max_tokens"])
        endpoint = manifest["endpoints"][item["model"]]
        metadata = {**item, "manifest_sha256": digest(manifest), "dispatch_contract": {
            "provider_name": endpoint["provider_name"], "endpoint_model": endpoint["name"].split(" | ", 1)[1]}}
        result = ledger.call(key, stage + f"-{index:04d}", stage, money(manifest["stage_cap_usd"]), payload, metadata)
        row = {**item, **{k: v for k, v in result.items() if k not in {"key_id", "metadata", "at"}},
               "request_sha256": digest(payload), "expected": task["expected"], **score(result["answer"], task["expected"])}
        if index < len(existing):
            if existing[index] != row:
                raise PilotError("replay differs from saved evidence")
            row = existing[index]
        rows.append(row)
        if len(rows) > len(existing):
            write_json(existing_path, rows)
        if len(rows) >= len(existing):
            write_json(args.output / "summary.json", summarize(rows, manifest))
        print(json.dumps({"completed": len(rows), "total": len(manifest["schedule"]),
                          "model": item["model"], "contract": item["contract"], "max_tokens": item["max_tokens"],
                          "em": row["em"], "truncated": row["finish_reason"] == "length"}), flush=True)
    return {**summarize(rows, manifest), "ledger": ledger.summary()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    for name in ("env-file", "ledger", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--previous", type=Path, default=ROOT / "bench/repro/openrouter-pilot-2026-09-04/manifest.json")
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    try:
        key = load_key(args.env_file)
        report = prepare(args, key) if args.command == "prepare" else run(args, key)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Contract experiment stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
