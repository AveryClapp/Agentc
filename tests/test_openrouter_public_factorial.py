"""Public factorial tests: synthetic fixtures, fake keys and temporary ledgers."""

from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from bench import build_typed_hotpot_fixture as builder
from bench import openrouter_public_factorial as study
from bench import openrouter_pilot as pilot
from bench import typed_public_workload as public
from test_build_typed_hotpot_fixture import fixture as fixture

KEY = "literal-fake-public-key"
REAL_MATERIALIZE = study.materializer.materialize


def endpoint(model):
    return {
        "tag": "anthropic",
        "provider_name": "Anthropic",
        "name": "Anthropic | " + pilot.SCHEMA_ENDPOINTS[model][0],
        "context_length": pilot.SCHEMA_ENDPOINTS[model][1],
        "supported_parameters": [
            "max_tokens",
            "temperature",
            "structured_outputs",
            "response_format",
        ],
        "pricing": {
            "prompt": "0.000003" if model == study.live.SOURCE_MODEL else "0.000001",
            "completion": "0.000015"
            if model == study.live.SOURCE_MODEL
            else "0.000005",
        },
    }


def no_op(call, rule, bridge):
    return deepcopy(call), {
        "rule": rule,
        "applied": False,
        "retained_indices": list(range(len(call["messages"]))),
        "materialization_only": True,
        "abstention_reason": "not_applicable" if rule else "no_rule",
    }


def fake_coverage(args):
    bundle = study.load_bundle(args.bundle)
    models = [study.live.SOURCE_MODEL, study.live.TARGET_MODEL]
    cohorts = {
        "CC_actual_public": ("ContextCompress", "filter", False, "not_applicable"),
        "SD_synthetic_history": ("StateDrop", "answer", True, None),
        "SD_invalid_synthetic_history": (
            "StateDrop",
            "answer",
            False,
            "ambiguous_provenance",
        ),
    }
    records = [
        {
            "task_id": task["task_id"],
            "model": model,
            "cohort": cohort,
            "rule": rule,
            "stage": stage,
            "applied": applied,
            "abstention_reason": reason,
            "original_call_sha256": "a" * 64,
            "materialized_call_sha256": ("b" if applied else "a") * 64,
        }
        for task in bundle["tasks"]
        for model in models
        for cohort, (rule, stage, applied, reason) in cohorts.items()
    ]
    report = {
        "kind": "hotpot-public-rule-mechanics-v1",
        "source_sha256": {
            key: study.file_hash(study.ROOT / path)
            for key, path in {
                "builder": "bench/build_typed_hotpot_fixture.py",
                "adapter": "bench/typed_public_workload.py",
                "attention": "python/agentc/_attention.py",
                "bridge": study.materializer.BRIDGE_SOURCE,
            }.items()
        },
        "bridge_sha256": study.file_hash(args.bridge),
        "public_sha256": study.file_hash(args.bundle / "public.json"),
        "selection_sha256": bundle["selection"]["selection_sha256"],
        "contract_sha256": public.contract_hash(),
        "tasks": 38,
        "provider_calls": 0,
        "quality_evaluations": 0,
        "gold_access": False,
        "native_admission": False,
        "feedback_provided": False,
        "models": models,
        "records": records,
        "summary": {
            model: {
                cohort + "/" + ("applied" if applied else reason): 38
                for cohort, (_, _, applied, reason) in cohorts.items()
            }
            for model in models
        },
    }
    (args.bundle / "coverage.json").write_text(json.dumps(report))


