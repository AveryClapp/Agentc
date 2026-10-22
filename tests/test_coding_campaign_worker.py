"""Freeze coding scheduling before dispatch and preserve action cohort identity."""

from dataclasses import asdict, replace
from hashlib import sha256
from types import SimpleNamespace

import pytest

from bench.campaign_records import _fixed_identity
from bench.campaign_transport import CampaignStopped
from bench.coding_campaign_worker import (
    CodingCampaignWorkerSession,
    CodingTaskConfig,
    validate_grade,
)
from bench.coding_submission import MINI_REVISION
from bench.joint_campaign import CampaignError, ScheduledRun, _cell_storage_path
from bench.requests_fixture_preflight import DATASET_SHA256, EVAL_SCRIPT_SHA256, TASK


@pytest.mark.parametrize(
    "change",
    [
        {"step_limit": True},
        {"output_cap": 2049},
        {"cheap_model": "openai/strong"},
        {"network_policy": "forbidden"},
        {"rewrite_rules": ("OutputBudget", "OutputBudget")},
        {"max_overhead_ms": float("nan")},
        {"max_overhead_ms": float("inf")},
    ],
)
def test_invalid_configuration_cannot_become_an_enrollment(change):
    with pytest.raises(CampaignStopped):
        replace(CodingTaskConfig("openai/strong", "openai/cheap"), **change).validate()


def scheduled(tmp_path):
    cell = ScheduledRun("coding", TASK, "joint_guarded", 0, 42, 2)
    worker = object.__new__(CodingCampaignWorkerSession)
    worker.config = CodingTaskConfig(
        "openai/strong", "openai/cheap", network_policy="loopback_only"
    )
    worker.cells, worker._next, worker._entered, worker._failed = (
        (cell,),
        0,
        True,
        False,
    )
    worker.campaign_output, worker.workload_id = tmp_path, "coding"
    request = {
        "workload_id": "coding",
        "task_id": TASK,
        "arm": "joint_guarded",
        "repetition": 0,
        "run_seed": 42,
        "schedule_ordinal": 2,
        "model_pair": {"strong": "openai/strong", "cheap": "openai/cheap"},
        "network_policy": "loopback_only",
        "stage": "E0",
        "paper_evidence": False,
        "storage_path": str(_cell_storage_path(tmp_path, cell)),
        "workload_configuration": asdict(worker.config)
        | {"rewrite_rules": list(worker.config.rewrite_rules)},
        "arm_configuration": {},
        "workload_provenance": {
            "upstream_commit": MINI_REVISION,
            "task_universe_sha256": DATASET_SHA256,
        },
    }
    return worker, request


@pytest.mark.parametrize(
    "change",
    [
        {"stage": "C"},
        {"stage": "P"},
        {"stage": "T"},
        {"paper_evidence": True},
        {"stage": "E1"},
        {"schedule_ordinal": 3},
        {"arm": "best_static_joint"},
        {"storage_path": "/tmp/unfrozen-store"},
        {"run_seed": 41},
        {"network_policy": "provider_allowed"},
        {"arm_configuration": {"extra": True}},
        {"workload_configuration": {}},
    ],
)
def test_schedule_or_unavailable_sensor_rejected_before_dispatch(tmp_path, change):
    worker, request = scheduled(tmp_path)
    assert worker._validate_request(request) == worker.cells[0]
    with pytest.raises(CampaignError):
        worker._validate_request(request | change)


def test_stopped_or_completed_session_cannot_resume(tmp_path):
    worker, request = scheduled(tmp_path)
    worker._failed = True
    with pytest.raises(CampaignStopped):
        worker._validate_request(request)
    worker._failed, worker._next = False, 1
    with pytest.raises(CampaignStopped):
        worker._validate_request(request)


def test_fixed_dispatch_identity_binds_parser_settings_and_workload():
    call = {
        "request": {"model": "openai/strong", "max_tokens": 1024},
        "metadata": {
            "actor": "agent",
            "requested_model": "openai/strong",
            "text_action_comparator_version": "mini-swe-bash-v1",
            "text_action_settings_version": "text-action-settings-v1:" + "a" * 64,
        },
    }
    original = _fixed_identity(call, "mini-swe-fixed-dispatch-policy-v1")
    call["metadata"]["text_action_settings_version"] = (
        "text-action-settings-v1:" + "b" * 64
    )
    assert original != _fixed_identity(call, "mini-swe-fixed-dispatch-policy-v1")
    assert original != _fixed_identity(call, "tau-fixed-dispatch-policy-v1")


def test_no_completion_seal_before_every_owned_gate(tmp_path):
    worker, _ = scheduled(tmp_path)
    worker.output = tmp_path
    worker.binding = SimpleNamespace(assert_complete=lambda: None)
    worker._stack = SimpleNamespace(__exit__=lambda *exc: False)
    with pytest.raises(CampaignStopped, match="incomplete"):
        worker.__exit__(None, None, None)
    assert not (tmp_path / "completion.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"complete": False},
        {"official": {"resolved": False}},
        {"resolved": 1},
        {"status": "incomplete_reference_tests"},
        {"task": "different-task"},
        {"eval_script_sha256": "0" * 64},
        {"patch_sha256": "0" * 64},
        {"submission": {}},
    ],
)
def test_partial_or_mismatched_grade_cannot_supply_an_official_score(tmp_path, change):
    path = tmp_path / "submission.json"
    path.write_bytes(b"exact persisted submission\n")
    submission = {"patch_sha256": "a" * 64, "upstream_status": "Submitted"}
    grade = {
        "task": TASK,
        "eval_script_sha256": EVAL_SCRIPT_SHA256,
        "patch_sha256": "a" * 64,
        "status": "graded",
        "complete": True,
        "official": {"resolved": True},
        "resolved": True,
        "submission": submission
        | {"artifact_sha256": sha256(path.read_bytes()).hexdigest()},
    }
    validate_grade(grade, submission, path)
    with pytest.raises(CampaignStopped):
        validate_grade(grade | change, submission, path)
