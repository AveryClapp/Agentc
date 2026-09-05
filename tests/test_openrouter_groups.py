"""Temporary ledgers, literal fake keys, and process crashes; never real APIs."""

from copy import deepcopy
from decimal import Decimal
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from bench import openrouter_groups as groups
from bench import openrouter_pilot as pilot
from bench.openrouter_attempts import LEGACY_HOLD, accounting

KEY = "literal-fake-group-test-key"


def response(payload):
    return {
        "id": "fake-generation-" + payload["model"],
        "model": payload["model"],
        "provider": "Test",
        "usage": {"cost": "0.001", "prompt_tokens": 5, "completion_tokens": 1},
        "choices": [{"message": {"content": "4"}, "finish_reason": "stop"}],
    }


def calls():
    return {
        slot: {
            "id": slot + "-call",
            "payload": pilot.make_request(
                "fake/" + slot,
                ["test"],
                [{"role": "user", "content": slot + " exact input"}],
                max_tokens=16,
            ),
            "metadata": {"scope": "protected", "slot": slot},
        }
        for slot in groups.SLOTS
    }


def rows(ledger):
    with ledger.locked() as handle:
        return ledger.read(handle)


def committed(ledger):
    return Decimal(ledger.summary()["conservative_committed_usd"])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("real transport must never run in budget-group tests")

    monkeypatch.setattr(pilot.urllib.request, "build_opener", forbidden)
    monkeypatch.setattr(pilot, "account", lambda _: {"usage": "0"})
    dispatches = []

    def request(path, key, payload=None):
        assert key == KEY and path == "/chat/completions"
        dispatches.append(deepcopy(payload))
        return response(payload)

    monkeypatch.setattr(pilot, "request_json", request)
    ledger = pilot.Ledger(tmp_path / "ledger.jsonl", KEY)
    specs = calls()
    bound = sum((pilot.upper_cost(c["payload"]) for c in specs.values()), Decimal(0))
    return ledger, specs, bound, dispatches


def reserve(setup, cap=None):
    ledger, specs, bound, _ = setup
    return groups.reserve_pair(
        ledger, KEY, "pair", "stage", bound if cap is None else cap, **specs
    )


def test_atomic_pair_transfers_slots_and_replays_existing_results(setup):
    ledger, specs, bound, dispatches = setup
    receipt = reserve(setup)
    assert [r["event"] for r in rows(ledger)] == ["origin", "budget_group"]
    assert committed(ledger) == bound
    assert ledger.summary()["unresolved_calls"] == ["candidate-call", "source-call"]
    with pytest.raises(pilot.PilotError, match="source completion"):
        groups.call_slot(ledger, KEY, receipt, "candidate")
    for slot in groups.SLOTS:
        first = groups.call_slot(ledger, KEY, receipt, slot)
        assert (
            groups.call_slot(ledger, KEY, receipt, slot)["attempt_id"]
            == first["attempt_id"]
        )
    assert dispatches == [specs[s]["payload"] for s in groups.SLOTS]
    assert committed(ledger) == Decimal(".002")
    assert ledger.summary()["unresolved_calls"] == []
    state = accounting(rows(ledger))
    assert len(state["reserves"]) == 2 and state["pending_usd"] == 0


def test_successful_source_preserves_candidate_allowance_until_proven_release(setup):
    ledger, specs, _, _ = setup
    receipt = reserve(setup)
    groups.call_slot(ledger, KEY, receipt, "source")
    assert committed(ledger) == Decimal(".001") + pilot.upper_cost(
        specs["candidate"]["payload"]
    )
    with pytest.raises(pilot.PilotError, match="unresolved"):
        ledger.call(
            KEY, "unrelated", "other", Decimal(1), specs["source"]["payload"], {}
        )
    release = groups.release_unused(
        ledger, receipt, "candidate", "Protected source delivered"
    )
    assert release["unclaimed_prefix_sha256"] == pilot.digest(rows(ledger)[:-1])
    assert committed(ledger) == Decimal(".001")
    with pytest.raises(pilot.PilotError, match="released"):
        groups.call_slot(ledger, KEY, receipt, "candidate")


