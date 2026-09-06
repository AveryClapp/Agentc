"""Actual native controls; all provider resources/results are explicitly fake."""

from collections import Counter, defaultdict
import sqlite3

import pytest

from bench import openrouter_protected_pair as pair
from bench import openrouter_pilot as pilot
from test_openrouter_protected_pair import KEY, events, setup as setup


def configured(original, calls=20, tokens=256):
    frozen = pair.enrollment(
        original.frozen["catalog"],
        original.frozen["endpoints"],
        original.bridge.sha256,
        max_calls_per_site=calls,
        max_output_tokens=tokens,
    )
    return pair.Coordinator(
        original.ledger,
        KEY,
        original.bridge,
        frozen,
        run_id=original.run_id,
        budget_stage=original.budget_stage,
        stage_cap=original.stage_cap,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_calls_per_site", True),
        ("max_calls_per_site", 20.0),
        ("max_calls_per_site", 0),
        ("max_calls_per_site", 61),
        ("max_output_tokens", 256.0),
        ("max_output_tokens", True),
        ("max_output_tokens", 0),
        ("max_output_tokens", 513),
    ],
)
def test_policy_rejects_unfrozen_values_before_any_native_effect(setup, field, value):
    original, state, _ = setup
    with pytest.raises(pilot.PilotError):
        pair.enrollment(
            original.frozen["catalog"],
            original.frozen["endpoints"],
            original.bridge.sha256,
            **{field: value},
        )
    assert state["counter"].value == 0 and not original.bridge.store.exists()


def test_default_policy_and_store_contract_are_explicit(setup):
    original, state, tasks = setup
    assert original.frozen["experiment_policy"] == {
        "kind": "protected-pair-experiment-policy-v1",
        "max_calls_per_site": 20,
        "max_output_tokens": 512,
    }
    original.execute(tasks[0], "filter")
    changed = configured(original, 60, 256)
    with pytest.raises(pilot.PilotError):
        changed.execute(tasks[1], "filter")
    assert state["counter"].value == 1
    assert original.binding["experiment_policy"] == original.frozen["experiment_policy"]


def test_pure_source_call_binds_allowance_and_cap_without_changing_schema(setup):
    original, _, tasks = setup
    policies = [(20, 512), (20, 256), (60, 256)]
    calls = [
        pair.source_call(
            tasks[0],
            "filter",
            {},
            configured(original, allowance, cap).frozen,
            original.run_id,
        )
        for allowance, cap in policies
    ]
    assert all(call["messages"] == calls[0]["messages"] for call in calls)
    assert all(
        call["parameters"]["extra"]["response_format"]
        == pair.typed.schema_for("filter")
        for call in calls
    )
    assert [call["parameters"]["max_output_tokens"] for call in calls] == [
        512,
        256,
        256,
    ]
    assert (
        len(
            {
                call["parameters"]["extra"]["agentc_route_context"][
                    "protected_attempt"
                ]["rule_contract_sha256"]
                for call in calls
            }
        )
        == 3
    )


def test_native_rejects_wrong_frozen_cap_before_callback_fence(setup):
    original, state, tasks = setup
    coordinator = configured(original, 60, 256)
    call = pair.source_call(
        tasks[0], "filter", {}, coordinator.frozen, coordinator.run_id
    )
    call["parameters"]["max_output_tokens"] = 512
    with pytest.raises(pilot.PilotError):
        coordinator.bridge.call(
            coordinator.binding,
            coordinator.frozen["catalog"],
            {"op": "plan", "call": call, "now_us": 1},
            "fake-wrong-cap",
            {"count": 1, "sha256": "a" * 64},
        )
    assert not coordinator.bridge.store.exists() and state["counter"].value == 0