@pytest.fixture
def experiment(fixture, tmp_path, monkeypatch):
    root, cache, bundle_dir, selection, parquet = fixture
    monkeypatch.setattr(builder, "committed_selection", lambda *a: deepcopy(selection))
    builder.materialize(
        bundle_dir / "selection.json",
        cache,
        "a" * 40,
        bundle_dir,
        parquet=parquet,
        root=root,
    )
    bridge = tmp_path / "fake-bridge"
    bridge.write_text("fake bridge, never executed")
    ledger = pilot.Ledger(tmp_path / "campaign.jsonl", KEY)
    with ledger.locked() as handle:
        ledger.append(handle, {"event": "origin", "usage_usd": "0"})
    eps = {
        model: endpoint(model)
        for model in (study.live.SOURCE_MODEL, study.live.TARGET_MODEL)
    }
    monkeypatch.setattr(study, "endpoint_snapshot", lambda _: deepcopy(eps))
    monkeypatch.setattr(study.materializer, "materialize", no_op)
    monkeypatch.setattr(pilot, "account", lambda _: {"usage": 0})
    monkeypatch.setattr(
        pilot.urllib.request,
        "build_opener",
        lambda *a: pytest.fail("real network forbidden"),
    )
    dispatched = []
    mode = {
        "invalid_at": None,
        "fail_at": None,
        "cost": "0.001",
        "answer": "deliberately wrong pilot answer",
    }

    def network(path, key, payload=None):
        assert key == KEY
        if payload is None:
            model = next(model for model in eps if model in path)
            return {"data": {"endpoints": [deepcopy(eps[model])]}}
        dispatched.append(deepcopy(payload))
        if mode["fail_at"] == len(dispatched):
            raise pilot.ProviderFailure(
                "fake timeout", {"kind": "timeout", "retryable": True}
            )
        answer = None if mode["invalid_at"] == len(dispatched) else mode["answer"]
        model = payload["model"]
        return {
            "id": "fake-generation-" + str(len(dispatched)),
            "model": model,
            "provider": "Anthropic",
            "usage": {
                "cost": mode["cost"],
                "prompt_tokens": len(pilot.canonical(payload)) // 4 + 40,
                "completion_tokens": 12,
                "is_byok": False,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "end_turn",
                    "message": {"content": json.dumps({"result": answer})},
                }
            ],
            "openrouter_metadata": {
                "requested": model,
                "attempt": 1,
                "is_byok": False,
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

    monkeypatch.setattr(pilot, "request_json", network)
    args = SimpleNamespace(
        output=tmp_path / "pilot",
        bundle=bundle_dir,
        ledger=ledger.path,
        bridge=bridge,
        pilot=None,
        pilot_manifest_sha256=None,
        manifest_sha256=None,
    )
    fake_coverage(args)
    frozen = study.freeze(args, KEY, "pilot")
    args.manifest_sha256 = frozen["manifest_sha256"]
    manifest = study.read(args.output / "manifest.json")
    return args, manifest, ledger, dispatched, mode, eps


def prepare_primary(experiment):
    args, manifest, ledger, dispatched, mode, eps = experiment
    study.run(args, KEY, manifest)
    primary = SimpleNamespace(
        **{
            **vars(args),
            "output": args.output.parent / "primary",
            "pilot": args.output,
            "pilot_manifest_sha256": args.manifest_sha256,
        }
    )
    frozen = study.freeze(primary, KEY, "primary")
    primary.manifest_sha256 = frozen["manifest_sha256"]
    return primary, study.read(primary.output / "manifest.json")


def deny_labels(monkeypatch):
    original = Path.open

    def guarded(path, *args, **kwargs):
        if path.name == "labels.json":
            pytest.fail(
                "pilot, acquisition, partial analysis, and scope gate may not open labels"
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)


def test_complete_pilot_advances_on_format_and_finance_without_accuracy_or_labels(
    experiment, monkeypatch
):
    args, manifest, ledger, dispatched, _, _ = experiment
    deny_labels(monkeypatch)
    primary, frozen = prepare_primary(experiment)
    assert len(dispatched) == 36
    checked = study.audit(args, manifest)
    assert checked["schedule_complete"] and all(checked["valid_by_call"].values())
    assert ledger.summary()["spent_usd"] == "0.036"
    assert frozen["pilot_evidence"]["accuracy_used"] is False
    assert Decimal(frozen["stage_cap_usd"]) == Decimal("13.964")
    assert tuple(frozen["arms"]) == tuple(sorted(study.FOUR_ARMS))
    assert len(frozen["schedule"]) == 32 * 4
    assert frozen["scope_decision"]["accuracy_used"] is False
    assert (
        study.validate(primary, frozen)[0]["selection"]
        == checked["bundle"]["selection"]
    )
    report = frozen["forecast"]
    assert frozen["selection_sha256"] == study.digest(checked["bundle"]["selection"])
    assert (
        study.read(args.bundle / "coverage.json")["selection_sha256"]
        == checked["bundle"]["selection"]["selection_sha256"]
    )
    assert (
        frozen["selection_sha256"] != checked["bundle"]["selection"]["selection_sha256"]
    )
    assert report["selection_metric"] == "conservative_observed_usd"
    assert set(report["options"]) == {"four"}
    assert Decimal(report["options"]["four"]["conservative_observed_usd"]) <= 7
    assert Decimal(
        report["options"]["four"]["conservative_universal_price_cap_usd"]
    ) > Decimal(report["options"]["four"]["conservative_observed_usd"])
    assert report["capacity_bound"] is False and report["spending_guarantee"] is False


def test_primary_full_own_history_run_and_all_32_cluster_analysis(
    experiment, monkeypatch
):
    args, _, ledger, dispatched, mode, _ = experiment
    primary, manifest = prepare_primary(experiment)
    mode["answer"] = "Synthetic answer"
    mode["invalid_at"] = 37
    with monkeypatch.context() as guarded:
        deny_labels(guarded)
        result = study.run(primary, KEY, manifest)
        assert result == {"completed_calls": 384, "schedule_complete": True}
        assert study.audit(primary, manifest)["schedule_complete"]
    # An invalid filter remains contagious, even though later fake provider
    # responses in that workflow are plausible and well-formed.
    assert public.ERROR_INPUT in json.dumps(dispatched[37]["messages"])
    assert public.ERROR_INPUT in json.dumps(dispatched[38]["messages"])
    assert public.ERROR_INPUT not in json.dumps(dispatched[39]["messages"])
    result = study.analyze(primary, manifest)
    assert (
        result["schedule_complete"]
        and result["paired_tasks"] == 32
        and result["labels_read"]
    )
    report = study.read(primary.output / "analysis.json")
    first = manifest["schedule"][0]
    cell = next(
        row for row in report["paired_tasks"] if row["task_id"] == first["task_id"]
    )["cells"][first["arm"]]
    assert cell["em"] == cell["f1"] == 0 and cell["invalid_path"]
    assert report["scheduled_factorial_factors"] == ["model", "rules_bundle"]
    assert report["applied_transformations"] == []
    assert report["individual_cc_sd_identified"] is False
    assert "modelxrules_bundle/em" in report["descriptive_task_cluster_intervals"]
    assert "ccxsd/em" not in report["descriptive_task_cluster_intervals"]
    assert Decimal(report["pilot_inclusive_known_usd"]) == Decimal(".420")
    assert ledger.summary()["completed_calls"] == 420
    assert not (args.output / "analysis.json").exists()


def test_invalid_pilot_completes_remaining_stages_but_cannot_advance(
    experiment, monkeypatch
):
    args, manifest, ledger, dispatched, mode, _ = experiment
    mode["invalid_at"] = 1
    deny_labels(monkeypatch)
    assert study.run(args, KEY, manifest)["completed_calls"] == 36
    assert public.ERROR_INPUT in json.dumps(dispatched[1]["messages"])
    assert public.ERROR_INPUT in json.dumps(dispatched[2]["messages"])
    next_args = SimpleNamespace(
        **{
            **vars(args),
            "pilot": args.output,
            "pilot_manifest_sha256": args.manifest_sha256,
        }
    )
    with pytest.raises(pilot.PilotError, match="structural/resource"):
        study.verified_pilot(next_args, KEY, study.load_bundle(args.bundle))
    assert ledger.summary()["spent_usd"] == "0.036"
    assert study.analyze(args, manifest)["labels_read"] is False


@pytest.mark.parametrize("cost,completed", [(".021", 36), (".04", 19)])
def test_pilot_known_progress_and_final_gate_both_apply(experiment, cost, completed):
    args, manifest, _, dispatched, mode, _ = experiment
    mode["cost"] = cost
    if completed < 36:
        with pytest.raises(pilot.PilotError, match="progress stop"):
            study.run(args, KEY, manifest)
    else:
        study.run(args, KEY, manifest)
    assert len(dispatched) == completed
    gate_args = SimpleNamespace(
        **{
            **vars(args),
            "pilot": args.output,
            "pilot_manifest_sha256": args.manifest_sha256,
        }
    )
    with pytest.raises(pilot.PilotError, match="structural/resource"):
        study.verified_pilot(gate_args, KEY, study.load_bundle(args.bundle))


def test_provider_failure_retains_bound_stops_and_partial_analysis_never_reads_labels(
    experiment, monkeypatch
):
    args, manifest, ledger, dispatched, mode, _ = experiment
    mode["fail_at"] = 1
    deny_labels(monkeypatch)
    with pytest.raises(pilot.ProviderFailure):
        study.run(args, KEY, manifest)
    model = dispatched[0]["model"]
    assert Decimal(ledger.summary()["retained_uncertainty_usd"]) == pilot.upper_cost(
        dispatched[0]
    )
    assert model in pilot.SCHEMA_ENDPOINTS
    assert len(dispatched) == 1
    report = study.analyze(args, manifest)
    assert not report["schedule_complete"] and not report["labels_read"]
    with pytest.raises(pilot.PilotError, match="one-shot"):
        study.run(args, KEY, manifest)


@pytest.mark.parametrize("failure", [False, True])
def test_full_selection_phase_claim_survives_copy_and_new_manifest_timestamp(
    experiment, tmp_path, failure
):
    args, manifest, ledger, dispatched, mode, _ = experiment
    clone = SimpleNamespace(**{**vars(args), "output": tmp_path / "clone"})
    shutil.copytree(args.output, clone.output)
    changed = deepcopy(manifest)
    changed["created_at"] = "2026-09-06T00:00:00+00:00"
    clone.manifest_sha256 = pilot.digest(changed)
    (clone.output / "manifest.json").write_text(json.dumps(changed))
    if failure:
        mode["fail_at"] = 1
        with pytest.raises(pilot.ProviderFailure):
            study.run(args, KEY, manifest)
    else:
        study.run(args, KEY, manifest)
    before, count = ledger.path.read_bytes(), len(dispatched)
    with pytest.raises(pilot.PilotError, match="one-shot"):
        study.run(clone, KEY, changed)
    assert ledger.path.read_bytes() == before and len(dispatched) == count
    assert manifest["selection_sha256"] in str(
        study.phase_claim_path(args.ledger, manifest)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "bridge",
        "budget",
        "schema",
        "selection",
        "key",
        "ledger",
        "disk_manifest",
    ],
)
def test_changed_frozen_contract_stops_before_paid_dispatch(
    experiment, monkeypatch, tmp_path, mutation
):
    args, manifest, ledger, dispatched, _, _ = experiment
    key = KEY
    if mutation == "source":
        monkeypatch.setattr(study, "sources", lambda: {"changed": "source"})
    elif mutation == "bridge":
        args.bridge.write_text("changed")
    elif mutation == "budget":
        manifest["stage_cap_usd"] = "50"
    elif mutation == "schema":
        manifest["response_formats"]["filter"]["json_schema"]["strict"] = 1
    elif mutation == "selection":
        manifest["selection_sha256"] = "f" * 64
    elif mutation == "key":
        key = "other-fake-key"
    elif mutation == "disk_manifest":
        changed = deepcopy(manifest)
        changed["created_at"] = "unrelated disk content"
        (args.output / "manifest.json").write_text(json.dumps(changed))
    else:
        args.ledger = tmp_path / "copy.jsonl"
        args.ledger.write_bytes(ledger.path.read_bytes())
    before = ledger.path.read_bytes()
    with pytest.raises(pilot.PilotError):
        study.run(args, key, manifest)
    assert not dispatched and ledger.path.read_bytes() == before


