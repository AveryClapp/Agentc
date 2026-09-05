"""Offline factorial contracts, actual fake-network Ledger accounting, no paid API."""

from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from bench import openrouter_typed_factorial as f
from bench import openrouter_pilot as p

REAL_MATERIALIZE = f.materialize


def test_source_freeze_uses_real_workspace_and_transitive_native_dependencies():
    frozen = f.sources()
    assert "Cargo.toml" in frozen and "Cargo.lock" in frozen
    assert f.BRIDGE_SOURCE in frozen
    assert "crates/agentc-embed/Cargo.toml" in frozen
    assert "crates/agentc-embed/src/lib.rs" in frozen
    assert all((f.ROOT / name).is_file() for name in frozen)


def test_existing_real_native_bridge_across_all_factorial_inputs():
    path = os.environ.get("AGENTC_TYPED_MATERIALIZER")
    if not path:
        pytest.skip(
            "supply the existing materializer; this test never builds or calls a provider"
        )
    bridge = Path(path)
    attention = f.load_module(
        "factorial_actual_bridge_attention", f.ROOT / "python/agentc/_attention.py"
    )
    synth = {
        "sku": "K100",
        "vendor": "V200",
        "qty": 1,
        "unit": 2,
        "total": 2,
        "region": "EU",
    }
    for task in f.typed.make_tasks(f.SEED, 32):
        for arm, (model, rewrites) in f.ARMS.items():
            history = {}
            for stage in f.live.STAGES:
                call = f.restrict_call(
                    f.typed.call(
                        task,
                        stage,
                        history,
                        attention,
                        model=model,
                        run_identity="offline-" + arm,
                    ),
                    stage,
                )
                rule = f.RULES.get(stage) if rewrites else None
                selected, report = f.materialize(call, rule, bridge)
                assert selected["messages"] == [
                    call["messages"][i] for i in report["retained_indices"]
                ]
                assert len(selected["messages"]) == (
                    5
                    if rule == "ContextCompress"
                    else 3
                    if rule == "StateDrop"
                    else len(call["messages"])
                )
                assert selected["model"] == model
                assert (
                    selected["parameters"]["extra"]["response_format"]
                    == call["parameters"]["extra"]["response_format"]
                )
                assert (
                    "input_tokens_upper_bound"
                    not in selected["parameters"]["extra"]["agentc_route_context"]
                )
                value = (
                    ["D01", "D02", "D03"]
                    if stage == "filter"
                    else synth
                    if stage == "synthesize"
                    else "M01"
                )
                history[stage] = json.dumps({"result": value})


def endpoint(model):
    return {
        "tag": "anthropic",
        "provider_name": "Anthropic",
        "name": "Anthropic | " + p.SCHEMA_ENDPOINTS[model][0],
        "context_length": p.SCHEMA_ENDPOINTS[model][1],
        "supported_parameters": [
            "max_tokens",
            "temperature",
            "structured_outputs",
            "response_format",
        ],
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
    }


def raw_call():
    task = f.typed.make_tasks(f.SEED, 1)[0]
    attention = f.load_module(
        "factorial_test_attention", f.ROOT / "python/agentc/_attention.py"
    )
    return f.restrict_call(
        f.typed.call(
            task,
            "filter",
            {},
            attention,
            model=f.live.SOURCE_MODEL,
            run_identity="test",
        ),
        "filter",
    )


def bridge_response(call, indices):
    native = deepcopy(call)
    native["messages"] = [native["messages"][i] for i in indices]
    return {
        "rule": "ContextCompress",
        "applied": len(indices) != len(call["messages"]),
        "call": native,
        "retained_indices": indices,
        "materialization_only": True,
        "abstention_reason": None
        if len(indices) != len(call["messages"])
        else "not_applicable",
    }


