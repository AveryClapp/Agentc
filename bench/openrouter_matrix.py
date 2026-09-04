"""Frozen exploratory model x ContextCompress matrix, with real paid warmup.

This characterizes the opportunity for joint planning. It is NOT the learned
joint policy: current_greedy proposes ContextCompress without quality admission.
No synthetic observations, test labels, or reference answers enter the planner.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import statistics
import string
import subprocess
import tempfile
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench.openrouter_pilot import (
    Ledger, PilotError, canonical, digest, load_key, make_request, money, request_json,
)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "openrouter.chat.completions.v1"
MODELS = [
    ("anthropic/claude-sonnet-4.5", "anthropic"),
    ("anthropic/claude-haiku-4.5", "anthropic"),
    ("google/gemini-2.5-flash-lite", "google-ai-studio"),
    ("qwen/qwen3-30b-a3b-instruct-2507", "nebius/fp8"),
]
SETTINGS = {
    "AGENTC_ENABLED_RULES": "ContextCompress",
    "AGENTC_EVAL_PLANNER_MODE": "current_greedy",
    "AGENTC_OPTIMIZE": "1",
    "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3",
    "AGENTC_OPTIMIZE_EXPLORATION": "0",
    "AGENTC_OPTIMIZE_SHADOW": "0",
    "AGENTC_COMPOSE": "0",
    "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "100",
}
SYSTEM = ("Answer the question using the supplied passages. Respond with only the "
          "short answer, without explanation. If the answer is unavailable, say unknown.")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any, *, immutable: bool = False) -> None:
    data = canonical(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable:
        if path.exists():
            if path.read_bytes() != data:
                raise PilotError("refusing to change a frozen experiment artifact")
        else:
            with path.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        return
    temporary = path.with_suffix(path.suffix + ".next")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_module(name: str, path: Path, *, native: bool = False) -> Any:
    loader = importlib.machinery.ExtensionFileLoader(name, str(path)) if native else None
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    if spec is None or spec.loader is None:
        raise PilotError("cannot load experiment module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize(answer: str) -> str:
    answer = answer.lower().translate(str.maketrans("", "", string.punctuation))
    return " ".join(re.sub(r"\b(a|an|the)\b", " ", answer).split())


def score(answer: str, expected: str) -> dict[str, float]:
    predicted, gold = normalize(answer), normalize(expected)
    if predicted in {"yes", "no", "noanswer"} or gold in {"yes", "no", "noanswer"}:
        return {"em": float(predicted == gold), "f1": float(predicted == gold)}
    common = sum((Counter(predicted.split()) & Counter(gold.split())).values())
    f1 = (2 * common / (len(predicted.split()) + len(gold.split()))
          if common else float(predicted == gold))
    return {"em": float(predicted == gold), "f1": f1}


def messages_for(task: dict[str, Any]) -> list[dict[str, str]]:
    return ([{"role": "system", "content": SYSTEM}] +
            [{"role": "user", "content": p["title"] + "\n" + " ".join(p["sentences"])}
             for p in task["meta"]["paragraphs"]] +
            [{"role": "user", "content": "Question: " + task["prompt"]}])


def make_schedule(tasks: list[dict[str, Any]], count: int, calibration: int) -> list[dict[str, Any]]:
    if count < 1 or calibration < 1 or len(tasks) < count + calibration + 3:
        raise PilotError("insufficient tasks for disjoint warmup/calibration/holdout")
    if len({task["task_id"] for task in tasks}) != len(tasks):
        raise PilotError("fixture contains duplicate task identities")
    ordered = sorted(tasks, key=lambda t: digest(["openrouter-matrix-seed-20260904", t["task_id"]]))
    schedule = []
    for index, task in enumerate(ordered[:3 + calibration + count]):
        phase = "warmup" if index < 3 else "calibration" if index < 3 + calibration else "holdout"
        arms = ["full"] if phase == "warmup" else ["full", "compress"]
        block = [{"task_id": task["task_id"], "phase": phase, "model": model,
                  "provider_tag": tag, "arm": arm}
                 for model, tag in MODELS for arm in arms]
        # Counterbalance model/arm order independently of labels and outcomes.
        block.sort(key=lambda row: digest(["order-v1", index, row["model"], row["arm"]]))
        schedule.extend(block)
    return schedule


def source_hashes() -> dict[str, str]:
    paths = [ROOT / "Cargo.toml", ROOT / "Cargo.lock", Path(__file__),
             ROOT / "bench/openrouter_pilot.py", ROOT / "python/agentc/_attention.py"]
    paths += sorted((ROOT / "crates").rglob("*.rs"))
    paths += sorted((ROOT / "crates").rglob("Cargo.toml"))
    return {str(path.relative_to(ROOT)): file_hash(path) for path in paths}


def prepare(args: argparse.Namespace, key: str) -> dict[str, Any]:
    if (args.output / "manifest.json").exists():
        raise PilotError("manifest already exists; use run to resume")
    tasks = json.loads(args.fixture.read_text())
    schedule = make_schedule(tasks, args.holdout, args.calibration)
    endpoints = {}
    for model, tag in MODELS:
        data = request_json("/models/" + model + "/endpoints", key)["data"]
        selected = [e for e in data["endpoints"] if e["tag"] == tag]
        if len(selected) != 1:
            raise PilotError("frozen provider endpoint not uniquely available")
        endpoint = selected[0]
        if not {"max_tokens", "temperature"}.issubset(endpoint["supported_parameters"]):
            raise PilotError("endpoint cannot honor controlled generation parameters")
        for side, cap in [("prompt", 6), ("completion", 30)]:
            if money(endpoint["pricing"][side]) * 1_000_000 > cap:
                raise PilotError("endpoint price exceeds pilot ceiling")
        endpoints[model] = endpoint
    observed = datetime.now(timezone.utc).isoformat()
    version = "openrouter-matrix-v1-" + digest(endpoints)[:20]
    targets = []
    for model, tag in MODELS:
        endpoint = endpoints[model]
        url = "https://openrouter.ai/api/v1/models/" + model + "/endpoints"
        targets.append({
            "adapter_protocol": PROTOCOL, "provider_namespace": "openrouter",
            "model_id": model, "model_version": model + "@" + version + "/" + tag,
            "revision_kind": "catalog_observation", "aliases": [],
            "routing_group": "openrouter-text-pilot", "context_window_tokens": min(65536, endpoint["context_length"]),
            "max_output_tokens": 128, "output_token_parameter": "max_tokens",
            "capabilities": {"text_input": True, "image_input": False, "tool_calling": False,
                             "structured_outputs": False, "streaming": False},
            "price": {"input_per_million_tokens_usd": float(money(endpoint["pricing"]["prompt"]) * 1_000_000),
                      "output_per_million_tokens_usd": float(money(endpoint["pricing"]["completion"]) * 1_000_000),
                      "table_version": version, "source_url": url, "observed_at_utc": observed},
            "provenance": {"catalog_version": version, "source_url": url, "observed_at_utc": observed},
        })
    manifest = {
        "schema_version": 1, "paper_evidence": False, "kind": "exploratory_factorial",
        "created_at": observed, "stage_cap_usd": "5", "native_sha256": file_hash(args.native),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_files": source_hashes(), "fixture_sha256": file_hash(args.fixture),
        "settings": SETTINGS, "max_tokens": 128, "epsilon": 0.15,
        "warmup_tasks": 3, "calibration_tasks": args.calibration, "holdout_tasks": args.holdout,
        "schedule": schedule, "endpoints": endpoints,
        "catalog": {"catalog_version": version, "price_table_version": version,
                    "observed_at_utc": observed, "targets": targets},
        "limitations": ["Single workload and repetition; exploratory, not paper evidence.",
                        "Current-greedy rewrite characterization, not learned joint-policy performance.",
                        "Host contention contaminates wall-clock latency; no latency claim.",
                        "Gateway aliases are observed cohorts, not immutable serving binaries.",
                        "No reference sampling or quality admission; all warmup and matrix calls are billed.",
                        "Normalized answer EM/F1; no substring-tolerant acceptance."],
    }
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {"scheduled_calls": len(schedule), "manifest_sha256": digest(manifest),
            "paid_calls": 0, "stage_cap_usd": "5"}


def native_call(task: dict[str, Any], item: dict[str, Any], attention: Any) -> dict[str, Any]:
    messages = messages_for(task)
    scores, follow = attention.compute_attention_scores(messages, None)
    deps = [{"kind": "literal"} for _ in messages]
    deps[-1] = {"kind": "user_input", "span_id": "00" * 8}
    return {
        "call_site_id": "openrouter-matrix-v1/qa/" + item["model"],
        "trace_id": digest(task["task_id"])[:32], "span_id": digest(item)[:16],
        "model": item["model"], "messages": messages,
        "parameters": {"max_output_tokens": 128, "temperature": 0.0, "extra": {
            # Missing attention makes the full reference ineligible for CC;
            # provider-visible messages/parameters remain exactly identical.
            "attention_scores": scores if item["arm"] == "compress" else [],
            "follow_on_tokens": follow, "dead_attention_epsilon": 0.15, "message_deps": deps,
            "agentc_route_context": {"provider_protocol": PROTOCOL, "provider_namespace": "openrouter",
                "input_tokens_upper_bound": len(canonical(messages)) + 1024 + 64 * len(messages),
                "input_tokens_upper_bound_basis": "json_utf8_bytes_v1", "image_input": False,
                "tool_calling": False, "structured_outputs": False, "streaming": False},
        }}, "tools": [], "input_deps": deps, "occurrence_ix": 0,
    }


def analyze(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["phase"], row["model"], row["arm"])].append(row)
    aggregates = []
    for (phase, model, arm), values in sorted(groups.items()):
        aggregates.append({"phase": phase, "model": model, "arm": arm, "n": len(values),
            "em": statistics.mean(v["em"] for v in values), "f1": statistics.mean(v["f1"] for v in values),
            "cost_usd": str(sum((money(v["cost_usd"]) for v in values), Decimal(0))),
            "input_tokens": sum(v["usage"]["prompt_tokens"] for v in values),
            "output_tokens": sum(v["usage"]["completion_tokens"] for v in values),
            "latency_median_ms": statistics.median(v["latency_ms"] for v in values),
            "rewritten": sum(v["native_plan"]["kind"] == "rewritten" for v in values),
            "truncated": sum(v["finish_reason"] == "length" for v in values)})
    return {"paper_evidence": False, "manifest_sha256": digest(manifest),
            "completed_calls": len(rows), "scheduled_calls": len(manifest["schedule"]),
            "cost_usd": str(sum((money(v["cost_usd"]) for v in rows), Decimal(0))),
            "aggregates": aggregates, "limitations": manifest["limitations"]}


def saved_rows(output: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate existing evidence before replay; never truncate it to a prefix."""
    path = output / "results.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or len(rows) > len(manifest["schedule"]):
        raise PilotError("saved results exceed the frozen schedule")
    stage = "matrix-v1-" + digest(manifest)[:20]
    for index, row in enumerate(rows):
        item = manifest["schedule"][index]
        if (not isinstance(row, dict) or any(row.get(k) != v for k, v in item.items())
                or row.get("stage") != stage or row.get("id") != stage + f"-{index:04d}"
                or row.get("paper_evidence") is not False
                or not row.get("native_plan", {}).get("agentc_observation_context")):
            raise PilotError("saved results do not match the frozen schedule")
    return rows


