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
    ledger = pilot.Ledger(args.ledger, "fake-key")
    with ledger.locked() as handle:
        ledger.append(handle, {"event": "origin", "usage_usd": "0"})
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


def ledger_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def replace_ledger(path, events):
    """Deliberately corrupt only this test's synthetic, temporary ledger."""
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def reject_before_ledger_call(args, manifest, state, monkeypatch, *, key="fake-key", match=None):
    before_requests = len(state["requests"])
    before_reads = state["endpoint_reads"]
    before = args.ledger.read_bytes() if args.ledger.exists() else None

    def forbidden(*args, **kwargs):
        pytest.fail("preflight reached Ledger.call, even if it might have been cached")

    monkeypatch.setattr(pilot.Ledger, "call", forbidden)
    with pytest.raises(pilot.PilotError, match=match):
        driver.run_pilot(args, key, manifest)
    assert len(state["requests"]) == before_requests
    assert state["endpoint_reads"] == before_reads
    assert (args.ledger.read_bytes() if args.ledger.exists() else None) == before


def test_manifest_freezes_existing_campaign_origin_without_writing(experiment):
    args, manifest, _, _ = experiment
    events = ledger_events(args.ledger)
    assert len(events) == 1
    assert manifest["campaign_ledger"] == {
        "resolved_path": str(args.ledger.resolve()),
        "origin_event_sha256": pilot.digest(events[0]),
    }
    before = args.ledger.read_bytes()
    driver.validate(args, manifest, "fake-key")
    assert args.ledger.read_bytes() == before


@pytest.mark.parametrize("prefix", [0, 4])
@pytest.mark.parametrize("kind", ["missing", "copied", "foreign-origin"])
def test_wrong_ledger_rejected_even_for_zero_saved_rows(experiment, monkeypatch, prefix, kind):
    args, manifest, _, state = experiment
    if prefix:
        args.max_calls = prefix
        driver.run_pilot(args, "fake-key", manifest)
    original = args.ledger
    args.ledger = original.with_name("wrong-ledger.jsonl")
    if kind != "missing":
        events = ledger_events(original)
        if kind == "foreign-origin":
            events = [{**events[0], "at": "different-campaign"}]
        replace_ledger(args.ledger, events)
    original_bytes = original.read_bytes()
    args.max_calls = None
    reject_before_ledger_call(args, manifest, state, monkeypatch)
    assert original.read_bytes() == original_bytes


@pytest.mark.parametrize("prefix", [0, 4])
def test_wrong_dispatch_key_rejected_before_any_ledger_call(experiment, monkeypatch, prefix):
    args, manifest, _, state = experiment
    if prefix:
        args.max_calls = prefix
        driver.run_pilot(args, "fake-key", manifest)
    args.max_calls = None
    reject_before_ledger_call(args, manifest, state, monkeypatch, key="other-key", match="different API key")


@pytest.mark.parametrize("mutation", ["missing-origin", "duplicate-origin", "changed-origin", "empty", "missing-binding"])
def test_origin_binding_is_not_optional_on_run(experiment, monkeypatch, mutation):
    args, manifest, _, state = experiment
    events = ledger_events(args.ledger)
    if mutation in {"missing-origin", "empty"}:
        events = []
    elif mutation == "duplicate-origin":
        events.append(events[0])
    elif mutation == "changed-origin":
        events[0]["usage_usd"] = "1"
    else:
        del manifest["campaign_ledger"]
        args.manifest_sha256 = pilot.digest(manifest)
        write_json(args.output / "manifest.json", manifest)
    replace_ledger(args.ledger, events)
    reject_before_ledger_call(args, manifest, state, monkeypatch)


@pytest.mark.parametrize("mutation", ["missing", "empty", "wrong-key", "duplicate-origin"])
def test_prepare_requires_existing_key_bound_origin(experiment, mutation):
    args, _, _, state = experiment
    args.output = args.output.parent / "new-pilot"
    events = ledger_events(args.ledger)
    if mutation == "missing":
        args.ledger = args.ledger.with_name("not-created.jsonl")
    elif mutation == "empty":
        replace_ledger(args.ledger, [])
    elif mutation == "duplicate-origin":
        replace_ledger(args.ledger, events + events)
    key = "other-key" if mutation == "wrong-key" else "fake-key"
    before = args.ledger.read_bytes() if args.ledger.exists() else None
    with pytest.raises(pilot.PilotError):
        driver.prepare(args, key, pilot=True)
    assert (args.ledger.read_bytes() if args.ledger.exists() else None) == before
    assert not args.output.exists()
    assert not state["requests"]


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("mutation", ["answer", "status", "cost", "extra-field"])
def test_entire_saved_prefix_reconstructed_before_any_ledger_call(experiment, monkeypatch, index, mutation):
    args, manifest, _, state = experiment
    args.max_calls = 4
    driver.run_pilot(args, "fake-key", manifest)
    path = args.output / "calls.json"
    rows = json.loads(path.read_text())
    row = rows[index]
    if mutation == "answer":
        row["answer"] = workload.application_text(row["answer"])
    elif mutation == "status":
        row["finish_reason"] = "length"
    elif mutation == "cost":
        row["cost_usd"] = "0.002"
    else:
        row["invented"] = True
    write_json(path, rows)
    args.max_calls = None
    reject_before_ledger_call(args, manifest, state, monkeypatch)


