"""Synthetic append-only ledger tests; never read actual experiment evidence."""

import hashlib
import json
import socket
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_openrouter_public_factorial import (  # noqa: F401 - pytest fixtures
    KEY,
    experiment,
    fixture,
    prepare_primary,
)

from bench import openrouter_pilot
from bench import openrouter_public_snapshot as snapshot
from bench.openrouter_pilot import PilotError, canonical, digest

study = snapshot.study


@pytest.fixture
def terminal(request):
    setup = request.getfixturevalue("experiment")
    _, _, ledger, dispatched, mode, _ = setup
    primary, manifest = prepare_primary(setup)
    mode["fail_at"] = 38  # Synthetic pilot36 + one valid primary + timeout.
    with pytest.raises(PilotError):
        study.run(primary, KEY, manifest)
    study.analyze(primary, manifest)  # Fake incomplete diagnostic only; no labels.
    raw = ledger.path.read_bytes()
    events = [json.loads(line) for line in raw.splitlines()]
    primary = SimpleNamespace(
        **vars(primary),
        ledger_event_count=len(events),
        ledger_prefix_bytes=len(raw),
        ledger_raw_sha256=hashlib.sha256(raw).hexdigest(),
        ledger_canonical_sha256=digest(events),
        analysis_sha256=study.file_hash(primary.output / "analysis.json"),
    )
    return primary, manifest, ledger, dispatched, mode, events


def append_retirement_and_later_charge(terminal, monkeypatch):
    _, _, ledger, dispatched, mode, events = terminal
    monkeypatch.setattr(openrouter_pilot, "REQUEST_DEADLINE_SECONDS", 0)
    failure = events[-1]
    ledger.abandon(
        failure["reserve_sha256"],
        digest(events),
        "synthetic permanent retirement; retain full bound",
    )
    mode["fail_at"] = None
    reserve = next(event for event in events if event["event"] == "reserve")
    ledger.call(
        KEY,
        "synthetic-later-charge",
        "unrelated-later-stage",
        Decimal(7),
        dispatched[0],
        reserve["metadata"],
    )


def forbid_inference(monkeypatch):
    monkeypatch.setattr(
        openrouter_pilot, "request_json", lambda *a, **k: pytest.fail("provider access")
    )
    monkeypatch.setattr(
        openrouter_pilot, "load_key", lambda *a, **k: pytest.fail("credential access")
    )


def test_snapshot_replays_original_tip_and_reports_complete_later_accounting(
    terminal, monkeypatch
):
    args, manifest, ledger, dispatched, _, events = terminal
    original_files = {
        name: (args.output / name).read_bytes()
        for name in (*study.PHASE_FILES, "analysis.json")
    }
    append_retirement_and_later_charge(terminal, monkeypatch)
    with pytest.raises(PilotError, match="summary inconsistent"):
        study.audit(args, manifest)  # Reproduce the original tip-coupling bug.
    latest_bytes = ledger.path.read_bytes()
    expected = ledger.summary()
    calls_before = len(dispatched)
    forbid_inference(monkeypatch)
    result = snapshot.audit_snapshot(args)
    old = result["historical_snapshot"]
    current = result["current_ledger"]
    assert old["calls"] == 1 and old["intents"] == 2
    assert old["schedule_complete"] is False
    assert old["finance"]["known_stage_usd"] == "0.001"
    assert old["finance"]["retained_uncertainty_usd"] == events[-1]["budget_hold_usd"]
    assert current["event_count"] > old["event_count"]
    assert current["raw_sha256"] == hashlib.sha256(latest_bytes).hexdigest()
    assert current["accounting"]["known_usd"] == expected["spent_usd"] == "0.038"
    assert (
        current["accounting"]["retained_uncertainty_usd"]
        == expected["retained_uncertainty_usd"]
    )
    assert current["accounting"]["known_by_stage"]["unrelated-later-stage"] == "0.001"
    assert current["accounting"]["unresolved_calls"] == []
    assert events[-1]["id"] in current["accounting"]["abandoned_calls"]
    assert current["not_dispatch_authorization"] is True
    assert (
        result["labels_read"]
        is result["efficacy_claim"]
        is result["analysis_regenerated"]
        is False
    )
    assert result["provider_calls"] == 0 and len(dispatched) == calls_before
    assert latest_bytes == ledger.path.read_bytes()
    assert original_files == {
        name: (args.output / name).read_bytes() for name in original_files
    }


def test_original_exact_tip_also_works_without_abandonment(terminal):
    args, _, _, _, _, events = terminal
    result = snapshot.audit_snapshot(args)
    assert result["current_ledger"]["event_count"] == len(events)
    assert result["current_ledger"]["raw_sha256"] == args.ledger_raw_sha256
    assert result["current_ledger"]["accounting"]["unresolved_calls"] == [
        events[-1]["id"]
    ]
    assert (
        result["historical_snapshot"]["finance"]["snapshot_financially_complete"]
        is False
    )


