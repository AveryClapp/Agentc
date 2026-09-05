import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bench import openrouter_pilot as pilot
from bench import openrouter_rules_recovery as recovery
from bench.openrouter_matrix import file_hash
from bench.openrouter_pilot import Ledger, PilotError, digest, make_request, upper_cost


def raw_events():
    payload = make_request("model", ["provider"], [{"role": "user", "content": "text"}], max_tokens=16)
    metadata = {}
    return [
        {"event": "origin", "usage_usd": "0"},
        {"event": "result", "id": "prior", "stage": "old", "cost_usd": ".3"},
        {"event": "reserve", "id": recovery.CALL_ID, "stage": recovery.STAGE,
            "at": (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(),
            "upper_cost_usd": str(upper_cost(payload)), "request": payload, "metadata": metadata,
            "fingerprint": digest({"payload": payload, "metadata": metadata, "stage": recovery.STAGE})}]


def samples(events):
    second_at = datetime.now(timezone.utc)
    account = {"usage": ".3", "byok_usage": 0}
    return (recovery.observation(events, account, second_at-timedelta(seconds=61)),
            recovery.observation(events, account, second_at))


def receipt(events):
    first, second = samples(events)
    return {"event": recovery.EVENT, "id": recovery.CALL_ID, "stage": recovery.STAGE,
        "first_observation": first, "second_observation": second,
        "reserve_sha256": second["reserve_sha256"], "budget_hold_usd": events[-1]["upper_cost_usd"],
        "recovery_source_sha256": file_hash(recovery.Path(recovery.__file__))}


def test_observations_do_not_claim_exact_zero_cost_and_keep_whole_bound():
    events = raw_events()
    a, b = samples(events)
    recovery.validate_observations(a, b)
    assert a["maximum_unobserved_failure_cost_usd"] == events[-1]["upper_cost_usd"]
    assert "cannot prove" in a["limitations"]


@pytest.mark.parametrize("mutation", ["different_id", "multiple_pending", "response", "usage_lag", "extra_charge", "byok", "too_soon"])
def test_unproven_or_unrelated_recovery_fails_closed(mutation):
    events = raw_events()
    at = datetime.now(timezone.utc)
    account = {"usage": ".3", "byok_usage": 0}
    if mutation == "different_id":
        events[-1]["id"] = "unrelated"
    elif mutation == "multiple_pending":
        events.append({**events[-1], "id": "other"})
    elif mutation == "response":
        events.append({"event": "response", "id": recovery.CALL_ID})
    elif mutation == "usage_lag":
        account["usage"] = ".29"
    elif mutation == "extra_charge":
        account["usage"] = ".31"
    elif mutation == "byok":
        account["byok_usage"] = ".001"
    else:
        events[-1]["at"] = at.isoformat()
    with pytest.raises(PilotError):
        recovery.observation(events, account, at)


def test_two_snapshots_must_be_separated_and_identical_except_time():
    a, b = samples(raw_events())
    with pytest.raises(PilotError, match="60 seconds"):
        recovery.validate_observations(a, a)
    b["ledger_sha256"] = "changed"
    with pytest.raises(PilotError, match="changed"):
        recovery.validate_observations(a, b)


@pytest.mark.parametrize("mutation", ["hold", "source", "reserve", "extra_event"])
def test_receipt_cannot_weaken_allowance_or_rebind_history(mutation):
    events = raw_events()
    r = receipt(events)
    if mutation == "hold":
        r["budget_hold_usd"] = "0"
    elif mutation == "source":
        r["recovery_source_sha256"] = "changed"
    elif mutation == "reserve":
        events[-1]["upper_cost_usd"] = ".001"
    else:
        events.append({"event": "unreviewed"})
    with pytest.raises(PilotError):
        recovery.receipt_from(events + [r])


def prepare_ledger(tmp_path):
    events = raw_events()
    key_id = hashlib.sha256(b"fake-key").hexdigest()
    events = [{**e, "key_id": key_id} for e in events]
    r = receipt(events)
    path = tmp_path / "ledger.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    ledger = Ledger(path, "fake-key")
    with ledger.locked() as handle:
        ledger.append(handle, r)
    return ledger, recovery.RecoveryLedger(path, "fake-key"), events, r


def test_overlay_preserves_raw_reserve_and_retries_identical_payload_once(tmp_path, monkeypatch):
    base, overlay, events, r = prepare_ledger(tmp_path)
    assert base.summary()["unresolved_calls"] == [recovery.CALL_ID]
    assert overlay.summary()["unresolved_calls"] == []
    monkeypatch.setattr(pilot, "account", lambda _: {"usage": ".3", "limit_remaining": None})
    requests = []
    def respond(path, key, payload):
        requests.append(payload)
        return {"usage": {"cost": .001, "prompt_tokens": 2, "completion_tokens": 1},
            "model": "model", "provider": "provider", "id": "generation-retry",
            "choices": [{"message": {"content": "reply"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(pilot, "request_json", respond)
    args = ("fake-key", recovery.CALL_ID, recovery.STAGE, Decimal("1"), events[-1]["request"], {})
    result = overlay.call(*args)
    replayed = overlay.call(*args)
    assert {k: v for k, v in replayed.items() if k not in {"at", "key_id"}} == result
    assert requests == [events[-1]["request"]]
    assert pilot.HARD_CAP == Decimal("50")
    with base.locked() as handle:
        raw = base.read(handle)
    assert sum(e["event"] == "reserve" and e["id"] == recovery.CALL_ID for e in raw) == 2
    assert sum(e["event"] == "result" and e["id"] == recovery.CALL_ID for e in raw) == 1
    assert recovery.receipt_from(raw)["budget_hold_usd"] == r["budget_hold_usd"]


def test_both_caps_reduced_by_full_hold_and_restored_after_error(tmp_path, monkeypatch):
    base, overlay, events, r = prepare_ledger(tmp_path)
    seen = []
    def check(self, key, call_id, stage, stage_cap, payload, metadata):
        seen.append((stage_cap, pilot.HARD_CAP))
        raise PilotError("simulated provider failure")
    monkeypatch.setattr(Ledger, "call", check)
    with pytest.raises(PilotError, match="simulated"):
        overlay.call("fake-key", recovery.CALL_ID, recovery.STAGE, Decimal("1"), events[-1]["request"], {})
    hold = Decimal(r["budget_hold_usd"])
    assert seen == [(Decimal("1")-hold, Decimal("50")-hold)]
    assert pilot.HARD_CAP == Decimal("50")


def test_second_unresolved_attempt_is_not_automatically_cleared(tmp_path):
    base, overlay, events, r = prepare_ledger(tmp_path)
    with base.locked() as handle:
        base.append(handle, {k: v for k, v in events[-1].items() if k not in {"at", "key_id"}})
    assert overlay.summary()["unresolved_calls"] == [recovery.CALL_ID]