@pytest.mark.parametrize("slot", groups.SLOTS)
def test_release_is_terminal_and_released_source_cannot_authorize_candidate(
    setup, slot
):
    ledger, _, bound, dispatches = setup
    receipt = reserve(setup)
    groups.release_unused(ledger, receipt, slot, "Cancel never-claimed slot")
    assert committed(ledger) == bound - Decimal(
        receipt["slots"][slot]["upper_cost_usd"]
    )
    with pytest.raises(pilot.PilotError):
        groups.release_unused(ledger, receipt, slot, "Duplicate release")
    if slot == "source":
        with pytest.raises(pilot.PilotError, match="source completion"):
            groups.call_slot(ledger, KEY, receipt, "candidate")
    groups.release_unused(
        ledger,
        receipt,
        "candidate" if slot == "source" else "source",
        "Cancel remaining slot",
    )
    assert committed(ledger) == 0 and not dispatches


@pytest.mark.parametrize(
    "failure",
    ["timeout", "http400", "refusal", "native_context", "unknown", "cancelled"],
)
def test_source_failure_never_authorizes_candidate_or_retry(
    setup, monkeypatch, failure
):
    ledger, specs, bound, dispatches = setup
    receipt = reserve(setup)

    def fail(path, key, payload):
        dispatches.append(payload)
        if failure == "cancelled":
            raise KeyboardInterrupt()
        if failure == "unknown":
            raise RuntimeError("ambiguous fake failure")
        if failure in {"refusal", "native_context"}:
            body = response(payload)
            body["choices"][0]["finish_reason"] = (
                "length" if failure == "native_context" else "stop"
            )
            body["choices"][0]["native_finish_reason"] = (
                "model_context_window_exceeded"
                if failure == "native_context"
                else "end_turn"
            )
            if failure == "refusal":
                body["choices"][0]["message"]["refusal"] = "fake refusal"
            return body
        raise pilot.ProviderFailure(
            "fake failure",
            {
                "kind": "http_error" if failure == "http400" else "timeout",
                "http_status": 400 if failure == "http400" else None,
                "retryable": True,
            },
        )

    monkeypatch.setattr(pilot, "request_json", fail)
    with pytest.raises((pilot.PilotError, RuntimeError, KeyboardInterrupt)):
        groups.call_slot(ledger, KEY, receipt, "source")
    assert committed(ledger) == bound and ledger.summary()["completed_calls"] == 0
    for slot in groups.SLOTS:
        with pytest.raises(pilot.PilotError):
            groups.call_slot(ledger, KEY, receipt, slot)
    with pytest.raises(pilot.PilotError, match="claimed"):
        groups.release_unused(ledger, receipt, "source", "Cannot erase possible spend")
    groups.release_unused(
        ledger, receipt, "candidate", "Source failed before candidate claim"
    )
    assert committed(ledger) == pilot.upper_cost(specs["source"]["payload"])
    with pytest.raises(pilot.PilotError, match="slot authorization"):
        ledger.call(
            KEY,
            specs["source"]["id"],
            "stage",
            bound,
            specs["source"]["payload"],
            specs["source"]["metadata"],
        )
    assert len(dispatches) == 1


@pytest.mark.parametrize("kind", ["stage", "campaign", "external", "remaining"])
def test_combined_allowance_cannot_partially_reserve_over_budget(
    setup, monkeypatch, kind
):
    ledger, _, bound, dispatches = setup
    ceiling = bound - Decimal(".000001")
    if kind == "campaign":
        monkeypatch.setattr(pilot, "HARD_CAP", ceiling)
    elif kind == "remaining":
        monkeypatch.setattr(
            pilot, "account", lambda _: {"usage": 0, "limit_remaining": str(ceiling)}
        )
    elif kind == "external":
        with ledger.locked() as handle:
            ledger.append(handle, {"event": "origin", "usage_usd": "0"})
        monkeypatch.setattr(pilot, "account", lambda _: {"usage": str(pilot.HARD_CAP)})
    with pytest.raises(pilot.PilotError):
        reserve(setup, ceiling if kind == "stage" else bound)
    assert not any(e["event"] == "budget_group" for e in rows(ledger))
    assert not dispatches


@pytest.mark.parametrize("legacy", ["hold", "pending", "failure"])
def test_existing_holds_and_unresolved_attempts_still_block(setup, monkeypatch, legacy):
    ledger, _, bound, _ = setup
    with ledger.locked() as handle:
        reservation = ledger.append(
            handle,
            {
                "event": "reserve",
                "id": "historical",
                "stage": "old",
                "fingerprint": "old",
                "upper_cost_usd": "1",
                "attempt_id": "historical-attempt",
            },
        )
        if legacy != "pending":
            ledger.append(
                handle,
                {
                    "event": LEGACY_HOLD if legacy == "hold" else "attempt_failure",
                    "id": "historical",
                    "stage": "old",
                    "attempt_id": "historical-attempt",
                    "reserve_sha256": pilot.digest(reservation),
                    "budget_hold_usd": "1",
                    "reported_cost_usd": None,
                },
            )
    monkeypatch.setattr(pilot, "HARD_CAP", bound + Decimal(".5"))
    with pytest.raises(pilot.PilotError):
        reserve(setup)
    assert committed(ledger) == 1


