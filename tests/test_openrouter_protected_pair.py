"""Actual native subprocess + temporary fake ledger/provider; no paid transport."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from bench import openrouter_protected_pair as pair
from bench import openrouter_pilot as pilot
from bench.openrouter_attempts import accounting

KEY = "literal-fake-protected-pair-key"
SYNTH = {
    "sku": "K001",
    "vendor": "V001",
    "qty": 1,
    "unit": 2,
    "total": 2,
    "region": "EU",
}


def endpoints():
    return {
        model: {
            "tag": "anthropic",
            "provider_name": "Anthropic",
            "name": "Anthropic | " + identity,
            "context_length": ceiling,
            "supported_parameters": [
                "max_tokens",
                "temperature",
                "response_format",
                "structured_outputs",
            ],
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        }
        for model, (identity, ceiling) in pilot.SCHEMA_ENDPOINTS.items()
    }


def catalog():
    version, observed = "fake-protected-catalog-v1", "2026-09-06T00:00:00Z"
    targets = []
    for model, endpoint in endpoints().items():
        url = "https://example.invalid/fake-catalog"
        targets.append(
            {
                "adapter_protocol": "openrouter.chat.completions.v1",
                "provider_namespace": "openrouter",
                "model_id": model,
                "model_version": model + "@" + version,
                "revision_kind": "catalog_observation",
                "aliases": [],
                "routing_group": "fake-text",
                "context_window_tokens": endpoint["context_length"],
                "max_output_tokens": 512,
                "output_token_parameter": "max_tokens",
                "capabilities": {
                    "text_input": True,
                    "image_input": False,
                    "tool_calling": False,
                    "structured_outputs": True,
                    "streaming": False,
                },
                "price": {
                    "input_per_million_tokens_usd": 3.0 if "sonnet" in model else 1.0,
                    "output_per_million_tokens_usd": 15.0 if "sonnet" in model else 5.0,
                    "table_version": version,
                    "source_url": url,
                    "observed_at_utc": observed,
                },
                "provenance": {
                    "catalog_version": version,
                    "source_url": url,
                    "observed_at_utc": observed,
                },
            }
        )
    return {
        "catalog_version": version,
        "price_table_version": version,
        "observed_at_utc": observed,
        "targets": targets,
    }


def events(coordinator):
    with coordinator.ledger.locked() as handle:
        return coordinator.ledger.read(handle)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    binary = os.environ.get("AGENTC_PROTECTED_PAIR_BRIDGE")
    if not binary:
        pytest.skip(
            "supply prebuilt protected_pair_bridge; tests never build or load keys"
        )
    expected = os.environ["AGENTC_PROTECTED_PAIR_BRIDGE_SHA256"]
    bridge = pair.NativeBridge(Path(binary), tmp_path / "native/state.db", expected)
    ledger = pilot.Ledger(tmp_path / "fake-ledger.jsonl", KEY)
    frozen = pair.enrollment(catalog(), endpoints(), expected)
    coordinator = pair.Coordinator(
        ledger,
        KEY,
        bridge,
        frozen,
        run_id="fake-run",
        budget_stage="fake-stage",
        stage_cap="14",
    )
    state = {
        "dispatches": [],
        "fail_slot": None,
        "invalid_slot": None,
        "candidate_text": None,
        "counter": multiprocessing.get_context("fork").Value("i", 0),
    }

    def forbidden(*args, **kwargs):
        pytest.fail("real transport forbidden in protected pair tests")

    monkeypatch.setattr(pilot.urllib.request, "build_opener", forbidden)
    monkeypatch.setattr(
        pilot, "account", lambda key: {"usage": "0", "limit_remaining": "50"}
    )

    def transport(path, key, payload=None):
        assert key == KEY
        if payload is None:
            model = path.removeprefix("/models/").removesuffix("/endpoints")
            return {"data": {"endpoints": [endpoints()[model]]}}
        assert path == "/chat/completions"
        saved = [json.loads(line) for line in ledger.path.read_text().splitlines()]
        reserve = [e for e in saved if e["event"] == "reserve"][-1]
        slot = reserve["metadata"]["protected_pair"]["slot"]
        stage = (
            payload["response_format"]["json_schema"]["name"]
            .removeprefix("typed_procurement_")
            .removesuffix("_v1")
        )
        state["dispatches"].append(
            {
                "slot": slot,
                "stage": stage,
                "payload": deepcopy(payload),
                "id": reserve["id"],
            }
        )
        with state["counter"].get_lock():
            state["counter"].value += 1
        if state["fail_slot"] == slot:
            raise pilot.ProviderFailure(
                "literal fake provider failure",
                {"kind": "fake-failure", "retryable": False},
            )
        value = (
            ["D01", "D02", "D03"]
            if stage == "filter"
            else SYNTH
            if stage == "synthesize"
            else "M01"
        )
        text = json.dumps({"result": value})
        if state["invalid_slot"] in {slot, "both"}:
            text = pair.typed.ERROR
        elif slot == "candidate" and state["candidate_text"] is not None:
            text = state["candidate_text"]
        model = payload["model"]
        return {
            "id": "fake-generation-" + str(len(state["dispatches"])),
            "model": model,
            "provider": "Anthropic",
            "usage": {"cost": "0.001", "prompt_tokens": 500, "completion_tokens": 40},
            "choices": [
                {
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "openrouter_metadata": {
                "requested": model,
                "attempt": 1,
                "is_byok": False,
                "pipeline": [],
                "endpoints": {
                    "available": [
                        {
                            "selected": True,
                            "provider": "Anthropic",
                            "model": pilot.SCHEMA_ENDPOINTS[model][0],
                        }
                    ]
                },
            },
        }

    monkeypatch.setattr(pilot, "request_json", transport)
    tasks = pair.typed.make_tasks(2026090601, 8)
    return coordinator, state, tasks


def warm(setup, all_stages=False):
    coordinator, _, tasks = setup
    for task in tasks[:3]:
        for stage in pair.typed.STAGES if all_stages else ("filter",):
            coordinator.execute(task, stage)


def inspect(coordinator):
    start = next(e for e in events(coordinator) if e["event"] == pair.EVENT)
    return coordinator.bridge.call(
        coordinator.binding,
        coordinator.frozen["catalog"],
        {"op": "inspect", "call_site_id": start["source_call"]["call_site_id"]},
    )["result"]


def test_real_cold_sources_then_actual_native_group_and_read_only_replay(setup):
    coordinator, state, tasks = setup
    warm(setup)
    assert inspect(coordinator) == {
        "source_observations": 3,
        "completed_leases": 0,
        "failed_leases": 0,
    }
    value = coordinator.execute(tasks[3], "filter")
    assert value == '{"result":["D01","D02","D03"]}'
    assert [r["slot"] for r in state["dispatches"]] == ["source"] * 4 + ["candidate"]
    snapshot = inspect(coordinator)
    assert snapshot == {
        "source_observations": 4,
        "completed_leases": 1,
        "failed_leases": 0,
    }
    assert coordinator.execute(tasks[3], "filter") == value
    assert len(state["dispatches"]) == 5 and inspect(coordinator) == snapshot
    groups = [e for e in events(coordinator) if e["event"] == "budget_group"]
    assert len(groups) == 1
    group = groups[0]
    expected = (
        "12.030720"
        if group["slots"]["candidate"]["request"]["model"] == pair.live.SOURCE_MODEL
        else "7.230720"
    )
    assert Decimal(group["upper_cost_usd"]) == Decimal(expected)
    assert coordinator.ledger.summary()["conservative_committed_usd"] == "0.005"
    rows = pair._pairs(events(coordinator))
    assert all(r[1]["reply"]["result"]["kind"] == "pass_through" for r in rows.values())


@pytest.mark.parametrize("slot", ["source", "candidate"])
def test_provider_failure_keeps_exact_hold_and_never_records_semantic_pair(setup, slot):
    coordinator, state, tasks = setup
    warm(setup)
    state["fail_slot"] = slot
    with pytest.raises(pilot.ProviderFailure):
        coordinator.execute(tasks[3], "filter")
    ledger_state = accounting(events(coordinator))
    group = next(e for e in events(coordinator) if e["event"] == "budget_group")
    retained = sum(
        (
            Decimal(v["upper_cost_usd"])
            for k, v in group["slots"].items()
            if slot == "source" or k == "candidate"
        ),
        Decimal(0),
    )
    assert ledger_state["holds_usd"] + ledger_state["pending_usd"] == retained
    assert inspect(coordinator) == {
        "source_observations": 3 + (slot == "candidate"),
        "completed_leases": 0,
        "failed_leases": 1,
    }
    count = len(state["dispatches"])
    for task in (tasks[3], tasks[4]):
        with pytest.raises(pilot.PilotError):
            coordinator.execute(task, "filter")
    assert len(state["dispatches"]) == count


def test_invalid_source_still_charges_candidate_and_uses_unchanged_metric(setup):
    coordinator, state, tasks = setup
    warm(setup)
    state["invalid_slot"] = "source"
    assert coordinator.execute(tasks[3], "filter") == pair.typed.ERROR
    feedback = [e for e in events(coordinator) if e.get("action") == "feedback_claim"]
    assert len(feedback) == 1 and feedback[0]["divergence"] == 1.0
    assert len(state["dispatches"]) == 5
    assert inspect(coordinator)["completed_leases"] == 1
    coordinator.execute(tasks[3], "synthesize")
    assert any(
        pair.typed.ERROR_INPUT in m["content"]
        for m in state["dispatches"][-1]["payload"]["messages"]
    )


def test_candidate_never_enters_own_source_history(setup):
    coordinator, state, tasks = setup
    warm(setup, all_stages=True)
    state["candidate_text"] = '{"result":["D04","D05","D06"]}'
    source = coordinator.execute(tasks[3], "filter")
    assert source == '{"result":["D01","D02","D03"]}'
    state["candidate_text"] = None
    coordinator.execute(tasks[3], "synthesize")
    starts = [
        e
        for e in events(coordinator)
        if e.get("action") == "start" and e["task_id"] == tasks[3]["task_id"]
    ]
    synthesis = starts[-1]["source_call"]
    expected = pair.restrict_call(
        pair.typed.call(
            tasks[3],
            "synthesize",
            {"filter": source},
            coordinator.attention,
            model=pair.live.SOURCE_MODEL,
            run_identity=coordinator.run_id,
        ),
        "synthesize",
    )
    assert synthesis["messages"] == expected["messages"]
    assert len(state["dispatches"]) == 13


def test_wrong_key_enrollment_or_store_rejected_without_provider_work(setup, tmp_path):
    coordinator, state, tasks = setup
    with pytest.raises(pilot.PilotError):
        pair.Coordinator(
            coordinator.ledger,
            "another-literal-fake-key",
            coordinator.bridge,
            coordinator.frozen,
            run_id=coordinator.run_id,
            budget_stage="fake-stage",
            stage_cap="14",
        )
    bad = deepcopy(coordinator.frozen)
    bad["typed_contract_sha256"] = "a" * 64
    with pytest.raises(pilot.PilotError):
        pair.Coordinator(
            coordinator.ledger,
            KEY,
            coordinator.bridge,
            bad,
            run_id=coordinator.run_id,
            budget_stage="fake-stage",
            stage_cap="14",
        )
    coordinator.execute(tasks[0], "filter")
    other = pair.Coordinator(
        pilot.Ledger(tmp_path / "other-ledger.jsonl", KEY),
        KEY,
        coordinator.bridge,
        coordinator.frozen,
        run_id=coordinator.run_id,
        budget_stage="fake-stage",
        stage_cap="14",
    )
    with pytest.raises(pilot.PilotError):
        other.execute(tasks[1], "filter")
    assert len(state["dispatches"]) == 1


def test_native_callback_fence_rejects_duplicate_or_wrong_ack(setup):
    coordinator, _, tasks = setup
    coordinator.execute(tasks[0], "filter")
    saved = pair._pairs(events(coordinator))
    rows = next(iter(saved.values()))
    start = rows[0]
    with pytest.raises(pilot.PilotError):
        coordinator._native(
            start["pair_id"], {"op": "plan", "call": start["source_call"], "now_us": 1}
        )
    bad = deepcopy(rows[1]["reply"])
    bad["result"]["kind"] = "cached"
    with pytest.raises(pilot.PilotError):
        coordinator.bridge.verify(
            coordinator.binding,
            coordinator.frozen["catalog"],
            start["pair_id"] + "/plan",
            bad,
            {"op": "plan", "call": start["source_call"], "now_us": start["now_us"]},
            events(coordinator),
        )
    assert inspect(coordinator)["source_observations"] == 1


def _crash_worker(coordinator, task, point):
    append = coordinator.ledger.append
    native = coordinator._native

    def crashing_append(handle, event):
        name = event.get("action", event["event"])
        if event.get("id", "").endswith("/source"):
            name += ":source"
        elif event.get("id", "").endswith("/candidate"):
            name += ":candidate"
        if point == name + ":before":
            os._exit(73)
        result = append(handle, event)
        if point == name + ":after":
            os._exit(73)
        return result

    def crashing_native(pair_id, command):
        name = "native:" + command["op"]
        if point == name + ":before":
            os._exit(73)
        result = native(pair_id, command)
        if point == name + ":after":
            os._exit(73)
        return result

    coordinator.ledger.append = crashing_append
    coordinator._native = crashing_native
    coordinator.execute(task, "filter")


@pytest.mark.parametrize(
    "point",
    [
        "start:after",
        "native:plan:before",
        "native:plan:after",
        "planned:after",
        "budget_group:after",
        "reserve:source:before",
        "reserve:source:after",
        "response:source:before",
        "response:source:after",
        "result:source:before",
        "result:source:after",
        "observe_claim:before",
        "observe_claim:after",
        "native:observe:before",
        "native:observe:after",
        "observed:after",
        "reserve:candidate:before",
        "reserve:candidate:after",
        "response:candidate:before",
        "response:candidate:after",
        "result:candidate:before",
        "result:candidate:after",
        "feedback_claim:before",
        "feedback_claim:after",
        "native:complete:before",
        "native:complete:after",
        "feedback:after",
        "complete:before",
        "complete:after",
    ],
)
def test_process_crash_never_repeats_provider_or_native_work(setup, point):
    coordinator, state, tasks = setup
    warm(setup)
    process = multiprocessing.get_context("fork").Process(
        target=_crash_worker, args=(coordinator, tasks[3], point)
    )
    process.start()
    process.join(15)
    assert process.exitcode == 73, point
    before = events(coordinator)
    callbacks = inspect(coordinator)
    count = state["counter"].value
    if point == "complete:after":
        assert (
            coordinator.execute(tasks[3], "filter") == '{"result":["D01","D02","D03"]}'
        )
    else:
        with pytest.raises(pilot.PilotError):
            coordinator.execute(tasks[3], "filter")
        with pytest.raises(pilot.PilotError):
            coordinator.execute(tasks[4], "filter")
    assert state["counter"].value == count and inspect(coordinator) == callbacks
    assert events(coordinator) == before
    assert not any(e["event"] == "budget_group_release" for e in before)
    groups = [e for e in before if e["event"] == "budget_group"]
    if groups:
        state = accounting(before)
        expected = sum(
            (
                Decimal(spec["upper_cost_usd"])
                for spec in groups[0]["slots"].values()
                if spec["id"] not in state["results"]
            ),
            Decimal(0),
        )
        assert state["holds_usd"] + state["pending_usd"] == expected


def test_cold_failure_preserves_single_source_allowance_without_native_pair(setup):
    coordinator, state, tasks = setup
    state["fail_slot"] = "source"
    with pytest.raises(pilot.ProviderFailure):
        coordinator.execute(tasks[0], "filter")
    assert not any(e["event"] == "budget_group" for e in events(coordinator))
    assert accounting(events(coordinator))["holds_usd"] == Decimal("6.015360")
    assert inspect(coordinator) == {
        "source_observations": 0,
        "completed_leases": 0,
        "failed_leases": 0,
    }


def test_inherited_hold_budget_and_pending_blockers_are_not_bypassed(setup):
    coordinator, state, tasks = setup
    # Fake legacy unresolved attempt, with no dispatch or provider observation.
    with coordinator.ledger.locked() as handle:
        coordinator.ledger.append(
            handle,
            {
                "event": "reserve",
                "id": "older-pending",
                "stage": "older",
                "fingerprint": "fake-older-fingerprint",
                "upper_cost_usd": "1",
            },
        )
    with pytest.raises(pilot.PilotError, match="unfinished"):
        coordinator.execute(tasks[0], "filter")
    assert state["counter"].value == 0
    assert not coordinator.bridge.store.exists()


def test_different_pairs_cannot_race_the_start_claim(setup, monkeypatch):
    coordinator, state, _ = setup
    barrier = threading.Barrier(2)

    def start(pair_id):
        barrier.wait(timeout=5)
        return coordinator._append(pair_id, "start")

    # Exercise the actual atomic boundary without bypassing the provider's
    # intentional main-thread-only deadline guard.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start, pair_id) for pair_id in ("first", "second")]
        values = []
        for future in futures:
            try:
                values.append(future.result(timeout=10))
            except pilot.PilotError:
                values.append(None)
    assert values.count(None) == 1 and state["counter"].value == 0
    assert len(pair._pairs(events(coordinator))) == 1


def test_two_ledger_bindings_cannot_enroll_one_fresh_native_store(
    setup, tmp_path, monkeypatch
):
    coordinator, state, tasks = setup
    other = pair.Coordinator(
        pilot.Ledger(tmp_path / "other.jsonl", KEY),
        KEY,
        coordinator.bridge,
        coordinator.frozen,
        run_id="different-run",
        budget_stage="fake-stage",
        stage_cap="14",
    )
    barrier = threading.Barrier(2)
    original = coordinator.bridge.call

    class BeforeNative(Exception):
        pass

    def stopped(*args):
        raise BeforeNative

    commands = []
    for index, c in enumerate((coordinator, other)):
        with monkeypatch.context() as patch:
            patch.setattr(c, "_native", stopped)
            with pytest.raises(BeforeNative):
                c.execute(tasks[index], "filter")
        start = next(iter(pair._pairs(events(c)).values()))[0]
        commands.append(
            (
                start["pair_id"],
                {"op": "plan", "call": start["source_call"], "now_us": start["now_us"]},
            )
        )

    def synchronized(binding, catalog, command, *args, **kwargs):
        if command["op"] == "plan":
            barrier.wait(timeout=5)
        return original(binding, catalog, command, *args, **kwargs)

    monkeypatch.setattr(coordinator.bridge, "call", synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(c._native, *commands[index])
            for index, c in enumerate((coordinator, other))
        ]
        winners = []
        for index, future in enumerate(futures):
            try:
                future.result(timeout=10)
                winners.append(index)
            except pilot.PilotError:
                pass
    assert len(winners) == 1
    # Rust has exited; this read cannot mix SQLite owners with a live bridge.
    with sqlite3.connect(coordinator.bridge.store) as conn:
        stored = json.loads(
            conn.execute("SELECT binding FROM protected_bridge_binding").fetchone()[0]
        )
        assert stored["binding"] == (coordinator, other)[winners[0]].binding
        assert (
            conn.execute("SELECT COUNT(*) FROM protected_bridge_callbacks").fetchone()[
                0
            ]
            == 1
        )
    assert state["counter"].value == 0


@pytest.mark.parametrize("mode", ["empty", "earlier", "mutated_origin"])
def test_same_path_journal_rollback_cannot_reuse_warm_native_state(setup, mode):
    coordinator, state, tasks = setup
    coordinator.execute(tasks[0], "filter")
    earlier = events(coordinator)
    coordinator.execute(tasks[1], "filter")
    restored = [] if mode == "empty" else deepcopy(earlier)
    if mode == "mutated_origin":
        next(e for e in restored if e["event"] == "origin")["usage_usd"] = "1"
    coordinator.ledger.path.write_bytes(
        b"".join(pilot.canonical(e) + b"\n" for e in restored)
    )
    before = coordinator.ledger.path.read_bytes()
    for task in (tasks[0], tasks[2]):
        with pytest.raises(pilot.PilotError, match="journal"):
            coordinator.execute(task, "filter")
    assert coordinator.ledger.path.read_bytes() == before
    assert state["counter"].value == 2


def test_expected_callback_command_rejects_joint_latency_and_claim_tamper(setup):
    coordinator, _, tasks = setup
    coordinator.execute(tasks[0], "filter")
    original = events(coordinator)
    rows = next(iter(pair._pairs(original).values()))
    start = rows[0]
    source = next(e for e in original if e["event"] == "result")
    changed = deepcopy(source)
    changed["latency_ms"] += 1.0
    command = {
        "op": "observe",
        "primary": pair._primary(rows[1]["reply"]["result"]),
        "outcome": pair._outcome(changed, start["source_call"]),
    }
    observed = next(e for e in rows if e["action"] == "observed")
    # The native prefix is intact here, isolating command reconstruction from
    # watermark detection: even a mutually changed result/claim is not the ack.
    changed_claim = deepcopy(next(e for e in rows if e["action"] == "observe_claim"))
    changed_claim["source_receipt_sha256"] = pilot.digest(changed)
    assert changed_claim["source_receipt_sha256"] == pilot.digest(changed)
    with pytest.raises(pilot.PilotError, match="reconstructed"):
        coordinator.bridge.verify(
            coordinator.binding,
            coordinator.frozen["catalog"],
            start["pair_id"] + "/observe",
            observed["reply"],
            command,
            original,
        )


@pytest.mark.parametrize("slot", ["source", "candidate"])
def test_joint_journal_receipt_claim_and_chain_tamper_rejected(setup, slot):
    coordinator, state, tasks = setup
    warm(setup)
    coordinator.execute(tasks[3], "filter")
    changed = events(coordinator)
    current = list(pair._pairs(changed).values())[-1]
    receipt = next(
        e
        for e in changed
        if e["event"] == "result" and e["id"] == current[0]["pair_id"] + "/" + slot
    )
    receipt["latency_ms"] += 1.0
    previous = None
    for row in current:
        if slot + "_receipt_sha256" in row:
            row[slot + "_receipt_sha256"] = pilot.digest(receipt)
        row["previous_sha256"] = pilot.digest(previous) if previous else None
        previous = row
    # Both receipt digest and event chain are internally consistent; neither
    # may replace the actual command whose native effect already happened.
    pair._pairs(changed)
    coordinator.ledger.path.write_bytes(
        b"".join(pilot.canonical(e) + b"\n" for e in changed)
    )
    with pytest.raises(pilot.PilotError, match="journal"):
        coordinator.execute(tasks[3], "filter")
    assert state["counter"].value == 5


@pytest.mark.parametrize("invalid", ["candidate", "both"])
def test_candidate_or_both_invalid_remain_charged_loss(setup, invalid):
    coordinator, state, tasks = setup
    warm(setup)
    state["invalid_slot"] = invalid
    result = coordinator.execute(tasks[3], "filter")
    assert (result == pair.typed.ERROR) == (invalid == "both")
    assert [
        e["divergence"]
        for e in events(coordinator)
        if e.get("action") == "feedback_claim"
    ] == [1.0]
    assert state["counter"].value == 5
    assert accounting(events(coordinator))["known_usd"] == Decimal("0.005")


def test_combined_allowance_blocks_both_dispatches_when_stage_cannot_fit_pair(setup):
    original, state, tasks = setup
    coordinator = pair.Coordinator(
        original.ledger,
        KEY,
        original.bridge,
        original.frozen,
        run_id=original.run_id,
        budget_stage=original.budget_stage,
        stage_cap="6.1",
    )
    warm((coordinator, state, tasks))
    with pytest.raises(pilot.PilotError):
        coordinator.execute(tasks[3], "filter")
    assert state["counter"].value == 3
    assert not any(e["event"] == "budget_group" for e in events(coordinator))
    assert inspect(coordinator)["source_observations"] == 3
    with pytest.raises(pilot.PilotError):
        coordinator.execute(tasks[3], "filter")


def test_original_abandoned_hold_still_exhausts_global_allowance(setup):
    coordinator, state, tasks = setup
    with coordinator.ledger.locked() as handle:
        reserve = coordinator.ledger.append(
            handle,
            {
                "event": "reserve",
                "id": "old-hold",
                "stage": "older",
                "fingerprint": "fake-fingerprint",
                "upper_cost_usd": "50",
            },
        )
        coordinator.ledger.append(
            handle,
            {
                "event": "attempt_abandoned",
                "id": "old-hold",
                "stage": "older",
                "reserve_sha256": pilot.digest(reserve),
                "budget_hold_usd": "50",
            },
        )
    with pytest.raises(pilot.PilotError):
        coordinator.execute(tasks[0], "filter")
    assert state["counter"].value == 0
    assert accounting(events(coordinator))["holds_usd"] == Decimal("50")
    assert inspect(coordinator)["source_observations"] == 0


def test_readonly_replay_and_stale_checkpoint_cas_have_no_effects(setup):
    coordinator, state, tasks = setup
    coordinator.execute(tasks[0], "filter")
    before = events(coordinator)
    rows = next(iter(pair._pairs(before).values()))
    with sqlite3.connect(coordinator.bridge.store) as conn:
        snapshot = list(conn.iterdump())
    coordinator.execute(tasks[0], "filter")
    inspect(coordinator)
    with pytest.raises(pilot.PilotError):
        coordinator.bridge.call(
            coordinator.binding,
            coordinator.frozen["catalog"],
            {"op": "plan", "call": rows[0]["source_call"], "now_us": rows[0]["now_us"]},
            "different-callback",
            {"count": len(before), "sha256": pilot.digest(before)},
            rows[1]["reply"]["ledger_prefix"],
        )
    with sqlite3.connect(coordinator.bridge.store) as conn:
        assert list(conn.iterdump()) == snapshot
    assert events(coordinator) == before and state["counter"].value == 1


def test_ambiguous_native_ack_cannot_replay_completed_journal(setup):
    coordinator, state, tasks = setup
    coordinator.execute(tasks[0], "filter")
    # Simulate persistence interrupted after native state but before ack hash.
    with sqlite3.connect(coordinator.bridge.store) as conn:
        conn.execute(
            "UPDATE protected_bridge_callbacks SET reply_sha256=NULL WHERE callback_id LIKE '%/observe'"
        )
    with pytest.raises(pilot.PilotError):
        coordinator.execute(tasks[0], "filter")
    assert state["counter"].value == 1


@pytest.mark.parametrize("mode", ["missing", "older", "missing_latest_ack"])
def test_intact_journal_rejects_native_rollback_before_distinct_next_task(
    setup, tmp_path, mode
):
    coordinator, state, tasks = setup
    warm(setup)
    backup = tmp_path / "older-native.db"
    with (
        sqlite3.connect(coordinator.bridge.store) as source,
        sqlite3.connect(backup) as saved,
    ):
        source.backup(saved)
    coordinator.execute(tasks[3], "filter")
    assert inspect(coordinator) == {
        "source_observations": 4,
        "completed_leases": 1,
        "failed_leases": 0,
    }
    journal = coordinator.ledger.path.read_bytes()
    if mode == "missing":
        coordinator.bridge.store.rename(tmp_path / "removed-native.db")
        snapshot = None
    else:
        with sqlite3.connect(coordinator.bridge.store) as native:
            if mode == "older":
                with sqlite3.connect(backup) as saved:
                    saved.backup(native)
            else:
                native.execute(
                    "DELETE FROM protected_bridge_callbacks WHERE callback_id LIKE '%/complete'"
                )
            snapshot = list(native.iterdump())
    # A distinct task must not silently warm a reset store; same-task replay
    # checks alone would not protect this path.
    with pytest.raises(pilot.PilotError):
        coordinator.execute(tasks[4], "filter")
    assert state["counter"].value == 5
    assert coordinator.ledger.path.read_bytes() == journal
    if snapshot is None:
        assert not coordinator.bridge.store.exists()
    else:
        with sqlite3.connect(coordinator.bridge.store) as native:
            assert list(native.iterdump()) == snapshot


def test_native_effect_without_journal_ack_blocks_distinct_task(setup, monkeypatch):
    coordinator, state, tasks = setup
    coordinator.execute(tasks[0], "filter")
    append = coordinator._append

    class LostAcknowledgement(Exception):
        pass

    def lose(pair_id, action, **data):
        if action == "planned":
            raise LostAcknowledgement
        return append(pair_id, action, **data)

    with monkeypatch.context() as patch:
        patch.setattr(coordinator, "_append", lose)
        with pytest.raises(LostAcknowledgement):
            coordinator.execute(tasks[1], "filter")
    journal = coordinator.ledger.path.read_bytes()
    with sqlite3.connect(coordinator.bridge.store) as native:
        snapshot = list(native.iterdump())
    with pytest.raises(pilot.PilotError, match="latest journal acknowledgement"):
        coordinator.execute(tasks[2], "filter")
    assert coordinator.ledger.path.read_bytes() == journal
    assert state["counter"].value == 1
    with sqlite3.connect(coordinator.bridge.store) as native:
        assert list(native.iterdump()) == snapshot


def test_public_audit_has_no_key_ledger_or_mutating_native_capability(
    setup, monkeypatch
):
    coordinator, state, tasks = setup
    warm(setup)
    value = coordinator.execute(tasks[3], "filter")
    journal = events(coordinator)
    rows = list(pair._pairs(journal).values())[-1]
    with sqlite3.connect(coordinator.bridge.store) as native:
        snapshot = list(native.iterdump())
    original = coordinator.bridge.call
    operations = []

    def readonly(binding, catalog, command, *args, **kwargs):
        operations.append(command["op"])
        assert command["op"] in {"checkpoint", "verify"}
        return original(binding, catalog, command, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("pure audit attempted a keyed or ledger effect")

    monkeypatch.setattr(coordinator.bridge, "call", readonly)
    monkeypatch.setattr(pilot.Ledger, "__init__", forbidden)
    monkeypatch.setattr(pilot.Ledger, "locked", forbidden)
    monkeypatch.setattr(pilot, "account", forbidden)
    monkeypatch.setattr(pilot, "request_json", forbidden)
    del coordinator.key
    assert pair.audit_native_state(
        coordinator.bridge, coordinator.frozen, coordinator.binding, journal
    ) == pair._pairs(journal)
    assert (
        pair.audit_completed(
            coordinator.bridge, coordinator.frozen, coordinator.binding, rows, journal
        )
        == value
    )
    assert set(operations) == {"checkpoint", "verify"}
    assert state["counter"].value == 5
    with sqlite3.connect(coordinator.bridge.store) as native:
        assert list(native.iterdump()) == snapshot


@pytest.mark.parametrize("stop_at", ["budget_group", "reserve"])
def test_public_audit_validates_unfinished_exact_specs_without_missing_receipts(
    setup, monkeypatch, stop_at
):
    coordinator, state, tasks = setup
    warm(setup)
    append = coordinator.ledger.append

    class Interrupted(Exception):
        pass

    def stop(handle, event):
        result = append(handle, event)
        if event["event"] == stop_at:
            raise Interrupted
        return result

    monkeypatch.setattr(coordinator.ledger, "append", stop)
    with pytest.raises(Interrupted):
        coordinator.execute(tasks[3], "filter")
    journal = events(coordinator)
    rows = list(
        pair.audit_native_state(
            coordinator.bridge, coordinator.frozen, coordinator.binding, journal
        ).values()
    )[-1]
    with pytest.raises(pilot.PilotError, match="interrupted"):
        pair.audit_completed(
            coordinator.bridge, coordinator.frozen, coordinator.binding, rows, journal
        )
    changed = deepcopy(journal)
    group = next(event for event in changed if event["event"] == "budget_group")
    if stop_at == "budget_group":
        slot = group["slots"]["candidate"]
        slot["request"]["max_tokens"] -= 1
        slot["fingerprint"] = pilot.digest(
            {
                "payload": slot["request"],
                "metadata": slot["metadata"],
                "stage": group["stage"],
            }
        )
    else:
        # Alter an already-claimed source, consistently updating its group and
        # fingerprints; the unchanged native plan still requires the old call.
        group["slots"]["source"]["request"]["max_tokens"] -= 1
        slot = group["slots"]["source"]
        slot["fingerprint"] = pilot.digest(
            {
                "payload": slot["request"],
                "metadata": slot["metadata"],
                "stage": group["stage"],
            }
        )
        claim = next(
            event
            for event in changed
            if event["event"] == "reserve" and event["id"] == slot["id"]
        )
        claim.update(deepcopy(slot))
        claim["group_sha256"] = pilot.digest(group)
    accounting(changed)  # Conservative accounting alone is not exact plan proof.
    with pytest.raises(pilot.PilotError, match="group slot"):
        pair.audit_native_state(
            coordinator.bridge, coordinator.frozen, coordinator.binding, changed
        )
    assert state["counter"].value == 3