@pytest.mark.parametrize(
    "change",
    [
        "raw_hash",
        "canonical_hash",
        "count",
        "count_bool",
        "bytes",
        "truncated",
        "mutated_prefix",
        "wrong_path",
        "wrong_origin",
        "wrong_current_key",
        "scalar_suffix",
        "analysis_hash",
        "manifest_hash",
        "wrong_tip",
    ],
)
def test_reject_changed_prefix_identity_or_external_pins(terminal, change, tmp_path):
    args, _, ledger, _, _, events = terminal
    if change == "raw_hash":
        args.ledger_raw_sha256 = "f" * 64
    elif change == "canonical_hash":
        args.ledger_canonical_sha256 = "f" * 64
    elif change == "count":
        args.ledger_event_count -= 1
    elif change == "count_bool":
        args.ledger_event_count = True
    elif change == "bytes":
        args.ledger_prefix_bytes -= 1
    elif change == "truncated":
        ledger.path.write_bytes(ledger.path.read_bytes()[:-100])
    elif change == "mutated_prefix":
        ledger.path.write_bytes(
            ledger.path.read_bytes().replace(
                b'"cost_usd":"0.001"', b'"cost_usd":"0.002"', 1
            )
        )
    elif change == "wrong_path":
        args.ledger = tmp_path / "copied-campaign.jsonl"
        args.ledger.write_bytes(ledger.path.read_bytes())
    elif change == "wrong_origin":
        events[0]["usage_usd"] = "1"
        ledger.path.write_bytes(b"".join(canonical(event) + b"\n" for event in events))
    elif change == "wrong_current_key":
        with ledger.locked() as handle:
            handle.write(
                json.dumps({"event": "synthetic-extra", "key_id": "another-key"}) + "\n"
            )
    elif change == "analysis_hash":
        args.analysis_sha256 = "f" * 64
    elif change == "scalar_suffix":
        with ledger.locked() as handle:
            handle.write('"not-an-event-object"\n')
    elif change == "manifest_hash":
        args.manifest_sha256 = "f" * 64
    elif change == "wrong_tip":
        events = events[:-1]
        raw = b"".join(canonical(event) + b"\n" for event in events)
        args.ledger_event_count, args.ledger_prefix_bytes = len(events), len(raw)
        args.ledger_raw_sha256, args.ledger_canonical_sha256 = (
            hashlib.sha256(raw).hexdigest(),
            digest(events),
        )
    with pytest.raises(PilotError):
        snapshot.audit_snapshot(args)
    assert snapshot._ACTIVE is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("labels_read", True),
        ("schedule_complete", True),
        ("paired_tasks", [{"fake": "quality"}]),
        ("descriptive_task_cluster_intervals", {}),
        ("efficacy_claim", True),
    ],
)
def test_complete_or_label_bearing_metadata_fails_before_frozen_audit(
    terminal, monkeypatch, field, value
):
    args = terminal[0]
    path = args.output / "analysis.json"
    report = study.read(path)
    report[field] = value
    path.write_text(json.dumps(report))
    args.analysis_sha256 = study.file_hash(path)
    monkeypatch.setattr(
        study, "audit", lambda *a, **k: pytest.fail("efficacy metadata reached audit")
    )
    with pytest.raises(PilotError, match="prohibits efficacy"):
        snapshot.audit_snapshot(args)


@pytest.mark.parametrize(
    "action",
    [
        "labels",
        "env",
        "alias_labels",
        "write",
        "network",
        "inference",
        "analysis",
        "different_ledger",
        "key",
    ],
)
def test_forbidden_access_and_exception_restore_injection(
    terminal, monkeypatch, tmp_path, action
):
    args, _, ledger, _, _, _ = terminal
    original_reader = study.finance_helpers.typed_ledger_snapshot
    original_call, original_analyze = snapshot.Ledger.call, study.analyze
    original_bytecode = sys.dont_write_bytecode
    env = tmp_path / ".env"
    env.write_text("SYNTHETIC ONLY")
    alias = tmp_path / "label-alias"
    alias.symlink_to(args.bundle / "labels.json")

    def forbidden(*a, **k):
        if action == "labels":
            (args.bundle / "labels.json").read_text()
        elif action == "env":
            env.read_text()
        elif action == "alias_labels":
            alias.read_text()
        elif action == "write":
            (tmp_path / "forbidden-output").write_text("forbidden")
        elif action == "network":
            socket.create_connection(("127.0.0.1", 9))
        elif action == "inference":
            ledger.call(None)
        elif action == "analysis":
            study.analyze(None, None)
        elif action == "different_ledger":
            study.finance_helpers.typed_ledger_snapshot(env)
        elif action == "key":
            study.finance_helpers.typed_ledger_snapshot(args.ledger, "forbidden-key")
        pytest.fail("forbidden operation was accepted")

    monkeypatch.setattr(study, "audit", forbidden)
    with pytest.raises(PilotError):
        snapshot.audit_snapshot(args)
    assert study.finance_helpers.typed_ledger_snapshot is original_reader
    assert snapshot.Ledger.call is original_call and study.analyze is original_analyze
    assert sys.dont_write_bytecode is original_bytecode
    assert snapshot._ACTIVE is None
    assert not (tmp_path / "forbidden-output").exists()


def test_fail_closed_on_concurrent_process_and_restore_after_normal_audit(
    terminal, monkeypatch
):
    args = terminal[0]
    original_reader = study.finance_helpers.typed_ledger_snapshot
    with monkeypatch.context() as patcher:
        patcher.setattr(snapshot.threading, "active_count", lambda: 2)
        with pytest.raises(PilotError, match="single-threaded"):
            snapshot.audit_snapshot(args)
    snapshot.audit_snapshot(args)
    assert study.finance_helpers.typed_ledger_snapshot is original_reader


def test_cli_prints_only_readonly_accounting_and_sanitizes_failures(
    terminal, monkeypatch, capsys
):
    args = terminal[0]
    argv = ["snapshot"]
    for name, value in vars(args).items():
        argv.extend(["--" + name.replace("_", "-"), str(value)])
    monkeypatch.setattr(sys, "argv", argv)
    assert snapshot.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["labels_read"] is False
    argv[argv.index("--analysis-sha256") + 1] = "f" * 64
    assert snapshot.main() == 1
    assert "no raw diagnostics" in capsys.readouterr().out