@pytest.mark.parametrize("mutation", [
    "missing-result", "missing-stage", "raw-response", "request", "metadata",
    "reserve-bound", "endpoint-observation", "response-timing", "orphan-response",
])
def test_authoritative_evidence_reconstructed_before_dispatch(experiment, monkeypatch, mutation):
    args, manifest, _, state = experiment
    args.max_calls = 1
    driver.run_pilot(args, "fake-key", manifest)
    events = ledger_events(args.ledger)
    if mutation == "missing-result":
        events = [event for event in events if event["event"] != "result"]
    elif mutation == "missing-stage":
        events = events[:1]
    elif mutation == "raw-response":
        next(event for event in events if event["event"] == "response")["response"]["choices"][0]["message"]["content"] += " "
    elif mutation == "response-timing":
        next(event for event in events if event["event"] == "response")["latency_ms"] += 1
    elif mutation == "orphan-response":
        orphan = deepcopy(next(event for event in events if event["event"] == "response"))
        orphan["id"] += "-orphan"
        events.append(orphan)
    else:
        reserve = next(event for event in events if event["event"] == "reserve")
        if mutation == "request":
            reserve["request"]["messages"][0]["content"] += " "
        elif mutation == "metadata":
            reserve["metadata"]["scope"] = "shadow"
        elif mutation == "endpoint-observation":
            reserve["schema_endpoint_observation"]["context_length"] *= 2
        else:
            reserve["upper_cost_usd"] = "0.1"
    replace_ledger(args.ledger, events)
    args.max_calls = None
    reject_before_ledger_call(args, manifest, state, monkeypatch)


@pytest.mark.parametrize("journal", ["calls.json", "decisions.json", "intents.json"])
def test_missing_journal_never_means_unspent(experiment, monkeypatch, journal):
    args, manifest, _, state = experiment
    args.max_calls = 1
    driver.run_pilot(args, "fake-key", manifest)
    (args.output / journal).unlink()
    args.max_calls = None
    reject_before_ledger_call(args, manifest, state, monkeypatch)


def test_completed_result_before_calls_write_halts_for_review(experiment, monkeypatch):
    args, manifest, _, state = experiment
    original_write = live.write_json

    def crash_before_calls(path, value, **kwargs):
        if path.name == "calls.json":
            raise OSError("synthetic crash before application result persistence")
        return original_write(path, value, **kwargs)

    with monkeypatch.context() as crash:
        crash.setattr(live, "write_json", crash_before_calls)
        with pytest.raises(OSError, match="synthetic crash"):
            driver.run_pilot(args, "fake-key", manifest)
    assert len(state["requests"]) == 1
    assert not (args.output / "calls.json").exists()
    assert sum(event["event"] == "result" for event in ledger_events(args.ledger)) == 1
    reject_before_ledger_call(args, manifest, state, monkeypatch, match="review required")


@pytest.mark.parametrize("terminal", ["attempt_failure", "attempt_abandoned", "pending"])
def test_first_provider_failure_permanently_stops_frozen_pilot(experiment, monkeypatch, terminal):
    args, manifest, _, state = experiment
    transport = pilot.request_json

    def fail_request(path, key, payload=None):
        if payload is None:
            return transport(path, key, payload)
        state["requests"].append(deepcopy(payload))
        raise pilot.ProviderFailure("synthetic 503", {"kind": "http_503", "retryable": True})

    monkeypatch.setattr(pilot, "request_json", fail_request)
    with pytest.raises(pilot.ProviderFailure):
        driver.run_pilot(args, "fake-key", manifest)
    events = ledger_events(args.ledger)
    reserve = next(event for event in events if event["event"] == "reserve")
    failed = next(event for event in events if event["event"] == "attempt_failure")
    assert failed["budget_hold_usd"] == reserve["upper_cost_usd"]
    monkeypatch.setattr(pilot.time, "time", lambda: failed["retry_not_before_epoch"] + 100)
    if terminal == "attempt_abandoned":
        ledger = pilot.Ledger(args.ledger, "fake-key")
        with ledger.locked() as handle:
            ledger.append(handle, {**failed, "event": "attempt_abandoned", "reason": "synthetic reviewed abandonment"})
    elif terminal == "pending":
        replace_ledger(args.ledger, [event for event in events if event["event"] != "attempt_failure"])
    reject_before_ledger_call(args, manifest, state, monkeypatch)
    assert len(state["requests"]) == 1
