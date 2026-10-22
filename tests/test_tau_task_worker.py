"""Enrollment and complete-call projection invariants; real Tau runs use E0."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from bench.campaign_transport import CampaignStopped
from bench.tau_task_worker import TauCell, TauTaskConfig, TauTaskWorker, cell_calls
from bench.tau_safety import TauSafetyConfig


@pytest.mark.parametrize("enabled", [False, True])
def test_safety_sensor_cannot_be_added_or_removed_after_enrollment(enabled):
    worker = object.__new__(TauTaskWorker)
    worker.safety_config = TauSafetyConfig() if enabled else None
    worker._enrolled_safety_sensor = (
        asdict(worker.safety_config) if worker.safety_config else None
    )
    worker._assert_safety_enrollment()
    worker.safety_config = None if enabled else TauSafetyConfig()
    with pytest.raises(CampaignStopped, match="safety sensor changed"):
        worker._assert_safety_enrollment()


def config():
    return TauTaskConfig(
        task_ids=("0", "2"),
        task_inventory_sha256="a" * 64,
        strong_model="openai/gpt-4o",
        cheap_model="openai/gpt-4o-mini",
        user_model="openai/gpt-4o-mini",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"task_ids": ("0", "0")},
        {"task_ids": ["0", "2"]},
        {"task_inventory_sha256": "unfrozen"},
        {"strong_model": "gpt-4o"},
        {"cheap_model": "openai/gpt-4o"},
        {"agent_output_cap": True},
        {"judge_output_cap": 2049},
        {"max_steps": 0},
        {"max_errors": False},
        {"agent_temperature": float("nan")},
        {"user_temperature": True},
        {"rewrite_rules": ("CacheHit",)},
        {"rewrite_rules": ("OutputBudget", "OutputBudget")},
        {"runtime_overrides": (("AGENTC_OPTIMIZE_SHADOW", "0"),)},
        {"runtime_overrides": (("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "nan"),)},
        {"runtime_overrides": (("AGENTC_OPTIMIZE_HOT_THRESHOLD", "1.5"),)},
        {
            "runtime_overrides": (
                ("AGENTC_OPTIMIZE_HOT_THRESHOLD", "3"),
                ("AGENTC_OPTIMIZE_HOT_THRESHOLD", "4"),
            )
        },
        {"network_policy": "forbidden"},
    ],
)
def test_unfrozen_or_invalid_configuration_rejected(changes):
    with pytest.raises(CampaignStopped):
        replace(config(), **changes).validate()


@pytest.mark.parametrize(
    "cells",
    [
        (TauCell("0", "joint_guarded", 0, 1, 0),),
        (
            TauCell("missing", "joint_guarded", 0, 1, 0),
            TauCell("2", "joint_guarded", 0, 1, 1),
        ),
        (
            TauCell("0", "best_static_joint", 0, 1, 0),
            TauCell("2", "joint_guarded", 0, 1, 1),
        ),
        (
            TauCell("0", "joint_guarded", 0, 1, 0),
            TauCell("2", "joint_guarded", 0, 1, 0),
        ),
        (
            TauCell("0", "joint_guarded", 0, 1, 2),
            TauCell("2", "joint_guarded", 0, 1, 1),
        ),
        (
            TauCell("0", "joint_guarded", 0, 1, 0),
            TauCell("2", "joint_guarded", 0, 1, 1),
            TauCell("0", "joint_guarded", 0, 2, 2),
        ),
    ],
)
def test_missing_tasks_unimplemented_arms_reordering_and_implicit_retries_rejected(
    tmp_path, cells
):
    with pytest.raises(CampaignStopped):
        TauTaskWorker(
            transport=None,
            source=tmp_path,
            output=tmp_path / "output",
            config=config(),
            cells=cells,
        )
    assert not (tmp_path / "output").exists()


def test_native_environment_is_frozen_and_restored_between_arms(tmp_path, monkeypatch):
    worker = TauTaskWorker(
        transport=None,
        source=tmp_path,
        output=tmp_path / "output",
        config=config(),
        cells=(
            TauCell("0", "joint_guarded", 0, 1, 0),
            TauCell("2", "joint_guarded", 0, 1, 1),
        ),
    )
    monkeypatch.setenv("AGENTC_OPTIMIZE_HOT_THRESHOLD", "99")
    monkeypatch.setenv("AGENTC_ENABLED_RULES", "CacheHit")
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "invalid")
    with worker._runtime_environment("routing_only") as env:
        assert "AGENTC_OPTIMIZE_HOT_THRESHOLD" not in env
        assert env["AGENTC_ENABLED_RULES"] == "ModelDowngrade"
        assert env["AGENTC_EVAL_PLANNER_MODE"] == "joint_guarded"
        assert env["AGENTC_OPTIMIZE_SHADOW"] == "0.02"
    with worker._runtime_environment("rewrite_only_fixed_strong") as env:
        assert "ModelDowngrade" not in env["AGENTC_ENABLED_RULES"]
    with worker._runtime_environment("current_greedy") as env:
        assert env["AGENTC_EVAL_PLANNER_MODE"] == "current_greedy"
    import os

    assert os.environ["AGENTC_ENABLED_RULES"] == "CacheHit"
    assert os.environ["AGENTC_OPTIMIZE_HOT_THRESHOLD"] == "99"
    assert os.environ["AGENTC_EVAL_PLANNER_MODE"] == "invalid"


def test_unentered_worker_rejects_run_before_importing_optional_upstream(tmp_path):
    worker = TauTaskWorker(
        transport=None,
        source=tmp_path,
        output=tmp_path / "output",
        config=config(),
        cells=(
            TauCell("0", "joint_guarded", 0, 1, 0),
            TauCell("2", "joint_guarded", 0, 1, 1),
        ),
    )
    with pytest.raises(CampaignStopped):
        worker.run_next()
    assert not worker.output.exists()


@pytest.mark.parametrize(
    "failure", ["unfinished", "sticky_failure", "accounting_stop", "exception"]
)
def test_cohort_completion_is_never_published_after_partial_or_failed_work(
    tmp_path, failure
):
    worker = TauTaskWorker(
        transport=SimpleNamespace(assert_complete=lambda: {}),
        source=tmp_path,
        output=tmp_path / "output",
        config=config(),
        cells=(
            TauCell("0", "joint_guarded", 0, 1, 0),
            TauCell("2", "joint_guarded", 0, 1, 1),
        ),
    )
    worker.output.mkdir()
    worker.binding = SimpleNamespace(__exit__=lambda *args: None)
    worker._entered = True
    worker._next = 2
    if failure == "unfinished":
        worker._next = 1
    elif failure == "sticky_failure":
        worker._failed = True
    elif failure == "accounting_stop":

        def stopped():
            raise CampaignStopped("unresolved owned provider call")

        worker.transport.assert_complete = stopped
    if failure == "exception":
        worker.__exit__(RuntimeError, RuntimeError("fixture"), None)
    else:
        with pytest.raises(CampaignStopped):
            worker.__exit__(None, None, None)
    assert not (worker.output / "completion.json").exists()
    assert not worker._entered


def receipts():
    events = []
    for kind in ("primary", "exploration", "shadow", "fallback"):
        attempt = kind + "-attempt"
        metadata = {
            "cell_id": "cell",
            "turn_id": "agent-1",
            "kind": kind,
            "actor": "agent",
        }
        events.extend(
            [
                {
                    "event": "reserve",
                    "session_id": "session",
                    "attempt_id": attempt,
                    "id": kind,
                    "metadata": metadata,
                    "request": {"model": "actual/model"},
                },
                {
                    "event": "response",
                    "attempt_id": attempt,
                    "response": {"id": "actual-" + kind},
                },
                {"event": "result", "attempt_id": attempt, "cost_usd": "0.00123"},
            ]
        )
    return events


def test_all_physical_calls_keep_actual_billing_and_planning_without_fabricated_candidate_counts():
    plan = {
        "turn_id": "agent-1",
        "planning_overhead_us": 12.345,
        "native_plan_json": "actual opaque native JSON",
    }
    calls = cell_calls(
        SimpleNamespace(events=receipts()),
        session_id="session",
        cell_id="cell",
        plans=[plan],
    )
    assert len(calls) == 4
    assert [call["billed_result"]["cost_usd"] for call in calls] == ["0.00123"] * 4
    assert [call["planning_cost_attributed_to_this_call"] for call in calls] == [
        True,
        False,
        False,
        False,
    ]
    assert all(call["candidate_count"] is None for call in calls)
    assert calls[1]["planning"] == plan


def test_native_selector_count_is_attributed_once_without_recounting_counterfactual_calls():
    plan = {
        "turn_id": "agent-1",
        "selection_audit": {"schema_version": 1, "candidate_count": 7},
    }
    calls = cell_calls(
        SimpleNamespace(events=receipts()),
        session_id="session",
        cell_id="cell",
        plans=[plan],
    )
    assert [call["candidate_count"] for call in calls] == [7, None, None, None]
    assert [call["candidate_count_available"] for call in calls] == [
        True,
        False,
        False,
        False,
    ]


def test_worker_requires_native_joint_audit_before_dispatch_but_keeps_legacy_unknown(
    tmp_path,
):
    worker = TauTaskWorker(
        transport=None,
        source=tmp_path,
        output=tmp_path / "output",
        config=config(),
        cells=(
            TauCell("0", "joint_guarded", 0, 1, 0),
            TauCell("2", "current_greedy", 0, 1, 1),
        ),
    )
    worker._running = True
    event = {
        "cell_id": worker.cells[0].cell_id,
        "native_plan_json": '{"kind":"pass_through"}',
    }
    with pytest.raises(CampaignStopped):
        worker._capture_plan(event)
    assert worker._plans == []
    event["native_plan_json"] = (
        '{"kind":"pass_through","agentc_selection_audit":{"schema_version":1,"candidate_count":1}}'
    )
    worker._capture_plan(event)
    assert worker._plans[0]["selection_audit"]["candidate_count"] == 1
    worker._next = 1
    event = {
        "cell_id": worker.cells[1].cell_id,
        "native_plan_json": '{"kind":"pass_through"}',
    }
    worker._capture_plan(event)
    assert not worker._plans[-1]["selection_audit_available"]


@pytest.mark.parametrize(
    "mutation",
    ["omit_receipt", "omit_result", "duplicate_receipt", "other_session", "other_cell"],
)
def test_incomplete_duplicate_or_unowned_call_history_cannot_publish(mutation):
    events = receipts()
    if mutation == "omit_receipt":
        events.pop(1)
    elif mutation == "omit_result":
        events.pop(2)
    elif mutation == "duplicate_receipt":
        events.append(events[1])
    else:
        for event in events:
            if event["event"] == "reserve":
                if mutation == "other_session":
                    event["session_id"] = "other"
                else:
                    event["metadata"]["cell_id"] = "other"
    with pytest.raises(CampaignStopped):
        cell_calls(
            SimpleNamespace(events=events),
            session_id="session",
            cell_id="cell",
            plans=[],
        )
