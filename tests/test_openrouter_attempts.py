"""No-network fault injection for durable attempts and shared spending holds."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import format_datetime
from io import BytesIO
import json
import time
from urllib.error import HTTPError

import pytest

from bench import openrouter_pilot as pilot
from bench.openrouter_attempts import accounting, LEGACY_HOLD


def response(cost="0.001"):
    return {
        "id": "generation",
        "model": "test/model",
        "provider": "Test",
        "usage": {"cost": cost, "prompt_tokens": 15, "completion_tokens": 1},
        "choices": [{"message": {"content": "4"}, "finish_reason": "stop"}],
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    ledger = pilot.Ledger(tmp_path / "ledger.jsonl", "fake-key")
    payload = pilot.make_request(
        "test/model", ["test"], [{"role": "user", "content": "2+2?"}], max_tokens=16
    )
    monkeypatch.setattr(pilot, "account", lambda _: {"usage": 0})

    def call(call_id="one", stage="stage", cap=Decimal("1"), metadata=None):
        return ledger.call("fake-key", call_id, stage, cap, payload, metadata or {})

    def events():
        with ledger.locked() as handle:
            return ledger.read(handle)

    return ledger, payload, call, events


def fail(monkeypatch, *, retryable=True, reported=None):
    def request(*_):
        if reported is not None:
            value = response(reported)
            value["choices"][0].update(finish_reason="error", error={"code": 503})
            return value
        raise pilot.ProviderFailure(
            "safe failure", {"kind": "timeout", "retryable": retryable}
        )

    monkeypatch.setattr(pilot, "request_json", request)


def allow_retry(monkeypatch, events):
    retry_at = events()[-1]["retry_not_before_epoch"]
    monkeypatch.setattr(pilot.time, "time", lambda: retry_at + 1)


def test_failed_attempt_is_not_a_result_and_retains_full_allowance(setup, monkeypatch):
    ledger, payload, call, events = setup
    fail(monkeypatch)
    with pytest.raises(pilot.ProviderFailure):
        call()
    rows = events()
    assert [r["event"] for r in rows] == ["origin", "reserve", "attempt_failure"]
    assert rows[-1]["reserve_sha256"] == pilot.digest(rows[-2])
    assert rows[-1]["attempt_id"] == rows[-2]["attempt_id"]
    summary = ledger.summary()
    assert summary["completed_calls"] == 0
    assert Decimal(summary["retained_uncertainty_usd"]) == pilot.upper_cost(payload)
    assert summary["unresolved_calls"] == ["one"]
    with pytest.raises(pilot.PilotError, match="unresolved"):
        call("two", stage="new")


def test_exact_retry_has_new_identity_and_cached_replay_ignores_failed_body(
    setup, monkeypatch
):
    ledger, payload, call, events = setup
    fail(monkeypatch, reported="0.0002")
    with pytest.raises(pilot.ProviderFailure):
        call()
    with pytest.raises(pilot.ProviderFailure, match="backoff"):
        call()
    allow_retry(monkeypatch, events)
    monkeypatch.setattr(pilot, "request_json", lambda *_: response())
    result = call()

    def no_network(*_):
        pytest.fail("successful replay must be free")

    monkeypatch.setattr(pilot, "request_json", no_network)
    monkeypatch.setattr(pilot, "account", no_network)
    assert call()["attempt_id"] == result["attempt_id"]
    reserves = [e for e in events() if e["event"] == "reserve"]
    assert len(reserves) == 2 and reserves[0]["attempt_id"] != reserves[1]["attempt_id"]
    state = accounting(events())
    assert state["known_usd"] == Decimal(".0012")
    assert state["holds_usd"] == pilot.upper_cost(payload) - Decimal(".0002")
    assert ledger.summary()["unresolved_calls"] == []


@pytest.mark.parametrize("change", ["metadata", "stage"])
def test_retry_cannot_change_frozen_identity(setup, monkeypatch, change):
    _, _, call, events = setup
    fail(monkeypatch)
    with pytest.raises(pilot.ProviderFailure):
        call()
    allow_retry(monkeypatch, events)
    with pytest.raises(pilot.PilotError, match="exact failed"):
        call(**{change: {"changed": True} if change == "metadata" else "other"})
    assert len([e for e in events() if e["event"] == "reserve"]) == 1


def test_repeated_transient_failure_exhausts_two_attempts(setup, monkeypatch):
    ledger, payload, call, events = setup
    fail(monkeypatch)
    for _ in range(2):
        with pytest.raises(pilot.ProviderFailure):
            call()
        allow_retry(monkeypatch, events)
    with pytest.raises(pilot.PilotError, match="exhausted"):
        call()
    assert Decimal(
        ledger.summary()["retained_uncertainty_usd"]
    ) == 2 * pilot.upper_cost(payload)


def test_nonretryable_and_cancelled_attempts_require_review(setup, monkeypatch):
    ledger, _, call, events = setup

    def cancel(*_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(pilot, "request_json", cancel)
    with pytest.raises(KeyboardInterrupt):
        call()
    assert events()[-1]["failure"] == {"kind": "cancelled", "retryable": False}
    assert ledger.summary()["completed_calls"] == 0
    with pytest.raises(pilot.PilotError, match="explicit review"):
        call()


def age_attempt(events):
    rows = deepcopy(events)
    reserve = next(r for r in rows if r["event"] == "reserve")
    reserve["at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    for row in rows:
        if "reserve_sha256" in row:
            row["reserve_sha256"] = pilot.digest(reserve)
    return rows, reserve


def replace_fixture(ledger, rows):
    # Test fixture only. Production ledgers are strictly append-only.
    ledger.path.write_text("".join(json.dumps(e) + "\n" for e in rows))


@pytest.mark.parametrize("terminal", [False, True])
def test_explicit_abandonment_keeps_money_and_never_fabricates_result(
    setup, monkeypatch, terminal
):
    ledger, payload, call, events = setup
    fail(monkeypatch, reported=".0002" if terminal else None)
    with pytest.raises(pilot.ProviderFailure):
        call()
    rows, reserve = age_attempt(events())
    if not terminal:
        rows = [e for e in rows if e["event"] != "attempt_failure"]
    replace_fixture(ledger, rows)
    receipt = ledger.abandon(
        pilot.digest(reserve),
        pilot.digest(rows),
        "Stopped process; retain full unresolved bound",
    )
    summary = ledger.summary()
    assert receipt["event"] == "attempt_abandoned"
    assert summary["completed_calls"] == 0 and summary["unresolved_calls"] == []
    assert Decimal(summary["conservative_committed_usd"]) == pilot.upper_cost(payload)
    with pytest.raises(pilot.PilotError, match="abandoned"):
        call()
    with pytest.raises(pilot.PilotError):
        ledger.abandon(pilot.digest(reserve), pilot.digest(events()), "duplicate")


def test_abandonment_requires_exact_reviewed_digest_and_expired_deadline(
    setup, monkeypatch
):
    ledger, _, call, events = setup
    fail(monkeypatch)
    with pytest.raises(pilot.ProviderFailure):
        call()
    rows = events()
    reserve = next(e for e in rows if e["event"] == "reserve")
    with pytest.raises(pilot.PilotError, match="ledger changed"):
        ledger.abandon(pilot.digest(reserve), "wrong", "test")
    with pytest.raises(pilot.PilotError, match="deadline"):
        ledger.abandon(pilot.digest(reserve), pilot.digest(rows), "test")
    assert len(events()) == len(rows)


@pytest.mark.parametrize("cap_kind", ["stage", "campaign"])
def test_retry_reservation_counts_old_full_hold_against_each_ceiling(
    setup, monkeypatch, cap_kind
):
    ledger, payload, call, events = setup
    fail(monkeypatch)
    with pytest.raises(pilot.ProviderFailure):
        call()
    allow_retry(monkeypatch, events)
    ceiling = pilot.upper_cost(payload) * Decimal("1.5")
    if cap_kind == "campaign":
        monkeypatch.setattr(pilot, "HARD_CAP", ceiling)
    with pytest.raises(pilot.PilotError, match="spending limit"):
        call(cap=ceiling)
    assert len([e for e in events() if e["event"] == "reserve"]) == 1


def test_later_plain_ledger_dispatch_cannot_lose_legacy_allowance(setup, monkeypatch):
    ledger, payload, call, events = setup
    bound = pilot.upper_cost(payload)
    with ledger.locked() as handle:
        reserve = ledger.append(
            handle,
            {
                "event": "reserve",
                "id": "historical",
                "stage": "old",
                "fingerprint": "old",
                "upper_cost_usd": str(bound),
            },
        )
        ledger.append(
            handle,
            {
                "event": LEGACY_HOLD,
                "id": "historical",
                "stage": "old",
                "reserve_sha256": pilot.digest(reserve),
                "budget_hold_usd": str(bound),
            },
        )
    monkeypatch.setattr(pilot, "HARD_CAP", bound * Decimal("1.5"))
    with pytest.raises(pilot.PilotError, match="spending limit"):
        call(cap=pilot.HARD_CAP)
    assert ledger.summary()["completed_calls"] == 0


@pytest.mark.parametrize("problem", ["above_bound", "byok"])
def test_out_of_contract_charges_cannot_be_abandoned_or_skipped(
    setup, monkeypatch, problem
):
    ledger, payload, call, events = setup
    value = response(
        str(pilot.upper_cost(payload) + 1) if problem == "above_bound" else ".001"
    )
    if problem == "byok":
        value["usage"]["is_byok"] = True
    monkeypatch.setattr(pilot, "request_json", lambda *_: value)
    with pytest.raises(pilot.PilotError):
        call()
    if problem == "above_bound":
        assert Decimal(ledger.summary()["spent_usd"]) == Decimal(value["usage"]["cost"])
    rows, reserve = age_attempt(events())
    replace_fixture(ledger, rows)
    with pytest.raises(pilot.PilotError, match="reconciliation"):
        ledger.abandon(pilot.digest(reserve), pilot.digest(rows), "cannot clear this")
    with pytest.raises(pilot.PilotError):
        call("other")


@pytest.mark.parametrize("mutation", ["bound", "attempt", "reserve", "duplicate"])
def test_failure_receipt_cannot_weaken_or_rebind_allowance(
    setup, monkeypatch, mutation
):
    _, _, call, events = setup
    fail(monkeypatch)
    with pytest.raises(pilot.ProviderFailure):
        call()
    rows = events()
    if mutation == "bound":
        rows[-1]["budget_hold_usd"] = "0"
    elif mutation == "attempt":
        rows[-1]["attempt_id"] = "wrong"
    elif mutation == "reserve":
        rows[-1]["reserve_sha256"] = "wrong"
    else:
        rows.append(deepcopy(rows[-1]))
    with pytest.raises(pilot.PilotError):
        accounting(rows)


def test_deadline_preflight_rejection_precedes_account_and_reserve(setup, monkeypatch):
    ledger, _, call, _ = setup

    def reject():
        raise RuntimeError("unsupported deadline")

    monkeypatch.setattr("bench.openrouter_transport.require_deadline_support", reject)
    with pytest.raises(RuntimeError, match="unsupported"):
        call()
    assert not ledger.path.exists()


def test_http_status_and_retry_after_are_safe_and_durable(setup, monkeypatch):
    ledger, _, call, events = setup

    class Opener:
        def open(self, *_args, **_kwargs):
            raise HTTPError(
                "https://example.invalid",
                429,
                "private error text",
                {"Retry-After": "60"},
                BytesIO(b"secret body"),
            )

    monkeypatch.setattr(pilot.urllib.request, "build_opener", lambda *_: Opener())
    started = time.time()
    with pytest.raises(pilot.ProviderFailure) as caught:
        call()
    assert "secret" not in str(caught.value) and "private" not in str(caught.value)
    row = events()[-1]
    assert row["failure"] == {
        "kind": "http_error",
        "http_status": 429,
        "retry_after_seconds": 60.0,
        "retryable": True,
    }
    assert row["retry_not_before_epoch"] >= started + 60
    assert "secret" not in ledger.path.read_text()


def test_http_date_retry_after_and_malformed_values():
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert 58 < pilot.retry_delay(format_datetime(future)) <= 60
    for value in (None, "bad", "-1", "nan", "Infinity"):
        assert pilot.retry_delay(value) == 0


def test_heartbeat_read_timeout_is_not_a_native_observation(setup, monkeypatch):
    ledger, payload, call, events = setup

    class Heartbeat(BytesIO):
        def read(self, *_):
            for _ in range(100):
                time.sleep(0.005)
            return b"{}"

    class Opener:
        def open(self, *_args, **_kwargs):
            return Heartbeat()

    monkeypatch.setattr(pilot, "REQUEST_DEADLINE_SECONDS", 0.03)
    monkeypatch.setattr(pilot.urllib.request, "build_opener", lambda *_: Opener())
    started = time.monotonic()
    with pytest.raises(pilot.ProviderFailure, match="deadline"):
        call()
    assert time.monotonic() - started < 0.3
    assert events()[-1]["failure"]["kind"] == "timeout"
    assert Decimal(ledger.summary()["conservative_committed_usd"]) == pilot.upper_cost(
        payload
    )
    assert ledger.summary()["completed_calls"] == 0