@pytest.mark.parametrize("field", ["pricing", "context_length", "name"])
def test_serving_price_or_endpoint_change_stops_before_reserve(experiment, field):
    args, manifest, ledger, dispatched, _, eps = experiment
    model = manifest["arms"][manifest["schedule"][0]["arm"]]["model"]
    if field == "pricing":
        eps[model][field]["completion"] = "0.000006"
    elif field == "context_length":
        eps[model][field] -= 1
    else:
        eps[model][field] = "Anthropic | wrong"
    with pytest.raises(pilot.PilotError):
        study.run(args, KEY, manifest)
    assert not dispatched and ledger.summary()["conservative_committed_usd"] == "0"


@pytest.mark.parametrize(
    "mutation", ["intent", "metadata_type", "summary_count", "summary_ids", "claim"]
)
def test_audit_rejects_altered_artifacts_and_entire_summary(experiment, mutation):
    args, manifest, _, _, _, _ = experiment
    study.run(args, KEY, manifest)
    name = "intents.json" if mutation in {"intent", "metadata_type"} else "summary.json"
    path = (
        study.phase_claim_path(args.ledger, manifest)
        if mutation == "claim"
        else args.output / name
    )
    data = study.read(path)
    if mutation == "intent":
        data[1]["original_call"]["messages"][1]["content"] = "another arm"
    elif mutation == "metadata_type":
        data[0]["materialization"]["applied"] = 0
    elif mutation == "summary_count":
        data["stage_accounting"]["ledger_completed_calls"] = 999
    elif mutation == "summary_ids":
        data["stage_accounting"]["unincorporated_completed_ids"] = ["hidden"]
    else:
        data["manifest_sha256"] = "changed"
    path.write_text(json.dumps(data))
    with pytest.raises(pilot.PilotError):
        study.audit(args, manifest)


