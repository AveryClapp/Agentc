"""Single-attempt, capped direct-Claude gateway diagnostic (not a policy test)."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

from bench.openrouter_contract import messages
from bench.openrouter_frontier import ROOT, load_tasks
from bench.openrouter_matrix import file_hash, score, write_json
from bench.openrouter_pilot import Ledger, NoRedirect, PilotError, canonical, digest, money, load_key as openrouter_key

MODELS = {
    "anthropic/claude-sonnet-4.5": {"snapshot": "claude-sonnet-4-5-20250929", "prompt": "0.000003", "completion": "0.000015"},
    "anthropic/claude-haiku-4.5": {"snapshot": "claude-haiku-4-5-20251001", "prompt": "0.000001", "completion": "0.000005"},
}
STAGE_CAP = Decimal("3")
CAP = 512
SOURCE_PATHS = ("bench/direct_anthropic.py", "bench/openrouter_contract.py", "bench/openrouter_frontier.py",
    "bench/openrouter_matrix.py", "bench/openrouter_pilot.py")


def load_key(path):
    values = []
    for line in path.read_text().splitlines():
        match = re.match(r"^\s*(?:export\s+)?ANTHROPIC_API_KEY\s*=\s*(.*?)\s*$", line)
        if match:
            value = match.group(1)
            if value.startswith(("'", '"')) and value[-1:] == value[:1]:
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].strip()
            values.append(value)
    if len(values) != 1 or not values[0] or any(c.isspace() for c in values[0]):
        raise PilotError("dotenv must contain one nonempty ANTHROPIC_API_KEY")
    return values[0]


def request_for(model, original):
    if model not in MODELS or not original or original[0].get("role") != "system":
        raise PilotError("direct diagnostic requires fixed model and leading system text")
    if any(set(m) != {"role", "content"} or not isinstance(m["content"], str) for m in original):
        raise PilotError("direct diagnostic requires plain text messages")
    if len(original) < 2 or any(m["role"] != "user" for m in original[1:]):
        raise PilotError("direct diagnostic requires user-only context/question after system")
    payload = {"model": MODELS[model]["snapshot"], "system": original[0]["content"],
        "messages": [{"role": "user", "content": [{"type": "text", "text": m["content"]}]} for m in original[1:]],
        "max_tokens": CAP, "temperature": 0, "stream": False, "service_tier": "standard_only", "thinking": {"type": "disabled"}}
    if len(canonical(payload)) > 65536:
        raise PilotError("direct request exceeds 64KiB diagnostic envelope")
    return payload


def validate_request(model, payload):
    try:
        blocks = payload["messages"]
        if not blocks or any(set(m) != {"role", "content"} or m["role"] != "user" or len(m["content"]) != 1 for m in blocks):
            raise PilotError("direct request shape changed")
        if any(set(m["content"][0]) != {"type", "text"} or m["content"][0]["type"] != "text" for m in blocks):
            raise PilotError("direct request introduced a cache marker or nontext block")
        original = [{"role": "system", "content": payload["system"]}] + [
            {"role": "user", "content": m["content"][0]["text"]} for m in blocks]
        if canonical(request_for(model, original)) != canonical(payload):
            raise PilotError("direct request changed frozen generation/service controls")
    except (KeyError, TypeError, IndexError) as exc:
        raise PilotError("invalid direct request shape") from exc


def upper_cost(model):
    # Reserve full documented base context capacity, not an unproven byte/token
    # estimate. No long-context beta, tools, thinking, priority, or cache writes.
    rates = MODELS[model]
    return Decimal(200_000) * money(rates["prompt"]) + CAP * money(rates["completion"])


def send(key, payload):
    request = urllib.request.Request("https://api.anthropic.com/v1/messages", data=canonical(payload),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            value = json.load(response)
            transport = {"request_id": response.headers.get("request-id"), "http_status": response.status}
    except urllib.error.HTTPError as exc:
        raise PilotError(f"direct provider HTTP {exc.code}; response suppressed; reservation remains unresolved") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise PilotError("direct transport/JSON failure; no retry; reservation remains unresolved") from None
    if not isinstance(value, dict) or "error" in value:
        raise PilotError("direct response is not a successful object")
    return value, transport


def parse_response(model, response):
    rates = MODELS[model]
    if (response.get("model") != rates["snapshot"] or response.get("type") != "message"
            or response.get("role") != "assistant" or not isinstance(response.get("id"), str)):
        raise PilotError("direct response attribution differs from exact snapshot")
    usage = response.get("usage", {})
    if not isinstance(usage, dict):
        raise PilotError("invalid direct usage object")
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PilotError("direct token accounting missing or invalid")
    if usage["input_tokens"] > 200_000 or usage["output_tokens"] > CAP:
        raise PilotError("direct token usage exceeds reserved capacity")
    # No cache_control was sent. Missing counters are preserved as missing;
    # nonzero cache usage would invalidate this uncached diagnostic contract.
    for field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value != 0):
            raise PilotError("unexpected direct cached-token usage; reconcile before continuing")
    if usage.get("service_tier") not in (None, "standard"):
        raise PilotError("direct response reported nonstandard service tier")
    content = response.get("content")
    if not isinstance(content, list) or not content or any(not isinstance(b, dict) or b.get("type") != "text" or not isinstance(b.get("text"), str) for b in content):
        raise PilotError("direct response is not plain text")
    if response.get("stop_reason") not in {"end_turn", "max_tokens", "stop_sequence", "refusal"}:
        raise PilotError("unexpected direct stop reason")
    cost = usage["input_tokens"] * money(rates["prompt"]) + usage["output_tokens"] * money(rates["completion"])
    return {"answer": "".join(b["text"] for b in content), "usage": usage, "generation_id": response["id"],
        "model": response["model"], "finish_reason": response["stop_reason"], "stop_sequence": response.get("stop_sequence"),
        "service_tier_reported": usage.get("service_tier"), "cost_usd": str(cost),
        "cost_basis": "tariff_reconstructed_not_provider_billed", "paper_evidence": False}


class DirectLedger(Ledger):
    """Reuse credential-bound durable file I/O, not OpenRouter dispatch/accounting."""
    def __init__(self, path, key, openrouter):
        super().__init__(path, key)
        self.openrouter = openrouter

    def campaign_exposure(self):
        with self.openrouter.locked() as handle:
            events = self.openrouter.read(handle)
        done = {e["id"]: e for e in events if e["event"] == "result"}
        return sum((money(e["cost_usd"]) for e in done.values()), Decimal(0)) + sum((money(e["upper_cost_usd"])
            for e in events if e["event"] == "reserve" and e["id"] not in done), Decimal(0))

    def call(self, key, call_id, stage, model, payload, metadata):
        if hashlib.sha256(key.encode()).hexdigest() != self.key_id:
            raise PilotError("direct dispatch key differs from ledger key")
        validate_request(model, payload)
        fingerprint = digest({"payload": payload, "metadata": metadata, "stage": stage, "model": model})
        with self.locked() as handle:
            events = self.read(handle)
            done = {e["id"]: e for e in events if e["event"] == "result"}
            if call_id in done:
                if done[call_id]["fingerprint"] != fingerprint:
                    raise PilotError("direct resume inputs changed")
                return done[call_id]
            if any(e["event"] == "reserve" and e["id"] not in done for e in events):
                raise PilotError("unresolved direct request; reconcile before any new dispatch")
            reserve = upper_cost(model)
            total = sum((money(e["cost_usd"]) for e in done.values()), Decimal(0))
            if total + reserve > STAGE_CAP:
                raise PilotError("direct next-call reservation exceeds cumulative USD3 cap")
            if total + reserve + self.campaign_exposure() > Decimal("50"):
                raise PilotError("direct request would exceed combined campaign ceiling")
            self.append(handle, {"event": "reserve", "id": call_id, "stage": stage,
                "upper_cost_usd": str(reserve), "fingerprint": fingerprint, "request": payload, "metadata": metadata})
            started = time.perf_counter()
            response, transport = send(key, payload)
            latency = (time.perf_counter() - started) * 1000
            self.append(handle, {"event": "response", "id": call_id, "response": response, "latency_ms": latency, **transport})
            parsed = parse_response(model, response)
            if money(parsed["cost_usd"]) > reserve:
                raise PilotError("direct reconstructed cost exceeds reservation")
            result = {**parsed, "event": "result", "id": call_id, "stage": stage,
                "fingerprint": fingerprint, "latency_ms": latency, "metadata": metadata, **transport}
            self.append(handle, result)
            return result

    def summary(self):
        result = super().summary()
        result["hard_cap_usd"] = str(STAGE_CAP)
        result["tariff_reconstructed_cost_usd"] = result.pop("spent_usd")
        result["cost_basis"] = "tariff_reconstructed_not_provider_billed"
        return result


def sources():
    return {p: file_hash(ROOT / p) for p in SOURCE_PATHS}


def schedule_for(frontier):
    ids = list(dict.fromkeys(r["task_id"] for r in frontier["schedule"] if r["phase"] == "holdout"))[:32]
    schedule = [{k: item[k] for k in ("task_id", "phase", "context", "model", "arm")}
        for item in frontier["schedule"] if item["phase"] == "holdout" and item["task_id"] in ids
        and item["model"] in MODELS and item["arm"] == "full"]
    if len(ids) != 32 or len(schedule) != 128:
        raise PilotError("direct diagnostic requires32 fixed question IDs across2contexts and2models")
    return schedule


def prepare(args):
    frontier = json.loads((args.frontier / "manifest.json").read_text())
    if frontier["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}:
        raise PilotError("direct fixtures differ from frontier")
    schedule = schedule_for(frontier)
    tasks = load_tasks(args.natural, args.extended)
    for item in schedule:
        request_for(item["model"], messages(tasks[item["context"]][item["task_id"]], frontier["contract"]))
    manifest = {"paper_evidence": False, "frontier_manifest_sha256": digest(frontier), "fixtures": frontier["fixtures"],
        "source_files": sources(), "models": MODELS, "contract": frontier["contract"], "stage_cap_usd": str(STAGE_CAP),
        "schedule": schedule, "scheduled_calls": len(schedule), "max_tokens": CAP,
        "subset_rule": "first32 holdout question IDs in frozen acquisition chronology; no outcome-based selection",
        "limitations": ["Content/order preserved via top-level system and ordered user text blocks; gateway serialization not proven identical.",
            "Direct exact snapshot names cannot prove the gateway alias used the same immutable backend revision.",
            "Direct costs are reconstructed from published standard uncached tariffs, not API-returned billed dollars.",
            "No cache_control, tools, thinking, long-context beta, SDK retries, or automatic fallback.",
            "Single noninterleaved direct sample per question/context/model; gateway latency and quality comparisons are descriptive.",
            "Subset uses already frozen questions, not independent replication or a learned policy test."]}
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {"scheduled_calls": len(schedule), "stage_cap_usd": str(STAGE_CAP), "manifest_sha256": digest(manifest)}


def run(args):
    manifest = json.loads((args.output / "manifest.json").read_text())
    frontier = json.loads((args.frontier / "manifest.json").read_text())
    if (manifest["frontier_manifest_sha256"] != digest(frontier) or manifest["source_files"] != sources()
            or manifest["models"] != MODELS or manifest["stage_cap_usd"] != str(STAGE_CAP)
            or manifest["schedule"] != schedule_for(frontier) or manifest["contract"] != frontier["contract"]
            or manifest["scheduled_calls"] != 128 or manifest["max_tokens"] != CAP
            or manifest["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}):
        raise PilotError("direct frozen source, fixture, model or budget changed")
    key = load_key(args.env_file)
    ledger = DirectLedger(args.ledger, key, Ledger(args.openrouter_ledger, openrouter_key(args.env_file)))
    tasks = load_tasks(args.natural, args.extended)
    output = args.output / "results.json"
    existing = json.loads(output.read_text()) if output.exists() else []
    stop = min(args.max_calls or len(manifest["schedule"]), len(manifest["schedule"]))
    if stop < len(existing):
        raise PilotError("direct prefix resume cannot truncate acquired results")
    rows = []
    stage = "direct-claude-v1-" + digest(manifest)[:20]
    for i, item in enumerate(manifest["schedule"][:stop]):
        task = tasks[item["context"]][item["task_id"]]
        original = messages(task, manifest["contract"])
        payload = request_for(item["model"], original)
        result = ledger.call(key, stage + f"-{i:05d}", stage, item["model"], payload,
            {"manifest_sha256": digest(manifest), **item})
        row = {**{k: v for k, v in result.items() if k not in {"metadata", "at", "key_id"}},
            **item, "returned_model": result["model"], "request_sha256": digest(payload),
            "original_messages_sha256": digest(original), "expected": task["expected"], **score(result["answer"], task["expected"])}
        if i < len(existing) and row != existing[i]:
            raise PilotError("direct cached row differs from existing artifact")
        rows.append(row)
        if len(rows) >= len(existing):
            write_json(output, rows)
        print(json.dumps({"completed": len(rows), "total": len(manifest["schedule"]), "model": item["model"],
            "context": item["context"], "reconstructed_cost_usd": row["cost_usd"]}), flush=True)
    summary = {"paper_evidence": False, "manifest_sha256": digest(manifest), "results_sha256": digest(rows),
        "completed_calls": len(rows), "scheduled_calls": len(manifest["schedule"]), "ledger": ledger.summary()}
    write_json(args.output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    for name in ("frontier", "natural", "extended", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--openrouter-ledger", type=Path)
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    try:
        if args.command == "run" and (args.env_file is None or args.ledger is None or args.openrouter_ledger is None or (args.max_calls is not None and args.max_calls < 1)):
            raise PilotError("run requires credential file, ledger and positive optional max-calls")
        print(json.dumps(prepare(args) if args.command == "prepare" else run(args), indent=2))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Direct diagnostic stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
