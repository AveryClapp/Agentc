"""Frozen order/store isolation, null-score cells, and completion gates."""

from dataclasses import asdict, replace
from types import SimpleNamespace
import json
import sys

import pytest

from bench import swe_agent_campaign_worker as worker
from bench.campaign_accounting import digest
from bench.joint_campaign import ScheduledRun, _cell_storage_path, derive_run_seed
from bench.swebench_cohort import (
    AGENT_REVISION,
    DATASET_SHA256,
    PROTOCOL_SHA256,
    CohortError,
)


@pytest.fixture
def session(tmp_path, monkeypatch):
    tasks = [
        {
            "task_id": f"repo__task-{i}",
            "stage": stage,
            "public_task_sha256": digest({"public": i}),
        }
        for i, stage in enumerate(("C", "P", "T"))
    ]
    inputs = SimpleNamespace(cohort={"tasks": tasks}, verify=lambda: None)
    config = worker.SweAgentTaskConfig(
        "openai/strong", "openai/cheap", network_policy="loopback_only"
    )
    cell = ScheduledRun(
        "coding",
        tasks[0]["task_id"],
        "joint_guarded",
        0,
        derive_run_seed("coding", tasks[0]["task_id"], "joint_guarded", 0),
        3,
    )
    report = tmp_path / "report"
    report.mkdir()
    (report / "summary.json").write_text("immutable report")
    obj = worker.SweAgentCampaignWorkerSession(
        transport=SimpleNamespace(session_id="owned"),
        inputs=inputs,
        environment_report=report,
        bundle=SimpleNamespace(verify=lambda: None),
        output=tmp_path / "worker",
        campaign_output=tmp_path / "campaign",
        workload_id="coding",
        cells=(cell,),
        config=config,
        process_isolation=False,
    )
    obj.output.mkdir()
    obj._entered = True
    obj._report_sha256 = worker.file_sha(report / "summary.json")
    obj.enrollment = {"cohort_sha256": digest(inputs.cohort), "runtime": {}}
    obj.enrollment_sha256 = digest(obj.enrollment)
    obj.admission = {
        "checks": [
            {"task_id": t["task_id"], "environment_admitted": False} for t in tasks
        ]
    }
    obj.binding = SimpleNamespace(
        assert_complete=lambda: None, settings_version="fixture-settings"
    )
    obj._stack = SimpleNamespace(__exit__=lambda *args: None)
    obj._ledger_event_start = 0
    obj.transport.ledger = tmp_path / "ledger"
    monkeypatch.setattr(
        worker, "read_snapshot", lambda path: SimpleNamespace(events=[])
    )
    monkeypatch.setattr(worker, "runtime_manifest", lambda: {})
    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(errors=SimpleNamespace(ImageNotFound=LookupError)),
    )
    request = {
        "schema_version": 1,
        "campaign_id": "fixture",
        "family": "coding",
        "split": "engineering",
        "workload_id": cell.workload_id,
        "task_id": cell.task_id,
        "arm": cell.arm,
        "repetition": cell.repetition,
        "run_seed": cell.run_seed,
        "schedule_ordinal": cell.ordinal,
        "model_pair": {"strong": config.strong_model, "cheap": config.cheap_model},
        "network_policy": config.network_policy,
        "stage": "E0",
        "paper_evidence": False,
        "protocol_sha256": PROTOCOL_SHA256,
        "storage_path": str(_cell_storage_path(obj.campaign_output, cell)),
        "workload_configuration": json.loads(json.dumps(asdict(config))),
        "arm_configuration": {},
        "workload_provenance": {
            "upstream_commit": AGENT_REVISION,
            "task_universe_sha256": DATASET_SHA256,
        },
    }
    return obj, request


@pytest.mark.parametrize(
    "change",
    [
        {"output_cap": True},
        {"output_cap": 2049},
        {"cheap_model": "openai/strong"},
        {"network_policy": "forbidden"},
        {"rewrite_rules": ("OutputBudget", "OutputBudget")},
        {"max_overhead_ms": float("nan")},
        {"max_overhead_ms": float("inf")},
    ],
)
def test_invalid_configuration_does_not_enroll(change):
    with pytest.raises(CohortError):
        replace(
            worker.SweAgentTaskConfig("openai/strong", "openai/cheap"), **change
        ).validate()


