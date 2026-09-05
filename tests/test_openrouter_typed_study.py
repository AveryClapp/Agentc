"""Typed pilot acquisition and independent reconstruction through real Ledger."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from bench import openrouter_joint_analysis as analysis
from bench import openrouter_pilot as pilot
from bench import openrouter_rules_live as live
from bench import openrouter_structured_study as driver
from bench import typed_relational_workload as workload
from bench.openrouter_matrix import ROOT, load_module, write_json
from test_typed_relational_workload import reference


def endpoint(model):
    return {
        "tag": "anthropic", "provider_name": "Anthropic",
        "name": "Anthropic | " + pilot.SCHEMA_ENDPOINTS[model][0],
        "context_length": pilot.SCHEMA_ENDPOINTS[model][1],
        "supported_parameters": ["max_tokens", "temperature", "response_format", "structured_outputs"],
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
    }


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    args = SimpleNamespace(
        output=tmp_path / "pilot", fixture=tmp_path / "pilot/fixture.json",
        ledger=tmp_path / "ledger.jsonl", max_calls=None, pilot=tmp_path / "pilot",
        template=ROOT / "bench/repro/openrouter-joint-input-rules-2026-09-05/manifest.json",
        workload=driver.TYPED_WORKLOAD,
    )
    monkeypatch.setattr(driver, "endpoints", lambda key: {m: endpoint(m) for m in driver.PILOT_ARMS.values()})
    report = driver.prepare(args, "fake-key", pilot=True)
    manifest = json.loads((args.output / "manifest.json").read_text())
    args.manifest_sha256 = args.pilot_manifest_sha256 = report["manifest_sha256"]
    tasks = {t["task_id"]: t for t in json.loads(args.fixture.read_text())}
    steps = [{**item, "workflow_stage": stage} for item in manifest["schedule"] for stage in live.STAGES]
    state = {"requests": [], "endpoint_reads": 0, "finish": "stop", "invalid_at": None}

    def transport(path, key, payload=None):
        if payload is None:
            state["endpoint_reads"] += 1
            model = path.removeprefix("/models/").removesuffix("/endpoints")
            return {"data": {"endpoints": [endpoint(model)]}}
        assert path == "/chat/completions"
        item = steps[len(state["requests"])]
        stage = item["workflow_stage"]
        assert payload["response_format"] == workload.schema_for(stage)
        assert "expected" not in json.dumps(payload)
        state["requests"].append(deepcopy(payload))
        text = reference(tasks[item["task_id"]])[stage]
        # Formatting is retained in the receipt, canonicalized only for the app.
        text = " \n" + json.dumps(json.loads(text), indent=2) + "\t"
        if state["invalid_at"] == len(state["requests"]):
            text = workload.ERROR
        model = payload["model"]
        return {
            "id": f"typed-generation-{len(state['requests'])}", "model": model,
            "provider": "Anthropic",
            "choices": [{"finish_reason": state["finish"], "message": {"role": "assistant", "content": text}}],
            "usage": {"cost": "0.001", "prompt_tokens": 500, "completion_tokens": 40},
            "openrouter_metadata": {
                "requested": model, "attempt": 1, "is_byok": False, "pipeline": [],
                "endpoints": {"available": [{"selected": True, "provider": "Anthropic", "model": pilot.SCHEMA_ENDPOINTS[model][0]}]},
            },
        }
    monkeypatch.setattr(pilot, "request_json", transport)
    monkeypatch.setattr(pilot, "account", lambda key: {"usage": "0", "limit_remaining": "50"})
    return args, manifest, tasks, state


def test_all54_real_ledger_calls_schema_and_status_reconstructed(experiment):
    args, manifest, _, state = experiment
    driver.validate(args, manifest)
    summary = driver.run_pilot(args, "fake-key", manifest)
    assert summary["gate"]["pass"]
    assert summary["gate"]["exact_answers_by_arm"] == {"source": 6.0, "repeat": 6.0, "target": 6.0}
    assert len(state["requests"]) == state["endpoint_reads"] == 54
    data, finance = driver.verified_pilot(args)
    assert finance["known_stage_usd"] == "0.054"
    assert finance["snapshot_financially_complete"]
    calls = data["calls.json"]
    assert all(r["answer"].startswith(" \n") for r in calls)
    assert all(live.application_answer(manifest, r).startswith('{"result":') for r in calls)
    driver.run_pilot(args, "fake-key", manifest)
    assert len(state["requests"]) == state["endpoint_reads"] == 54


@pytest.mark.parametrize("finish,invalid", [("length", None), ("stop", 1)])
def test_invalid_application_stops_and_cannot_promote(experiment, finish, invalid):
    args, manifest, _, state = experiment
    state.update(finish=finish, invalid_at=invalid)
    with pytest.raises(pilot.PilotError, match="invalid application"):
        driver.run_pilot(args, "fake-key", manifest)
    summary = json.loads((args.output / "summary.json").read_text())
    assert summary["completed_calls"] == 1 and not summary["gate"]["pass"]
    calls = json.loads((args.output / "calls.json").read_text())
    assert live.application_answer(manifest, calls[0]) == workload.ERROR
    assert calls[0]["answer"] != workload.ERROR or invalid == 1
    assert summary["stage_accounting"]["paid_stage_cost_usd"] == "0.001"
    with pytest.raises(pilot.PilotError):
        driver.verified_pilot(args)
    with pytest.raises(pilot.PilotError, match="invalid application"):
        driver.run_pilot(args, "fake-key", manifest)
    assert len(state["requests"]) == 1


def test_partial_prefix_and_resume_preserve_typed_state(experiment):
    args, manifest, _, state = experiment
    args.max_calls = 4
    first = driver.run_pilot(args, "fake-key", manifest)
    assert first["completed_calls"] == 4 and not first["gate"]["pass"]
    args.max_calls = None
    assert driver.run_pilot(args, "fake-key", manifest)["gate"]["pass"]
    driver.verified_pilot(args)
    assert len(state["requests"]) == 54


@pytest.mark.parametrize("mutation", ["raw", "status", "schema", "summary", "pipeline"])
def test_reconstruction_rejects_tampered_receipts_or_contract(experiment, mutation):
    args, manifest, _, _ = experiment
    driver.run_pilot(args, "fake-key", manifest)
    name = "calls.json"
    data = json.loads((args.output / name).read_text())
    if mutation == "raw":
        data[0]["answer"] = workload.application_text(data[0]["answer"])
    elif mutation == "status":
        data[0]["finish_reason"] = "length"
    elif mutation == "pipeline":
        data[0]["router_metadata"]["pipeline"] = [{"type": "response_healing"}]
    elif mutation == "schema":
        name = "intents.json"
        data = json.loads((args.output / name).read_text())
        data[0]["original_call"]["parameters"]["extra"]["response_format"]["json_schema"]["name"] = "different"
    else:
        name = "summary.json"
        data = json.loads((args.output / name).read_text())
        data["comparisons"][0]["score"]["em"] = 0
    write_json(args.output / name, data)
    with pytest.raises(pilot.PilotError):
        driver.verified_pilot(args)


@pytest.mark.parametrize("field,value", [
    ("bounded_transport_retry", True), ("stop_on_invalid_application", False),
    ("native_execution_allowed", True), ("training_cap_usd", "8"),
    ("stage_cap_usd", "8"), ("feedback_contract", "raw-lexical"),
])
def test_typed_failure_budget_and_metric_controls_frozen(experiment, field, value):
    args, manifest, _, _ = experiment
    manifest[field] = value
    args.manifest_sha256 = pilot.digest(manifest)
    with pytest.raises(pilot.PilotError):
        driver.validate(args, manifest)


def test_pilot_does_not_grant_native_execution(experiment):
    args, manifest, _, _ = experiment
    args.output = args.output.parent / "native-study"
    with pytest.raises(pilot.PilotError, match="context contract"):
        driver.prepare(args, "fake-key", pilot=False)


def test_native_runner_rejects_typed_before_loading_binary(experiment):
    args, _, _, _ = experiment
    with pytest.raises(pilot.PilotError, match="context contract"):
        live.run(args, "fake-key")


def test_schema_disagreement_is_value_based_with_status_failure(experiment):
    _, manifest, tasks, _ = experiment
    task = next(iter(tasks.values()))
    attention = load_module("typed_study_attention", ROOT / "python/agentc/_attention.py")
    call = live.workflow_for(manifest, task, "filter", {}, attention, model=live.SOURCE_MODEL, run_identity="test")
    text = reference(task)["filter"]
    row = {"answer": text, "finish_reason": "stop", "workflow_stage": "filter"}
    assert live.divergence_for(manifest, "filter", call, text, live.application_answer(manifest, row)) == 0
    row["finish_reason"] = "length"
    assert live.divergence_for(manifest, "filter", call, text, live.application_answer(manifest, row)) == 1
    assert "input_tokens_upper_bound" not in call["parameters"]["extra"]["agentc_route_context"]
    assert live.application_answer({}, {"answer": "unchanged raw"}) == "unchanged raw"


def test_finance_rechecks_schema_native_refusal_in_raw_body(experiment):
    args, manifest, tasks, _ = experiment
    driver.run_pilot(args, "fake-key", manifest)
    data = {n: json.loads((args.output / n).read_text()) for n in ("calls.json", "decisions.json", "intents.json")}
    attention = load_module("typed_finance_attention", ROOT / "python/agentc/_attention.py")
    _, expected, _ = analysis.validate_journals(manifest, data["decisions.json"], data["calls.json"], data["intents.json"], tasks, attention)
    events, _ = analysis.read_ledger_snapshot(args.ledger)
    body = next(e["response"] for e in events if e["event"] == "response")
    body["choices"][0]["native_finish_reason"] = "refusal"
    finance = analysis.ledger_report(events, "rules-live-dev-v1-" + pilot.digest(manifest)[:20], data["calls.json"], expected)
    assert finance["failed_provider_results"]
