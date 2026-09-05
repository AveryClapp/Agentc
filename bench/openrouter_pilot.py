"""Small, resumable OpenRouter experiments with pre-dispatch spend reservations.

The append-only ledger is shared across stages and worktrees. A lost response
leaves its reservation unresolved and blocks further calls until reconciled.
Credentials, authorization headers and provider error bodies are never logged.
This pilot is exploratory evidence, not the frozen MLSys confirmatory campaign.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator


API = "https://openrouter.ai/api/v1"
HARD_CAP = Decimal("50")
Json = dict[str, Any]


class PilotError(RuntimeError):
    """Abort the pilot without making another paid request."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def money(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PilotError("missing or invalid monetary value")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise PilotError("invalid monetary value") from exc
    if not result.is_finite() or result < 0:
        raise PilotError("monetary values must be finite and non-negative")
    return result


def load_key(env_file: Path) -> str:
    """Read only the named credential; never evaluate a shell dotenv file."""
    values = []
    for line in env_file.read_text().splitlines():
        match = re.match(r"^\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.*?)\s*$", line)
        if match:
            value = match.group(1)
            if value.startswith(("'", '"')) and value[-1:] == value[:1]:
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].strip()
            values.append(value)
    if len(values) != 1 or not values[0] or any(c.isspace() for c in values[0]):
        raise PilotError("dotenv must contain one non-empty OPENROUTER_API_KEY")
    return values[0]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise PilotError("unexpected API redirect")


def request_json(path: str, key: str, payload: Json | None = None) -> Json:
    if not path.startswith("/") or path.startswith("//"):
        raise PilotError("invalid API path")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
               "X-OpenRouter-Title": "Agentc research pilot",
               "X-OpenRouter-Metadata": "enabled"}
    req = urllib.request.Request(API + path, headers=headers,
                                 data=None if payload is None else canonical(payload))
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=45) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        raise PilotError(f"provider returned HTTP {exc.code}; response body suppressed") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise PilotError("API transport or JSON failure; response suppressed") from None
    if not isinstance(value, dict) or "error" in value:
        raise PilotError("API response is not a successful JSON object")
    return value


def account(key: str) -> Json:
    data = request_json("/key", key)["data"]
    return {name: data.get(name) for name in
            ("usage", "limit", "limit_remaining", "limit_reset", "byok_usage", "is_free_tier")}


def make_request(model: str, providers: list[str], messages: list[Json], *,
                 max_tokens: int = 128, allowed_models: list[str] | None = None) -> Json:
    if not model or not providers or not 1 <= max_tokens <= 2048:
        raise PilotError("model, providers and bounded max_tokens are required")
    if not messages or len(messages) > 128 or any(
        set(m) != {"role", "content"} or m["role"] not in {"system", "user", "assistant"}
        or not isinstance(m["content"], str) for m in messages
    ):
        raise PilotError("pilot accepts bounded text-only messages")
    payload: Json = {
        "model": model, "messages": messages, "max_tokens": max_tokens,
        "temperature": 0, "stream": False, "transforms": [], "service_tier": "default",
        "provider": {"only": providers, "allow_fallbacks": False,
                     "require_parameters": True, "data_collection": "deny",
                     "max_price": {"prompt": 6, "completion": 30}},
    }
    if model == "openrouter/auto":
        if not allowed_models:
            raise PilotError("auto router requires a bounded model pool")
        payload["plugins"] = [{"id": "auto-router", "allowed_models": allowed_models}]
    elif len(providers) != 1 or allowed_models:
        raise PilotError("fixed arms require exactly one provider and no router plugin")
    if len(canonical(payload)) > 65536:
        raise PilotError("pilot request exceeds 64 KiB")
    return payload


def upper_cost(payload: Json) -> Decimal:
    # Text-only UTF-8 bytes upper-bound byte-fallback tokenization. Add ample
    # per-message/template overhead. API price caps are dollars per million.
    tokens = len(canonical(payload)) + 1024 + 64 * len(payload["messages"])
    return (Decimal(tokens) * Decimal("6") +
            Decimal(payload["max_tokens"]) * Decimal("30")) / Decimal(1_000_000)