@pytest.mark.parametrize(
    "change",
    [
        {"stage": "C"},
        {"stage": "P"},
        {"stage": "T"},
        {"paper_evidence": True},
        {"stage": "E1"},
        {"schedule_ordinal": 4},
        {"run_seed": 1},
        {"storage_path": "/tmp/foreign-store"},
        {"arm_configuration": {"override": True}},
        {"workload_configuration": {}},
        {"protocol_sha256": "a" * 64},
    ],
)
def test_changed_schedule_or_unqualified_stage_is_rejected(session, change):
    obj, request = session
    assert obj._validate_request(request) == obj.cells[0]
    with pytest.raises(CohortError):
        obj._validate_request(request | change)


def test_public_partitions_models_arms_and_repetitions_have_distinct_stores(session):
    obj, request = session
    original = obj.cells[0]
    paths = {obj.storage_for_cell(replace(original, task_id=t)) for t in obj.tasks}
    assert len(paths) == 3
    assert (
        obj.storage_for_cell(replace(original, arm="trace_only_fixed_strong"))
        not in paths
    )
    assert obj.storage_for_cell(replace(original, repetition=1)) not in paths
    old = obj.storage_for_cell(original)
    obj.config = replace(obj.config, cheap_model="anthropic/cheap")
    assert obj.storage_for_cell(original) != old


def test_unavailable_cell_is_recorded_without_dispatch_or_invented_score(session):
    obj, request = session
    records = obj.run(request)
    assert len(records) == 1 and obj._next == 1
    assert records[0]["task_status"] == "incomplete"
    assert records[0]["official_score"] is None
    assert records[0]["metric_availability"]["official_score"] is False
    assert records[0]["provider_call_count"] == 0
    assert records[0]["all_call_cost_usd"] == "0"
    assert records[0]["conformance"]["official_score"] is False
    assert records[0]["cohort_stage"] == "C"
    assert not obj.storage_for_cell(obj.cells[0]).exists()
    obj.__exit__(None, None, None)
    completion = json.loads((obj.output / "completion.json").read_text())
    assert completion["visited_cells"] == 1
    assert completion["official_score_unknown_cells"] == 1
    with pytest.raises(CohortError):
        obj.run(request)


def test_admitted_but_evicted_image_retains_provisioning_failure(session):
    obj, request = session
    obj.admission["checks"][0]["environment_admitted"] = True
    obj.inputs.pins = {"checks": [{"task_id": request["task_id"]}]}

    def unavailable(pin):
        raise worker.ProvisioningError("explicit pull limit")

    obj.image_cache = SimpleNamespace(acquire=unavailable)
    record = obj.run(request)[0]
    assert record["official_score"] is None
    assert record["termination_reason"] == "image_provisioning_unavailable"
    assert obj._next == 1 and not obj._failed


def test_uncertain_image_ownership_stops_without_a_completion_seal(session):
    obj, request = session
    obj.admission["checks"][0]["environment_admitted"] = True
    obj.inputs.pins = {"checks": [{"task_id": request["task_id"]}]}

    def unknown(pin):
        raise worker.ImageOwnershipError("foreign reference")

    obj.image_cache = SimpleNamespace(acquire=unknown)
    with pytest.raises(worker.ImageOwnershipError):
        obj.run(request)
    assert obj._failed and obj._next == 0
    assert not (obj.output / "completion.json").exists()


def test_omitted_owned_bill_prevents_completion_even_when_all_cells_visited(
    session, monkeypatch
):
    obj, request = session
    obj.run(request)
    monkeypatch.setattr(
        worker,
        "read_snapshot",
        lambda path: SimpleNamespace(
            events=[
                {
                    "event": "result",
                    "session_id": "owned",
                    "cost_usd": "0.1",
                    "metadata": {"cell_id": "foreign"},
                }
            ]
        ),
    )
    with pytest.raises(CohortError, match="billed invocations"):
        obj.__exit__(None, None, None)
    assert not (obj.output / "completion.json").exists()


def test_partial_schedule_never_seals_completion(session):
    obj, request = session
    with pytest.raises(CohortError, match="incomplete cells"):
        obj.__exit__(None, None, None)
    assert not (obj.output / "completion.json").exists()
