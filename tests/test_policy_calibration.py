"""Selection separation, owned receipt consistency and sealed task partitions."""

from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from bench.campaign_accounting import digest
from bench.policy_calibration import (
    ARMS,
    CalibrationContract,
    CalibrationError,
    configuration_sha256,
    fit_policies,
    FrozenPolicyBundle,
    main,
    policy_identity_sha256,
    read_artifact,
    seal_policies,
)
from bench.tau_campaign_worker import normalize_tau_cell
from test_tau_campaign_worker import acquisition

MODELS = {"strong": "openai/strong", "cheap": "openai/cheap"}


def config(target, cap=None):
    return {
        "strong_model": MODELS["strong"],
        "cheap_model": MODELS["cheap"],
        "divergence_threshold": 0.05,
        "static_plan": {
            "target_model": target,
            "rewrites": []
            if cap is None
            else [{"rule": "OutputBudget", "max_output_tokens": cap}],
        },
        "router": None,
        "rewrite_by_target": {},
    }


def contract():
    return CalibrationContract(
        "tau",
        json.dumps(MODELS),
        1024,
        -0.02,
        "E0",
        "engineering_fixture",
        "a" * 64,
        "b" * 64,
        task_partitions_json=json.dumps(
            {
                "training": ["train-a", "train-b"],
                "validation": ["validation-a", "validation-b"],
                "held_out": ["held-out"],
            }
        ),
    )


@pytest.mark.parametrize(
    "field",
    [
        "workload_id",
        "models",
        "task_ids",
        "output_cap",
        "task_universe_sha256",
        "network_policy",
        "protocol_sha256",
        "quality_margin",
    ],
)
def test_fixture_lock_rejects_foreign_deployment(field):
    c = contract()
    registry, rows = training()
    fit = fit_policies(c, registry, rows)
    reg, val = validation(fit)
    bundle = FrozenPolicyBundle.from_lock(
        seal_policies(c, fit, reg, val, held_out_task_ids=["held-out"])
    )
    arguments = {
        "workload_id": "tau",
        "models": MODELS,
        "task_ids": ["held-out"],
        "stage": "E0",
        "paper_evidence": False,
        "network_policy": "loopback_only",
        "output_cap": 1024,
        "task_universe_sha256": "a" * 64,
        "protocol_sha256": "b" * 64,
        "quality_margin": -0.02,
    }
    arguments[field] = {
        "workload_id": "foreign",
        "models": {"strong": "foreign", "cheap": MODELS["cheap"]},
        "task_ids": ["train-a"],
        "output_cap": 2048,
        "task_universe_sha256": "c" * 64,
        "network_policy": "provider_allowed",
        "protocol_sha256": "c" * 64,
        "quality_margin": -0.5,
    }[field]
    with pytest.raises(CalibrationError):
        bundle.authorize(**arguments)


def test_physical_receipt_cannot_be_reused_between_trials():
    registry, rows = training()
    rows[1]["calls"][0]["attempt_id"] = rows[0]["calls"][0]["attempt_id"]
    rows[1]["calls"][0]["billed_result"]["attempt_id"] = rows[0]["calls"][0][
        "attempt_id"
    ]
    rows[1]["calls"][0]["http_receipt"]["attempt_id"] = rows[0]["calls"][0][
        "attempt_id"
    ]
    rows[1]["task"]["calls_sha256"] = digest(rows[1]["calls"])
    with pytest.raises(CalibrationError, match="reuses"):
        fit_policies(contract(), registry, rows)


