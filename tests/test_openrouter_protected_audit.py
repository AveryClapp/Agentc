"""Synthetic public inputs and temporary fake receipts; never campaign data."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from bench import openrouter_protected_audit as audit
from bench import openrouter_protected_fixture as fixture
from bench import openrouter_protected_pair as pair
from bench import openrouter_protected_study as study
from bench.openrouter_matrix import load_module, ROOT, file_hash
from test_openrouter_protected_pair import catalog, endpoints, SYNTH

KEY = "literal-fake-readonly-audit-key"


def public_bundle():
    selection = fixture.build_selection([])
    tasks = [
        {
            "task_id": task_id,
            "prompt": "Tenant TX; requisition R001. Synthetic public request.",
            "meta": {
                "paragraphs": [
                    {"title": f"D{i:02}", "sentences": [f"Synthetic document row {i}."]}
                    for i in range(1, 21)
                ]
            },
        }
        for task_id in selection["pilot_ids"] + selection["study_ids"]
    ]
    provenance = {
        "kind": fixture.KIND,
        "reviewed_checkpoint": "c" * 40,
        "count": 69,
        "selection_sha256": selection["selection_sha256"],
        "selection_file_sha256": hashlib.sha256(
            study.pilot.canonical(selection) + b"\n"
        ).hexdigest(),
        "public_sha256": hashlib.sha256(
            study.pilot.canonical(tasks) + b"\n"
        ).hexdigest(),
        "labels_sha256": "d" * 64,
        "source_contract": selection["source_contract"],
        "generator_runtime": selection["generator_runtime"],
        "generation": fixture.GENERATION,
        "label_use": fixture.LABEL_USE,
    }
    return {
        "kind": fixture.KIND,
        "selection": selection,
        "provenance": provenance,
        "tasks": tasks,
    }


@pytest.fixture
def experiment(tmp_path, monkeypatch, request):
    bundle = public_bundle()
    if getattr(request, "param", None) == "short_schedule":
        schedule = study.schedules(bundle)
        schedule["study"] = schedule["study"][:12] + schedule["study"][189:195]
        monkeypatch.setattr(study, "schedules", lambda bundle: deepcopy(schedule))
    ledger = study.pilot.Ledger(tmp_path / "fake-journal.jsonl", KEY)
    with ledger.locked() as handle:
        origin = ledger.append(handle, {"event": "origin", "usage_usd": "0"})
    binary = tmp_path / "never-executed-pilot-binary"
    binary.write_bytes(b"synthetic pilot requires no native operation")
    native = Path(os.environ.get("AGENTC_PROTECTED_PAIR_BRIDGE", binary))
    bridge = pair.NativeBridge(
        native,
        tmp_path / "native.db",
        os.environ.get("AGENTC_PROTECTED_PAIR_BRIDGE_SHA256", file_hash(native)),
    )
    binary = Path(os.environ.get("AGENTC_TYPED_MATERIALIZER", binary))
    frozen = pair.enrollment(
        catalog(),
        endpoints(),
        bridge.sha256,
        max_calls_per_site=60,
        max_output_tokens=256,
    )
    source_map = {"synthetic-test-source": "a" * 64}
    monkeypatch.setattr(study, "sources", lambda: deepcopy(source_map))
    prepilot = {
        "evidence_kind": "historical_pre_pilot",
        "provenance": {
            "enrollment_sha256": study.pilot.digest(frozen),
            "selection_sha256": bundle["selection"]["selection_sha256"],
            "materializer_sha256": file_hash(binary),
        },
        "prospective_pilot_usd": "0.1",
        "training_usd": "1",
        "HR_usd": "0.1",
        "largest_pair_allowance_usd": "12.01536",
    }
    controls = {"synthetic_fixture": True}

    def check_controls(report, enrollment):
        assert report == controls and enrollment == frozen

    monkeypatch.setattr(study, "validate_controls", check_controls)
    manifest = study.manifest_core(
        bundle,
        {
            "resolved_path": str(ledger.path.resolve()),
            "origin_event_sha256": study.pilot.digest(origin),
        },
        frozen,
        bridge.store,
        file_hash(binary),
        [origin],
        prepilot,
        controls,
        "2026-09-06T00:00:00+00:00",
    )
    state = {
        "dispatches": 0,
        "invalid_at": None,
        "failure_at": None,
        "invalid_candidate": False,
        "provider_error_at": None,
        "unsafe_failure": False,
    }

    def transport(path, key, payload=None):
        assert key == KEY
        if payload is None:
            model = path.removeprefix("/models/").removesuffix("/endpoints")
            return {"data": {"endpoints": [endpoints()[model]]}}
        state["dispatches"] += 1
        if state["dispatches"] == state["failure_at"]:
            raise study.pilot.ProviderFailure(
                "synthetic failure", {"kind": "fake", "retryable": False}
            )
        name = payload["response_format"]["json_schema"]["name"]
        step = name.removeprefix("typed_procurement_").removesuffix("_v1")
        value = (
            ["D01", "D02", "D03"]
            if step == "filter"
            else SYNTH
            if step == "synthesize"
            else "M01"
        )
        content = json.dumps({"result": value})
        if state["dispatches"] == state["invalid_at"] or (
            state["invalid_candidate"] and "haiku" in payload["model"]
        ):
            content = study.typed.ERROR
        model = payload["model"]
        response = {
            "id": f"synthetic-audit-generation-{state['dispatches']}",
            "model": model,
            "provider": "Anthropic",
            "usage": {"cost": "0.001", "prompt_tokens": 500, "completion_tokens": 40},
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
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
                            "model": study.pilot.SCHEMA_ENDPOINTS[model][0],
                        }
                    ]
                },
            },
        }
        if state["dispatches"] == state["provider_error_at"]:
            response["choices"][0]["message"]["refusal"] = "synthetic refusal"
            if state["unsafe_failure"]:
                response["usage"]["is_byok"] = True
        return response

    monkeypatch.setattr(
        study.pilot, "account", lambda key: {"usage": "0", "limit_remaining": "50"}
    )
    monkeypatch.setattr(study.pilot, "request_json", transport)
    monkeypatch.setattr(
        study.pilot.urllib.request,
        "build_opener",
        lambda *a, **k: pytest.fail("real network"),
    )
    monkeypatch.setattr(
        study.pilot, "load_key", lambda *a: pytest.fail("credential read")
    )
    return manifest, bundle, ledger, bridge, binary, state


def acquire_pilot(experiment, count=36, *, invalid=None, failure=None):
    manifest, bundle, ledger, bridge, binary, state = experiment
    state.update(invalid_at=invalid, failure_at=failure)
    claim = study.claim_phase(ledger, manifest, "pilot", None)
    tasks = {task["task_id"]: task for task in bundle["tasks"]}
    attention = load_module(
        "audit_test_attention", ROOT / "python/agentc/_attention.py"
    )
    intents, history, workflow = [], {}, None
    for item in manifest["schedule"]["pilot"][:count]:
        identity = (item["task_id"], item["arm"])
        if identity != workflow:
            history, workflow = {}, identity
        intent = study.fixed_intent(
            manifest, item, tasks[item["task_id"]], history, attention, binary
        )
        intents.append(intent)
        try:
            result = ledger.call(
                KEY,
                intent["id"],
                study.stage_id(manifest, "pilot"),
                study.pilot.money(claim["stage_cap_usd"]),
                intent["payload"],
                intent["metadata"],
            )
        except study.pilot.PilotError:
            break
        step = item["workflow_stage"]
        history[step] = study.typed.application_text(
            result["answer"], result["finish_reason"], stage=step
        )
        if study.typed.parse(step, history[step]) is None:
            break
    with ledger.locked() as handle:
        events = ledger.read(handle)
    calls, protected = study.extract_records(manifest, "pilot", intents, events)
    return {
        "intents": intents,
        "calls": calls,
        "protected_events": protected,
        "events": events,
    }


def checked(experiment, data, monkeypatch, phase="pilot"):
    manifest, bundle, ledger, bridge, binary, state = experiment
    before = ledger.path.read_bytes()
    copied = deepcopy(data)
    native_before = None
    if bridge.store.exists():
        with sqlite3.connect(bridge.store) as native:
            native_before = list(native.iterdump())
    original = bridge.call

    def readonly(binding, catalog, command, *args, **kwargs):
        assert command["op"] in {"checkpoint", "verify"}
        return original(binding, catalog, command, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("audit attempted mutable or credential-bearing operation")

    with monkeypatch.context() as patch:
        patch.setattr(bridge, "call", readonly)
        for name in ("load_key", "account", "request_json"):
            patch.setattr(study.pilot, name, forbidden)
        for name in ("__init__", "locked", "read", "call", "append"):
            patch.setattr(study.pilot.Ledger, name, forbidden)
        patch.setattr(study.typed, "make_tasks", forbidden)
        patch.setattr(study.typed, "score_answer", forbidden)
        report = audit.audit_phase(
            manifest, bundle, phase, bridge=bridge, materializer=binary, **data
        )
    assert data == copied and ledger.path.read_bytes() == before
    if native_before is not None:
        with sqlite3.connect(bridge.store) as native:
            assert list(native.iterdump()) == native_before
    else:
        assert not bridge.store.exists()
    return report


def test_complete_pilot_reconstructs_receipts_and_never_opens_ledger(
    experiment, monkeypatch
):
    data = acquire_pilot(experiment)
    result = checked(experiment, data, monkeypatch)
    assert result["schedule_complete"] and result["pilot_format_gate"]
    assert result["successful_calls"] == result["completed_scheduled_steps"] == 36
    assert result["finance"]["known_stage_usd"] == "0.036"
    assert result["finance"]["retained_uncertainty_usd"] == "0"
    assert set(result["resources_by_arm"]) == {"S0", "H0"}
    assert not result["labels_opened"] and not result["efficacy_claim"]
    assert not result["learning"]["admission_assessed"]


@pytest.mark.parametrize("count", [0, 1, 3, 35])
def test_intact_prefix_never_passes_full_gate(experiment, monkeypatch, count):
    result = checked(experiment, acquire_pilot(experiment, count), monkeypatch)
    assert not result["schedule_complete"] and not result["pilot_format_gate"]
    assert result["successful_calls"] == count


@pytest.mark.parametrize("position", [5, 36])
def test_invalid_pilot_completion_retained_as_failed_gate(
    experiment, monkeypatch, position
):
    result = checked(
        experiment, acquire_pilot(experiment, invalid=position), monkeypatch
    )
    assert not result["schedule_complete"] and not result["pilot_format_gate"]
    assert sum(result["invalid_application_steps"].values()) == 1
    assert (
        study.pilot.money(result["finance"]["known_stage_usd"])
        == study.pilot.money("0.001") * position
    )
    assert result["all_scheduled_steps_returned"] == (position == 36)


def test_provider_failure_retains_full_allowance(experiment, monkeypatch):
    result = checked(experiment, acquire_pilot(experiment, failure=2), monkeypatch)
    assert not result["schedule_complete"] and not result["pilot_format_gate"]
    assert result["successful_calls"] == 1
    assert result["finance"]["known_stage_usd"] == "0.001"
    assert study.pilot.money(result["finance"]["retained_uncertainty_usd"]) > 1
    assert len(result["finance"]["failed_attempt_ids"]) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_call",
        "duplicate_call",
        "changed_call",
        "wrong_history",
        "extra_intent",
        "claim_after_result",
        "duplicate_claim",
        "wrong_claim_manifest",
        "bad_allowance",
        "wrong_endpoint",
        "raw_latency",
        "raw_content",
        "duplicate_generation",
        "orphan_response",
        "foreign_stage",
        "protected_artifact",
        "gate_on_pilot",
        "reverse_workflows",
    ],
)
def test_adversarial_artifacts_and_receipts_rejected(experiment, monkeypatch, mutation):
    data = acquire_pilot(experiment, count=6)
    rows = data["events"]
    result = next(row for row in rows if row["event"] == "result")
    reserve = next(row for row in rows if row["event"] == "reserve")
    raw = next(row for row in rows if row["event"] == "response")
    claim = next(row for row in rows if row["event"] == study.CLAIM_EVENT)
    if mutation == "missing_call":
        data["calls"].pop()
    elif mutation == "duplicate_call":
        data["calls"].append(deepcopy(data["calls"][-1]))
    elif mutation == "changed_call":
        data["calls"][0]["answer"] = "different"
    elif mutation == "wrong_history":
        data["intents"][1]["payload"]["messages"][-1]["content"] = "different"
    elif mutation == "extra_intent":
        data["intents"].append(deepcopy(data["intents"][-1]))
    elif mutation == "claim_after_result":
        rows.remove(claim)
        rows.append(claim)
    elif mutation == "duplicate_claim":
        rows.append(deepcopy(claim))
    elif mutation == "wrong_claim_manifest":
        claim["manifest_sha256"] = "0" * 64
    elif mutation == "bad_allowance":
        reserve["upper_cost_usd"] = "0.001"
    elif mutation == "wrong_endpoint":
        reserve["schema_endpoint_observation"]["pricing"]["prompt"] = "0.000004"
    elif mutation == "raw_latency":
        raw["latency_ms"] += 1
    elif mutation == "raw_content":
        raw["response"]["choices"][0]["message"]["content"] = "changed"
    elif mutation == "duplicate_generation":
        other = next(
            row for row in rows if row["event"] == "result" and row is not result
        )
        other["generation_id"] = result["generation_id"]
    elif mutation == "orphan_response":
        rows.append({**deepcopy(raw), "id": claim["stage"] + "/orphan"})
    elif mutation == "foreign_stage":
        for row in rows:
            if row.get("id") == result["id"]:
                row["stage"] = "foreign"
    elif mutation == "protected_artifact":
        data["protected_events"].append({"event": "fake"})
    elif mutation == "reverse_workflows":
        # Both workflows retain their internally valid own-history chains.
        # Only their prospectively frozen chronological order is violated.
        prefix = rows[: rows.index(reserve)]
        first_ids = {intent["id"] for intent in data["intents"][:3]}
        first = [row for row in rows if row.get("id") in first_ids]
        second = [row for row in rows[len(prefix) :] if row.get("id") not in first_ids]
        rows[:] = prefix + second + first
        data["calls"] = data["calls"][3:] + data["calls"][:3]
    else:
        claim["gate"] = {"passed": True}
        claim["gate_sha256"] = study.pilot.digest(claim["gate"])
    with pytest.raises(study.pilot.PilotError):
        checked(experiment, data, monkeypatch)


@pytest.mark.parametrize(
    "key", ["source_files", "public_bundle_sha256", "bundle_files", "native_store"]
)
def test_frozen_contract_mutation_rejected(experiment, monkeypatch, key):
    data = acquire_pilot(experiment, count=1)
    experiment[0][key] = {} if key.endswith("files") else "changed"
    with pytest.raises(study.pilot.PilotError):
        checked(experiment, data, monkeypatch)


def study_prefix(
    experiment,
    monkeypatch,
    count=0,
    *,
    interrupt_action=None,
    invalid_candidate=False,
    invalid_source=False,
    failure=None,
):
    """Real coordinator, fake provider, public-only synthetic inputs."""
    if not os.environ.get("AGENTC_PROTECTED_PAIR_BRIDGE"):
        pytest.skip("requires the prebuilt policy bridge; never builds")
    manifest, bundle, ledger, bridge, materializer, state = experiment
    pilot_data = acquire_pilot(experiment)
    state["invalid_candidate"] = invalid_candidate
    if invalid_source:
        state["invalid_at"] = state["dispatches"] + 1
    if failure is not None:
        state["failure_at"] = state["dispatches"] + failure
    from bench import openrouter_protected_forecast as forecast

    # Forecast arithmetic has its own actual-materializer tests. This fixture
    # pins a synthetic nonbinding resource estimate, never a quality decision.
    estimate = {
        "evidence_kind": "fresh_pilot",
        "training_usd": "1",
        "HR_usd": ".1",
        "largest_pair_allowance_usd": "12.01536",
    }
    monkeypatch.setattr(forecast, "forecast", lambda *args: deepcopy(estimate))
    evidence = {
        "manifest_sha256": study.pilot.digest(manifest),
        "selection_sha256": manifest["selection_sha256"],
        "calls_sha256": study.pilot.digest(pilot_data["calls"]),
        "intents_sha256": study.pilot.digest(pilot_data["intents"]),
        "known_cost_usd": "0.036",
    }
    gate = study.resource_gate(
        manifest, audit.accounting(pilot_data["events"]), estimate, evidence
    )
    claim = study.claim_phase(ledger, manifest, "study", gate)
    coordinator = pair.Coordinator(
        ledger,
        KEY,
        bridge,
        manifest["enrollment"],
        run_id=study.stage_id(manifest, "study"),
        budget_stage=study.stage_id(manifest, "study"),
        stage_cap=claim["stage_cap_usd"],
    )
    tasks = {task["task_id"]: task for task in bundle["tasks"]}
    attention = load_module(
        "audit_test_study_attention", ROOT / "python/agentc/_attention.py"
    )
    intents, history, workflow = [], {}, None
    append = ledger.append

    class Interrupted(Exception):
        pass

    def stop(handle, row):
        result = append(handle, row)
        if row.get("action") == interrupt_action and row.get("event") == pair.EVENT:
            raise Interrupted
        return result

    with monkeypatch.context() as patch:
        if interrupt_action:
            patch.setattr(ledger, "append", stop)
        for item in manifest["schedule"]["study"][:count]:
            identity = (item["task_id"], item["arm"])
            if workflow != identity:
                history, workflow = {}, identity
            try:
                if item["arm"] == "source":
                    coordinator.execute(tasks[item["task_id"]], item["workflow_stage"])
                else:
                    intent = study.fixed_intent(
                        manifest,
                        item,
                        tasks[item["task_id"]],
                        history,
                        attention,
                        materializer,
                    )
                    intents.append(intent)
                    row = ledger.call(
                        KEY,
                        intent["id"],
                        claim["stage"],
                        study.pilot.money(claim["stage_cap_usd"]),
                        intent["payload"],
                        intent["metadata"],
                    )
                    history[item["workflow_stage"]] = study.typed.application_text(
                        row["answer"],
                        row["finish_reason"],
                        stage=item["workflow_stage"],
                    )
            except (Interrupted, study.pilot.ProviderFailure):
                break
    with ledger.locked() as handle:
        events = ledger.read(handle)
    calls, protected = study.extract_records(manifest, "study", intents, events)
    return {
        "intents": intents,
        "calls": calls,
        "protected_events": protected,
        "events": events,
    }


@pytest.mark.parametrize("count", [0, 1, 3, 12])
def test_actual_native_study_prefix_uses_own_source_history(
    experiment, monkeypatch, count
):
    data = study_prefix(experiment, monkeypatch, count)
    result = checked(experiment, data, monkeypatch, "study")
    assert not result["schedule_complete"]
    assert result["completed_scheduled_steps"] == count
    assert (
        result["resources_by_arm"].get("source", {}).get("successful_calls", 0) == count
    )
    assert result["pilot_format_gate"] is None
    assert result["learning"]["current_guard_state_assessed"] is False
    assert result["finance"]["campaign_known_usd"] == str(
        study.pilot.money(".036")
        + study.pilot.money(result["finance"]["known_stage_usd"])
    )
    if count == 12:
        assert result["resources_by_arm"]["probe"]["successful_calls"] >= 1
        assert result["learning"]["exact_plan_profiles"]


@pytest.mark.parametrize("action", ["start", "observe_claim", "feedback_claim"])
def test_unacknowledged_native_claim_is_ambiguous_not_learning(
    experiment, monkeypatch, action
):
    data = study_prefix(experiment, monkeypatch, 12, interrupt_action=action)
    result = checked(experiment, data, monkeypatch, "study")
    assert not result["schedule_complete"] and not result["integrity_verified"]
    assert result["known_receipt_and_checkpoint_integrity_verified"]
    assert result["learning"]["ambiguous_native_claim_ids"]
    assert not result["learning"]["learning_evidence_reported"]
    assert result["learning"]["exact_plan_profiles"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        "gate_finances",
        "gate_evidence",
        "pilot_missing",
        "source_history",
        "ack",
        "protected_missing",
        "probe_missing",
    ],
)
def test_study_gate_receipts_and_native_evidence_tamper_rejected(
    experiment, monkeypatch, mutation
):
    data = study_prefix(experiment, monkeypatch, 12)
    rows = data["events"]
    claim = next(
        row
        for row in rows
        if row.get("event") == study.CLAIM_EVENT and row["phase"] == "study"
    )
    if mutation == "gate_finances":
        claim["gate"]["financial_snapshot"]["known_usd"] = "0"
        claim["gate_sha256"] = study.pilot.digest(claim["gate"])
    elif mutation == "gate_evidence":
        claim["gate"]["pilot_evidence"]["calls_sha256"] = "0" * 64
        claim["gate_sha256"] = study.pilot.digest(claim["gate"])
    elif mutation == "pilot_missing":
        rows.remove(next(row for row in rows if row["event"] == "result"))
    elif mutation == "source_history":
        start = next(row for row in rows if row.get("action") == "start")
        start["source_call"]["messages"][-1]["content"] = "foreign history"
    elif mutation == "ack":
        ack = next(row for row in rows if row.get("action") == "planned")
        ack["reply"]["request_sha256"] = "0" * 64
    elif mutation == "protected_missing":
        data["protected_events"].pop()
    else:
        data["calls"].remove(
            next(row for row in data["calls"] if row["arm"] == "probe")
        )
    with pytest.raises(study.pilot.PilotError):
        checked(experiment, data, monkeypatch, "study")


@pytest.mark.parametrize("experiment", ["short_schedule"], indirect=True)
@pytest.mark.parametrize("invalid", [False, True])
def test_short_synthetic_complete_source_then_hr_reconstructs_real_materializer(
    experiment, monkeypatch, invalid
):
    if not os.environ.get("AGENTC_TYPED_MATERIALIZER"):
        pytest.skip("requires prebuilt fixed materializer; never builds")
    data = study_prefix(
        experiment, monkeypatch, 18, invalid_candidate=invalid, invalid_source=invalid
    )
    result = checked(experiment, data, monkeypatch, "study")
    assert result["schedule_complete"] and result["integrity_verified"]
    assert result["completed_scheduled_steps"] == 18
    assert result["resources_by_arm"]["source"]["successful_calls"] == 12
    assert result["resources_by_arm"]["HR"]["successful_calls"] == 6
    assert all(row["arm"] == "HR" for row in data["calls"][-6:])
    assert result["labels_opened"] is result["efficacy_claim"] is False
    assert result["learning"]["admission_assessed"] is False
    if invalid:
        assert result["invalid_application_steps"]["source/filter"] == 1
        assert result["invalid_application_steps"]["HR/filter"] == 2
        assert any(
            p["disagreements"] for p in result["learning"]["exact_plan_profiles"]
        )
    else:
        assert result["invalid_application_steps"] == {}
        assert all(
            p["disagreements"] == 0 for p in result["learning"]["exact_plan_profiles"]
        )


def test_failed_actual_native_pair_retains_all_provider_liability(
    experiment, monkeypatch
):
    data = study_prefix(experiment, monkeypatch, 12, failure=11)
    result = checked(experiment, data, monkeypatch, "study")
    assert not result["schedule_complete"]
    assert result["finance"]["failed_attempt_ids"]
    assert study.pilot.money(result["finance"]["retained_uncertainty_usd"]) > 1
    expected = audit.accounting(data["events"])
    sid = study.stage_id(experiment[0], "study")
    assert study.pilot.money(result["finance"]["stage_commitment_usd"]) == (
        expected["known_by_stage"][sid]
        + expected["holds_by_stage"][sid]
        + sum(
            (
                study.pilot.money(row["upper_cost_usd"])
                for row in expected["pending"]
                if row["stage"] == sid
            ),
            study.pilot.money(0),
        )
    )


@pytest.mark.parametrize("experiment", ["short_schedule"], indirect=True)
@pytest.mark.parametrize(
    "mutation",
    [
        "hr_before_source",
        "hr_history",
        "hr_materialization",
        "hr_schema",
        "missing_last",
    ],
)
def test_fixed_hr_evidence_cannot_replace_source_history_or_skip_source_block(
    experiment, monkeypatch, mutation
):
    if not os.environ.get("AGENTC_TYPED_MATERIALIZER"):
        pytest.skip("requires prebuilt fixed materializer; never builds")
    data = study_prefix(experiment, monkeypatch, 18)
    if mutation == "hr_before_source":
        rows = data["events"]
        hr_ids = {intent["id"] for intent in data["intents"]}
        fixed_rows = [row for row in rows if row.get("id") in hr_ids]
        rows[:] = [row for row in rows if row.get("id") not in hr_ids]
        index = (
            next(
                i
                for i, row in enumerate(rows)
                if row.get("event") == study.CLAIM_EVENT and row["phase"] == "study"
            )
            + 1
        )
        rows[index:index] = fixed_rows
    elif mutation == "hr_history":
        data["intents"][1]["original_call"]["messages"][-1]["content"] = (
            "candidate-owned text"
        )
    elif mutation == "hr_materialization":
        data["intents"][0]["materialization"]["abstention_reason"] = "invented"
    elif mutation == "hr_schema":
        data["intents"][0]["payload"]["response_format"]["json_schema"]["strict"] = (
            False
        )
    else:
        data["calls"].pop()
    with pytest.raises(study.pilot.PilotError):
        checked(experiment, data, monkeypatch, "study")


@pytest.mark.parametrize("unsafe", [False, True])
def test_raw_failed_receipt_charge_plus_residual_never_becomes_success(
    experiment, monkeypatch, unsafe
):
    experiment[-1].update(provider_error_at=2, unsafe_failure=unsafe)
    data = acquire_pilot(experiment)
    result = checked(experiment, data, monkeypatch)
    finance = result["finance"]
    assert not result["schedule_complete"] and result["successful_calls"] == 1
    assert finance["known_stage_usd"] == "0.002"
    failed = next(row for row in data["events"] if row["event"] == "attempt_failure")
    assert study.pilot.money(finance["retained_uncertainty_usd"]) == (
        study.pilot.money(failed["budget_hold_usd"]) - study.pilot.money(".001")
    )
    assert finance["unsafe_failed_attempt_ids"] == ([failed["id"]] if unsafe else [])
    assert sum(row["event"] == "response" for row in data["events"]) == 2
    assert len(data["calls"]) == 1


@pytest.mark.parametrize("stop", [None, "invalid_final", "transport_failure"])
def test_actual_run_phase_pinned_ledger_terminal_audit_and_one_shot(
    experiment, monkeypatch, tmp_path, stop
):
    manifest, bundle, ledger, bridge, materializer, state = experiment
    if stop == "invalid_final":
        state["invalid_at"] = 36
    elif stop == "transport_failure":
        state["failure_at"] = 2
    pinned = study.PinnedLedger(ledger.path, KEY, manifest, "pilot")
    output = tmp_path / "actual-runner-output"

    def run():
        return study.run_phase(
            output,
            manifest,
            bundle,
            "pilot",
            pinned,
            KEY,
            bridge,
            materializer,
            expected_manifest_sha256=study.pilot.digest(manifest),
        )

    if stop is None:
        assert run()["schedule_complete"] is True
    else:
        with pytest.raises(study.pilot.PilotError):
            run()
    directory = output / "pilot"
    summary = json.loads((directory / "summary.json").read_text())
    with ledger.locked() as handle:
        events = ledger.read(handle)
    data = {
        "events": events,
        "intents": json.loads((directory / "intents.json").read_text()),
        "calls": json.loads((directory / "calls.json").read_text()),
        "protected_events": json.loads(
            (directory / "protected-events.json").read_text()
        ),
    }
    result = checked(experiment, data, monkeypatch)
    assert summary["schedule_complete"] is result["schedule_complete"] is (stop is None)
    assert result["successful_calls"] == (1 if stop == "transport_failure" else 36)
    assert all(
        row["request"]["max_tokens"] == 256
        for row in events
        if row["event"] == "reserve"
    )
    assert sum(row["event"] == study.CLAIM_EVENT for row in events) == 1
    for name, digest in summary["artifact_files"].items():
        assert file_hash(directory / name) == digest
    if stop is None:
        assert json.loads((directory / "audit.json").read_text()) == result
    dispatches, original = state["dispatches"], ledger.path.read_bytes()
    with pytest.raises(study.pilot.PilotError):
        run()
    assert state["dispatches"] == dispatches and ledger.path.read_bytes() == original


@pytest.mark.parametrize("ceiling", ["campaign", "stage", "pilot_progress"])
def test_small_terminal_charge_cannot_hide_excess_predispatch_exposure(
    experiment, ceiling
):
    data = acquire_pilot(experiment, count=1)
    rows = data["events"]
    reserve = next(row for row in rows if row["event"] == "reserve")
    bound, sid = study.pilot.money(reserve["upper_cost_usd"]), reserve["stage"]
    cap = bound + study.pilot.money(".05")
    # Legacy known-charge records are valid accounting inputs. They exercise
    # only this pure budget primitive, not the full phase/manifest verifier.
    prior = {
        "event": "result",
        "id": "synthetic-prior-charge",
        "fingerprint": "prior",
        "stage": "other" if ceiling == "campaign" else sid,
        "cost_usd": str(study.pilot.HARD_CAP - bound / 2)
        if ceiling == "campaign"
        else ".6"
        if ceiling == "pilot_progress"
        else ".1",
    }
    rows.insert(rows.index(reserve), prior)
    final = audit.accounting(rows)
    assert final["known_usd"] < study.pilot.HARD_CAP
    assert final["known_by_stage"].get(sid, 0) < cap
    with pytest.raises(study.pilot.PilotError, match="exposure ceiling|progress stop"):
        audit._dispatch_budgets(
            rows,
            sid,
            cap,
            pilot_known_stop=study.PILOT_PROGRESS_STOP
            if ceiling == "pilot_progress"
            else None,
        )