@pytest.mark.parametrize(
    "mutation",
    ["key", "ledger", "copy", "payload", "metadata", "cap", "receipt", "bound"],
)
def test_wrong_binding_cannot_claim_or_dispatch(setup, tmp_path, mutation):
    ledger, _, bound, dispatches = setup
    receipt = reserve(setup)
    changed, key, target = deepcopy(receipt), KEY, ledger
    if mutation == "key":
        key = "other-literal-fake-key"
    elif mutation in {"ledger", "copy"}:
        target = pilot.Ledger(tmp_path / "other-ledger.jsonl", KEY)
        if mutation == "copy":
            target.path.write_bytes(ledger.path.read_bytes())
    elif mutation == "payload":
        changed["slots"]["source"]["request"]["messages"][0]["content"] = "different"
    elif mutation == "metadata":
        changed["slots"]["source"]["metadata"]["scope"] = "different"
    elif mutation == "bound":
        changed["slots"]["source"]["upper_cost_usd"] = "0"
    elif mutation == "cap":
        changed["stage_cap_usd"] = "2"
    else:
        changed["id"] = "different"
    with pytest.raises(pilot.PilotError):
        groups.call_slot(target, key, changed, "source")
    assert committed(ledger) == bound and not dispatches


def test_mutating_original_request_does_not_change_frozen_slot(setup):
    ledger, specs, _, dispatches = setup
    receipt = reserve(setup)
    original = deepcopy(specs["source"]["payload"])
    specs["source"]["payload"]["messages"][0]["content"] = "later mutation"
    specs["source"]["metadata"]["scope"] = "later mutation"
    groups.call_slot(ledger, KEY, receipt, "source")
    assert dispatches == [original]


def test_call_receipt_is_snapshotted_before_account_preflight(setup, monkeypatch):
    ledger, _, _, dispatches = setup
    receipt = reserve(setup)
    original = deepcopy(receipt["slots"]["source"]["request"])

    def account(_):
        receipt["slots"]["source"]["request"]["messages"][0]["content"] = (
            "racing mutation"
        )
        return {"usage": 0}

    monkeypatch.setattr(pilot, "account", account)
    groups.call_slot(ledger, KEY, receipt, "source")
    assert dispatches == [original]
    assert ledger.summary()["completed_calls"] == 1


def test_existing_financial_analysis_sees_unclaimed_group_liability(setup):
    from bench.openrouter_joint_analysis import ledger_report

    ledger, _, bound, _ = setup
    receipt = reserve(setup)
    report = ledger_report(rows(ledger), "stage", [], {})
    assert not report["snapshot_financially_complete"]
    assert report["unresolved_stage_calls"] == ["candidate-call", "source-call"]
    assert Decimal(report["pending_upper_bound_usd"]) == bound
    assert Decimal(report["campaign_conservative_commitment_usd"]) == bound
    for slot in groups.SLOTS:
        groups.release_unused(ledger, receipt, slot, "Never claimed")
    report = ledger_report(rows(ledger), "stage", [], {})
    assert report["snapshot_financially_complete"]
    assert Decimal(report["campaign_conservative_commitment_usd"]) == 0


def test_failed_candidate_keeps_full_bound_and_successful_source(setup, monkeypatch):
    ledger, specs, _, dispatches = setup
    receipt = reserve(setup)
    source = groups.call_slot(ledger, KEY, receipt, "source")

    def fail(*_):
        raise pilot.ProviderFailure(
            "fake timeout", {"kind": "timeout", "retryable": True}
        )

    monkeypatch.setattr(pilot, "request_json", fail)
    with pytest.raises(pilot.ProviderFailure):
        groups.call_slot(ledger, KEY, receipt, "candidate")
    assert committed(ledger) == Decimal(".001") + pilot.upper_cost(
        specs["candidate"]["payload"]
    )
    assert (
        groups.call_slot(ledger, KEY, receipt, "source")["attempt_id"]
        == source["attempt_id"]
    )
    with pytest.raises(pilot.PilotError, match="claimed"):
        groups.call_slot(ledger, KEY, receipt, "candidate")
    assert len(dispatches) == 1