def row(name, trial, task_id, *, cost="0.001", score=1.0, repetition=0):
    result, calls, request = acquisition()
    request["task_id"] = result["task_id"] = task_id
    request["arm"] = result["arm"] = (
        "unmodified_fixed_strong"
        if trial["role"] == "reference"
        else "fixed_cheap"
        if trial["role"] == "cheap_original"
        else trial["mode"]
    )
    request["repetition"] = result["repetition"] = repetition
    result["official_score"] = score
    result["cell_id"] = digest(
        {
            "task_id": task_id,
            "arm": result["arm"],
            "repetition": result["repetition"],
            "seed": result["seed"],
            "ordinal": result["ordinal"],
        }
    )
    call = calls[0]
    call_id = digest({"trial": name, "task": task_id, "repetition": repetition})
    call["call_id"] = call["billed_result"]["id"] = call["http_receipt"]["id"] = call_id
    call["attempt_id"] = call["billed_result"]["attempt_id"] = call["http_receipt"][
        "attempt_id"
    ] = call_id
    metadata = call["metadata"]
    metadata["arm"] = result["arm"]
    metadata.update(
        calibration_trial_id=name, calibration_authorization_sha256="f" * 64
    )
    metadata["task_id"], metadata["cell_id"] = task_id, result["cell_id"]
    if trial["role"] in {"reference", "cheap_original"}:
        target = MODELS["strong" if trial["role"] == "reference" else "cheap"]
        call["planning"] = None
        call["candidate_count"] = None
        call["planning_cost_attributed_to_this_call"] = False
        metadata.pop("native_plan_json", None)
    else:
        cfg = trial["configuration"]
        target = (
            cfg["static_plan"]["target_model"]
            if cfg["static_plan"]
            else MODELS["cheap"]
        )
        call["planning"]["selection_audit"]["policy"] = {
            "configuration_sha256": configuration_sha256(cfg),
            "calibration_lock_sha256": "f" * 64,
        }
        call["planning"]["selection_audit"]["planner_mode"] = trial["mode"]
    call["request"]["model"] = call["billed_result"]["model"] = target
    metadata["requested_model"] = (
        MODELS["strong"]
        if trial["role"] not in {"reference", "cheap_original"}
        else target
    )
    call["request"]["messages"] = [{"role": "user", "content": "request " + task_id}]
    call["billed_result"]["cost_usd"] = cost
    call["billed_result"]["usage"]["cost"] = cost
    call["http_receipt"]["response"]["usage"]["cost"] = cost
    call["request_digest"], call["response_digest"] = (
        digest(call["request"]),
        digest(call["http_receipt"]["response"]),
    )
    result["all_call_cost_usd"], result["calls_sha256"] = cost, digest(calls)
    task = normalize_tau_cell(result, calls, request)[0]
    task.update(
        calls_sha256=digest(calls),
        provider_call_count=len(calls),
        all_call_cost_usd=cost,
        calibration_trial_id=name,
        calibration_authorization_sha256="f" * 64,
    )
    return {"trial_id": name, "task": task, "calls": calls}


def training():
    registry = {
        "reference": {"role": "reference", "mode": "reference"},
        "cheap-original": {"role": "cheap_original", "mode": "original"},
        "static-good": {
            "role": "static_candidate",
            "mode": "best_static_joint",
            "configuration": config(MODELS["cheap"], 128),
        },
        "static-bad": {
            "role": "static_candidate",
            "mode": "best_static_joint",
            "configuration": config(MODELS["cheap"], 64),
        },
        "rewrite-strong": {
            "role": "fixed_recipe",
            "mode": "best_static_joint",
            "configuration": config(MODELS["strong"], 384),
        },
        "rewrite-cheap": {
            "role": "fixed_recipe",
            "mode": "best_static_joint",
            "configuration": config(MODELS["cheap"], 128),
        },
    }
    rows = [
        row(
            name,
            trial,
            task,
            cost="0.00001"
            if name == "static-bad"
            else "0.001"
            if name == "reference"
            else "0.0005",
            score=0.0 if name == "static-bad" else 1.0,
        )
        for name, trial in registry.items()
        for task in ["train-a", "train-b"]
    ]
    for item in rows:
        item["task"]["calibration_registry_sha256"] = digest(registry)
    return registry, rows


def validation(fit):
    registry = {
        "reference": {"role": "reference", "mode": "reference"},
        **{
            mode: {
                "role": "validation_policy",
                "mode": mode,
                "configuration": fit["configurations"][mode],
            }
            for mode in ARMS
        },
    }
    rows = [
        row(name, trial, task)
        for name, trial in registry.items()
        for task in ["validation-a", "validation-b"]
    ]
    for item in rows:
        item["task"]["calibration_registry_sha256"] = digest(registry)
    return registry, rows