@pytest.mark.parametrize("four,choice", [("3.5", 4), ("7", 4), ("7.01", 0)])
def test_predeclared_scope_gate_has_no_quality_input(four, choice):
    forecast = {
        "policy": study.FORECAST_POLICY,
        "selection_metric": "conservative_observed_usd",
        "options": {
            "four": {"conservative_observed_usd": four},
        },
    }
    if choice:
        assert len(study.choose_arms(forecast)) == choice
    else:
        with pytest.raises(pilot.PilotError, match="exceeds"):
            study.choose_arms(forecast)


@pytest.mark.parametrize(
    "mutation",
    ["source", "selection", "gold", "count", "duplicate", "cc_applied", "schema_bool"],
)
def test_four_cell_scope_requires_bound_complete_zero_cc_preflight(
    experiment, mutation
):
    args, manifest, _, dispatched, _, _ = experiment
    path = args.bundle / "coverage.json"
    report = study.read(path)
    if mutation == "source":
        report["source_sha256"]["adapter"] = "changed"
    elif mutation == "selection":
        report["selection_sha256"] = "changed"
    elif mutation == "gold":
        report["gold_access"] = True
    elif mutation == "count":
        report["summary"][study.live.SOURCE_MODEL][
            "CC_actual_public/not_applicable"
        ] = 37
    elif mutation == "duplicate":
        report["records"][-1] = report["records"][0]
    elif mutation == "cc_applied":
        report["records"][0]["applied"] = True
    else:
        report["records"][0]["applied"] = 0
    path.write_text(json.dumps(report))
    with pytest.raises(pilot.PilotError, match="coverage"):
        study.coverage_evidence(args, study.load_bundle(args.bundle))
    with pytest.raises(pilot.PilotError):
        study.run(args, KEY, manifest)
    assert not dispatched


