"""Read-only accounting for the original append-only provider campaign journal.

The historical attempt/group replay rules are adapted from d019016. Failed and
abandoned reservations retain their whole bounds; successful legacy billing
rows remain charges even when their provider output was unsuitable for replay.
This module cannot dispatch, append, release holds, or recreate a missing file.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


HARD_CAP_USD = Decimal("50")
Json = dict[str, Any]
POLICY = "protected-source-first-v1"
SLOTS = ("source", "candidate")
LEGACY_HOLD = "reviewed_http429_retry_with_budget_hold"
TERMINALS = {LEGACY_HOLD, "attempt_failure", "attempt_abandoned"}
KNOWN_EVENTS = {
    "origin",
    "reserve",
    "response",
    "result",
    *TERMINALS,
    "protected_study_phase",
    "protected_pair",
    "budget_group",
    "budget_group_release",
    "campaign_session",
}


class AccountingError(RuntimeError):
    """Journal integrity or complete financial reconciliation is unproven."""


class ProviderFailure(AccountingError):
    def __init__(self, message: str, details: dict[str, Any]):
        super().__init__(message)
        self.details = details


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
        raise AccountingError("missing or invalid monetary value")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise AccountingError("invalid monetary value") from exc
    if not result.is_finite() or result < 0:
        raise AccountingError("monetary values must be finite and non-negative")
    return result


def _text_choice(response: Json) -> Json:
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
        or (not isinstance(choices[0], dict))
    ):
        raise AccountingError("missing model/provider/choice attribution")
    choice = choices[0]
    if (
        response.get("error") is not None
        or choice.get("error") is not None
        or choice.get("finish_reason") not in ("stop", "length")
    ):
        raise AccountingError(
            "provider did not complete a text response; preserve reservation and reconcile"
        )
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise AccountingError("provider returned no text answer")
    return choice


def _schema_pipeline_allowed(pipeline: Any) -> bool:
    """Allow only no stages or the exact observed passed-moderation diagnostic.

    Router metadata documents moderation verdicts separately from healing and
    compression: https://openrouter.ai/docs/guides/features/router-metadata
    Its engine-specific data is open-ended, not a no-mutation guarantee. This is
    a narrow prospective policy exception for one observed shape, not permission
    for arbitrary guardrails. Extra fields/stages require independent review.
    """
    if not isinstance(pipeline, list):
        return False
    if not pipeline:
        return True
    if len(pipeline) != 1 or not isinstance(pipeline[0], dict):
        return False
    stage = pipeline[0]
    data = stage.get("data")
    return (
        isinstance(data, dict)
        and data.get("flagged") is False
        and (
            stage
            == {
                "type": "guardrail",
                "name": "moderation",
                "summary": "Content passed OpenAI moderation",
                "data": {
                    "engine": "openai-moderations",
                    "flagged": False,
                    "moderator_ids": ["OpenAIOmniModerator"],
                },
            }
        )
    )


def _schema_text_choice(response: Json) -> Json:
    """Keep billed truncations, but never accept refusal/healing as valid text.

    The typed application adapter treats every length completion as invalid;
    retaining its raw receipt here is necessary for complete cost accounting.
    """
    choice = _text_choice(response)
    if (
        choice["message"].get("refusal") is not None
        or choice.get("native_finish_reason")
        not in (None, "end_turn", "stop_sequence", "max_tokens", "stop", "length")
        or (
            choice.get("native_finish_reason") in {"max_tokens", "length"}
            and choice["finish_reason"] != "length"
        )
        or (
            not _schema_pipeline_allowed(
                (response.get("openrouter_metadata") or {}).get("pipeline", [])
            )
        )
    ):
        raise AccountingError(
            "schema response has refusal, unknown finish or material pipeline changes"
        )
    return choice


def _validate_group_completion(claim, body, result):
    """Revalidate the exact source receipt before it can authorize its partner.

    Legacy result rows remain billing records under their existing rules. Only
    grouped completions have this stronger dispatch/replay contract.
    """
    choice = _schema_text_choice(body)
    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise AccountingError("group response lacks provider token accounting")
    cost = money(usage.get("cost"))
    if cost > money(claim["upper_cost_usd"]) or usage.get("is_byok"):
        raise AccountingError("group response violates its billing contract")
    for field in ("prompt_tokens", "completion_tokens"):
        if type(usage.get(field)) is not int or usage[field] < 0:
            raise AccountingError(
                "group response has invalid provider token accounting"
            )
    if (
        body.get("model") != claim["request"]["model"]
        or not isinstance(body.get("provider"), str)
        or (not body["provider"])
    ):
        raise AccountingError("group response differs from its fixed model/provider")
    contract = claim["metadata"].get("dispatch_contract")
    if contract is not None:
        router = body.get("openrouter_metadata") or {}
        selected = [
            e
            for e in router.get("endpoints", {}).get("available", [])
            if e.get("selected") is True
        ]
        if (
            body["provider"] != contract["provider_name"]
            or len(selected) != 1
            or selected[0].get("provider") != contract["provider_name"]
            or (selected[0].get("model") != contract["endpoint_model"])
            or (router.get("requested") != claim["request"]["model"])
            or (type(router.get("attempt")) is not int)
            or (router["attempt"] != 1)
            or (router.get("is_byok") is not False)
        ):
            raise AccountingError("group response differs from its frozen endpoint")
    expected = {
        "id": claim["id"],
        "stage": claim["stage"],
        "attempt_id": claim["attempt_id"],
        "fingerprint": claim["fingerprint"],
        "metadata": claim["metadata"],
        "answer": choice["message"]["content"],
        "finish_reason": choice["finish_reason"],
        "usage": usage,
        "model": body["model"],
        "provider": body["provider"],
        "generation_id": body.get("id"),
        "router_metadata": body.get("openrouter_metadata"),
    }
    if (
        canonical({k: result.get(k) for k in expected}) != canonical(expected)
        or money(result["cost_usd"]) != cost
    ):
        raise AccountingError("group result differs from its durable response")


def _replay_groups(events):
    """Validate transitions in order and return only still-unclaimed liabilities."""
    groups, call_ids, used_ids = ({}, {}, set())
    responses, completed = ({}, set())
    for index, event in enumerate(events):
        kind = event["event"]
        if kind == "budget_group":
            key = digest(event)
            if (
                event.get("policy") != POLICY
                or event.get("id") in used_ids
                or (not event.get("id"))
                or (not event.get("stage"))
                or (not event.get("ledger_binding"))
                or (key in groups)
                or (set(event.get("slots", {})) != set(SLOTS))
            ):
                raise AccountingError("invalid or duplicate budget group")
            used_ids.add(event["id"])
            total = Decimal(0)
            for slot in SLOTS:
                spec = event["slots"][slot]
                if not spec.get("id") or spec["id"] in used_ids:
                    raise AccountingError("budget group reuses a call identity")
                if spec.get("fingerprint") != digest(
                    {
                        "payload": spec["request"],
                        "metadata": spec["metadata"],
                        "stage": event["stage"],
                    }
                ):
                    raise AccountingError("budget group request fingerprint differs")
                bound = money(spec["upper_cost_usd"])
                if bound <= 0:
                    raise AccountingError("group slots need positive allowances")
                total += bound
                call_ids[spec["id"]] = (key, slot)
                used_ids.add(spec["id"])
            if total != money(event["upper_cost_usd"]) or total > money(
                event["stage_cap_usd"]
            ):
                raise AccountingError("budget group combined allowance differs")
            groups[key] = {"reservation": event, "claimed": {}, "released": set()}
        elif kind == "reserve" and (
            event.get("group_sha256") or event["id"] in call_ids
        ):
            pair = call_ids.get(event["id"])
            if pair != (event.get("group_sha256"), event.get("group_slot")):
                raise AccountingError("group slot claim differs from its reservation")
            key, slot = pair
            state = groups[key]
            spec = state["reservation"]["slots"][slot]
            if slot in state["claimed"] or slot in state["released"]:
                raise AccountingError("budget group slot already claimed or released")
            if (
                not event.get("attempt_id")
                or event["stage"] != state["reservation"]["stage"]
                or canonical({k: event.get(k) for k in spec}) != canonical(spec)
                or (
                    event["fingerprint"]
                    != digest(
                        {
                            "payload": event["request"],
                            "metadata": event["metadata"],
                            "stage": event["stage"],
                        }
                    )
                )
            ):
                raise AccountingError(
                    "group attempt changed its exact request or allowance"
                )
            if (
                slot == "candidate"
                and state["reservation"]["slots"]["source"]["id"] not in completed
            ):
                raise AccountingError("candidate requires durable source completion")
            state["claimed"][slot] = event
        elif kind == "budget_group_release":
            key, slot = (event.get("group_sha256"), event.get("group_slot"))
            state = groups.get(key)
            if state is None or slot not in SLOTS:
                raise AccountingError("release has no exact budget group slot")
            spec = state["reservation"]["slots"][slot]
            if (
                slot in state["claimed"]
                or slot in state["released"]
                or event.get("id") != spec["id"]
                or (event.get("stage") != state["reservation"]["stage"])
                or (event.get("upper_cost_usd") != spec["upper_cost_usd"])
                or (event.get("unclaimed_prefix_sha256") != digest(events[:index]))
                or (not event.get("reason"))
            ):
                raise AccountingError("release lacks durable never-claimed proof")
            state["released"].add(slot)
        elif kind.startswith("budget_group"):
            raise AccountingError("unknown budget group event")
        elif event.get("id") in call_ids:
            key, slot = call_ids[event["id"]]
            claim = groups[key]["claimed"].get(slot)
            if claim is None or event.get("attempt_id") != claim["attempt_id"]:
                raise AccountingError("group outcome has no exact claimed attempt")
            if kind == "response":
                if event["id"] in responses:
                    raise AccountingError("group attempt has duplicate responses")
                responses[event["id"]] = event["response"]
            elif kind == "result":
                body = responses.get(event["id"])
                if body is None:
                    raise AccountingError("group result lacks a durable response")
                _validate_group_completion(claim, body, event)
                completed.add(event["id"])
        else:
            if event.get("group_sha256") or event.get("group_slot"):
                raise AccountingError("orphan budget group reference")
            if event.get("id"):
                used_ids.add(event["id"])
    pending = []
    for key, state in groups.items():
        group = state["reservation"]
        for slot in SLOTS:
            if slot not in state["claimed"] and slot not in state["released"]:
                pending.append(
                    {
                        **group["slots"][slot],
                        "event": "budget_group_slot",
                        "stage": group["stage"],
                        "group_sha256": key,
                        "group_slot": slot,
                    }
                )
    return {"groups": groups, "group_call_ids": set(call_ids), "group_pending": pending}


def accounting(events):
    reserves = {digest(e): e for e in events if e["event"] == "reserve"}
    if len(reserves) != sum((e["event"] == "reserve" for e in events)):
        raise AccountingError("duplicate reservation record")
    by_call, responses, positions = (defaultdict(list), defaultdict(list), {})
    attempt_ids, result_positions = (set(), {})
    for index, event in enumerate(events):
        if event["event"] == "reserve":
            key = digest(event)
            by_call[event["id"]].append((key, event))
            positions[key] = index
            attempt_id = event.get("attempt_id")
            if attempt_id is not None:
                if not attempt_id or attempt_id in attempt_ids:
                    raise AccountingError("duplicate or empty attempt identity")
                attempt_ids.add(attempt_id)
        elif event["event"] == "response":
            responses[event["id"], event.get("attempt_id")].append((index, event))
    terminals, results, pending = ({}, {}, [])
    known_by_stage, holds_by_stage = (defaultdict(Decimal), defaultdict(Decimal))
    closed_ids = set()
    for index, event in enumerate(events):
        kind = event["event"]
        if kind in TERMINALS:
            key = event["reserve_sha256"]
            reserve = reserves.get(key)
            previous = terminals.get(key)
            if reserve is None or positions[key] >= index:
                raise AccountingError(
                    "failure allowance has no unique exact reservation"
                )
            if previous is not None and (
                not (
                    previous["event"] == "attempt_failure"
                    and kind == "attempt_abandoned"
                    and (
                        previous.get("reported_cost_usd")
                        == event.get("reported_cost_usd")
                    )
                )
            ):
                raise AccountingError(
                    "failure allowance has no unique exact reservation"
                )
            if any((event.get(k) != reserve.get(k) for k in ("id", "stage"))):
                raise AccountingError(
                    "failure allowance identity differs from reservation"
                )
            if kind != LEGACY_HOLD and event.get("attempt_id") != reserve.get(
                "attempt_id"
            ):
                raise AccountingError(
                    "failure allowance attempt differs from reservation"
                )
            bound = money(reserve["upper_cost_usd"])
            if money(event["budget_hold_usd"]) != bound:
                raise AccountingError(
                    "failure allowance must retain the entire reserved bound"
                )
            charge = Decimal(0)
            if event.get("reported_cost_usd") is not None:
                charge = money(event["reported_cost_usd"])
                bodies = [
                    e
                    for position, e in responses[event["id"], event.get("attempt_id")]
                    if positions[key] < position < index
                ]
                if (
                    len(bodies) != 1
                    or money(bodies[0]["response"].get("usage", {}).get("cost"))
                    != charge
                ):
                    raise AccountingError(
                        "failed-attempt charge is not bound to its response"
                    )
            terminals[key] = event
            if kind == "attempt_abandoned":
                closed_ids.add(event["id"])
        elif kind == "result":
            if event["id"] in results:
                raise AccountingError("multiple results for one logical call")
            results[event["id"]] = event
            result_positions[event["id"]] = index
            known_by_stage[event["stage"]] += money(event["cost_usd"])
    unsafe_ids = set()
    for key, event in terminals.items():
        bound = money(reserves[key]["upper_cost_usd"])
        charge = money(event.get("reported_cost_usd") or 0)
        known_by_stage[event["stage"]] += charge
        holds_by_stage[event["stage"]] += max(Decimal(0), bound - charge)
        bodies = responses[event["id"], event.get("attempt_id")]
        if charge > bound or any(
            (
                isinstance(e["response"].get("usage"), dict)
                and e["response"]["usage"].get("is_byok")
                for _, e in bodies
            )
        ):
            unsafe_ids.add(event["id"])
    matched = set()
    for row in results.values():
        choices = [(k, r) for k, r in by_call[row["id"]] if k not in terminals]
        if row.get("attempt_id") is not None:
            choices = [
                (k, r) for k, r in choices if r.get("attempt_id") == row["attempt_id"]
            ]
            if len(choices) != 1:
                raise AccountingError("successful result has no unique live attempt")
        elif len(choices) > 1:
            raise AccountingError(
                "legacy logical result cannot resolve multiple attempts"
            )
        for key, reserve in choices:
            if (
                reserve["stage"] != row["stage"]
                or reserve["fingerprint"] != row["fingerprint"]
                or positions[key] >= result_positions[row["id"]]
            ):
                raise AccountingError("result differs from its reservation")
            matched.add(key)
    for key, reserve in reserves.items():
        if key not in terminals and key not in matched:
            pending.append(reserve)
    groups = _replay_groups(events)
    pending.extend(groups["group_pending"])
    return {
        **groups,
        "results": results,
        "reserves": reserves,
        "terminals": terminals,
        "pending": pending,
        "closed_ids": closed_ids,
        "unsafe_ids": unsafe_ids,
        "failed_calls": {
            e["id"]
            for e in terminals.values()
            if e["event"] == "attempt_failure"
            and e["id"] not in results
            and (e["id"] not in closed_ids)
        },
        "known_by_stage": dict(known_by_stage),
        "holds_by_stage": dict(holds_by_stage),
        "known_usd": sum(known_by_stage.values(), Decimal(0)),
        "holds_usd": sum(holds_by_stage.values(), Decimal(0)),
        "pending_usd": sum((money(r["upper_cost_usd"]) for r in pending), Decimal(0)),
    }


@dataclass(frozen=True)
class LedgerSnapshot:
    sha256: str
    byte_count: int
    events: list[dict[str, Any]] = dataclass_field(repr=False)
    state: dict[str, Any] = dataclass_field(repr=False)
    checksum_state: Any = dataclass_field(repr=False)

    def summary(self) -> dict[str, Any]:
        known, holds, pending = (
            self.state[name] for name in ("known_usd", "holds_usd", "pending_usd")
        )
        return {
            "sha256": self.sha256,
            "bytes": self.byte_count,
            "event_count": len(self.events),
            "event_types": dict(Counter(event["event"] for event in self.events)),
            "billed_result_records": len(self.state["results"]),
            "known_cost_usd": str(known),
            "retained_uncertainty_usd": str(holds),
            "pending_upper_bound_usd": str(pending),
            "conservative_committed_usd": str(known + holds + pending),
            "hard_cap_usd": str(HARD_CAP_USD),
            "ledger_only_remaining_usd": str(
                max(Decimal(0), HARD_CAP_USD - known - holds - pending)
            ),
            "unresolved_calls": sorted(
                {event["id"] for event in self.state["pending"]}
                | self.state["failed_calls"]
                | self.state["unsafe_ids"]
            ),
            "live_account_reconciled": False,
            "dispatch_authorized": False,
        }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AccountingError("journal has duplicate JSON keys")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise AccountingError("journal contains a non-finite JSON number")


def json_object(raw: bytes) -> dict[str, Any]:
    """Decode a finite, unambiguous JSON object without exposing its contents."""
    try:
        value = json.loads(
            raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, UnicodeError, AccountingError):
        raise AccountingError("invalid JSON object; contents suppressed") from None
    if not isinstance(value, dict):
        raise AccountingError("missing JSON object; contents suppressed")
    return value


def replay_history(
    events: list[dict[str, Any]],
    *,
    ledger_path: Path,
    expected_key_id: str | None = None,
) -> dict[str, Any]:
    """Validate binding and financial transitions without changing historical data."""
    binding = digest(str(ledger_path.resolve()))
    bound_key = expected_key_id
    origin_seen = False
    for event in events:
        if not isinstance(event, dict) or event.get("event") not in KNOWN_EVENTS:
            raise AccountingError("journal has an unknown or malformed event")
        key_id = event.get("key_id")
        if (
            not isinstance(key_id, str)
            or len(key_id) != 64
            or any(c not in "0123456789abcdef" for c in key_id)
        ):
            raise AccountingError("journal has an invalid credential binding")
        if bound_key is None:
            bound_key = key_id
        if key_id != bound_key:
            raise AccountingError("journal belongs to a different API key")
        if event["event"] == "origin":
            if origin_seen:
                raise AccountingError("journal contains multiple account origins")
            origin_seen = True
            money(event.get("usage_usd"))
        if event["event"] == "budget_group" and event.get("ledger_binding") != binding:
            raise AccountingError(
                "budget group belongs to a different original journal"
            )
    try:
        if any(event["event"] == "campaign_session" for event in events):
            from bench.campaign_chat import validate_campaign_events

            validate_campaign_events(events, ledger_path=ledger_path)
        return accounting(events)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AccountingError("journal financial record is malformed") from exc


def read_snapshot(path: Path, *, expected_key_id: str | None = None) -> LedgerSnapshot:
    """Read the complete original file under a shared lock; never create it.

    A credential-free offline audit may use the journal's existing key binding.
    Paid transport must supply its actual credential hash and independently
    reconcile live account usage. Ledger-only headroom is not spend permission.
    """
    try:
        with path.open("rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            return read_locked_snapshot(stream, path, expected_key_id=expected_key_id)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AccountingError(
            "cannot read and validate the complete original journal"
        ) from exc


def read_locked_snapshot(
    stream: Any,
    path: Path,
    *,
    expected_key_id: str | None = None,
    expected_prefix: tuple[str, int] | None = None,
) -> LedgerSnapshot:
    """Read an already locked existing descriptor; the caller owns its lock."""
    checksum = hashlib.sha256()
    byte_count = 0
    events = []
    stream.seek(0)
    before = os.fstat(stream.fileno())
    prefix_seen = expected_prefix is None or expected_prefix == (
        checksum.hexdigest(),
        0,
    )
    for line in stream:
        if not line.endswith(b"\n") or not line.strip():
            raise AccountingError("journal has an incomplete or empty record")
        event = json.loads(
            line, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if isinstance(event, dict) and event.get("event") == "campaign_session":
            if (
                event.get("prefix_sha256") != checksum.hexdigest()
                or event.get("prefix_bytes") != byte_count
            ):
                raise AccountingError(
                    "campaign enrollment differs from its original prefix"
                )
        checksum.update(line)
        byte_count += len(line)
        if expected_prefix is not None and byte_count == expected_prefix[1]:
            if checksum.hexdigest() != expected_prefix[0]:
                raise AccountingError("previously observed journal prefix was changed")
            prefix_seen = True
        events.append(event)
    if not prefix_seen:
        raise AccountingError("previously observed journal prefix was truncated")
    after, current = os.fstat(stream.fileno()), path.stat()

    def identity(value: os.stat_result) -> tuple[int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if identity(before) != identity(after) or identity(after) != identity(current):
        raise AccountingError("original journal changed during its read-only audit")
    state = replay_history(events, ledger_path=path, expected_key_id=expected_key_id)
    return LedgerSnapshot(checksum.hexdigest(), after.st_size, events, state, checksum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--expected-key-id")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output is not None and args.output.resolve() == args.ledger.resolve():
            raise AccountingError("audit output must differ from the original journal")
        snapshot = read_snapshot(args.ledger, expected_key_id=args.expected_key_id)
        if args.expected_sha256 is not None and snapshot.sha256 != args.expected_sha256:
            raise AccountingError(
                "original journal digest differs from the audit enrollment"
            )
        summary = snapshot.summary()
        if args.output is not None:
            # Explicit output is a separate audit artifact. Exclusive creation
            # cannot overwrite the original journal or another existing file.
            descriptor = os.open(
                args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as stream:
                json.dump(summary, stream, sort_keys=True, indent=2, allow_nan=False)
                stream.write("\n")
        print(json.dumps(summary, sort_keys=True, indent=2, allow_nan=False))
        return 0
    except (AccountingError, OSError) as exc:
        print(f"Journal audit failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # The structured validator imports this module by its package name. Use
    # that same module for CLI error handling rather than a second __main__
    # AccountingError class that would not catch its validation failures.
    from bench.campaign_accounting import main as run_cli

    raise SystemExit(run_cli())
