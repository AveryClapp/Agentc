"""Unsealed acquisition scopes, candidate separation and physical trial labels."""

from copy import deepcopy
from dataclasses import replace

import pytest

from bench.campaign_accounting import digest
from bench.campaign_records import calibration_metadata_json
from bench.campaign_transport import CampaignStopped
from bench.policy_calibration import (
    CalibrationError,
    CalibrationTrialBundle,
    FrozenPolicyBundle,
    fit_policies,
)
from bench.tau_task_worker import TauCell, TauTaskConfig, TauTaskWorker
from test_policy_calibration import MODELS, contract, training, validation


def bundle():
    registry, _ = training()
    return CalibrationTrialBundle.create(
        contract(), registry, partition="training", repetitions=2
    )


def test_counterbalanced_schedule_keeps_equal_arm_candidates_separate(tmp_path):
    scope = bundle()
    assert len(scope.cells) == 24
    cells = tuple(
        TauCell(c["task_id"], c["arm"], c["repetition"], c["seed"], c["ordinal"])
        for c in scope.cells
    )
    scope.validate_cells(cells)
    config = TauTaskConfig(
        task_ids=("train-a", "train-b"),
        task_inventory_sha256="a" * 64,
        strong_model=MODELS["strong"],
        cheap_model=MODELS["cheap"],
        user_model=MODELS["cheap"],
        network_policy="loopback_only",
    )
    worker = TauTaskWorker(
        transport=None,
        source=tmp_path,
        output=tmp_path / "acquisition",
        config=config,
        cells=cells,
        policy_bundle=scope,
    )
    assert len({worker.storage_for_cell(c) for c in cells}) == 12
    with pytest.raises(CampaignStopped, match="invalid fresh ordered"):
        TauTaskWorker(
            transport=None,
            source=tmp_path,
            output=tmp_path / "other",
            config=config,
            cells=cells,
        )
    with pytest.raises(CalibrationError, match="registered calibration schedule"):
        scope.validate_cells(cells[:-1])
    assert bundle().lock == scope.lock


@pytest.mark.parametrize(
    "change", [None, "held-out", "paper", "wrong-cell", "wrong-task", "wrong-protocol"]
)
def test_trial_authorization_cannot_become_a_held_out_policy(change):
    scope = bundle()
    cell = scope.cells[0]
    args = dict(
        workload_id="tau",
        models=MODELS,
        task_ids=[cell["task_id"]],
        stage="E0",
        paper_evidence=False,
        network_policy="loopback_only",
        output_cap=1024,
        quality_margin=-0.02,
        task_universe_sha256="a" * 64,
        protocol_sha256="b" * 64,
        cell=cell.copy(),
    )
    if change == "held-out":
        args.update(stage="T", network_policy="provider_allowed", paper_evidence=True)
    elif change == "paper":
        args["paper_evidence"] = True
    elif change == "wrong-cell":
        args["cell"]["ordinal"] += 1
    elif change == "wrong-task":
        args["task_ids"] = ["held-out"]
    elif change == "wrong-protocol":
        args["protocol_sha256"] = "c" * 64
    if change:
        with pytest.raises(CalibrationError):
            scope.authorize(**args)
    else:
        scope.authorize(**args)
    with pytest.raises(CalibrationError, match="unknown policy lock schema"):
        FrozenPolicyBundle.from_lock(scope.lock)


def test_validation_acquisition_is_bound_to_the_unchanged_fit():
    registry, rows = training()
    fitted = fit_policies(contract(), registry, rows)
    reg, _ = validation(fitted)
    scope = CalibrationTrialBundle.create(
        contract(), reg, partition="validation", fitted=fitted, repetitions=1
    )
    assert len(scope.cells) == 8
    changed = deepcopy(fitted)
    changed["training_task_ids"].append("validation-a")
    with pytest.raises(CalibrationError, match="unchanged training fit"):
        CalibrationTrialBundle.create(
            contract(), reg, partition="validation", fitted=changed
        )
    reg["route_then_rewrite"]["configuration"] = deepcopy(
        reg["route_then_rewrite"]["configuration"]
    )
    reg["route_then_rewrite"]["configuration"]["router"]["cheap_max_prompt_bytes"] += 1
    with pytest.raises(CalibrationError, match="changed a fitted policy"):
        CalibrationTrialBundle.create(
            contract(), reg, partition="validation", fitted=fitted
        )


