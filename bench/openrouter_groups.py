"""Opt-in source-first budget pairs in the original append-only pilot ledger.

This is an accounting prerequisite, not native unknown-capacity admission. No
failure authorizes a fallback. A slot's ordinary ``reserve`` row is its one claim
and dispatch authorization; a lost process after that row retains the allowance.
Only an unclaimed slot can be released, with the exact journal prefix as proof.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from bench import openrouter_pilot as pilot

POLICY = "protected-source-first-v1"
SLOTS = ("source", "candidate")


def ledger_binding(ledger):
    return pilot.digest(str(ledger.path.resolve()))


def validate_binding(ledger, events):
    if any(
        e["event"] == "budget_group"
        and e.get("ledger_binding") != ledger_binding(ledger)
        for e in events
    ):
        raise pilot.PilotError("budget group belongs to a different ledger")


def _request(payload):
    expected = pilot.make_request(
        payload["model"],
        payload["provider"]["only"],
        payload["messages"],
        max_tokens=payload["max_tokens"],
        allowed_models=(
            payload["plugins"][0]["allowed_models"]
            if payload["model"] == "openrouter/auto"
            else None
        ),
        response_format=payload.get("response_format"),
    )
    if pilot.canonical(expected) != pilot.canonical(payload):
        raise pilot.PilotError("group request changed frozen dispatch controls")
    if payload["model"] == "openrouter/auto":
        raise pilot.PilotError("protected groups require exact fixed models")


def validate_completion(claim, body, result):
    """Revalidate the exact source receipt before it can authorize its partner.

    Legacy result rows remain billing records under their existing rules. Only
    grouped completions have this stronger dispatch/replay contract.
    """
    choice = pilot.schema_text_choice(body)
    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise pilot.PilotError("group response lacks provider token accounting")
    cost = pilot.money(usage.get("cost"))
    if cost > pilot.money(claim["upper_cost_usd"]) or usage.get("is_byok"):
        raise pilot.PilotError("group response violates its billing contract")
    for field in ("prompt_tokens", "completion_tokens"):
        if type(usage.get(field)) is not int or usage[field] < 0:
            raise pilot.PilotError(
                "group response has invalid provider token accounting"
            )
    if (
        body.get("model") != claim["request"]["model"]
        or not isinstance(body.get("provider"), str)
        or not body["provider"]
    ):
        raise pilot.PilotError("group response differs from its fixed model/provider")
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
            or selected[0].get("model") != contract["endpoint_model"]
            or router.get("requested") != claim["request"]["model"]
            or type(router.get("attempt")) is not int
            or router["attempt"] != 1
            or router.get("is_byok") is not False
        ):
            raise pilot.PilotError("group response differs from its frozen endpoint")
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
        pilot.canonical({k: result.get(k) for k in expected})
        != pilot.canonical(expected)
        or pilot.money(result["cost_usd"]) != cost
    ):
        raise pilot.PilotError("group result differs from its durable response")


def replay(events):
    """Validate transitions in order and return only still-unclaimed liabilities."""
    groups, call_ids, used_ids = {}, {}, set()
    responses, completed = {}, set()
    for index, event in enumerate(events):
        kind = event["event"]
        if kind == "budget_group":
            key = pilot.digest(event)
            if (
                event.get("policy") != POLICY
                or event.get("id") in used_ids
                or not event.get("id")
                or not event.get("stage")
                or not event.get("ledger_binding")
                or key in groups
                or set(event.get("slots", {})) != set(SLOTS)
            ):
                raise pilot.PilotError("invalid or duplicate budget group")
            used_ids.add(event["id"])
            total = Decimal(0)
            for slot in SLOTS:
                spec = event["slots"][slot]
                if not spec.get("id") or spec["id"] in used_ids:
                    raise pilot.PilotError("budget group reuses a call identity")
                if spec.get("fingerprint") != pilot.digest(
                    {
                        "payload": spec["request"],
                        "metadata": spec["metadata"],
                        "stage": event["stage"],
                    }
                ):
                    raise pilot.PilotError("budget group request fingerprint differs")
                bound = pilot.money(spec["upper_cost_usd"])
                if bound <= 0:
                    raise pilot.PilotError("group slots need positive allowances")
                total += bound
                call_ids[spec["id"]] = (key, slot)
                used_ids.add(spec["id"])
            if total != pilot.money(event["upper_cost_usd"]) or total > pilot.money(
                event["stage_cap_usd"]
            ):
                raise pilot.PilotError("budget group combined allowance differs")
            groups[key] = {"reservation": event, "claimed": {}, "released": set()}
        elif kind == "reserve" and (
            event.get("group_sha256") or event["id"] in call_ids
        ):
            pair = call_ids.get(event["id"])
            if pair != (event.get("group_sha256"), event.get("group_slot")):
                raise pilot.PilotError("group slot claim differs from its reservation")
            key, slot = pair
            state = groups[key]
            spec = state["reservation"]["slots"][slot]
            if slot in state["claimed"] or slot in state["released"]:
                raise pilot.PilotError("budget group slot already claimed or released")
            if (
                not event.get("attempt_id")
                or event["stage"] != state["reservation"]["stage"]
                or pilot.canonical({k: event.get(k) for k in spec})
                != pilot.canonical(spec)
                or event["fingerprint"]
                != pilot.digest(
                    {
                        "payload": event["request"],
                        "metadata": event["metadata"],
                        "stage": event["stage"],
                    }
                )
            ):
                raise pilot.PilotError(
                    "group attempt changed its exact request or allowance"
                )
            if (
                slot == "candidate"
                and state["reservation"]["slots"]["source"]["id"] not in completed
            ):
                raise pilot.PilotError("candidate requires durable source completion")
            state["claimed"][slot] = event
        elif kind == "budget_group_release":
            key, slot = event.get("group_sha256"), event.get("group_slot")
            state = groups.get(key)
            if state is None or slot not in SLOTS:
                raise pilot.PilotError("release has no exact budget group slot")
            spec = state["reservation"]["slots"][slot]
            if (
                slot in state["claimed"]
                or slot in state["released"]
                or event.get("id") != spec["id"]
                or event.get("stage") != state["reservation"]["stage"]
                or event.get("upper_cost_usd") != spec["upper_cost_usd"]
                or event.get("unclaimed_prefix_sha256") != pilot.digest(events[:index])
                or not event.get("reason")
            ):
                raise pilot.PilotError("release lacks durable never-claimed proof")
            state["released"].add(slot)
        elif kind.startswith("budget_group"):
            raise pilot.PilotError("unknown budget group event")
        elif event.get("id") in call_ids:
            key, slot = call_ids[event["id"]]
            claim = groups[key]["claimed"].get(slot)
            if claim is None or event.get("attempt_id") != claim["attempt_id"]:
                raise pilot.PilotError("group outcome has no exact claimed attempt")
            if kind == "response":
                if event["id"] in responses:
                    raise pilot.PilotError("group attempt has duplicate responses")
                responses[event["id"]] = event["response"]
            elif kind == "result":
                body = responses.get(event["id"])
                if body is None:
                    raise pilot.PilotError("group result lacks a durable response")
                validate_completion(claim, body, event)
                completed.add(event["id"])
        else:
            if event.get("group_sha256") or event.get("group_slot"):
                raise pilot.PilotError("orphan budget group reference")
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


def _receipt(ledger, state, receipt, slot):
    if slot not in SLOTS or receipt.get("ledger_binding") != ledger_binding(ledger):
        raise pilot.PilotError("wrong budget group slot or ledger")
    if receipt.get("key_id") != ledger.key_id:
        raise pilot.PilotError("budget group belongs to a different key")
    key = pilot.digest(receipt)
    group = state["groups"].get(key)
    if group is None:
        raise pilot.PilotError("budget group receipt differs from durable reservation")
    return key, group, group["reservation"]["slots"][slot]


def authorize(ledger, state, receipt, slot, call_id, stage, stage_cap, fingerprint):
    key, group, spec = _receipt(ledger, state, receipt, slot)
    if (
        spec["id"] != call_id
        or spec["fingerprint"] != fingerprint
        or receipt["stage"] != stage
        or pilot.money(receipt["stage_cap_usd"]) != stage_cap
    ):
        raise pilot.PilotError("dispatch differs from exact budget group slot")
    if slot in group["released"]:
        raise pilot.PilotError("released budget group slot cannot dispatch")
    return {"group_sha256": key, "group_slot": slot}


def ready(state, fields):
    key, slot = fields["group_sha256"], fields["group_slot"]
    group = state["groups"][key]
    if slot in group["claimed"]:
        raise pilot.PilotError("claimed budget group slot cannot dispatch again")
    if (
        any(
            e["event"] != "budget_group_slot" or e["group_sha256"] != key
            for e in state["pending"]
        )
        or state["failed_calls"]
        or state["unsafe_ids"]
    ):
        raise pilot.PilotError("unresolved paid request blocks budget group dispatch")
    if (
        slot == "candidate"
        and group["reservation"]["slots"]["source"]["id"] not in state["results"]
    ):
        raise pilot.PilotError("candidate requires durable source completion")


def reserve_pair(ledger, key, group_id, stage, stage_cap, *, source, candidate):
    """Atomically reserve both exact calls. Specs contain id, payload, metadata.

    Account checks are read-only preflight; tests supply fake account responses.
    There is no CLI integration or automatic release on exception/timeout.
    """
    from bench.openrouter_attempts import accounting

    if hashlib.sha256(key.encode()).hexdigest() != ledger.key_id:
        raise pilot.PilotError("dispatch key differs from ledger key")
    stage_cap = pilot.money(stage_cap)
    if not group_id or not stage or not 0 < stage_cap <= pilot.HARD_CAP:
        raise pilot.PilotError("invalid budget group identity or ceiling")
    slots = {}
    for slot, call in (("source", source), ("candidate", candidate)):
        _request(call["payload"])
        slots[slot] = {
            "id": call["id"],
            "request": call["payload"],
            "metadata": call["metadata"],
            "fingerprint": pilot.digest(
                {
                    "payload": call["payload"],
                    "metadata": call["metadata"],
                    "stage": stage,
                }
            ),
            "upper_cost_usd": str(pilot.upper_cost(call["payload"])),
        }
    # Detach the durable plan from caller-owned mutable request objects.
    slots = json.loads(pilot.canonical(slots))
    bound = sum((pilot.money(s["upper_cost_usd"]) for s in slots.values()), Decimal(0))
    proposed = {
        "event": "budget_group",
        "id": group_id,
        "policy": POLICY,
        "stage": stage,
        "stage_cap_usd": str(stage_cap),
        "upper_cost_usd": str(bound),
        "slots": slots,
        "ledger_binding": ledger_binding(ledger),
    }
    with ledger.locked() as handle:
        events = pilot.Ledger.read(ledger, handle)
        state = accounting(events)
        # Validate identities before any account query or durable append.
        replay(events + [proposed])
        if state["pending"] or state["failed_calls"] or state["unsafe_ids"]:
            raise pilot.PilotError(
                "unresolved paid request blocks budget group reservation"
            )
        total = state["known_usd"] + state["holds_usd"]
        stage_total = state["known_by_stage"].get(stage, Decimal(0)) + state[
            "holds_by_stage"
        ].get(stage, Decimal(0))
        if total + bound > pilot.HARD_CAP or stage_total + bound > stage_cap:
            raise pilot.PilotError("budget group exceeds the reserved spending limit")
        before = pilot.account(key)
        usage = pilot.money(before["usage"])
        origins = [e for e in events if e["event"] == "origin"]
        baseline = pilot.money(origins[0]["usage_usd"]) if origins else usage
        if (
            usage < baseline
            or max(total, usage - baseline + state["holds_usd"]) + bound
            > pilot.HARD_CAP
            or (
                before.get("limit_remaining") is not None
                and pilot.money(before["limit_remaining"]) < bound
            )
        ):
            raise pilot.PilotError(
                "account budget is below the combined group allowance"
            )
        if not origins:
            ledger.append(handle, {"event": "origin", "usage_usd": str(usage)})
        return ledger.append(
            handle, {**proposed, "account_usage_before_usd": str(usage)}
        )


def call_slot(ledger, key, receipt, slot):
    """Dispatch one frozen slot; only completed results can be freely replayed."""
    if slot not in SLOTS:
        raise pilot.PilotError("unknown budget group slot")
    receipt = json.loads(pilot.canonical(receipt))
    spec = receipt["slots"][slot]
    return ledger.call(
        key,
        spec["id"],
        receipt["stage"],
        pilot.money(receipt["stage_cap_usd"]),
        spec["request"],
        spec["metadata"],
        _group=(receipt, slot),
    )


def release_unused(ledger, receipt, slot, reason):
    """Release only a slot with no durable claim, under the ledger's same lock."""
    from bench.openrouter_attempts import accounting

    with ledger.locked() as handle:
        events = pilot.Ledger.read(ledger, handle)
        key, group, spec = _receipt(ledger, accounting(events), receipt, slot)
        if slot in group["claimed"] or slot in group["released"]:
            raise pilot.PilotError(
                "cannot release a claimed or already released group slot"
            )
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise pilot.PilotError("release requires a bounded reason")
        return ledger.append(
            handle,
            {
                "event": "budget_group_release",
                "group_sha256": key,
                "group_slot": slot,
                "id": spec["id"],
                "stage": receipt["stage"],
                "upper_cost_usd": spec["upper_cost_usd"],
                "reason": reason,
                "unclaimed_prefix_sha256": pilot.digest(events),
            },
        )