def test_independent_training_and_measured_validation_seal_only_disjoint_fixture_data():
    registry, rows = training()
    fit = fit_policies(contract(), registry, rows)
    assert fit["sealed"] is False
    assert fit["selected_trials"]["best_static_joint"] == "static-good"
    assert (
        fit["configurations"]["route_then_rewrite"]["rewrite_by_target"][
            MODELS["strong"]
        ][0]["max_output_tokens"]
        == 384
    )
    reg, val = validation(fit)
    lock = seal_policies(contract(), fit, reg, val, held_out_task_ids=["held-out"])
    assert lock["sha256"] == digest(lock["selections"])
    assert lock["selections"]["source_stage"] == "E0"
    assert lock["selections"]["evidence_kind"] == "engineering_fixture"
    bundle = FrozenPolicyBundle.from_lock(lock)
    bundle.authorize(
        workload_id="tau",
        models=MODELS,
        task_ids=["held-out"],
        stage="E0",
        paper_evidence=False,
        network_policy="loopback_only",
        output_cap=1024,
        task_universe_sha256="a" * 64,
        protocol_sha256="b" * 64,
        quality_margin=-0.02,
    )
    with pytest.raises(CalibrationError):
        bundle.authorize(
            workload_id="tau",
            models=MODELS,
            task_ids=["held-out"],
            stage="P",
            paper_evidence=True,
            network_policy="provider_allowed",
            output_cap=1024,
            task_universe_sha256="a" * 64,
            protocol_sha256="b" * 64,
            quality_margin=-0.02,
        )


@pytest.mark.parametrize(
    "change",
    [
        "held-out-stage",
        "missing-cell",
        "duplicate",
        "bill",
        "wrong-config",
        "unscored",
        "fixture-as-provider",
    ],
)
def test_invalid_or_incomplete_calibration_does_not_select(change):
    registry, rows = training()
    c = contract()
    if change == "held-out-stage":
        rows[0]["task"]["stage"] = "T"
    elif change == "missing-cell":
        rows.pop()
    elif change == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif change == "bill":
        rows[0]["calls"][0]["billed_result"]["cost_usd"] = "0"
    elif change == "wrong-config":
        registry["static-good"]["configuration"]["static_plan"]["rewrites"][0][
            "max_output_tokens"
        ] = 256
    elif change == "unscored":
        rows[0]["task"]["task_status"] = "incomplete"
    else:
        c = replace(c, stage="C", evidence_kind="provider_calibration")
    with pytest.raises(CalibrationError):
        fit_policies(c, registry, rows)


@pytest.mark.parametrize(
    "failure", ["overlap", "held-out", "missing-policy", "wrong-policy", "quality"]
)
def test_seal_requires_exact_policies_and_complete_held_out_validation(failure):
    registry, rows = training()
    fit = fit_policies(contract(), registry, rows)
    reg, val = validation(fit)
    held = ["held-out"]
    if failure == "overlap":
        fit["training_task_ids"].append("validation-a")
    elif failure == "held-out":
        held.append("train-a")
    elif failure == "missing-policy":
        val.pop()
    elif failure == "wrong-policy":
        reg["route_then_rewrite"]["configuration"] = config(MODELS["strong"])
    else:
        for r in val:
            if r["trial_id"] == "rewrite_then_route":
                r["task"]["official_score"] = 0
    with pytest.raises(CalibrationError):
        seal_policies(contract(), fit, reg, val, held_out_task_ids=held)


def test_omitting_a_task_from_every_trial_is_still_incomplete():
    registry, rows = training()
    rows = [r for r in rows if r["task"]["task_id"] != "train-b"]
    with pytest.raises(CalibrationError, match="enrolled task partition"):
        fit_policies(contract(), registry, rows)