@pytest.mark.parametrize(
    "change", ["schedule", "registry", "partition", "repetitions", "digest"]
)
def test_trial_manifest_mutation_is_rejected_even_with_a_new_root_checksum(change):
    value = bundle().lock
    body = value["selections"]
    if change == "schedule":
        body["cells"][0]["trial_id"] = "foreign"
    elif change == "registry":
        del body["registry"]["rewrite-strong"]
    elif change == "partition":
        body["partition"] = "held_out"
    elif change == "repetitions":
        body["repetitions"] = True
    if change != "digest":
        value["sha256"] = digest(body)
    else:
        value["sha256"] = "0" * 64
    import json

    with pytest.raises(CalibrationError):
        CalibrationTrialBundle(json.dumps(value))


def test_original_and_candidate_rows_cannot_be_relabeled_as_other_trials():
    registry, rows = training()
    rows[0]["trial_id"] = "cheap-original"
    with pytest.raises(CalibrationError, match="trial provenance"):
        fit_policies(contract(), registry, rows)
    registry, rows = training()
    rows[0]["calls"][0]["metadata"]["calibration_trial_id"] = "wrong"
    rows[0]["task"]["calls_sha256"] = digest(rows[0]["calls"])
    with pytest.raises(CalibrationError, match="relabeled a physical"):
        fit_policies(contract(), registry, rows)


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"calibration_trial_id": "trial"},
        {
            "calibration_trial_id": "trial",
            "calibration_authorization_sha256": "invalid",
        },
        {
            "calibration_trial_id": "trial",
            "calibration_authorization_sha256": "a" * 64,
            "actor": "judge",
        },
    ],
)
def test_invalid_provenance_cannot_overwrite_reserved_actor_metadata(metadata):
    with pytest.raises(CampaignStopped):
        calibration_metadata_json(metadata)


def test_provider_calibration_keeps_five_repetitions_and_cannot_use_fixture_shapes():
    c = replace(contract(), stage="C", evidence_kind="provider_calibration")
    registry, _ = training()
    assert (
        len(CalibrationTrialBundle.create(c, registry, partition="training").cells)
        == 60
    )
    with pytest.raises(CalibrationError, match="paired calibration repetition"):
        CalibrationTrialBundle.create(c, registry, partition="training", repetitions=1)


def test_coding_trial_candidates_use_separate_stores_with_duplicate_arm_keys(tmp_path):
    import json
    from bench.coding_campaign_worker import (
        CodingCampaignWorkerSession,
        CodingTaskConfig,
    )
    from bench.joint_campaign import ScheduledRun
    from bench.requests_fixture_preflight import BASE_COMMIT, DATASET_SHA256, TASK

    c = replace(
        contract(),
        workload_id="coding",
        task_universe_sha256=DATASET_SHA256,
        task_partitions_json=json.dumps(
            {
                "training": [TASK],
                "validation": ["unused-unit-validation"],
                "held_out": ["unused-unit-held-out"],
            }
        ),
    )
    registry, _ = training()
    scope = CalibrationTrialBundle.create(
        c, registry, partition="training", repetitions=1
    )
    cells = tuple(
        ScheduledRun(
            "coding", r["task_id"], r["arm"], r["repetition"], r["seed"], r["ordinal"]
        )
        for r in scope.cells
    )
    public = {
        "instance_id": TASK,
        "repo": "psf/requests",
        "base_commit": BASE_COMMIT,
        "problem_statement": "Constructor-only unit fixture",
        "version": "unit",
    }
    worker = CodingCampaignWorkerSession(
        transport=None,
        source=tmp_path,
        dataset=tmp_path / "data",
        pytest_wheels=tmp_path,
        output=tmp_path / "acquisition",
        campaign_output=tmp_path,
        workload_id="coding",
        public_task=public,
        cells=cells,
        config=CodingTaskConfig(
            MODELS["strong"], MODELS["cheap"], network_policy="loopback_only"
        ),
        policy_bundle=scope,
    )
    assert len(cells) == len({worker.storage_for_cell(cell) for cell in cells}) == 6
    assert len({cell.key for cell in cells}) == 3