def test_factorial_interaction_distinguishes_relative_cost_from_price_multiplication():
    cells = {
        arm: {
            "em": int(spec["cc"] and spec["sd"]),
            "f1": int(spec["cc"] and spec["sd"]),
            "cost_usd": str(
                (2 if spec["model"] == study.live.SOURCE_MODEL else 1)
                * (0.5 if spec["cc"] else 1)
                * (0.5 if spec["sd"] else 1)
            ),
        }
        for arm, spec in study.ARMS.items()
    }
    values = study.contrasts(cells)
    assert values["rules_bundle/em"] == 1 and values["modelxrules_bundle/em"] == 0
    assert values["modelxrules_bundle/log_cost"] == pytest.approx(0)
    four = study.contrasts({arm: cells[arm] for arm in study.FOUR_ARMS})
    assert "rules_bundle/em" in four and "cc/em" not in four
    cells["S0"]["cost_usd"] = "0"
    assert study.contrasts(cells)["model/log_cost"] is None


def test_new_adapter_has_no_native_semantic_feedback_contract():
    with pytest.raises(pilot.PilotError, match="no native semantic feedback"):
        study.live.divergence_for(
            {"workload": study.WORKLOAD}, "answer", {}, "same", "same"
        )


def test_real_existing_bridge_can_materialize_public_filter_and_answer(
    experiment, monkeypatch
):
    binary = os.environ.get("AGENTC_TYPED_MATERIALIZER")
    if not binary:
        pytest.skip(
            "opt in with the existing frozen bridge; never build or call a provider"
        )
    assert study.file_hash(Path(binary)) == (
        "b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122"
    )
    args, manifest, _, _, _, _ = experiment
    monkeypatch.setattr(study.materializer, "materialize", REAL_MATERIALIZE)
    bundle = study.load_bundle(args.bundle)
    task = study.selected_tasks(bundle, "primary")[0]
    attention = study.load_module(
        "public_real_bridge_attention", study.ROOT / "python/agentc/_attention.py"
    )
    manifest["arms"] = {arm: study.ARMS[arm] for arm in study.FOUR_ARMS}
    for stage, history in (
        ("filter", {}),
        (
            "answer",
            {"filter": '{"result":"quotes"}', "synthesize": '{"result":"draft"}'},
        ),
    ):
        for arm in ("SR", "HR"):
            intent = study.intent_for(
                manifest,
                {
                    "task_id": task["task_id"],
                    "phase": "primary",
                    "arm": arm,
                    "workflow_stage": stage,
                },
                task,
                history,
                attention,
                Path(binary),
            )
            assert intent["materialization"]["rule"] == (
                "ContextCompress" if stage == "filter" else "StateDrop"
            )
            assert (
                "input_tokens_upper_bound"
                not in intent["selected_call"]["parameters"]["extra"][
                    "agentc_route_context"
                ]
            )