def test_optimized_reference_cannot_train_the_original_request_router():
    registry, rows = training()
    rows[0]["calls"][0]["planning"] = {"selection_audit": {}}
    rows[0]["task"]["calls_sha256"] = digest(rows[0]["calls"])
    with pytest.raises(CalibrationError):
        fit_policies(contract(), registry, rows)


def provider_shape_rows(rows, registry):
    """Fabricated unit metadata only; these are never acquisition evidence."""
    expanded = []
    for source in rows:
        for repetition in range(5):
            item = row(
                source["trial_id"],
                registry[source["trial_id"]],
                source["task"]["task_id"],
                repetition=repetition,
                cost=source["task"]["all_call_cost_usd"],
                score=source["task"]["official_score"],
            )
            item["task"].update(
                stage="C", synthetic_billing=False, safety_failure=False
            )
            item["task"]["calibration_registry_sha256"] = digest(registry)
            item["task"]["conformance"]["provider_accounting"] = True
            item["task"]["metric_availability"]["safety_failure"] = True
            expanded.append(item)
    return expanded


def guard_admission(fit):
    return {
        "record_type": "guard_calibration_admission",
        "source_stage": "C",
        "synthetic_billing": False,
        "policies": {
            policy_identity_sha256(mode, cfg): {
                "mode": mode,
                "configuration_sha256": configuration_sha256(cfg),
                "divergence_threshold": cfg["divergence_threshold"],
                "paired_observations": 20,
                "harmful_cases": 10,
                "harmful_detected": 9,
                "benign_cases": 20,
                "benign_disabled": 1,
                "damage_bound_met": True,
                "complete_plan_identity_measured": True,
            }
            for mode, cfg in fit["configurations"].items()
        },
    }


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "missing",
        "order",
        "recall",
        "false-disables",
        "pairs",
        "fixture",
        "damage",
    ],
)
def test_provider_lock_rejects_unmeasured_guard_summaries_even_when_flags_pass(failure):
    c = replace(contract(), stage="C", evidence_kind="provider_calibration")
    registry, rows = training()
    fit = fit_policies(c, registry, provider_shape_rows(rows, registry))
    reg, val = validation(fit)
    val = provider_shape_rows(val, reg)
    admission = guard_admission(fit)
    cfg = fit["configurations"]["rewrite_then_route"]
    identity = policy_identity_sha256("rewrite_then_route", cfg)
    assert identity != policy_identity_sha256("route_then_rewrite", cfg)
    entry = admission["policies"][identity]
    if failure == "missing":
        admission = None
    elif failure == "order":
        del admission["policies"][identity]
    elif failure == "recall":
        entry["harmful_detected"] = 8
    elif failure == "false-disables":
        entry["benign_disabled"] = 2
    elif failure == "pairs":
        entry["paired_observations"] = 19
    elif failure == "fixture":
        admission["synthetic_billing"] = True
    elif failure == "damage":
        entry["damage_bound_met"] = False
    if failure:
        with pytest.raises(CalibrationError):
            seal_policies(
                c,
                fit,
                reg,
                val,
                held_out_task_ids=["held-out"],
                guard_admission=admission,
            )
    else:
        with pytest.raises(CalibrationError, match="summary-only guard admissions"):
            seal_policies(
                c,
                fit,
                reg,
                val,
                held_out_task_ids=["held-out"],
                guard_admission=admission,
            )


def test_rehashed_provider_lock_cannot_load_a_self_reported_guard_admission():
    registry, rows = training()
    fitted = fit_policies(contract(), registry, rows)
    reg, validation_rows = validation(fitted)
    lock = seal_policies(
        contract(), fitted, reg, validation_rows, held_out_task_ids=["held-out"]
    )
    selection = lock["selections"]
    selection["source_stage"] = selection["contract"]["stage"] = "C"
    selection["evidence_kind"] = selection["contract"]["evidence_kind"] = (
        "provider_calibration"
    )
    selection["guard_admission"] = guard_admission(fitted)
    lock["sha256"] = digest(selection)
    with pytest.raises(CalibrationError, match="summary-only guard admissions"):
        FrozenPolicyBundle.from_lock(lock)