@pytest.mark.parametrize("kind", ["campaign", "remaining"])
def test_source_preflight_protects_whole_group_after_external_spending(
    setup, monkeypatch, kind
):
    ledger, specs, bound, dispatches = setup
    receipt = reserve(setup)
    source_bound = pilot.upper_cost(specs["source"]["payload"])
    # A source alone fits, but spending external to this journal consumed part
    # of the candidate's protected allowance before the first dispatch.
    if kind == "campaign":
        usage = pilot.HARD_CAP - (bound + source_bound) / 2
        monkeypatch.setattr(pilot, "account", lambda _: {"usage": str(usage)})
    else:
        monkeypatch.setattr(
            pilot,
            "account",
            lambda _: {"usage": 0, "limit_remaining": str(source_bound)},
        )
    with pytest.raises(pilot.PilotError):
        groups.call_slot(ledger, KEY, receipt, "source")
    assert committed(ledger) == bound and not dispatches


@pytest.mark.parametrize("kind", ["budget_group", "reserve", "budget_group_release"])
def test_torn_journal_writes_block_recovery_dispatch(setup, kind):
    ledger, _, _, dispatches = setup
    receipt = reserve(setup)
    if kind == "reserve":
        groups.call_slot(ledger, KEY, receipt, "source")
    elif kind == "budget_group_release":
        groups.release_unused(ledger, receipt, "source", "Never claimed")
    events = rows(ledger)
    index = next(i for i, event in enumerate(events) if event["event"] == kind)
    prefix = b"".join(pilot.canonical(e) + b"\n" for e in events[:index])
    ledger.path.write_bytes(prefix + pilot.canonical(events[index])[:17])
    count = len(dispatches)
    with pytest.raises(pilot.PilotError, match="incomplete"):
        groups.call_slot(ledger, KEY, receipt, "source")
    with pytest.raises(pilot.PilotError, match="incomplete"):
        groups.release_unused(ledger, receipt, "candidate", "No unsafe recovery")
    assert len(dispatches) == count


@pytest.mark.parametrize(
    "mutation",
    [
        "release_prefix",
        "release_amount",
        "release_duplicate",
        "claim_duplicate",
        "claim_payload",
        "source_result",
        "sequence",
        "orphan",
    ],
)
def test_actual_journal_replay_rejects_invalid_group_transitions(setup, mutation):
    ledger, _, _, _ = setup
    receipt = reserve(setup)
    groups.call_slot(ledger, KEY, receipt, "source")
    if mutation.startswith("release"):
        groups.release_unused(ledger, receipt, "candidate", "Never claimed")
    else:
        groups.call_slot(ledger, KEY, receipt, "candidate")
    events = rows(ledger)
    claims = [i for i, e in enumerate(events) if e["event"] == "reserve"]
    if mutation == "release_prefix":
        events[-1]["unclaimed_prefix_sha256"] = "wrong"
    elif mutation == "release_amount":
        events[-1]["upper_cost_usd"] = "0"
    elif mutation == "release_duplicate":
        events.append(deepcopy(events[-1]))
    elif mutation == "claim_duplicate":
        event = deepcopy(events[claims[0]])
        event["attempt_id"] = "second-claim"
        events.append(event)
    elif mutation == "claim_payload":
        events[claims[0]]["request"]["messages"][0]["content"] = "changed"
    elif mutation == "source_result":
        events = [
            e
            for e in events
            if not (e["event"] == "result" and e["id"] == "source-call")
        ]
    elif mutation == "sequence":
        events.insert(claims[0], events.pop(claims[1]))
    else:
        events[claims[0]]["group_sha256"] = "unknown"
    ledger.path.write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(pilot.PilotError):
        ledger.summary()