def test_exact_native_deletions_remap_only_aligned_metadata(monkeypatch):
    call = raw_call()
    before = deepcopy(call)
    indices = [0, 1, len(call["messages"]) - 1]
    report = bridge_response(call, indices)
    monkeypatch.setattr(
        f.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=p.canonical(report)),
    )
    result, metadata = f.materialize(call, "ContextCompress", Path("/unused"))
    assert call == before
    assert result["messages"] == report["call"]["messages"]
    assert result["input_deps"] == [call["input_deps"][i] for i in indices]
    for key in ("message_deps", "attention_scores"):
        assert result["parameters"]["extra"][key] == [
            call["parameters"]["extra"][key][i] for i in indices
        ]
    assert (
        result["parameters"]["extra"]["response_format"]
        == call["parameters"]["extra"]["response_format"]
    )
    assert (
        "input_tokens_upper_bound"
        not in result["parameters"]["extra"]["agentc_route_context"]
    )
    assert metadata["materialization_only"] is True


@pytest.mark.parametrize(
    "change",
    [
        "model",
        "schema",
        "messages",
        "order",
        "duplicate",
        "bool",
        "negative",
        "empty",
        "applied",
        "extra",
    ],
)
def test_materializer_escape_rejected(monkeypatch, change):
    call = raw_call()
    report = bridge_response(call, [0, 1, 21])
    if change == "model":
        report["call"]["model"] = "other"
    elif change == "schema":
        report["call"]["parameters"]["extra"]["response_format"] = {}
    elif change == "messages":
        report["call"]["messages"][1]["content"] = "invented"
    elif change == "order":
        report["retained_indices"] = [1, 0, 21]
    elif change == "duplicate":
        report["retained_indices"] = [0, 0, 21]
    elif change == "bool":
        report["retained_indices"] = [False, 1, 21]
    elif change == "negative":
        report["retained_indices"] = [-1, 1, 21]
    elif change == "empty":
        report["retained_indices"] = []
    elif change == "applied":
        report["applied"] = False
    else:
        report["new"] = True
    monkeypatch.setattr(
        f.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=p.canonical(report)),
    )
    with pytest.raises(p.PilotError):
        f.materialize(call, "ContextCompress", Path("/unused"))


def test_no_rule_does_not_invoke_binary(monkeypatch):
    monkeypatch.setattr(
        f.subprocess, "run", lambda *a, **k: pytest.fail("no-op invoked binary")
    )
    call = raw_call()
    selected, report = f.materialize(call, None, Path("/unused"))
    assert selected == call and selected is not call
    assert report["applied"] is False