def test_provider_corpus_requires_exactly_five_paired_repetitions():
    c = replace(contract(), stage="C", evidence_kind="provider_calibration")
    registry, rows = training()
    expanded = provider_shape_rows(rows, registry)
    expanded = [r for r in expanded if r["task"]["repetition"] != 4]
    with pytest.raises(CalibrationError, match="five paired"):
        fit_policies(c, registry, expanded)


def test_direct_bundle_construction_cannot_skip_lock_validation():
    with pytest.raises(CalibrationError):
        FrozenPolicyBundle('{"selections":{},"sha256":"invalid"}')


@pytest.mark.parametrize("content", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_artifact_reader_rejects_ambiguous_json(tmp_path, content):
    path = tmp_path / "ambiguous.json"
    path.write_text(content)
    with pytest.raises(CalibrationError):
        read_artifact(path)


def test_cli_fit_and_seal_round_trip_without_overwriting_artifacts(tmp_path, capsys):
    registry, rows = training()
    values = {
        "contract": contract().__dict__,
        "training-registry": registry,
        "training": rows,
        "held-out": ["held-out"],
    }
    paths = {name: tmp_path / (name + ".json") for name in values}
    for name, value in values.items():
        paths[name].write_text(json.dumps(value))
    fitted = tmp_path / "fit.json"
    common = ["--contract", str(paths["contract"])]
    main(
        [
            "fit",
            *common,
            "--registry",
            str(paths["training-registry"]),
            "--rollouts",
            str(paths["training"]),
            "--output",
            str(fitted),
        ]
    )
    reg, val = validation(read_artifact(fitted))
    for name, value in {"validation-registry": reg, "validation": val}.items():
        paths[name] = tmp_path / (name + ".json")
        paths[name].write_text(json.dumps(value))
    lock = tmp_path / "lock.json"
    arguments = [
        "seal",
        *common,
        "--registry",
        str(paths["validation-registry"]),
        "--rollouts",
        str(paths["validation"]),
        "--fitted",
        str(fitted),
        "--held-out-tasks",
        str(paths["held-out"]),
        "--output",
        str(lock),
    ]
    main(arguments)
    FrozenPolicyBundle.from_lock(read_artifact(lock))
    assert fitted.stat().st_mode & 0o777 == lock.stat().st_mode & 0o777 == 0o600
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["operation"] for e in events] == ["fit", "seal"]
    assert all(e["provider_calls"] == 0 and e["source_stage"] == "E0" for e in events)
    original = lock.read_bytes()
    with pytest.raises(FileExistsError):
        main(arguments)
    assert lock.read_bytes() == original


def declared_bundle(workload_id, task_ids, universe="a" * 64):
    registry, rows = training()
    fit = fit_policies(contract(), registry, rows)
    c = replace(
        contract(),
        workload_id=workload_id,
        task_universe_sha256=universe,
        task_partitions_json=None,
    )
    return FrozenPolicyBundle.engineering_fixture(
        c, fit["configurations"], task_ids=task_ids
    )


@pytest.mark.parametrize(
    "change", [None, "missing-lock", "protocol", "margin", "replaced-bundle"]
)
def test_coding_policy_gate_matches_the_enrolled_lock_before_dispatch(tmp_path, change):
    from bench.joint_campaign import CampaignError, _cell_storage_path
    from bench.requests_fixture_preflight import DATASET_SHA256, TASK
    from test_coding_campaign_worker import scheduled

    worker, request = scheduled(tmp_path)
    worker.cells = (replace(worker.cells[0], arm="route_then_rewrite"),)
    worker.policy_bundle = declared_bundle("coding", [TASK], DATASET_SHA256)
    worker._policy_lock_sha256 = worker.policy_bundle.lock["sha256"]
    request.update(
        arm=worker.cells[0].arm,
        storage_path=str(_cell_storage_path(tmp_path, worker.cells[0])),
        calibration_lock=worker.policy_bundle.lock,
        protocol_sha256="b" * 64,
        quality_margin=-0.02,
    )
    if change == "missing-lock":
        del request["calibration_lock"]
    elif change == "protocol":
        request["protocol_sha256"] = "c" * 64
    elif change == "margin":
        request["quality_margin"] = -0.5
    elif change == "replaced-bundle":
        lock = worker.policy_bundle.lock
        lock["selections"]["configurations"]["best_static_joint"]["static_plan"][
            "rewrites"
        ][0]["max_output_tokens"] = 256
        lock["sha256"] = digest(lock["selections"])
        worker.policy_bundle = FrozenPolicyBundle.from_lock(lock)
        request["calibration_lock"] = worker.policy_bundle.lock
    if change:
        with pytest.raises((CalibrationError, CampaignError)):
            worker._validate_request(request)
    else:
        assert worker._validate_request(request) == worker.cells[0]


