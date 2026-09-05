"""Append-only attempt accounting shared by all paid pilot drivers.

An unsuccessful attempt is never a successful model observation. Its full
reservation remains committed (known charge plus uncertainty) until separately
reconciled. This module provides no operation that releases an allowance.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from bench.openrouter_pilot import PilotError, digest, money

LEGACY_HOLD = "reviewed_http429_retry_with_budget_hold"
TERMINALS = {LEGACY_HOLD, "attempt_failure", "attempt_abandoned"}


def accounting(events):
    reserves = {digest(e): e for e in events if e["event"] == "reserve"}
    if len(reserves) != sum(e["event"] == "reserve" for e in events):
        raise PilotError("duplicate reservation record")
    by_call, responses, positions = defaultdict(list), defaultdict(list), {}
    attempt_ids, result_positions = set(), {}
    for index, event in enumerate(events):
        if event["event"] == "reserve":
            key = digest(event)
            by_call[event["id"]].append((key, event))
            positions[key] = index
            attempt_id = event.get("attempt_id")
            if attempt_id is not None:
                if not attempt_id or attempt_id in attempt_ids:
                    raise PilotError("duplicate or empty attempt identity")
                attempt_ids.add(attempt_id)
        elif event["event"] == "response":
            responses[(event["id"], event.get("attempt_id"))].append((index, event))
    terminals, results, pending = {}, {}, []
    known_by_stage, holds_by_stage = defaultdict(Decimal), defaultdict(Decimal)
    closed_ids = set()
    for index, event in enumerate(events):
        kind = event["event"]
        if kind in TERMINALS:
            key = event["reserve_sha256"]
            reserve = reserves.get(key)
            previous = terminals.get(key)
            if reserve is None or positions[key] >= index:
                raise PilotError("failure allowance has no unique exact reservation")
            if previous is not None and not (
                previous["event"] == "attempt_failure"
                and kind == "attempt_abandoned"
                and previous.get("reported_cost_usd") == event.get("reported_cost_usd")
            ):
                raise PilotError("failure allowance has no unique exact reservation")
            if any(event.get(k) != reserve.get(k) for k in ("id", "stage")):
                raise PilotError("failure allowance identity differs from reservation")
            if kind != LEGACY_HOLD and event.get("attempt_id") != reserve.get(
                "attempt_id"
            ):
                raise PilotError("failure allowance attempt differs from reservation")
            bound = money(reserve["upper_cost_usd"])
            if money(event["budget_hold_usd"]) != bound:
                raise PilotError(
                    "failure allowance must retain the entire reserved bound"
                )
            charge = Decimal(0)
            if event.get("reported_cost_usd") is not None:
                charge = money(event["reported_cost_usd"])
                bodies = [
                    e
                    for position, e in responses[(event["id"], event.get("attempt_id"))]
                    if positions[key] < position < index
                ]
                if (
                    len(bodies) != 1
                    or money(bodies[0]["response"].get("usage", {}).get("cost"))
                    != charge
                ):
                    raise PilotError(
                        "failed-attempt charge is not bound to its response"
                    )
            terminals[key] = event
            if kind == "attempt_abandoned":
                closed_ids.add(event["id"])
        elif kind == "result":
            # Historical result rows are billing records even when an old driver
            # incorrectly marked partial provider output successful. Replay has
            # its own transport-validity gate; never erase their billed cost.
            if event["id"] in results:
                raise PilotError("multiple results for one logical call")
            results[event["id"]] = event
            result_positions[event["id"]] = index
            known_by_stage[event["stage"]] += money(event["cost_usd"])
    # Abandonment changes retry eligibility, never the financial commitment.
    unsafe_ids = set()
    for key, event in terminals.items():
        bound = money(reserves[key]["upper_cost_usd"])
        charge = money(event.get("reported_cost_usd") or 0)
        known_by_stage[event["stage"]] += charge
        holds_by_stage[event["stage"]] += max(Decimal(0), bound - charge)
        bodies = responses[(event["id"], event.get("attempt_id"))]
        if charge > bound or any(
            isinstance(e["response"].get("usage"), dict)
            and e["response"]["usage"].get("is_byok")
            for _, e in bodies
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
                raise PilotError("successful result has no unique live attempt")
        elif len(choices) > 1:
            raise PilotError("legacy logical result cannot resolve multiple attempts")
        for key, reserve in choices:
            if (
                reserve["stage"] != row["stage"]
                or reserve["fingerprint"] != row["fingerprint"]
                or positions[key] >= result_positions[row["id"]]
            ):
                raise PilotError("result differs from its reservation")
            matched.add(key)
    for key, reserve in reserves.items():
        if key not in terminals and key not in matched:
            pending.append(reserve)
    return {
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
            and e["id"] not in closed_ids
        },
        "known_by_stage": dict(known_by_stage),
        "holds_by_stage": dict(holds_by_stage),
        "known_usd": sum(known_by_stage.values(), Decimal(0)),
        "holds_usd": sum(holds_by_stage.values(), Decimal(0)),
        "pending_usd": sum((money(r["upper_cost_usd"]) for r in pending), Decimal(0)),
    }