def preserve_replayed_row(saved: dict[str, Any], replayed: dict[str, Any]) -> dict[str, Any]:
    # Exact provider payloads are checked separately by the ledger fingerprint.
    # Compare every persisted result field plus plan identity. Diagnostic plan
    # fields can vary with execution timing; retain their original evidence.
    original = {k: v for k, v in saved.items() if k != "native_plan"}
    current = {k: v for k, v in replayed.items() if k != "native_plan"}
    if original != current or any(saved["native_plan"].get(k) != replayed["native_plan"].get(k)
                                  for k in ("kind", "agentc_observation_context")):
        raise PilotError("replay differs from saved result or plan identity; evidence preserved")
    return saved


def run(args: argparse.Namespace, key: str) -> dict[str, Any]:
    manifest = json.loads((args.output / "manifest.json").read_text())
    if (manifest["source_files"] != source_hashes() or manifest["native_sha256"] != file_hash(args.native)
            or manifest["fixture_sha256"] != file_hash(args.fixture)):
        raise PilotError("source, native build, or fixture changed after freezing")
    existing = saved_rows(args.output, manifest)
    tasks = {t["task_id"]: t for t in json.loads(args.fixture.read_text())}
    native = load_module("_native", args.native, native=True)
    attention = load_module("pilot_attention", ROOT / "python/agentc/_attention.py")
    ledger = Ledger(args.ledger, key)
    stage = "matrix-v1-" + digest(manifest)[:20]
    saved = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    for name in saved:
        os.environ.pop(name)
    os.environ.update(manifest["settings"])
    rows = []
    try:
        with ExitStack() as stack:
            storage = stack.enter_context(tempfile.TemporaryDirectory(prefix="agentc-openrouter-matrix-"))
            stack.callback(native.optimize_reset)
            native.optimize_configure(storage, catalog_json=json.dumps(manifest["catalog"]))
            if json.loads(native.optimize_model_catalog()) != manifest["catalog"]:
                raise PilotError("native catalog differs from the frozen snapshot")
            for index, item in enumerate(manifest["schedule"]):
                if args.max_calls is not None and index >= args.max_calls:
                    break
                task = tasks[item["task_id"]]
                call = native_call(task, item, attention)
                plan = json.loads(native.optimize_plan(json.dumps(call)))
                if not plan.get("agentc_observation_context"):
                    raise PilotError("native plan lacks exact-plan observation context")
                if plan["kind"] not in {"pass_through", "rewritten"}:
                    raise PilotError("unexpected native plan kind")
                selected = plan.get("call", call)
                if selected["model"] != item["model"]:
                    raise PilotError("factorial arm unexpectedly routed to another model")
                if item["arm"] == "full" and selected["messages"] != call["messages"]:
                    raise PilotError("reference arm was rewritten")
                payload = make_request(item["model"], [item["provider_tag"]], selected["messages"])
                endpoint = manifest["endpoints"][item["model"]]
                contract = {"provider_name": endpoint["provider_name"],
                            "endpoint_model": endpoint["name"].split(" | ", 1)[1]}
                result = ledger.call(key, stage + f"-{index:04d}", stage, money(manifest["stage_cap_usd"]),
                    payload, {**item, "manifest_sha256": digest(manifest), "dispatch_contract": contract})
                outcome = {"input_tokens": result["usage"]["prompt_tokens"],
                    "output_tokens": result["usage"]["completion_tokens"],
                    "latency_ms": result["latency_ms"], "cost_usd": float(money(result["cost_usd"])),
                    "call_site_id": call["call_site_id"], "output_is_structured": False,
                    "output_is_short": result["usage"]["completion_tokens"] <= 128}
                token = native.optimize_observe(json.dumps(plan), json.dumps(outcome))
                if not token:
                    raise PilotError("native observation lacks exact-plan attribution")
                row = {**item, **{k: v for k, v in result.items() if k not in {"key_id", "metadata"}},
                       **score(result["answer"], task["expected"]), "native_plan": plan,
                       "expected": task["expected"], "original_message_count": len(call["messages"]),
                       "selected_message_count": len(selected["messages"])}
                if index < len(existing):
                    row = preserve_replayed_row(existing[index], row)
                rows.append(row)
                if len(rows) > len(existing):
                    write_json(args.output / "results.json", rows)
                    write_json(args.output / "summary.json", analyze(rows, manifest))
                print(json.dumps({"completed": len(rows), "total": len(manifest["schedule"]),
                    "phase": item["phase"], "model": item["model"], "arm": item["arm"],
                    "plan": plan["kind"], "em": row["em"], "cost_usd": result["cost_usd"]}), flush=True)
            native.optimize_reset()
    finally:
        native.optimize_reset()
        for name in list(os.environ):
            if name.startswith("AGENTC_"):
                os.environ.pop(name)
        os.environ.update(saved)
    return {**analyze(rows, manifest), "ledger": ledger.summary()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calibration", type=int, default=8)
    parser.add_argument("--holdout", type=int, default=12)
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    try:
        key = load_key(args.env_file)
        result = prepare(args, key) if args.command == "prepare" else run(args, key)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError, ValueError) as exc:
        print(f"Matrix stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