def test_schedule_is_paired_unique_and_deterministic():
    tasks = f.typed.make_tasks(f.SEED, 32)
    rows = f.schedule(tasks)
    assert rows == f.schedule(tasks) and len(rows) == 128
    for task in tasks:
        selected = [r for r in rows if r["task_id"] == task["task_id"]]
        assert {r["arm"] for r in selected} == set(f.ARMS)
        assert all(r["phase"] == "development" for r in selected)


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    monkeypatch.setattr(f, "COUNT", 1)
    monkeypatch.setattr(f, "sources", lambda: {"test_source": "frozen"})
    monkeypatch.setattr(f, "pilot_evidence", lambda args: {"reviewed_test_pilot": True})
    original = json.loads(
        (
            f.ROOT
            / "bench/repro/openrouter-typed-schema-v3-pilot-2026-09-05/manifest.json"
        ).read_text()
    )
    pilot = tmp_path / "pilot"
    pilot.mkdir()
    (pilot / "manifest.json").write_text(json.dumps(original))
    (pilot / "fixture.json").write_text(
        json.dumps(f.typed.make_tasks(f.feasibility.PILOT_SEED, 6))
    )
    bridge = tmp_path / "bridge"
    bridge.write_text("test-only-no-op")
    ledger = p.Ledger(tmp_path / "campaign.jsonl", "fake")
    with ledger.locked() as handle:
        ledger.append(handle, {"event": "origin", "usage_usd": "0"})
    args = SimpleNamespace(
        output=tmp_path / "result",
        pilot=pilot,
        bridge=bridge,
        ledger=ledger.path,
        pilot_manifest_sha256="test-only",
    )
    eps = {m: endpoint(m) for m in {a[0] for a in f.ARMS.values()}}
    monkeypatch.setattr(f, "endpoints", lambda key: eps)
    freeze = f.prepare(args, "fake")
    args.manifest_sha256 = freeze["manifest_sha256"]
    manifest = json.loads((args.output / "manifest.json").read_text())
    # Unit/integration tests isolate the paid coordinator from native mechanics;
    # the separate real bridge test checks deletion parity with actual Rust.
    monkeypatch.setattr(
        f,
        "materialize",
        lambda call, rule, bridge: (
            deepcopy(call),
            {
                "rule": rule,
                "applied": False,
                "retained_indices": list(range(len(call["messages"]))),
                "materialization_only": True,
                "abstention_reason": "not_applicable" if rule else "no_rule",
            },
        ),
    )
    dispatched = []

    def network(path, key, payload=None):
        if payload is None:
            model = next(m for m in eps if m in path)
            return {"data": {"endpoints": [eps[model]]}}
        dispatched.append(deepcopy(payload))
        stage = payload["response_format"]["json_schema"]["name"]
        value = (
            ["D01", "D02", "D03"]
            if "filter" in stage
            else "M01"
            if "answer" in stage
            else {
                "sku": "K100",
                "vendor": "V200",
                "qty": 1,
                "unit": 2,
                "total": 2,
                "region": "EU",
            }
        )
        # Deliberate first invalid output: coordinator must count it, continue
        # its own error state, and never substitute another cell's history.
        if len(dispatched) == 1:
            value = None
        model = payload["model"]
        return {
            "id": "gen-" + str(len(dispatched)),
            "model": model,
            "provider": "Anthropic",
            "usage": {
                "cost": "0.001",
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "is_byok": False,
            },
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "end_turn",
                    "message": {"content": json.dumps({"result": value})},
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
                            "model": p.SCHEMA_ENDPOINTS[model][0],
                        }
                    ]
                },
            },
        }

    monkeypatch.setattr(p, "request_json", network)
    monkeypatch.setattr(p, "account", lambda key: {"usage": 0})
    return args, manifest, ledger, dispatched


def test_actual_ledger_run_reconstruct_and_invalid_own_history(experiment):
    args, manifest, ledger, dispatched = experiment
    assert f.run(args, "fake", manifest) == {
        "completed_calls": 12,
        "schedule_complete": True,
    }
    assert len(dispatched) == 12
    assert f.typed.ERROR_INPUT in json.dumps(dispatched[1]["messages"])
    assert f.typed.ERROR_INPUT in json.dumps(dispatched[2]["messages"])
    assert f.typed.ERROR_INPUT not in json.dumps(dispatched[3]["messages"])
    result = f.analyze(args, "fake", manifest)
    assert result["schedule_complete"] and result["paired_tasks"] == 1
    report = json.loads((args.output / "analysis.json").read_text())
    first_arm = manifest["schedule"][0]["arm"]
    assert report["paired_tasks"][0]["cells"][first_arm]["em"] == 0
    assert (
        report["native_admission_tested"] is False and report["efficacy_claim"] is False
    )
    assert ledger.summary()["spent_usd"] == "0.012"
    before = ledger.path.read_bytes()
    with pytest.raises(p.PilotError, match="one-shot"):
        f.run(args, "fake", manifest)
    assert ledger.path.read_bytes() == before and len(dispatched) == 12


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "binary",
        "fixture",
        "identity",
        "cap",
        "key",
        "foreign-ledger",
        "empty-journal",
    ],
)
def test_fail_before_any_paid_call_on_changed_contract(
    experiment, monkeypatch, change, tmp_path
):
    args, manifest, ledger, dispatched = experiment
    key = "fake"
    if change == "source":
        monkeypatch.setattr(f, "sources", lambda: {"test_source": "changed"})
    elif change == "binary":
        args.bridge.write_text("changed")
    elif change == "fixture":
        (args.output / "fixture.json").write_text("[]")
    elif change == "identity":
        args.manifest_sha256 = "other"
    elif change == "cap":
        manifest["stage_cap_usd"] = "50"
    elif change == "key":
        key = "foreign"
    elif change == "foreign-ledger":
        other = p.Ledger(tmp_path / "other.jsonl", "fake")
        with other.locked() as handle:
            other.append(handle, {"event": "origin", "usage_usd": "0"})
        args.ledger = other.path
    else:
        (args.output / "calls.json").write_text("[]")
    before = ledger.path.read_bytes()
    with pytest.raises(p.PilotError):
        f.run(args, key, manifest)
    assert ledger.path.read_bytes() == before and not dispatched


