"""One explicitly witnessed HTTP429 retry, retaining its full spending bound.

This is a recovery overlay, not a change to the frozen acquisition. It never
invents a provider response or deletes ledger records. Even when account usage
shows no extra charge, the failed attempt's entire reservation remains charged
against both budget ceilings. It is not claimed as an exact zero-cost failure.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from bench import openrouter_pilot as pilot
from bench import openrouter_rules_live as live
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_pilot import Ledger, PilotError, digest, load_key, money

CALL_ID = "rules-live-dev-v1-d3e81e86aafc129930be-d238bb46ad07bbad4318b7ba"
STAGE = "rules-live-dev-v1-d3e81e86aafc129930be"
EVENT = "reviewed_http429_retry_with_budget_hold"
DOC_URL = "https://openrouter.ai/docs/api_reference/errors-and-debugging"


def now():
    return datetime.now(timezone.utc)


def observation(events, current_account, observed_at):
    done = {e["id"]: e for e in events if e["event"] == "result"}
    pending = [e for e in events if e["event"] == "reserve" and e["id"] not in done]
    if len(pending) != 1 or pending[0]["id"] != CALL_ID or pending[0]["stage"] != STAGE:
        raise PilotError("recovery is restricted to the one witnessed HTTP429 reservation")
    reserve = pending[0]
    if any(e["event"] in {"response", EVENT} and e.get("id") == CALL_ID for e in events):
        raise PilotError("response/recovery already exists; no automatic reconciliation")
    origins = [e for e in events if e["event"] == "origin"]
    if len(origins) != 1:
        raise PilotError("recovery requires one campaign account origin")
    spent = sum((money(e["cost_usd"]) for e in done.values()), Decimal(0))
    expected = money(origins[0]["usage_usd"]) + spent
    if money(current_account["usage"]) != expected or money(current_account["byok_usage"]) != 0:
        raise PilotError("account usage has not settled to all known charges")
    age = (observed_at - datetime.fromisoformat(reserve["at"])).total_seconds()
    if age < 120:
        raise PilotError("wait at least 120 seconds after the witnessed rate limit")
    return {"observed_at": observed_at.isoformat(), "id": CALL_ID, "stage": STAGE,
        "reserve_sha256": digest(reserve), "ledger_sha256": digest(events), "ledger_events": len(events),
        "known_completed_calls": len(done), "known_spent_usd": str(spent), "account_usage_usd": str(expected),
        "no_extra_billed_usage_observed": True, "maximum_unobserved_failure_cost_usd": reserve["upper_cost_usd"],
        "failed_attempt_status_witness": "root exec session71957 returned non-streaming HTTP429 after call60; response body was suppressed",
        "error_body_and_retry_after_available": False, "official_retry_reference": DOC_URL,
        "limitations": "Aggregate accounting cannot prove a particular failure cost zero; retain the entire reservation bound."}


def validate_observations(first, second):
    if {k: v for k, v in first.items() if k != "observed_at"} != {k: v for k, v in second.items() if k != "observed_at"}:
        raise PilotError("ledger or accounting changed between recovery observations")
    gap = (datetime.fromisoformat(second["observed_at"]) - datetime.fromisoformat(first["observed_at"])).total_seconds()
    if gap < 60:
        raise PilotError("settled recovery observations must be at least 60 seconds apart")


def receipt_from(events):
    receipts = [e for e in events if e["event"] == EVENT]
    if len(receipts) != 1:
        raise PilotError("one reviewed recovery receipt is required")
    receipt = receipts[0]
    if (receipt["id"] != CALL_ID or receipt["stage"] != STAGE
            or receipt["recovery_source_sha256"] != file_hash(Path(__file__))):
        raise PilotError("recovery receipt identity or source changed")
    validate_observations(receipt["first_observation"], receipt["second_observation"])
    index = events.index(receipt)
    before = events[:index]
    reserves = [e for e in before if e["event"] == "reserve" and e["id"] == CALL_ID]
    if (len(reserves) != 1 or digest(reserves[0]) != receipt["reserve_sha256"]
            or digest(before) != receipt["second_observation"]["ledger_sha256"]
            or receipt["reserve_sha256"] != receipt["second_observation"]["reserve_sha256"]
            or money(receipt["budget_hold_usd"]) != money(reserves[0]["upper_cost_usd"])):
        raise PilotError("recovery receipt is not bound to the exact failed reservation")
    return receipt


class RecoveryLedger(Ledger):
    def read(self, handle):
        events = super().read(handle)
        receipt = receipt_from(events)
        # Resolve only its concurrency block, never its conservative spend.
        return [e for e in events if not (e["event"] == "reserve" and digest(e) == receipt["reserve_sha256"])]

    def call(self, key, call_id, stage, stage_cap, payload, metadata):
        with self.locked() as handle:
            events = super().read(handle)
            receipt = receipt_from(events)
        if stage != STAGE:
            raise PilotError("recovery overlay cannot dispatch an unrelated stage")
        if call_id == CALL_ID:
            original = next(e for e in events if e["event"] == "reserve" and digest(e) == receipt["reserve_sha256"])
            if original["fingerprint"] != digest({"payload": payload, "metadata": metadata, "stage": stage}):
                raise PilotError("retry differs from the exact failed request")
        hold = money(receipt["budget_hold_usd"])
        original_cap = pilot.HARD_CAP
        if not 0 < hold < stage_cap <= original_cap:
            raise PilotError("invalid conservative recovery allowance")
        # The underlying dispatcher still checks every payload, endpoint,
        # fingerprint, account limit and reservation under its original lock.
        # This driver is single-threaded; global state is restored on every exit.
        try:
            pilot.HARD_CAP = original_cap - hold
            return super().call(key, call_id, stage, stage_cap-hold, payload, metadata)
        finally:
            pilot.HARD_CAP = original_cap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("observe", "record-reviewed-retry", "run"))
    for name in ("env-file", "ledger", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("fixture", "native"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    try:
        key = load_key(args.env_file)
        ledger = Ledger(args.ledger, key)
        recovery = args.output / "recovery"
        if args.command in {"observe", "record-reviewed-retry"}:
            with ledger.locked() as handle:
                events = ledger.read(handle)
                sample = observation(events, pilot.account(key), now())
                if args.command == "observe":
                    write_json(recovery / "first-observation.json", sample, immutable=True)
                    print(json.dumps(sample, indent=2))
                    return 0
                first = json.loads((recovery / "first-observation.json").read_text())
                validate_observations(first, sample)
                receipt = {"event": EVENT, "id": CALL_ID, "stage": STAGE,
                    "reserve_sha256": sample["reserve_sha256"], "budget_hold_usd": sample["maximum_unobserved_failure_cost_usd"],
                    "first_observation": first, "second_observation": sample,
                    "recovery_source_sha256": file_hash(Path(__file__))}
                ledger.append(handle, receipt)
                write_json(recovery / "receipt.json", receipt, immutable=True)
                print(json.dumps({"recorded_retry_receipt": True, "budget_hold_usd": receipt["budget_hold_usd"]}))
                return 0
        if args.fixture is None or args.native is None:
            raise PilotError("recovery run requires fixture and native")
        with ledger.locked() as handle:
            receipt = receipt_from(ledger.read(handle))
        saved = live.Ledger
        try:
            live.Ledger = RecoveryLedger
            result = live.run(args, key)
        finally:
            live.Ledger = saved
            # Even a second HTTP429 or a reservation-cap stop preserves this
            # sidecar and the underlying raw ledger; neither is paper success.
            status = RecoveryLedger(args.ledger, key).summary()
            status["retained_failed_attempt_allowance_usd"] = receipt["budget_hold_usd"]
            status["conservative_campaign_total_usd"] = str(money(status["spent_usd"]) + money(receipt["budget_hold_usd"]))
            write_json(recovery / "status.json", status)
        print(json.dumps({"schedule_complete": result["schedule_complete"], "completed_calls": result["completed_calls"],
                          "stage_known_billed_usd": result["cost_usd"], "recovery": status}, indent=2))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Workflow recovery stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
