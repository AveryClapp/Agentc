"""Small, resumable OpenRouter experiments with pre-dispatch spend reservations.

The append-only ledger is shared across stages and worktrees. A lost response
leaves its reservation unresolved and blocks further calls until reconciled.
Credentials and authorization headers are never logged. Returned inference JSON,
including provider-error details, is retained only in the private durable ledger;
console errors are sanitized. Outer HTTP error bodies remain suppressed.
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
import uuid
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator


API = "https://openrouter.ai/api/v1"
HARD_CAP = Decimal("50")
REQUEST_DEADLINE_SECONDS = 120.0
MAX_ATTEMPTS = 2
Json = dict[str, Any]


class PilotError(RuntimeError):
    """Abort the pilot without making another paid request."""


class ProviderFailure(PilotError):
    def __init__(self, message: str, details: Json):
        super().__init__(message)
        self.details = details


def retry_delay(value: str | None) -> float:
    if value is None:
        return 0.0
    try:
        seconds = (
            float(value)
            if re.fullmatch(r"[0-9]+", value)
            else (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)
            ).total_seconds()
        )
        return max(0.0, seconds) if seconds < float("inf") else 86400.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


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
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise PilotError("unexpected API redirect")


def request_json(path: str, key: str, payload: Json | None = None) -> Json:
    from bench.openrouter_transport import DeadlineExpired, total_deadline

    if not path.startswith("/") or path.startswith("//"):
        raise PilotError("invalid API path")
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "X-OpenRouter-Title": "Agentc research pilot",
        "X-OpenRouter-Metadata": "enabled",
    }
    req = urllib.request.Request(
        API + path,
        headers=headers,
        data=None if payload is None else canonical(payload),
    )
    try:
        with total_deadline(REQUEST_DEADLINE_SECONDS):
            with urllib.request.build_opener(NoRedirect).open(
                req, timeout=45
            ) as response:
                value = json.load(response)
    except urllib.error.HTTPError as exc:
        details = {
            "kind": "http_error",
            "http_status": exc.code,
            "retry_after_seconds": retry_delay(
                exc.headers.get("Retry-After") if exc.headers else None
            ),
            "retryable": exc.code in (429, 502, 503, 504),
        }
        exc.close()
        raise ProviderFailure(
            f"provider returned HTTP {exc.code}; response body suppressed", details
        ) from None
    except (DeadlineExpired, TimeoutError):
        raise ProviderFailure(
            "API deadline/timeout; response suppressed",
            {"kind": "timeout", "retryable": True},
        ) from None
    except (urllib.error.URLError, OSError):
        raise ProviderFailure(
            "API transport failure; response suppressed",
            {"kind": "transport_error", "retryable": True},
        ) from None
    except ValueError:
        raise ProviderFailure(
            "API JSON failure; response suppressed",
            {"kind": "invalid_json", "retryable": False},
        ) from None
    # Inference errors may arrive inside HTTP200 after partial generation. The
    # ledger must preserve the returned envelope before rejecting its outcome.
    # Read-only account/model queries still reject errors at this boundary.
    defer_error_validation = path == "/chat/completions" and payload is not None
    if not isinstance(value, dict) or ("error" in value and not defer_error_validation):
        raise PilotError("API response is not a successful JSON object")
    return value


def account(key: str) -> Json:
    data = request_json("/key", key)["data"]
    return {
        name: data.get(name)
        for name in (
            "usage",
            "limit",
            "limit_remaining",
            "limit_reset",
            "byok_usage",
            "is_free_tier",
        )
    }


def make_request(
    model: str,
    providers: list[str],
    messages: list[Json],
    *,
    max_tokens: int = 128,
    allowed_models: list[str] | None = None,
) -> Json:
    if not model or not providers or not 1 <= max_tokens <= 2048:
        raise PilotError("model, providers and bounded max_tokens are required")
    if (
        not messages
        or len(messages) > 128
        or any(
            set(m) != {"role", "content"}
            or m["role"] not in {"system", "user", "assistant"}
            or not isinstance(m["content"], str)
            for m in messages
        )
    ):
        raise PilotError("pilot accepts bounded text-only messages")
    payload: Json = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
        "transforms": [],
        "service_tier": "default",
        "provider": {
            "only": providers,
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "max_price": {"prompt": 6, "completion": 30},
        },
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
    return (
        Decimal(tokens) * Decimal("6") + Decimal(payload["max_tokens"]) * Decimal("30")
    ) / Decimal(1_000_000)


def text_choice(response: Json) -> Json:
    """Accept completed text, not partial output attached to a provider error.

    A length stop remains an observed, potentially damaging cap outcome. It is
    not discarded to improve quality scores. This is not a quality validator.
    """
    choices = response.get("choices")
    error = response.get("error")
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        error = error or choices[0].get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        raise ProviderFailure(
            "provider did not complete a text response; error body suppressed",
            {
                "kind": "provider_error",
                "http_status": code if isinstance(code, int) else None,
                "retryable": code in (429, 502, 503, 504),
            },
        )
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
    ):
        raise PilotError("missing model/provider/choice attribution")
    choice = choices[0]
    if (
        response.get("error") is not None
        or choice.get("error") is not None
        or choice.get("finish_reason") not in ("stop", "length")
    ):
        raise PilotError(
            "provider did not complete a text response; preserve reservation and reconcile"
        )
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

    def append(self, handle: Any, event: Json) -> Json:
        event = {
            **event,
            "key_id": self.key_id,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        handle.write(canonical(event).decode() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return event

    def summary(self) -> Json:
        from bench.openrouter_attempts import accounting

        with self.locked() as handle:
            state = accounting(Ledger.read(self, handle))
        return {
            "hard_cap_usd": str(HARD_CAP),
            "spent_usd": str(state["known_usd"]),
            "completed_calls": len(state["results"]),
            "retained_uncertainty_usd": str(state["holds_usd"]),
            "pending_upper_bound_usd": str(state["pending_usd"]),
            "conservative_committed_usd": str(
                state["known_usd"] + state["holds_usd"] + state["pending_usd"]
            ),
            "unresolved_calls": sorted(
                {e["id"] for e in state["pending"]}
                | state["failed_calls"]
                | state["unsafe_ids"]
            ),
        }

    def abandon(
        self, reserve_sha256: str, expected_ledger_sha256: str, reason: str
    ) -> Json:
        """Explicitly retire a stopped attempt while preserving its full bound."""
        from bench.openrouter_attempts import accounting

        with self.locked() as handle:
            events = Ledger.read(self, handle)
            if digest(events) != expected_ledger_sha256:
                raise PilotError("ledger changed since abandonment review")
            state = accounting(events)
            candidates = state["pending"] + [
                state["reserves"][key]
                for key, terminal in state["terminals"].items()
                if terminal["event"] == "attempt_failure"
                and terminal["id"] in state["failed_calls"]
            ]
            matches = [r for r in candidates if digest(r) == reserve_sha256]
            if len(matches) != 1 or not reason or len(reason) > 500:
                raise PilotError(
                    "abandonment requires one exact unresolved reservation and a reason"
                )
            reserve = matches[0]
            if reserve["id"] in state["unsafe_ids"]:
                raise PilotError(
                    "out-of-bound or BYOK charge requires separate reconciliation"
                )
            terminal = state["terminals"].get(reserve_sha256, {})
            reported = terminal.get("reported_cost_usd")
            # A killed process may have persisted a response but not a terminal.
            for event in events:
                if (
                    event["event"] == "response"
                    and event["id"] == reserve["id"]
                    and event.get("attempt_id") == reserve.get("attempt_id")
                ):
                    usage = event["response"].get("usage") or {}
                    if usage.get("is_byok"):
                        raise PilotError("BYOK charge requires separate reconciliation")
                    if usage.get("cost") is not None:
                        cost = money(usage["cost"])
                        if cost > money(reserve["upper_cost_usd"]):
                            raise PilotError(
                                "out-of-bound charge requires separate reconciliation"
                            )
                        reported = str(cost)
            age = (
                datetime.now(timezone.utc) - datetime.fromisoformat(reserve["at"])
            ).total_seconds()
            if age < REQUEST_DEADLINE_SECONDS:
                raise PilotError(
                    "cannot abandon an attempt inside its request deadline"
                )
            return self.append(
                handle,
                {
                    "event": "attempt_abandoned",
                    "id": reserve["id"],
                    "stage": reserve["stage"],
                    "attempt_id": reserve.get("attempt_id"),
                    "reserve_sha256": reserve_sha256,
                    "budget_hold_usd": reserve["upper_cost_usd"],
                    "reported_cost_usd": reported,
                    "reason": reason,
                    "reviewed_ledger_sha256": expected_ledger_sha256,
                },
            )

    def call(
        self,
        key: str,
        call_id: str,
        stage: str,
        stage_cap: Decimal,
        payload: Json,
        metadata: Json,
    ) -> Json:
        from bench.openrouter_attempts import accounting
        from bench.openrouter_transport import require_deadline_support

        require_deadline_support()
        if hashlib.sha256(key.encode()).hexdigest() != self.key_id:
            raise PilotError("dispatch key differs from ledger key")
        if not stage or not call_id or not 0 < stage_cap <= HARD_CAP:
            raise PilotError("invalid stage/call identity or budget")
        expected = make_request(
            payload["model"],
            payload["provider"]["only"],
            payload["messages"],
            max_tokens=payload["max_tokens"],
            allowed_models=(
                payload["plugins"][0]["allowed_models"]
                if payload["model"] == "openrouter/auto"
                else None
            ),
        )
        if canonical(payload) != canonical(expected):
            raise PilotError("request changed the frozen price or dispatch controls")
        fingerprint = digest({"payload": payload, "metadata": metadata, "stage": stage})
        with self.locked() as handle:
            events = Ledger.read(self, handle)
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
                    if (
                        event["event"] == "response"
                        and event.get("id") == call_id
                        and event.get("attempt_id")
                        in (None, done[call_id].get("attempt_id"))
                    ):
                        text_choice(event["response"])
                accounting(events)
                return done[call_id]
            state = accounting(events)
            if state["pending"] or state["failed_calls"] - {call_id}:
                raise PilotError(
                    "unresolved paid request; reconcile ledger before continuing"
                )
            if state["unsafe_ids"]:
                raise PilotError(
                    "out-of-bound or BYOK charge requires separate reconciliation"
                )
            if call_id in state["closed_ids"]:
                raise PilotError("abandoned logical call cannot be retried")
            attempts = [r for r in state["reserves"].values() if r["id"] == call_id]
            if any(r["fingerprint"] != fingerprint for r in attempts):
                raise PilotError("retry differs from the exact failed request")
            if attempts:
                last = state["terminals"].get(digest(attempts[-1]))
                if (
                    len(attempts) >= MAX_ATTEMPTS
                    or last is None
                    or not last.get("failure", {}).get("retryable", False)
                ):
                    raise PilotError(
                        "retry allowance exhausted or failure needs explicit review"
                    )
                if time.time() < last["retry_not_before_epoch"]:
                    raise ProviderFailure(
                        "retry backoff has not elapsed",
                        {
                            "kind": "backoff",
                            "retryable": True,
                            "retry_not_before_epoch": last["retry_not_before_epoch"],
                        },
                    )
            total = state["known_usd"] + state["holds_usd"]
            stage_total = state["known_by_stage"].get(stage, Decimal(0)) + state[
                "holds_by_stage"
            ].get(stage, Decimal(0))
            reserve = upper_cost(payload)
            if total + reserve > HARD_CAP or stage_total + reserve > stage_cap:
                raise PilotError(
                    "next request would exceed the reserved spending limit"
                )
            before = account(key)
            usage = money(before["usage"])
            origins = [e for e in events if e["event"] == "origin"]
            if not origins:
                self.append(handle, {"event": "origin", "usage_usd": str(usage)})
                baseline = usage
            else:
                baseline = money(origins[0]["usage_usd"])
            if (
                usage < baseline
                or max(total, usage - baseline + state["holds_usd"]) + reserve
                > HARD_CAP
            ):
                raise PilotError("account usage would exceed the campaign ceiling")
            remaining = before.get("limit_remaining")
            if remaining is not None and money(remaining) < reserve:
                raise PilotError("provider key budget is below the next reservation")
            reservation = self.append(
                handle,
                {
                    "event": "reserve",
                    "id": call_id,
                    "stage": stage,
                    "attempt_id": uuid.uuid4().hex,
                    "upper_cost_usd": str(reserve),
                    "fingerprint": fingerprint,
                    "request": payload,
                    "metadata": metadata,
                    "account_usage_before_usd": str(usage),
                },
            )
            context = {}
            try:
                return self._perform(
                    handle, reservation, key, payload, metadata, context
                )
            except BaseException as exc:
                # Persist a terminal attempt, not a fabricated provider result.
                # Every failed/cancelled attempt retains its full cost bound.
                details = getattr(
                    exc,
                    "details",
                    {
                        "kind": "cancelled"
                        if isinstance(exc, KeyboardInterrupt)
                        else "validation_error",
                        "retryable": False,
                    },
                )
                reported = None
                try:
                    reported_value = money(
                        context.get("response", {}).get("usage", {}).get("cost")
                    )
                    reported = str(reported_value)
                except (PilotError, AttributeError, TypeError):
                    pass
                self.append(
                    handle,
                    {
                        "event": "attempt_failure",
                        "id": call_id,
                        "stage": stage,
                        "attempt_id": reservation["attempt_id"],
                        "reserve_sha256": digest(reservation),
                        "budget_hold_usd": reservation["upper_cost_usd"],
                        "reported_cost_usd": reported,
                        "failure": details,
                        "retry_not_before_epoch": time.time()
                        + max(5.0, details.get("retry_after_seconds", 0.0)),
                    },
                )
                raise

    def _perform(self, handle, reservation, key, payload, metadata, context):
        call_id, stage, fingerprint = (
            reservation[k] for k in ("id", "stage", "fingerprint")
        )
        reserve = money(reservation["upper_cost_usd"])
        started = time.perf_counter()
        response = request_json("/chat/completions", key, payload)
        latency = (time.perf_counter() - started) * 1000
        # Preserve the returned body before validating it, including partial
        # provider errors; never feed one into optimizer success feedback.
        self.append(
            handle,
            {
                "event": "response",
                "id": call_id,
                "attempt_id": reservation["attempt_id"],
                "response": response,
                "latency_ms": latency,
            },
        )
        context["response"] = response
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
        if (
            not isinstance(returned, str)
            or not returned
            or not isinstance(provider, str)
            or not provider
        ):
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
            selected = [
                endpoint
                for endpoint in router.get("endpoints", {}).get("available", [])
                if endpoint.get("selected") is True
            ]
            if (
                provider != contract["provider_name"]
                or len(selected) != 1
                or selected[0].get("provider") != contract["provider_name"]
                or selected[0].get("model") != contract["endpoint_model"]
                or router.get("requested") != payload["model"]
                or router.get("attempt") != 1
                or router.get("is_byok") is not False
            ):
                raise PilotError(
                    "returned endpoint differs from frozen dispatch contract"
                )
        result = {
            "event": "result",
            "id": call_id,
            "stage": stage,
            "attempt_id": reservation["attempt_id"],
            "fingerprint": fingerprint,
            "cost_usd": str(cost),
            "latency_ms": latency,
            "model": returned,
            "provider": provider,
            "answer": answer,
            "finish_reason": choice["finish_reason"],
            "usage": usage_data,
            "generation_id": response.get("id"),
            "router_metadata": response.get("openrouter_metadata"),
            "metadata": metadata,
            "paper_evidence": False,
        }
        self.append(handle, result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("account", "smoke", "status", "abandon"))
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--model", default="anthropic/claude-haiku-4.5")
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--reservation-sha256")
    parser.add_argument("--expected-ledger-sha256")
    parser.add_argument("--reason")
    args = parser.parse_args()
    try:
        key = load_key(args.env_file)
        ledger = Ledger(args.ledger, key)
        if args.command == "account":
            result = account(key)
        elif args.command == "status":
            result = ledger.summary()
        elif args.command == "abandon":
            result = ledger.abandon(
                args.reservation_sha256, args.expected_ledger_sha256, args.reason
            )
        else:
            payload = make_request(
                args.model,
                [args.provider],
                [
                    {
                        "role": "user",
                        "content": "Reply with exactly the number 4. What is 2+2?",
                    }
                ],
                max_tokens=16,
            )
            result = ledger.call(
                key,
                "smoke-v1-" + digest(payload)[:16],
                "smoke-v1",
                Decimal("1"),
                payload,
                {"purpose": "accounting_smoke"},
            )
            if result["answer"].strip() != "4":
                raise PilotError("smoke response did not match expected answer")
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError) as exc:
        print(f"Pilot stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