@pytest.mark.parametrize("which", ["intent", "history", "result", "summary"])
def test_audit_rejects_altered_saved_evidence(experiment, which):
    args, manifest, _, dispatched = experiment
    f.run(args, "fake", manifest)
    name = (
        "summary.json"
        if which == "summary"
        else "calls.json"
        if which == "result"
        else "intents.json"
    )
    path = args.output / name
    data = json.loads(path.read_text())
    if which == "intent":
        data[0]["materialization"]["applied"] = True
    elif which == "history":
        data[1]["original_call"]["messages"][1]["content"] = "foreign-arm-state"
    elif which == "result":
        data[0]["cost_usd"] = "0"
    else:
        data["schedule_complete"] = False
    path.write_text(json.dumps(data))
    with pytest.raises(p.PilotError):
        f.analyze(args, "fake", manifest)
    assert len(dispatched) == 12


def test_multiplicative_price_savings_are_not_relative_interaction():
    cells = {a: {"em": 1, "cost_usd": c} for a, c in zip(f.ARMS, ["10", "5", "2", "1"])}
    result = f.contrasts(cells)
    assert result["quality_interaction"] == 0
    assert result["log_cost_ratio_interaction"] == pytest.approx(0)
    assert result["absolute_cost_interaction_usd"] == 4
    cells["HR"]["cost_usd"] = "0"
    assert f.contrasts(cells)["log_cost_ratio_interaction"] is None


def test_provider_failure_never_retried_retains_full_allowance(experiment, monkeypatch):
    args, manifest, ledger, dispatched = experiment
    original = p.request_json

    def failure(path, key, payload=None):
        if payload is not None:
            dispatched.append(payload)
            raise p.PilotError("test-provider-failure")
        return original(path, key, payload)

    monkeypatch.setattr(p, "request_json", failure)
    with pytest.raises(p.PilotError):
        f.run(args, "fake", manifest)
    first_model = manifest["arms"][manifest["schedule"][0]["arm"]][0]
    expected = (
        Decimal("6.01536") if first_model == f.live.SOURCE_MODEL else Decimal("1.21536")
    )
    assert Decimal(ledger.summary()["retained_uncertainty_usd"]) == expected
    with pytest.raises(p.PilotError, match="one-shot"):
        f.run(args, "fake", manifest)
    assert len(dispatched) == 1


@pytest.mark.parametrize("failed_first_runner", [False, True])
def test_copied_output_with_stale_preflight_cannot_claim_campaign_twice(
    experiment, monkeypatch, tmp_path, failed_first_runner
):
    args, manifest, ledger, dispatched = experiment
    copied = deepcopy(args)
    copied.output = tmp_path / "copied-output"
    shutil.copytree(args.output, copied.output)
    # B genuinely preflights before A starts, then is descheduled. Its output
    # directory and journals cannot reveal A's later provider failure.
    stale = f.validate(copied, manifest, "fake")
    if failed_first_runner:
        original = p.request_json

        def failure(path, key, payload=None):
            if payload is not None:
                dispatched.append(payload)
                raise p.PilotError("first runner failed")
            return original(path, key, payload)

        monkeypatch.setattr(p, "request_json", failure)
        with pytest.raises(p.PilotError):
            f.run(args, "fake", manifest)
    else:
        f.run(args, "fake", manifest)
    before = ledger.path.read_bytes()
    previous_calls = len(dispatched)
    monkeypatch.setattr(f, "validate", lambda *a, **k: stale)
    monkeypatch.setattr(
        p.Ledger,
        "call",
        lambda *a, **k: pytest.fail("copied runner entered paid boundary"),
    )
    with pytest.raises(p.PilotError, match="campaign/stage already claimed"):
        f.run(copied, "fake", manifest)
    assert len(dispatched) == previous_calls and ledger.path.read_bytes() == before
    assert not (copied.output / "intents.json").exists()


