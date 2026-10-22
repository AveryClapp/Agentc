"""Reconcile legacy liabilities without dispatching or altering a journal."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import campaign_accounting as accounting


KEY_ID = hashlib.sha256(b"literal-fixture-key").hexdigest()


def event(kind: str, **fields):
    return {
        "event": kind,
        "key_id": KEY_ID,
        "at": "2026-01-01T00:00:00+00:00",
        **fields,
    }


def reserve(call_id="call", attempt="attempt", bound="0.8"):
    return event(
        "reserve",
        id=call_id,
        stage="fixture",
        attempt_id=attempt,
        fingerprint="fixture-fingerprint",
        upper_cost_usd=bound,
    )


def failure(claim, *, charge=None, kind="attempt_failure"):
    return event(
        kind,
        id=claim["id"],
        stage=claim["stage"],
        attempt_id=claim["attempt_id"],
        reserve_sha256=accounting.digest(claim),
        budget_hold_usd=claim["upper_cost_usd"],
        reported_cost_usd=charge,
    )


def body(claim, *, cost="0.2", byok=False):
    return event(
        "response",
        id=claim["id"],
        attempt_id=claim["attempt_id"],
        response={"usage": {"cost": cost, "is_byok": byok}},
    )


def result(claim, *, cost="0.1", finish="stop"):
    return event(
        "result",
        id=claim["id"],
        stage=claim["stage"],
        attempt_id=claim["attempt_id"],
        fingerprint=claim["fingerprint"],
        cost_usd=cost,
        finish_reason=finish,
    )


def write_journal(path: Path, rows) -> bytes:
    data = b"".join(accounting.canonical(row) + b"\n" for row in rows)
    path.write_bytes(data)
    return data


@pytest.mark.parametrize("finish", ["stop", "length", "error"])
def test_legacy_billed_results_remain_charges_without_replay_permission(
    tmp_path, finish
):
    path = tmp_path / "ledger.jsonl"
    claim = reserve()
    data = write_journal(path, [claim, result(claim, finish=finish)])
    before = path.stat()
    snapshot = accounting.read_snapshot(path, expected_key_id=KEY_ID)
    summary = snapshot.summary()
    assert snapshot.sha256 == hashlib.sha256(data).hexdigest()
    assert summary["known_cost_usd"] == "0.1"
    assert summary["billed_result_records"] == 1
    assert summary["pending_upper_bound_usd"] == "0"
    assert not summary["dispatch_authorized"]
    assert not summary["live_account_reconciled"]
    assert path.read_bytes() == data
    assert path.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("abandoned", [False, True])
@pytest.mark.parametrize("charge", [None, "0.2"])
def test_failure_and_abandonment_retain_the_full_reserved_bound(
    tmp_path, charge, abandoned
):
    path = tmp_path / "ledger.jsonl"
    claim = reserve()
    rows = [claim]
    if charge is not None:
        rows.append(body(claim, cost=charge))
    rows.append(failure(claim, charge=charge))
    if abandoned:
        rows.append(failure(claim, charge=charge, kind="attempt_abandoned"))
    write_journal(path, rows)
    snapshot = accounting.read_snapshot(path)
    assert snapshot.state["known_usd"] + snapshot.state["holds_usd"] == Decimal("0.8")
    assert snapshot.state["pending_usd"] == 0
    assert snapshot.state["failed_calls"] == (set() if abandoned else {"call"})
    assert snapshot.state["closed_ids"] == ({"call"} if abandoned else set())


def test_historical_retry_success_does_not_release_the_previous_hold(tmp_path):
    path = tmp_path / "ledger.jsonl"
    first, second = reserve(attempt="one"), reserve(attempt="two")
    write_journal(
        path,
        [
            first,
            failure(first, kind=accounting.LEGACY_HOLD),
            second,
            result(second),
        ],
    )
    state = accounting.read_snapshot(path).state
    assert state["known_usd"] == Decimal("0.1")
    assert state["holds_usd"] == Decimal("0.8")
    assert state["pending_usd"] == 0


@pytest.mark.parametrize("byok,cost", [(True, "0.2"), (False, "1.0")])
def test_unsafe_provider_charges_remain_visible_and_unresolved(tmp_path, byok, cost):
    path = tmp_path / "ledger.jsonl"
    claim = reserve()
    write_journal(
        path, [claim, body(claim, cost=cost, byok=byok), failure(claim, charge=cost)]
    )
    snapshot = accounting.read_snapshot(path)
    assert snapshot.state["known_usd"] == Decimal(cost)
    assert snapshot.state["unsafe_ids"] == {"call"}
    assert snapshot.summary()["unresolved_calls"] == ["call"]


@pytest.mark.parametrize(
    "mutation", ["hold", "attempt", "stage", "call", "charge", "before", "duplicate"]
)
def test_unbound_or_reduced_failure_liabilities_are_rejected(tmp_path, mutation):
    path = tmp_path / "ledger.jsonl"
    claim = reserve()
    terminal = failure(claim)
    rows = [claim, terminal]
    if mutation == "hold":
        terminal["budget_hold_usd"] = "0.7"
    elif mutation == "attempt":
        terminal["attempt_id"] = "other"
    elif mutation == "stage":
        terminal["stage"] = "other"
    elif mutation == "call":
        terminal["id"] = "other"
    elif mutation == "charge":
        terminal["reported_cost_usd"] = "0.2"  # No durable response backs it.
    elif mutation == "before":
        rows.reverse()
    else:
        rows.append(deepcopy(terminal))
    write_journal(path, rows)
    with pytest.raises(accounting.AccountingError):
        accounting.read_snapshot(path)


@pytest.mark.parametrize("duplicate", ["reserve", "attempt", "result"])
def test_duplicate_attempts_or_results_cannot_change_totals(tmp_path, duplicate):
    path = tmp_path / "ledger.jsonl"
    first = reserve()
    rows = [first]
    if duplicate == "reserve":
        rows.append(deepcopy(first))
    elif duplicate == "attempt":
        rows.append(reserve(call_id="other", attempt=first["attempt_id"]))
    else:
        rows.extend([result(first), result(first)])
    write_journal(path, rows)
    with pytest.raises(accounting.AccountingError):
        accounting.read_snapshot(path)


def group(path: Path):
    slots = {}
    for name, bound in [("source", "0.2"), ("candidate", "0.3")]:
        request = {"model": "fixture/" + name}
        slots[name] = {
            "id": name,
            "request": request,
            "metadata": {},
            "fingerprint": accounting.digest(
                {"payload": request, "metadata": {}, "stage": "fixture"}
            ),
            "upper_cost_usd": bound,
        }
    return event(
        "budget_group",
        id="group",
        policy="protected-source-first-v1",
        stage="fixture",
        stage_cap_usd="1",
        upper_cost_usd="0.5",
        slots=slots,
        ledger_binding=accounting.digest(str(path.resolve())),
    )


def group_claim(pair, slot="source"):
    return event(
        "reserve",
        **pair["slots"][slot],
        stage=pair["stage"],
        group_sha256=accounting.digest(pair),
        group_slot=slot,
        attempt_id=slot + "-attempt",
    )


def group_completion(claim):
    response = {
        "id": "fixture-generation",
        "model": claim["request"]["model"],
        "provider": "Fixture",
        "usage": {"cost": "0.01", "prompt_tokens": 1, "completion_tokens": 1},
        "choices": [
            {"finish_reason": "stop", "message": {"content": "fixture answer"}}
        ],
    }
    receipt = result(claim, cost="0.01")
    receipt.update(
        metadata=claim["metadata"],
        answer="fixture answer",
        usage=response["usage"],
        model=response["model"],
        provider=response["provider"],
        generation_id=response["id"],
        router_metadata=None,
    )
    return [
        event(
            "response",
            id=claim["id"],
            attempt_id=claim["attempt_id"],
            response=response,
        ),
        receipt,
    ]


def test_unclaimed_group_capacity_survives_completed_source(tmp_path):
    path = tmp_path / "ledger.jsonl"
    pair = group(path)
    write_journal(path, [pair])
    assert accounting.read_snapshot(path).state["pending_usd"] == Decimal("0.5")
    claim = group_claim(pair)
    write_journal(path, [pair, claim, *group_completion(claim)])
    snapshot = accounting.read_snapshot(path)
    assert snapshot.state["known_usd"] == Decimal("0.01")
    assert snapshot.state["pending_usd"] == Decimal("0.3")
    assert snapshot.summary()["unresolved_calls"] == ["candidate"]


def test_copied_journal_cannot_change_the_original_group_binding(tmp_path):
    original, copied = tmp_path / "original.jsonl", tmp_path / "copied.jsonl"
    data = write_journal(original, [group(original)])
    copied.write_bytes(data)  # Synthetic fixture only; never copy the actual campaign.
    with pytest.raises(accounting.AccountingError, match="different original"):
        accounting.read_snapshot(copied)


def test_group_candidate_requires_completed_source_and_release_requires_unclaimed_proof(
    tmp_path,
):
    path = tmp_path / "ledger.jsonl"
    pair = group(path)
    write_journal(path, [pair, group_claim(pair, "candidate")])
    with pytest.raises(accounting.AccountingError, match="durable source"):
        accounting.read_snapshot(path)
    claim = group_claim(pair)
    rows = [pair, claim, *group_completion(claim)]
    release = event(
        "budget_group_release",
        group_sha256=accounting.digest(pair),
        group_slot="source",
        id="source",
        stage="fixture",
        upper_cost_usd="0.2",
        unclaimed_prefix_sha256=accounting.digest(rows),
        reason="fixture",
    )
    write_journal(path, [*rows, release])
    with pytest.raises(accounting.AccountingError, match="never-claimed"):
        accounting.read_snapshot(path)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"event":"origin"}',
        b"{}\n",
        b"\n",
        b'{"event":"origin","event":"result"}\n',
        b'{"value":NaN}\n',
        b'{"value":Infinity}\n',
        b"\xff\n",
    ],
)
def test_torn_ambiguous_or_nonfinite_records_are_rejected(tmp_path, raw):
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(raw)
    with pytest.raises(accounting.AccountingError):
        accounting.read_snapshot(path)


def test_missing_original_journal_is_not_recreated(tmp_path):
    path = tmp_path / "missing" / "ledger.jsonl"
    with pytest.raises(accounting.AccountingError, match="complete original"):
        accounting.read_snapshot(path)
    assert not path.parent.exists()


def test_wrong_or_mixed_credential_bindings_are_rejected_without_loading_credentials(
    tmp_path,
):
    path = tmp_path / "ledger.jsonl"
    write_journal(path, [event("origin", usage_usd="0")])
    with pytest.raises(accounting.AccountingError, match="different API key"):
        accounting.read_snapshot(path, expected_key_id="f" * 64)
    write_journal(
        path, [event("origin", usage_usd="0"), {**reserve(), "key_id": "f" * 64}]
    )
    with pytest.raises(accounting.AccountingError, match="different API key"):
        accounting.read_snapshot(path)


def test_raw_prompts_are_not_exposed_by_snapshot_repr_or_summary(tmp_path):
    path = tmp_path / "ledger.jsonl"
    claim = reserve()
    claim["request"] = {"messages": [{"content": "private fixture prompt"}]}
    write_journal(path, [claim])
    snapshot = accounting.read_snapshot(path)
    assert "private fixture prompt" not in repr(snapshot)
    assert "private fixture prompt" not in str(snapshot.summary())


def test_journal_mutation_during_audit_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    write_journal(path, [event("origin", usage_usd="0")])
    original = path.stat()
    values = {
        name: getattr(original, name)
        for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    }
    observations = iter(
        [
            SimpleNamespace(**values),
            SimpleNamespace(**{**values, "st_size": original.st_size + 1}),
        ]
    )
    monkeypatch.setattr(accounting.os, "fstat", lambda _: next(observations))
    with pytest.raises(accounting.AccountingError, match="changed during"):
        accounting.read_snapshot(path)


@pytest.mark.parametrize("usage", [None, True, "NaN", "-1"])
def test_invalid_account_origin_cannot_become_a_reconciliation_baseline(
    tmp_path, usage
):
    path = tmp_path / "ledger.jsonl"
    write_journal(path, [event("origin", usage_usd=usage)])
    with pytest.raises(accounting.AccountingError):
        accounting.read_snapshot(path)


def test_multiple_origins_cannot_reset_the_account_baseline(tmp_path):
    path = tmp_path / "ledger.jsonl"
    write_journal(
        path, [event("origin", usage_usd="0"), event("origin", usage_usd="10")]
    )
    with pytest.raises(accounting.AccountingError, match="multiple account origins"):
        accounting.read_snapshot(path)


def test_audit_cli_cannot_overwrite_the_journal_or_a_prior_report(tmp_path, capsys):
    path, report = tmp_path / "ledger.jsonl", tmp_path / "audit.json"
    data = write_journal(path, [event("origin", usage_usd="0")])
    assert accounting.main(["--ledger", str(path), "--output", str(path)]) == 1
    assert path.read_bytes() == data
    assert accounting.main(["--ledger", str(path), "--output", str(report)]) == 0
    first = report.read_bytes()
    assert accounting.main(["--ledger", str(path), "--output", str(report)]) == 1
    assert report.read_bytes() == first
    assert path.read_bytes() == data
    capsys.readouterr()


def test_audit_cli_rejects_a_different_enrolled_prefix_before_writing_report(
    tmp_path, capsys
):
    path, report = tmp_path / "ledger.jsonl", tmp_path / "audit.json"
    write_journal(path, [event("origin", usage_usd="0")])
    assert (
        accounting.main(
            [
                "--ledger",
                str(path),
                "--expected-sha256",
                "f" * 64,
                "--output",
                str(report),
            ]
        )
        == 1
    )
    assert not report.exists()
    assert "audit enrollment" in capsys.readouterr().err