def test_two_losses_disable_each_exact_plan_and_restart_does_not_reset(setup):
    original, state, _ = setup
    coordinator = configured(original, 60, 256)
    tasks = pair.typed.make_tasks(2026090604, 16)
    for task in tasks[:3]:
        coordinator.execute(task, "filter")
    state["invalid_slot"] = "candidate"
    for task in tasks[3:12]:
        coordinator.execute(task, "filter")
    feedback = [row for row in events(coordinator) if row.get("action") == "feedback"]
    counts = Counter(
        pilot.digest(row["reply"]["result"]["observation"]["key"]) for row in feedback
    )
    assert sorted(counts.values()) == [2, 2, 2]
    with sqlite3.connect(coordinator.bridge.store) as native:
        disabled = native.execute(
            "SELECT COUNT(*) FROM execution_plan_disabled"
        ).fetchone()[0]
        assert disabled == 3
    restarted = configured(original, 60, 256)
    state["invalid_slot"] = None
    before = len([row for row in state["dispatches"] if row["slot"] == "candidate"])
    for task in tasks[12:]:
        restarted.execute(task, "filter")
    assert (
        len([row for row in state["dispatches"] if row["slot"] == "candidate"])
        == before
        == 6
    )
    assert all(
        row["reply"]["result"]["kind"] == "pass_through"
        for row in events(restarted)
        if row.get("action") == "planned"
    )


@pytest.mark.parametrize("calls", [20, 60])
def test_actual_63_workflow_zero_loss_reachability_without_seeded_profiles(
    setup, monkeypatch, calls
):
    original, state, _ = setup
    coordinator = configured(original, calls, 256)
    tasks = pair.typed.make_tasks(2026090603, 63)
    # Synthetic completed-receipt latency is explicit, not a native policy
    # override or warm profile injection. All actual callbacks still execute.
    append = coordinator.ledger.append

    def completed_receipt(handle, event):
        if event["event"] == "result":
            event = {**event, "latency_ms": 1000.0}
        return append(handle, event)

    monkeypatch.setattr(coordinator.ledger, "append", completed_receipt)
    for index, task in enumerate(tasks):
        for stage in pair.typed.STAGES:
            result = coordinator.execute(task, stage)
            assert result != pair.typed.ERROR
        if index == 2:
            assert state["counter"].value == 9  # truly observed cold sources only
    saved = events(coordinator)
    pairs = pair._pairs(saved)
    counts, stage_counts, runtime_by_key = Counter(), Counter(), defaultdict(set)
    rules_by_stage = defaultdict(set)
    for rows in pairs.values():
        start = rows[0]
        stage = start["workflow_stage"]
        assert start["source_call"]["parameters"]["max_output_tokens"] == 256
        route = start["source_call"]["parameters"]["extra"]["agentc_route_context"]
        assert "input_tokens_upper_bound" not in route
        plan = rows[1]["reply"]["result"]
        assert plan["kind"] == "pass_through" and "call" not in plan
        feedback = next((row for row in rows if row["action"] == "feedback"), None)
        if feedback is None:
            continue
        observation = feedback["reply"]["result"]["observation"]
        key = pilot.digest(observation["key"])
        counts[key] += 1
        runtime_by_key[key].add(pilot.digest(observation["runtime_version"]))
        stage_counts[stage] += 1
        assert (
            next(row for row in rows if row["action"] == "feedback_claim")["divergence"]
            == 0.0
        )
        candidate = plan["agentc_exploration_context"]["candidate_plan"]
        rules = (
            [candidate["rule"]]
            if candidate["kind"] == "rewritten"
            else [r["rule"] for r in candidate["rules"]]
        )
        rules_by_stage[stage].add(tuple(rules))
    assert len(pairs) == 189
    assert len(counts) == 7 and all(
        len(values) == 1 for values in runtime_by_key.values()
    )
    assert all(count <= 20 for count in counts.values())
    if calls == 60:
        assert sorted(counts.values()) == [20] * 7
        assert stage_counts == {"filter": 60, "synthesize": 20, "answer": 60}
        assert state["counter"].value == 329
    else:
        assert stage_counts == {"filter": 20, "synthesize": 20, "answer": 20}
        assert sorted(counts.values()) != [20] * 7
        assert state["counter"].value == 249
    assert (
        len(rules_by_stage["filter"]) == 3
        and len(rules_by_stage["synthesize"]) == 1
        and len(rules_by_stage["answer"]) == 3
    )
    with sqlite3.connect(coordinator.bridge.store) as native:
        paired = [
            row[0]
            for row in native.execute(
                "SELECT paired_observations FROM execution_plan_profile WHERE paired_observations > 0"
            )
        ]
        assert sorted(paired) == sorted(counts.values())
    assert all(item["payload"]["max_tokens"] == 256 for item in state["dispatches"])