def test_real_bridge_invalid_error_history_is_safe_noop():
    path = os.environ.get("AGENTC_TYPED_MATERIALIZER")
    if not path:
        pytest.skip("supply an existing compatible materializer; no provider or build")
    task = f.typed.make_tasks(f.SEED, 1)[0]
    attention = f.load_module(
        "factorial_invalid_attention", f.ROOT / "python/agentc/_attention.py"
    )
    synth = '{"result":{"sku":"K100","vendor":"V200","qty":1,"unit":2,"total":2,"region":"EU"}}'
    for model in (f.live.SOURCE_MODEL, f.live.TARGET_MODEL):
        for history in (
            {"filter": f.typed.ERROR, "synthesize": synth},
            {"filter": '{"result":["D01","D02","D03"]}', "synthesize": f.typed.ERROR},
            {"filter": f.typed.ERROR, "synthesize": f.typed.ERROR},
        ):
            call = f.restrict_call(
                f.typed.call(
                    task,
                    "answer",
                    history,
                    attention,
                    model=model,
                    run_identity="invalid-offline",
                ),
                "answer",
            )
            selected, report = f.materialize(call, "StateDrop", Path(path))
            assert selected == call
            assert report["applied"] is False
            assert report["abstention_reason"] == "ambiguous_provenance"


def test_actual_native_and_ledger_invalid_rewritten_cell_completes(
    experiment, monkeypatch
):
    path = os.environ.get("AGENTC_TYPED_MATERIALIZER")
    if not path:
        pytest.skip("supply the real existing native materializer")
    args, manifest, ledger, dispatched = experiment
    args.bridge = Path(path)
    manifest["bridge_sha256"] = f.file_hash(args.bridge)
    args.manifest_sha256 = f.digest(manifest)
    (args.output / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(f, "materialize", REAL_MATERIALIZE)
    original = p.request_json

    def invalid_filters(path, key, payload=None):
        response = original(path, key, payload)
        if payload and "filter" in payload["response_format"]["json_schema"]["name"]:
            response["choices"][0]["message"]["content"] = f.typed.ERROR
        return response

    monkeypatch.setattr(p, "request_json", invalid_filters)
    assert f.run(args, "fake", manifest)["completed_calls"] == 12
    assert f.analyze(args, "fake", manifest)["schedule_complete"]
    report = json.loads((args.output / "analysis.json").read_text())
    assert report["materialization_abstentions"]["ambiguous_provenance"] == 2
    assert all(cell["em"] == 0 for cell in report["paired_tasks"][0]["cells"].values())
    assert ledger.summary()["spent_usd"] == "0.012" and len(dispatched) == 12


@pytest.mark.parametrize("generation", [None, "", 42, False])
def test_missing_generation_stops_before_another_paid_call(
    experiment, monkeypatch, generation
):
    args, manifest, ledger, dispatched = experiment
    original = p.request_json

    def missing(path, key, payload=None):
        response = original(path, key, payload)
        if payload:
            response["id"] = generation
        return response

    monkeypatch.setattr(p, "request_json", missing)
    with pytest.raises(p.PilotError, match="missing or duplicated"):
        f.run(args, "fake", manifest)
    assert len(dispatched) == 1
    assert ledger.summary()["spent_usd"] == "0.001"
    summary = json.loads((args.output / "summary.json").read_text())
    assert not summary["schedule_complete"] and summary["completed_calls"] == 0
    assert len(summary["stage_accounting"]["unincorporated_completed_ids"]) == 1