@pytest.mark.parametrize(
    "change", [None, "missing-lock", "protocol", "margin", "replaced-bundle", "paper"]
)
def test_tau_policy_gate_matches_the_enrolled_lock_before_dispatch(tmp_path, change):
    from bench.joint_campaign import CampaignError
    from bench.tau_campaign_worker import TauCampaignWorkerSession
    from bench.tau_task_worker import TauCell

    class DispatchReached(Exception):
        pass

    def run_next():
        raise DispatchReached

    bundle = declared_bundle("tau", ["held-out"])
    cell = TauCell("held-out", "rewrite_then_route", 0, 42, 2)
    config = SimpleNamespace(
        strong_model=MODELS["strong"],
        cheap_model=MODELS["cheap"],
        task_ids=("held-out",),
        network_policy="loopback_only",
        agent_output_cap=1024,
        task_timeout_seconds=10,
    )
    worker = SimpleNamespace(
        config=config,
        cells=(cell,),
        _next=0,
        run_next=run_next,
        policy_bundle=bundle,
        _policy_lock_sha256=bundle.lock["sha256"],
        storage_for_cell=lambda cell: tmp_path.resolve(),
    )
    request = {
        "stage": "E0",
        "paper_evidence": False,
        "workload_id": "tau",
        "model_pair": MODELS,
        "network_policy": "loopback_only",
        "worker_timeout_seconds": 20,
        "storage_path": str(tmp_path),
        "task_id": cell.task_id,
        "arm": cell.arm,
        "repetition": cell.repetition,
        "run_seed": cell.seed,
        "schedule_ordinal": cell.ordinal,
        "calibration_lock": bundle.lock,
        "workload_provenance": {"task_universe_sha256": "a" * 64},
        "protocol_sha256": "b" * 64,
        "quality_margin": -0.02,
    }
    if change == "missing-lock":
        del request["calibration_lock"]
    elif change == "protocol":
        request["protocol_sha256"] = "c" * 64
    elif change == "margin":
        request["quality_margin"] = -0.5
    elif change == "paper":
        request["paper_evidence"] = True
    elif change == "replaced-bundle":
        lock = bundle.lock
        lock["selections"]["configurations"]["best_static_joint"]["static_plan"][
            "rewrites"
        ][0]["max_output_tokens"] = 256
        lock["sha256"] = digest(lock["selections"])
        worker.policy_bundle = FrozenPolicyBundle.from_lock(lock)
        request["calibration_lock"] = worker.policy_bundle.lock
    session = TauCampaignWorkerSession(worker, workload_id="tau")
    if change:
        with pytest.raises((CalibrationError, CampaignError)):
            session.run(request)
    else:
        with pytest.raises(DispatchReached):
            session.run(request)
        assert worker._authorized_policy_cell == cell.cell_id


@pytest.mark.parametrize("stage", ["C", "P", "T"])
def test_tau_unknown_safety_stops_before_touching_the_worker(stage):
    from bench.joint_campaign import CampaignError
    from bench.tau_campaign_worker import TauCampaignWorkerSession

    with pytest.raises(CampaignError, match="no measured policy-safety sensor"):
        TauCampaignWorkerSession(None, workload_id="tau").run(
            {"stage": stage, "paper_evidence": False}
        )
