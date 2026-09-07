"""Synthetic-only replay equivalence; optional immutable native, never a build."""

from copy import deepcopy
import cProfile
import json
import os
import pstats
import sqlite3
import time

import pytest

from bench import openrouter_protected_pair as pair
from bench import openrouter_pilot as pilot
from tests.test_openrouter_protected_pair import events, setup as native_setup, warm


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return native_setup.__wrapped__(tmp_path, monkeypatch)


def sequential(coordinator, journal):
    pairs = pair.audit_native_state(
        coordinator.bridge, coordinator.frozen, coordinator.binding, journal
    )
    return {
        key: pair.audit_completed(
            coordinator.bridge, coordinator.frozen, coordinator.binding, rows, journal
        )
        for key, rows in pairs.items()
        if rows[0]["binding"]["store"] == coordinator.binding["store"]
    }


def batched(coordinator, journal):
    return pair.audit_completed_many(
        coordinator.bridge, coordinator.frozen, coordinator.binding, journal
    )


def sql_snapshot(coordinator):
    if not coordinator.bridge.store.exists():
        return None
    with sqlite3.connect(coordinator.bridge.store) as connection:
        return list(connection.iterdump())


def forbid_effects(coordinator, monkeypatch):
    original = coordinator.bridge.call
    operations = []

    def readonly(binding, catalog, command, *args, **kwargs):
        assert command["op"] in {"checkpoint", "verify"}
        operations.append(deepcopy(command))
        return original(binding, catalog, command, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("batch replay attempted a ledger, key or provider effect")

    monkeypatch.setattr(coordinator.bridge, "call", readonly)
    for name in ("__init__", "locked", "read", "append", "call"):
        monkeypatch.setattr(pilot.Ledger, name, forbidden)
    for name in ("account", "load_key", "request_json"):
        monkeypatch.setattr(pilot, name, forbidden)
    return operations


def test_prefixes_are_exact_canonical_array_hashes_and_snapshot_is_private():
    journal = [
        {"event": "synthetic", "payload": value}
        for value in ({"é": "🙂"}, [0, -0.0, 1.25e-30], True, None, '[,]\\"')
    ]
    snapshot = pair._ReplaySnapshot({}, {}, journal)
    for count in range(1, len(journal) + 1):
        expected = pilot.digest(journal[:count])
        assert snapshot.prefix_digest(snapshot.events, count) == expected
        pair._check_prefix(
            {"count": count, "sha256": expected}, snapshot.events, snapshot
        )
    with pytest.raises(pilot.PilotError, match="another snapshot"):
        snapshot.prefix_digest(deepcopy(journal), 1)
    with pytest.raises(pilot.PilotError, match="cannot be mutated"):
        snapshot.events.append({"event": "bad"})
    with pytest.raises(pilot.PilotError, match="cannot be mutated"):
        snapshot.events[0]["payload"]["é"] = "changed"
    journal[0]["payload"]["é"] = "outside mutation"
    assert snapshot.events[0]["payload"]["é"] == "🙂"
    with pytest.raises(pilot.PilotError, match="caller input changed"):
        snapshot.unchanged({}, {}, journal)


def test_snapshot_retains_all_accounting_liabilities_and_rejects_release_tamper():
    reserve = {
        "event": "reserve",
        "id": "historical",
        "stage": "historical",
        "fingerprint": "synthetic",
        "upper_cost_usd": "12.030720",
    }
    abandoned = {
        "event": "attempt_abandoned",
        "id": "historical",
        "stage": "historical",
        "reserve_sha256": pilot.digest(reserve),
        "budget_hold_usd": "12.030720",
    }
    journal = [
        reserve,
        abandoned,
        {
            "event": "reserve",
            "id": "still-pending",
            "stage": "another",
            "fingerprint": "synthetic-other",
            "upper_cost_usd": "7.230720",
        },
    ]
    snapshot = pair._ReplaySnapshot({}, {}, journal)
    assert snapshot.state == pair.accounting(journal)
    assert str(snapshot.state["holds_usd"]) == "12.030720"
    assert str(snapshot.state["pending_usd"]) == "7.230720"
    journal[1]["budget_hold_usd"] = "0"
    with pytest.raises(pilot.PilotError, match="entire reserved bound"):
        pair._ReplaySnapshot({}, {}, journal)


@pytest.mark.parametrize(
    "prefix",
    [
        False,
        {},
        {"count": True, "sha256": "0" * 64},
        {"count": 0, "sha256": "0" * 64},
        {"count": 2, "sha256": "0" * 64},
        {"count": 1, "sha256": "0" * 64},
        {"count": 1, "sha256": "0" * 64, "extra": 1},
    ],
)
def test_malformed_prefixes_reject_in_both_paths(prefix):
    journal = [{"event": "synthetic"}]
    snapshot = pair._ReplaySnapshot({}, {}, journal)
    with pytest.raises(pilot.PilotError):
        pair._check_prefix(prefix, journal)
    with pytest.raises(pilot.PilotError):
        pair._check_prefix(prefix, snapshot.events, snapshot)


def test_all_three_stages_exact_native_equivalence_one_accounting(setup, monkeypatch):
    coordinator, state, tasks = setup
    warm(setup, all_stages=True)
    for stage in pair.typed.STAGES:
        coordinator.execute(tasks[3], stage)
    journal = events(coordinator)
    original = deepcopy(journal)
    native_before = sql_snapshot(coordinator)
    ledger_before = coordinator.ledger.path.read_bytes()
    calls_before = state["counter"].value
    operations = forbid_effects(coordinator, monkeypatch)
    original_accounting = pair.accounting
    invocations = []

    def counted(rows):
        invocations.append(len(rows))
        return original_accounting(rows)

    monkeypatch.setattr(pair, "accounting", counted)
    expected = sequential(coordinator, journal)
    expected_ack_checks = {
        pilot.canonical(command) for command in operations if command["op"] == "verify"
    }
    assert len(invocations) > 12
    invocations.clear()
    operations.clear()
    actual = batched(coordinator, journal)
    assert actual == expected and len(actual) == 12
    assert invocations == [len(journal)]
    assert {command["op"] for command in operations} == {"checkpoint", "verify"}
    assert {
        pilot.canonical(command) for command in operations if command["op"] == "verify"
    } == expected_ack_checks
    assert journal == original
    assert coordinator.ledger.path.read_bytes() == ledger_before
    assert sql_snapshot(coordinator) == native_before
    assert state["counter"].value == calls_before


@pytest.mark.parametrize("invalid", ["source", "candidate", "both"])
def test_invalid_outputs_keep_exact_source_and_loss_feedback(
    setup, monkeypatch, invalid
):
    coordinator, state, tasks = setup
    warm(setup)
    state["invalid_slot"] = invalid
    coordinator.execute(tasks[3], "filter")
    journal = events(coordinator)
    forbid_effects(coordinator, monkeypatch)
    assert batched(coordinator, journal) == sequential(coordinator, journal)
    assert [
        e["divergence"] for e in journal if e.get("action") == "feedback_claim"
    ] == [1.0]


@pytest.mark.parametrize(
    "mode", ["origin", "body", "result", "text", "prefix", "chain"]
)
def test_corrupt_evidence_rejects_without_effects(setup, monkeypatch, mode):
    coordinator, state, tasks = setup
    warm(setup)
    coordinator.execute(tasks[3], "filter")
    journal = events(coordinator)
    changed = deepcopy(journal)
    if mode == "origin":
        changed[0]["usage_usd"] = "99"
    elif mode == "body":
        next(e for e in changed if e["event"] == "response")["response"]["usage"][
            "cost"
        ] = "0.002"
    elif mode == "result":
        next(e for e in changed if e["event"] == "result")["latency_ms"] += 1
    elif mode == "text":
        changed[-1]["application_text"] = "null"
    elif mode == "prefix":
        next(e for e in changed if e.get("action") == "planned")["reply"][
            "ledger_prefix"
        ]["sha256"] = "0" * 64
    else:
        changed[-1]["previous_sha256"] = "0" * 64
    before = sql_snapshot(coordinator)
    forbid_effects(coordinator, monkeypatch)
    for replay in (sequential, batched):
        with pytest.raises(pilot.PilotError):
            replay(coordinator, changed)
    assert sql_snapshot(coordinator) == before
    assert state["counter"].value == 5


@pytest.mark.parametrize("part", ["events", "frozen", "binding"])
def test_caller_mutation_during_native_verification_never_returns_cached_success(
    setup, monkeypatch, part
):
    coordinator, _, tasks = setup
    coordinator.execute(tasks[0], "filter")
    journal = events(coordinator)
    original = coordinator.bridge.call
    mutated = False

    def changing(binding, catalog, command, *args, **kwargs):
        nonlocal mutated
        assert command["op"] in {"checkpoint", "verify"}
        if not mutated:
            mutated = True
            target = {
                "events": journal[0],
                "frozen": coordinator.frozen,
                "binding": coordinator.binding,
            }[part]
            target["unexpected_mutation"] = True
        return original(binding, catalog, command, *args, **kwargs)

    monkeypatch.setattr(coordinator.bridge, "call", changing)
    with pytest.raises(pilot.PilotError, match="caller input changed"):
        batched(coordinator, journal)


def test_native_callback_cannot_mutate_cached_nested_catalog(setup, monkeypatch):
    coordinator, _, tasks = setup
    coordinator.execute(tasks[0], "filter")
    journal = events(coordinator)

    def changing(binding, catalog, command, *args, **kwargs):
        catalog["targets"][0]["capabilities"]["streaming"] = True

    monkeypatch.setattr(coordinator.bridge, "call", changing)
    with pytest.raises(pilot.PilotError, match="cannot be mutated"):
        batched(coordinator, journal)


@pytest.mark.parametrize("mode", ["missing_ack", "missing_store", "older_store"])
def test_native_rollback_or_ambiguity_rejects(setup, monkeypatch, tmp_path, mode):
    coordinator, _, tasks = setup
    coordinator.execute(tasks[0], "filter")
    backup = tmp_path / "earlier.db"
    with (
        sqlite3.connect(coordinator.bridge.store) as source,
        sqlite3.connect(backup) as target,
    ):
        source.backup(target)
    coordinator.execute(tasks[1], "filter")
    journal = events(coordinator)
    if mode == "missing_store":
        coordinator.bridge.store.rename(tmp_path / "removed.db")
    elif mode == "missing_ack":
        with sqlite3.connect(coordinator.bridge.store) as native:
            native.execute("UPDATE protected_bridge_callbacks SET reply_sha256=NULL")
    else:
        with (
            sqlite3.connect(backup) as source,
            sqlite3.connect(coordinator.bridge.store) as target,
        ):
            source.backup(target)
    before = sql_snapshot(coordinator)
    for replay in (sequential, batched):
        with pytest.raises(pilot.PilotError):
            replay(coordinator, journal)
    assert sql_snapshot(coordinator) == before


def test_final_checkpoint_rejects_state_change_after_all_completions(
    setup, monkeypatch
):
    coordinator, _, tasks = setup
    coordinator.execute(tasks[0], "filter")
    journal = events(coordinator)
    original = coordinator.bridge.call
    checkpoints = 0

    def changed(binding, catalog, command, *args, **kwargs):
        nonlocal checkpoints
        reply = original(binding, catalog, command, *args, **kwargs)
        if command["op"] == "checkpoint":
            checkpoints += 1
            if checkpoints == 3:  # Initial state, completed stage, final state.
                reply["result"] = None
        return reply

    monkeypatch.setattr(coordinator.bridge, "call", changed)
    with pytest.raises(pilot.PilotError, match="latest journal acknowledgement"):
        batched(coordinator, journal)


@pytest.mark.parametrize(
    "kind", ["start", "observe_claim", "feedback_claim", "failure"]
)
def test_incomplete_and_failed_pairs_cannot_be_batched(setup, monkeypatch, kind):
    coordinator, state, tasks = setup
    warm(setup)
    original = coordinator._append

    class Interrupted(Exception):
        pass

    def interrupted(pair_id, action, **data):
        result = original(pair_id, action, **data)
        if action == kind:
            raise Interrupted
        return result

    if kind == "failure":
        state["fail_slot"] = "candidate"
        with pytest.raises(pilot.ProviderFailure):
            coordinator.execute(tasks[3], "filter")
    else:
        with monkeypatch.context() as patch:
            patch.setattr(coordinator, "_append", interrupted)
            with pytest.raises(Interrupted):
                coordinator.execute(tasks[3], "filter")
    journal = events(coordinator)
    before = sql_snapshot(coordinator)
    money_before = pair.accounting(journal)
    forbid_effects(coordinator, monkeypatch)
    for replay in (sequential, batched):
        with pytest.raises(pilot.PilotError):
            replay(coordinator, journal)
    assert sql_snapshot(coordinator) == before
    assert pair.accounting(journal) == money_before


def test_no_cache_survives_between_batch_invocations(setup, monkeypatch):
    coordinator, _, tasks = setup
    coordinator.execute(tasks[0], "filter")
    journal = events(coordinator)
    assert batched(coordinator, journal)
    journal[-1]["application_text"] = "null"
    with pytest.raises(pilot.PilotError):
        batched(coordinator, journal)


def test_batch_cannot_claim_compatibility_with_old_frozen_source(setup):
    coordinator, _, tasks = setup
    coordinator.execute(tasks[0], "filter")
    frozen = deepcopy(coordinator.frozen)
    frozen["source_files"]["bench/openrouter_protected_pair.py"] = "0" * 64
    with pytest.raises(pilot.PilotError, match="source contract changed"):
        pair.audit_completed_many(
            coordinator.bridge, frozen, coordinator.binding, events(coordinator)
        )


@pytest.mark.skipif(
    not os.environ.get("AGENTC_REPLAY_PROFILE"),
    reason="explicit synthetic timing opt-in",
)
def test_profile_large_synthetic_background(setup):
    coordinator, _, tasks = setup
    journal = events(coordinator)
    for i in range(int(os.environ.get("AGENTC_REPLAY_PROFILE_ROWS", "1000"))):
        common = {
            "key_id": coordinator.ledger.key_id,
            "id": f"synthetic-old-{i}",
            "stage": "synthetic-old",
            "fingerprint": f"synthetic-{i}",
        }
        journal.extend(
            [
                {
                    **common,
                    "event": "reserve",
                    "upper_cost_usd": "0.001",
                    "request": {"padding": "x" * 8000},
                },
                {**common, "event": "result", "cost_usd": "0.000001"},
            ]
        )
    coordinator.ledger.path.write_bytes(
        b"".join(pilot.canonical(e) + b"\n" for e in journal)
    )
    for task in tasks[:4]:
        coordinator.execute(task, "filter")
    journal = events(coordinator)
    results = []
    for replay in (sequential, batched):
        profile = cProfile.Profile()
        start = time.perf_counter()
        profile.enable()
        results.append(replay(coordinator, journal))
        profile.disable()
        print(
            json.dumps(
                {
                    "implementation": replay.__name__,
                    "events": len(journal),
                    "canonical_bytes": len(pilot.canonical(journal)),
                    "seconds": time.perf_counter() - start,
                }
            )
        )
        pstats.Stats(profile).strip_dirs().sort_stats("cumulative").print_stats(12)
    assert results[0] == results[1]
