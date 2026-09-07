"""Prospective phase replay on temporary synthetic data; no campaign artifacts."""

from copy import deepcopy
import json
import os
import sqlite3
import time

import pytest

from bench import openrouter_protected_audit as audit
from bench import openrouter_protected_pair as pair
from bench import openrouter_protected_study as study
from bench.openrouter_matrix import file_hash
from test_openrouter_protected_audit import (
    acquire_pilot,
    experiment as base_experiment,
    study_prefix,
)


@pytest.fixture
def experiment(tmp_path, monkeypatch, request):
    # Reuse fake acquisition plumbing, but bind the ACTUAL successor closure.
    sources = study.sources
    result = base_experiment.__wrapped__(tmp_path, monkeypatch, request)
    monkeypatch.setattr(study, "sources", sources)
    result[0]["source_files"] = sources()
    return result


def audit_only(experiment, data, monkeypatch, phase="study", **options):
    manifest, bundle, ledger, bridge, materializer, transport = experiment
    before = study.pilot.digest([manifest, bundle, data])
    ledger_before, dispatches = file_hash(ledger.path), transport["dispatches"]
    native_before = file_hash(bridge.store) if bridge.store.exists() else None
    wal = bridge.store.with_name(bridge.store.name + "-wal")
    wal_before = wal.read_bytes() if wal.exists() else b""
    original = bridge.call

    def readonly(binding, catalog, command, *args, **kwargs):
        assert command["op"] in {"checkpoint", "verify"}
        return original(binding, catalog, command, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("read-only phase audit attempted a mutable or credential operation")

    with monkeypatch.context() as patch:
        patch.setattr(bridge, "call", readonly)
        for name in ("load_key", "account", "request_json"):
            patch.setattr(study.pilot, name, forbidden)
        for name in ("__init__", "locked", "read", "call", "append"):
            patch.setattr(study.pilot.Ledger, name, forbidden)
        for name in ("make_tasks", "score_answer"):
            patch.setattr(study.typed, name, forbidden)
        report = audit.audit_phase(
            manifest,
            bundle,
            phase,
            bridge=bridge,
            materializer=materializer,
            **data,
            **options,
        )
    assert study.pilot.digest([manifest, bundle, data]) == before
    assert file_hash(ledger.path) == ledger_before
    assert transport["dispatches"] == dispatches
    assert native_before == (file_hash(bridge.store) if bridge.store.exists() else None)
    # SQLite may create an empty sidecar on a read-only open; no pages may change.
    assert wal_before == (wal.read_bytes() if wal.exists() else b"")
    return report


def test_complete_pilot_output_is_identical_without_native_batch(
    experiment, monkeypatch
):
    data = acquire_pilot(experiment)
    expected = audit_only(experiment, data, monkeypatch, "pilot")
    monkeypatch.setattr(
        pair, "audit_completed_many", lambda *a: pytest.fail("pilot native")
    )
    assert (
        audit_only(experiment, data, monkeypatch, "pilot", completed_batch=True)
        == expected
    )


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_opt_in_is_strictly_boolean(experiment, monkeypatch, value):
    data = acquire_pilot(experiment, count=0)
    with pytest.raises(study.pilot.PilotError, match="must be a boolean"):
        audit_only(experiment, data, monkeypatch, "pilot", completed_batch=value)


@pytest.mark.parametrize(
    "count,invalid,failure",
    [(0, None, None), (35, None, None), (36, 36, None), (36, None, 2)],
)
def test_partial_pilot_default_unchanged_but_batch_refuses(
    experiment, monkeypatch, count, invalid, failure
):
    data = acquire_pilot(experiment, count=count, invalid=invalid, failure=failure)
    expected = audit_only(experiment, data, monkeypatch, "pilot")
    assert not expected["schedule_complete"]
    assert (
        audit_only(experiment, data, monkeypatch, "pilot", completed_batch=False)
        == expected
    )
    with pytest.raises(study.pilot.PilotError, match="complete phase schedule"):
        audit_only(experiment, data, monkeypatch, "pilot", completed_batch=True)


@pytest.mark.parametrize("experiment", ["short_schedule"], indirect=True)
@pytest.mark.parametrize("invalid", [False, True])
def test_complete_phase_exact_report_and_every_budget_boundary(
    experiment, monkeypatch, invalid
):
    if not os.environ.get("AGENTC_TYPED_MATERIALIZER"):
        pytest.skip("requires immutable fixed materializer; never builds")
    data = study_prefix(
        experiment, monkeypatch, 18, invalid_source=invalid, invalid_candidate=invalid
    )
    original_budgets, original_accounting = audit._dispatch_budgets, audit.accounting
    visits = []

    def budgets(events, stage, *args, **kwargs):
        boundary_visits = []

        def accounting(prefix):
            boundary_visits.append((len(prefix), study.pilot.digest(prefix)))
            return original_accounting(prefix)

        with monkeypatch.context() as patch:
            patch.setattr(audit, "accounting", accounting)
            original_budgets(events, stage, *args, **kwargs)
        expected = [
            (i + 1, study.pilot.digest(events[: i + 1]))
            for i, row in enumerate(events)
            if row.get("stage") == stage and row["event"] in {"reserve", "budget_group"}
        ]
        assert all(item in boundary_visits for item in expected)
        visits.append((stage, boundary_visits))

    monkeypatch.setattr(audit, "_dispatch_budgets", budgets)
    expected = audit_only(experiment, data, monkeypatch)
    legacy_visits = deepcopy(visits)
    visits.clear()
    original_batch, batches = pair.audit_completed_many, []

    def batch(*args):
        batches.append(True)
        return original_batch(*args)

    monkeypatch.setattr(pair, "audit_completed_many", batch)
    monkeypatch.setattr(
        pair, "audit_completed", lambda *a: pytest.fail("individual replay selected")
    )
    result = audit_only(experiment, data, monkeypatch, completed_batch=True)
    assert study.pilot.canonical(result) == study.pilot.canonical(expected)
    assert visits == legacy_visits and batches == [True]
    assert result["schedule_complete"] and result["completed_scheduled_steps"] == 18
    assert result["resources_by_arm"]["source"]["successful_calls"] == 12
    assert result["resources_by_arm"]["HR"]["successful_calls"] == 6
    assert result["resources_by_arm"]["probe"]["successful_calls"] > 0
    assert not result["labels_opened"] and not result["learning"]["admission_assessed"]


@pytest.mark.parametrize("experiment", ["short_schedule"], indirect=True)
@pytest.mark.parametrize(
    "stop",
    [
        "unstarted",
        "after_source",
        "start",
        "observe_claim",
        "feedback_claim",
        "source_failure",
        "probe_failure",
    ],
)
def test_partial_native_legacy_report_and_liabilities_preserved(
    experiment, monkeypatch, stop
):
    count = 0 if stop == "unstarted" else 12 if stop == "after_source" else 18
    data = study_prefix(
        experiment,
        monkeypatch,
        count,
        interrupt_action=stop if stop.endswith("claim") or stop == "start" else None,
        failure=1
        if stop == "source_failure"
        else 11
        if stop == "probe_failure"
        else None,
    )
    expected = audit_only(experiment, data, monkeypatch)
    assert not expected["schedule_complete"]
    before = deepcopy(audit.accounting(data["events"]))
    with pytest.raises(study.pilot.PilotError):
        audit_only(experiment, data, monkeypatch, completed_batch=True)
    assert audit.accounting(data["events"]) == before
    assert audit_only(experiment, data, monkeypatch) == expected


@pytest.mark.parametrize("experiment", ["short_schedule"], indirect=True)
@pytest.mark.parametrize(
    "mutation",
    [
        "history",
        "schema",
        "price",
        "latency",
        "ack",
        "prefix",
        "calls",
        "protected",
        "order",
    ],
)
def test_phase_tamper_rejected_by_both_paths(experiment, monkeypatch, mutation):
    if not os.environ.get("AGENTC_TYPED_MATERIALIZER"):
        pytest.skip("requires immutable fixed materializer; never builds")
    data = study_prefix(experiment, monkeypatch, 18)
    hr_id = data["intents"][0]["id"]
    if mutation == "history":
        data["intents"][1]["original_call"]["messages"][-1]["content"] = "probe history"
    elif mutation == "schema":
        data["intents"][0]["payload"]["response_format"]["json_schema"]["strict"] = (
            False
        )
    elif mutation == "price":
        reserve = next(
            row
            for row in data["events"]
            if row["event"] == "reserve" and row["id"] == hr_id
        )
        reserve["schema_endpoint_observation"]["pricing"]["prompt"] = "0.004"
    elif mutation == "latency":
        next(
            row
            for row in data["events"]
            if row["event"] == "response" and row["id"] == hr_id
        )["latency_ms"] += 1
    elif mutation == "ack":
        next(row for row in data["events"] if row.get("action") == "planned")["reply"][
            "request_sha256"
        ] = "0" * 64
    elif mutation == "prefix":
        data["events"][0]["usage_usd"] = "1"
    elif mutation in {"calls", "protected"}:
        data["calls" if mutation == "calls" else "protected_events"].pop()
    else:
        rows = data["events"]
        selected = [row for row in rows if row.get("id") == hr_id]
        rows[:] = [row for row in rows if row.get("id") != hr_id]
        index = next(i for i, row in enumerate(rows) if row.get("action") == "start")
        rows[index:index] = selected
    for mode in (False, True):
        with pytest.raises(study.pilot.PilotError):
            audit_only(experiment, data, monkeypatch, completed_batch=mode)


@pytest.mark.parametrize("experiment", ["short_schedule"], indirect=True)
@pytest.mark.parametrize("target", ["caller", "snapshot", "native"])
def test_late_mutation_after_batch_before_phase_return_rejected(
    experiment, monkeypatch, tmp_path, target
):
    if not os.environ.get("AGENTC_TYPED_MATERIALIZER"):
        pytest.skip("requires immutable fixed materializer; never builds")
    data = study_prefix(experiment, monkeypatch, 18)
    original = audit._dispatch_budgets
    stage = study.stage_id(experiment[0], "study")

    def changed(events, current, *args, **kwargs):
        original(events, current, *args, **kwargs)
        if current == stage:
            if target == "caller":
                data["calls"][0]["answer"] = "caller changed after snapshot"
            elif target == "snapshot":
                events.append({"event": "synthetic", "key_id": events[0]["key_id"]})
            else:
                experiment[3].store.rename(tmp_path / "removed-native.db")

    monkeypatch.setattr(audit, "_dispatch_budgets", changed)
    with pytest.raises(study.pilot.PilotError):
        audit_only(experiment, data, monkeypatch, completed_batch=True)


@pytest.mark.parametrize("experiment", ["short_schedule"], indirect=True)
@pytest.mark.parametrize("mode", ["missing", "older"])
def test_native_store_restoration_rejects_whole_phase(
    experiment, monkeypatch, tmp_path, mode
):
    if not os.environ.get("AGENTC_TYPED_MATERIALIZER"):
        pytest.skip("requires immutable fixed materializer; never builds")
    bridge, original, backup = (
        experiment[3],
        pair.Coordinator.execute,
        tmp_path / "old-native.db",
    )

    def execute(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if not backup.exists():
            with (
                sqlite3.connect(bridge.store) as source,
                sqlite3.connect(backup) as target,
            ):
                source.backup(target)
        return result

    monkeypatch.setattr(pair.Coordinator, "execute", execute)
    data = study_prefix(experiment, monkeypatch, 18)
    if mode == "missing":
        bridge.store.rename(tmp_path / "removed-native.db")
    else:
        with sqlite3.connect(backup) as source, sqlite3.connect(bridge.store) as target:
            source.backup(target)
    for batch in (False, True):
        with pytest.raises(study.pilot.PilotError):
            audit_only(experiment, data, monkeypatch, completed_batch=batch)


@pytest.mark.parametrize("experiment", ["short_schedule"], indirect=True)
def test_small_terminal_total_cannot_hide_historical_campaign_exposure(
    experiment, monkeypatch
):
    if not os.environ.get("AGENTC_TYPED_MATERIALIZER"):
        pytest.skip("requires immutable fixed materializer; never builds")
    data = study_prefix(experiment, monkeypatch, 18)
    rows, hr_id = data["events"], data["intents"][0]["id"]
    common = {
        "id": "synthetic-interleaved-hold",
        "stage": "old",
        "key_id": rows[0]["key_id"],
        "fingerprint": "fake",
    }
    index = next(i for i, row in enumerate(rows) if row.get("id") == hr_id)
    rows.insert(index, {**common, "event": "reserve", "upper_cost_usd": "49"})
    index = next(
        i
        for i, row in enumerate(rows)
        if row.get("id") == hr_id and row["event"] == "result"
    )
    rows.insert(index + 1, {**common, "event": "result", "cost_usd": "0"})
    assert audit.accounting(rows)["known_usd"] < study.pilot.HARD_CAP
    for batch in (False, True):
        with pytest.raises(
            study.pilot.PilotError, match="historical protected dispatch"
        ):
            audit_only(experiment, data, monkeypatch, completed_batch=batch)


def test_old_audit_source_is_not_accepted_for_either_path(experiment, monkeypatch):
    data = acquire_pilot(experiment)
    # Exact predecessor audit.py hash, not an enrollment compatibility exception.
    experiment[0]["source_files"]["bench/openrouter_protected_audit.py"] = (
        "f920daab75bd004f080cb7d4cc109cbd8aeb7bb565f584359a25509b8bf2e21b"
    )
    for batch in (False, True):
        with pytest.raises(study.pilot.PilotError, match="source or protocol contract"):
            audit_only(experiment, data, monkeypatch, "pilot", completed_batch=batch)


@pytest.mark.skipif(
    not os.environ.get("AGENTC_PHASE_BATCH_PROFILE"),
    reason="explicit full-phase synthetic timing opt-in",
)
def test_profile_complete_63_workflow_phase(experiment, monkeypatch):
    if not os.environ.get("AGENTC_TYPED_MATERIALIZER"):
        pytest.skip("requires immutable fixed materializer; never builds")
    data = study_prefix(experiment, monkeypatch, 378)
    reports, seconds = {}, {}
    for name, batch in (("individual", False), ("completed_batch", True)):
        start = time.perf_counter()
        reports[name] = audit_only(experiment, data, monkeypatch, completed_batch=batch)
        seconds[name] = time.perf_counter() - start
    assert study.pilot.canonical(reports["individual"]) == study.pilot.canonical(
        reports["completed_batch"]
    )
    report = reports["completed_batch"]
    assert report["schedule_complete"] and report["completed_scheduled_steps"] == 378
    assert report["resources_by_arm"]["source"]["successful_calls"] == 189
    assert report["resources_by_arm"]["HR"]["successful_calls"] == 189
    print(
        json.dumps(
            {
                "kind": "synthetic-complete-phase-replay-timing-v1",
                "events": len(data["events"]),
                "canonical_bytes": len(study.pilot.canonical(data["events"])),
                "source_workflows": 63,
                "HR_workflows": 63,
                "pilot_calls": 36,
                "study_calls": report["successful_calls"],
                "probe_calls": report["resources_by_arm"]
                .get("probe", {})
                .get("successful_calls", 0),
                "wall_seconds": seconds,
                "all_historical_budget_checks_retained": True,
                "identical_reports": True,
                "provider_calls": 0,
                "labels_opened": False,
                "not_paid_study_timing": True,
            },
            sort_keys=True,
        )
    )