@pytest.mark.parametrize(
    "mutation", ["request_integer", "claim_boolean", "result_boolean", "usage_integer"]
)
def test_canonical_identity_rejects_python_equal_type_mutations(setup, mutation):
    ledger, specs, _, dispatches = setup
    specs["source"]["metadata"]["flag"] = True
    receipt = reserve(setup)
    groups.call_slot(ledger, KEY, receipt, "source")
    events = rows(ledger)
    claim = next(e for e in events if e["event"] == "reserve")
    result = next(e for e in events if e["event"] == "result")
    if mutation == "request_integer":
        claim["request"]["max_tokens"] = 16.0
    elif mutation == "claim_boolean":
        claim["metadata"]["flag"] = 1
    elif mutation == "result_boolean":
        result["metadata"]["flag"] = 1
    else:
        result["usage"]["prompt_tokens"] = 5.0
    ledger.path.write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(pilot.PilotError):
        accounting(rows(ledger))
    with pytest.raises(pilot.PilotError):
        groups.call_slot(ledger, KEY, receipt, "candidate")
    assert len(dispatches) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "model",
        "provider",
        "selected_provider",
        "endpoint_model",
        "requested",
        "attempt",
        "byok",
        "boolean_tokens",
        "float_tokens",
        "negative_tokens",
        "missing_tokens",
        "result_model",
        "result_provider",
        "result_generation",
        "result_router",
        "result_answer",
    ],
)
def test_replay_revalidates_group_response_attribution_before_candidate(
    setup, monkeypatch, mutation
):
    ledger, specs, _, dispatches = setup
    specs["source"]["metadata"]["dispatch_contract"] = {
        "provider_name": "Test",
        "endpoint_model": "fake/native-source",
    }
    receipt = reserve(setup)

    def request(path, key, payload):
        dispatches.append(payload)
        return {
            **response(payload),
            "openrouter_metadata": {
                "requested": payload["model"],
                "attempt": 1,
                "is_byok": False,
                "endpoints": {
                    "available": [
                        {
                            "selected": True,
                            "provider": "Test",
                            "model": "fake/native-source",
                        }
                    ]
                },
            },
        }

    monkeypatch.setattr(pilot, "request_json", request)
    groups.call_slot(ledger, KEY, receipt, "source")
    events = rows(ledger)
    body = next(e for e in events if e["event"] == "response")["response"]
    result = next(e for e in events if e["event"] == "result")
    if mutation in {"model", "provider"}:
        body[mutation] = "fake/wrong"
        result[mutation] = "fake/wrong"
    elif mutation in {
        "selected_provider",
        "endpoint_model",
        "requested",
        "attempt",
        "byok",
    }:
        router = body["openrouter_metadata"]
        if mutation in {"selected_provider", "endpoint_model"}:
            router["endpoints"]["available"][0][
                "provider" if mutation == "selected_provider" else "model"
            ] = "fake/wrong"
        else:
            router[
                {"requested": "requested", "attempt": "attempt", "byok": "is_byok"}[
                    mutation
                ]
            ] = "fake/wrong" if mutation == "requested" else True
        result["router_metadata"] = deepcopy(router)
    elif mutation.endswith("tokens"):
        if mutation == "missing_tokens":
            body["usage"].pop("prompt_tokens")
        else:
            body["usage"]["prompt_tokens"] = {
                "boolean_tokens": True,
                "float_tokens": 5.0,
                "negative_tokens": -1,
            }[mutation]
        result["usage"] = deepcopy(body["usage"])
    else:
        field = {
            "result_model": "model",
            "result_provider": "provider",
            "result_generation": "generation_id",
            "result_router": "router_metadata",
            "result_answer": "answer",
        }[mutation]
        result[field] = "different"
    ledger.path.write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(pilot.PilotError):
        accounting(rows(ledger))
    with pytest.raises(pilot.PilotError):
        groups.call_slot(ledger, KEY, receipt, "candidate")
    assert len(dispatches) == 1


def test_prospective_source_manifests_freeze_group_replay_dependency():
    from bench import openrouter_auto, openrouter_contract, openrouter_frontier
    from bench import (
        openrouter_matrix,
        openrouter_rules_live,
        openrouter_typed_factorial,
    )

    path = "bench/openrouter_groups.py"
    expected = openrouter_matrix.file_hash(openrouter_matrix.ROOT / path)
    source_sets = [
        openrouter_auto.sources(),
        openrouter_contract.sources(),
        openrouter_frontier.sources(),
        openrouter_matrix.source_hashes(),
        openrouter_rules_live.sources(),
        openrouter_rules_live.sources("typed_relational_v1"),
        openrouter_typed_factorial.sources(),
    ]
    assert all(sources.get(path) == expected for sources in source_sets)