def text_choice(response: Json) -> Json:
    """Accept completed text, not partial output attached to a provider error.

    A length stop remains an observed, potentially damaging cap outcome. It is
    not discarded to improve quality scores. This is not a quality validator.
    """
    choices = response.get("choices")
    if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)):
        raise PilotError("missing model/provider/choice attribution")
    choice = choices[0]
    if (response.get("error") is not None or choice.get("error") is not None
            or choice.get("finish_reason") not in {"stop", "length"}):
        raise PilotError("provider did not complete a text response; preserve reservation and reconcile")
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise PilotError("provider returned no text answer")
    return choice


class Ledger:
    def __init__(self, path: Path, key: str):
        self.path = path
        self.key_id = hashlib.sha256(key.encode()).hexdigest()

    @contextmanager
    def locked(self) -> Iterator[Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield handle
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read(self, handle: Any) -> list[Json]:
        handle.seek(0)
        try:
            events = [json.loads(line) for line in handle if line.strip()]
        except ValueError as exc:
            raise PilotError("ledger is incomplete; reconcile before resuming") from exc
        if any(e.get("key_id") != self.key_id for e in events):
            raise PilotError("ledger belongs to a different API key")
        return events

    def append(self, handle: Any, event: Json) -> None:
        event = {**event, "key_id": self.key_id,
                 "at": datetime.now(timezone.utc).isoformat()}
        handle.write(canonical(event).decode() + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    def summary(self) -> Json:
        with self.locked() as handle:
            events = self.read(handle)
        done = {e["id"] for e in events if e["event"] == "result"}
        return {"hard_cap_usd": str(HARD_CAP),
                "spent_usd": str(sum((money(e["cost_usd"]) for e in events
                                      if e["event"] == "result"), Decimal(0))),
                "completed_calls": len(done),
                "unresolved_calls": [e["id"] for e in events
                                     if e["event"] == "reserve" and e["id"] not in done]}

    def call(self, key: str, call_id: str, stage: str, stage_cap: Decimal,
             payload: Json, metadata: Json) -> Json:
        if hashlib.sha256(key.encode()).hexdigest() != self.key_id:
            raise PilotError("dispatch key differs from ledger key")
        if not stage or not call_id or not 0 < stage_cap <= HARD_CAP:
            raise PilotError("invalid stage/call identity or budget")
        expected = make_request(
            payload["model"], payload["provider"]["only"], payload["messages"],
            max_tokens=payload["max_tokens"],
            allowed_models=(payload["plugins"][0]["allowed_models"]
                            if payload["model"] == "openrouter/auto" else None),
        )
        if canonical(payload) != canonical(expected):
            raise PilotError("request changed the frozen price or dispatch controls")
        fingerprint = digest({"payload": payload, "metadata": metadata, "stage": stage})
        with self.locked() as handle:
            events = self.read(handle)
            done = {e["id"]: e for e in events if e["event"] == "result"}
            if call_id in done:
                if done[call_id]["fingerprint"] != fingerprint:
                    raise PilotError("cannot resume a call with changed inputs")
                # Older drivers may have recorded an errored partial completion
                # as a result. Reject both its summary and any retained raw body
                # before it can be replayed into a fresh optimizer store.
                if done[call_id].get("finish_reason") not in {"stop", "length"}:
                    raise PilotError("cached result is not a completed text response")
                for event in events:
                    if event["event"] == "response" and event.get("id") == call_id:
                        text_choice(event["response"])
                return done[call_id]
            pending = [e for e in events if e["event"] == "reserve" and e["id"] not in done]
            if pending:
                raise PilotError("unresolved paid request; reconcile ledger before continuing")
            total = sum((money(e["cost_usd"]) for e in done.values()), Decimal(0))
            stage_total = sum((money(e["cost_usd"]) for e in done.values()
                               if e["stage"] == stage), Decimal(0))
            reserve = upper_cost(payload)
            if total + reserve > HARD_CAP or stage_total + reserve > stage_cap:
                raise PilotError("next request would exceed the reserved spending limit")
            before = account(key)
            usage = money(before["usage"])
            origins = [e for e in events if e["event"] == "origin"]
            if not origins:
                self.append(handle, {"event": "origin", "usage_usd": str(usage)})
                baseline = usage
            else:
                baseline = money(origins[0]["usage_usd"])
            if usage < baseline or max(total, usage - baseline) + reserve > HARD_CAP:
                raise PilotError("account usage would exceed the campaign ceiling")
            remaining = before.get("limit_remaining")
            if remaining is not None and money(remaining) < reserve:
                raise PilotError("provider key budget is below the next reservation")
            self.append(handle, {"event": "reserve", "id": call_id, "stage": stage,
                                 "upper_cost_usd": str(reserve), "fingerprint": fingerprint,
                                 "request": payload, "metadata": metadata,
                                 "account_usage_before_usd": str(usage)})
            started = time.perf_counter()
            response = request_json("/chat/completions", key, payload)
            latency = (time.perf_counter() - started) * 1000
            # Preserve the returned body before validating it, including partial
            # provider errors; never feed one into optimizer success feedback.
            self.append(handle, {"event": "response", "id": call_id,
                                 "response": response, "latency_ms": latency})
            choice = text_choice(response)
            usage_data = response.get("usage", {})
            cost = money(usage_data.get("cost"))
            if cost > reserve:
                raise PilotError("billed cost exceeds reserved bound; stop and reconcile")
            for name in ("prompt_tokens", "completion_tokens"):
                v = usage_data.get(name)
                if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                    raise PilotError("missing or invalid provider token accounting")
            returned = response.get("model")
            provider = response.get("provider")
            if (not isinstance(returned, str) or not returned or
                    not isinstance(provider, str) or not provider):
                raise PilotError("missing model/provider/choice attribution")
            if usage_data.get("is_byok"):
                raise PilotError("unexpected BYOK request; reconcile provider spend")
            answer = choice["message"]["content"]
            if payload["model"] == "openrouter/auto":
                pool = payload["plugins"][0]["allowed_models"]
                if returned not in pool:
                    raise PilotError("auto router returned a model outside the frozen pool")
            elif returned != payload["model"]:
                raise PilotError("fixed arm returned a different model")
            contract = metadata.get("dispatch_contract")
            if contract is not None:
                router = response.get("openrouter_metadata") or {}
                selected = [endpoint for endpoint in
                            router.get("endpoints", {}).get("available", [])
                            if endpoint.get("selected") is True]
                if (provider != contract["provider_name"] or len(selected) != 1
                        or selected[0].get("provider") != contract["provider_name"]
                        or selected[0].get("model") != contract["endpoint_model"]
                        or router.get("requested") != payload["model"]
                        or router.get("attempt") != 1 or router.get("is_byok") is not False):
                    raise PilotError("returned endpoint differs from frozen dispatch contract")
            result = {"event": "result", "id": call_id, "stage": stage,
                      "fingerprint": fingerprint, "cost_usd": str(cost),
                      "latency_ms": latency, "model": returned, "provider": provider,
                      "answer": answer, "finish_reason": choice["finish_reason"],
                      "usage": usage_data, "generation_id": response.get("id"),
                      "router_metadata": response.get("openrouter_metadata"),
                      "metadata": metadata, "paper_evidence": False}
            self.append(handle, result)
            return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("account", "smoke", "status"))
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--model", default="anthropic/claude-haiku-4.5")
    parser.add_argument("--provider", default="anthropic")
    args = parser.parse_args()
    try:
        key = load_key(args.env_file)
        ledger = Ledger(args.ledger, key)
        if args.command == "account":
            result = account(key)
        elif args.command == "status":
            result = ledger.summary()
        else:
            payload = make_request(args.model, [args.provider], [
                {"role": "user", "content": "Reply with exactly the number 4. What is 2+2?"}
            ], max_tokens=16)
            result = ledger.call(key, "smoke-v1-" + digest(payload)[:16], "smoke-v1",
                                 Decimal("1"), payload, {"purpose": "accounting_smoke"})
            if result["answer"].strip() != "4":
                raise PilotError("smoke response did not match expected answer")
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError) as exc:
        print(f"Pilot stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