def process_action(path, receipt, action, boundary, after, counter, queue, start=None):
    """Independent fake transport plus abrupt journal-boundary exit."""
    ledger = pilot.Ledger(Path(path), KEY)
    pilot.account = lambda _: {"usage": "0"}

    def fake_request(route, key, payload=None):
        assert key == KEY and route == "/chat/completions"
        with counter.get_lock():
            counter.value += 1
        if action == "failure":
            raise pilot.ProviderFailure(
                "fake timeout", {"kind": "timeout", "retryable": True}
            )
        return response(payload)

    pilot.request_json = fake_request
    original = ledger.append

    def append(handle, event):
        if event["event"] == boundary and not after:
            os._exit(73)
        result = original(handle, event)
        if event["event"] == boundary and after:
            os._exit(73)
        return result

    ledger.append = append
    if start is not None:
        start.wait(10)
    try:
        if action == "create":
            result = groups.reserve_pair(
                ledger, KEY, "pair", "stage", Decimal(1), **calls()
            )
        elif action == "release":
            result = groups.release_unused(ledger, receipt, "source", "Never claimed")
        else:
            result = groups.call_slot(ledger, KEY, receipt, "source")
        queue.put(("ok", result.get("attempt_id")))
    except pilot.PilotError:
        queue.put(("blocked", None))


@pytest.mark.parametrize(
    "boundary,action",
    [
        ("origin", "create"),
        ("budget_group", "create"),
        ("reserve", "call"),
        ("response", "call"),
        ("result", "call"),
        ("attempt_failure", "failure"),
        ("budget_group_release", "release"),
    ],
)
@pytest.mark.parametrize("after", [False, True])
def test_abrupt_crash_at_every_journal_transition(setup, boundary, action, after):
    ledger, _, bound, _ = setup
    receipt = None if action == "create" else reserve(setup)
    ctx = multiprocessing.get_context("spawn")
    counter, queue = ctx.Value("i", 0), ctx.Queue()
    process = ctx.Process(
        target=process_action,
        args=(str(ledger.path), receipt, action, boundary, after, counter, queue),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 73
    state = accounting(rows(ledger))
    assert (
        committed(ledger)
        == state["known_usd"] + state["holds_usd"] + state["pending_usd"]
    )
    if action == "create":
        assert not state["reserves"] and counter.value == 0
        assert committed(ledger) == (
            bound if boundary == "budget_group" and after else 0
        )
    elif action == "release":
        assert counter.value == 0
        assert committed(ledger) == bound - (
            Decimal(receipt["slots"]["source"]["upper_cost_usd"]) if after else 0
        )
    elif boundary == "reserve" and not after:
        assert counter.value == 0 and not state["reserves"]
        groups.release_unused(ledger, receipt, "source", "No claim exists after crash")
    else:
        assert len(state["reserves"]) == 1
        with pytest.raises(pilot.PilotError, match="claimed"):
            groups.release_unused(
                ledger, receipt, "source", "Claim is ambiguous or spent"
            )
        if boundary == "result" and after:
            assert state["results"]["source-call"]["cost_usd"] == "0.001"
            assert (
                groups.call_slot(ledger, KEY, receipt, "source")["id"] == "source-call"
            )
        else:
            assert committed(ledger) == bound and not state["results"]
            for slot in groups.SLOTS:
                with pytest.raises(pilot.PilotError):
                    groups.call_slot(ledger, KEY, receipt, slot)


@pytest.mark.parametrize(
    "actions", [("call", "call"), ("call", "release"), ("create", "create")]
)
def test_concurrent_processes_cannot_duplicate_claim_dispatch_or_release(
    setup, actions
):
    ledger, _, _, _ = setup
    receipt = None if actions[0] == "create" else reserve(setup)
    ctx = multiprocessing.get_context("spawn")
    counter, queue, start = ctx.Value("i", 0), ctx.Queue(), ctx.Event()
    processes = [
        ctx.Process(
            target=process_action,
            args=(
                str(ledger.path),
                receipt,
                action,
                None,
                False,
                counter,
                queue,
                start,
            ),
        )
        for action in actions
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    results = [queue.get(timeout=2) for _ in processes]
    state = accounting(rows(ledger))
    assert len(state["groups"]) == 1 and counter.value <= 1
    if actions == ("call", "call"):
        assert counter.value == 1
        assert results[0] == results[1] and results[0][0] == "ok"
    else:
        assert sorted(r[0] for r in results) == ["blocked", "ok"]


def test_native_schema_pair_liability_is_arithmetic_not_capacity_evidence():
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        },
    }
    bounds = [
        pilot.upper_cost(
            pilot.make_request(
                model,
                ["anthropic"],
                [{"role": "user", "content": "test"}],
                max_tokens=512,
                response_format=schema,
            )
        )
        for model in pilot.SCHEMA_ENDPOINTS
    ]
    assert bounds == [Decimal("6.01536"), Decimal("1.21536")]
    assert sum(bounds) == Decimal("7.23072")
