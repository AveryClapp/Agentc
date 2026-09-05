"""Strict workload adapter, real-Ledger pilot, and promotion integrity tests."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from bench import openrouter_joint_study as original_study
from bench import openrouter_pilot as pilot
from bench import openrouter_rules_live as live
from bench import openrouter_structured_study as driver
from bench import structured_relational_workload as workload
from bench.openrouter_matrix import ROOT, file_hash, load_module, write_json
from test_structured_relational_workload import reference


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    tasks = workload.make_tasks(driver.PILOT_SEED, 6)
    args = SimpleNamespace(
        output=tmp_path / "pilot",
        fixture=tmp_path / "pilot/fixture.json",
        ledger=tmp_path / "ledger.jsonl",
        max_calls=None,
        pilot=tmp_path / "pilot",
    )
    write_json(args.fixture, tasks)
    endpoints = {
        m: {
            "tag": "anthropic",
            "provider_name": "Anthropic",
            "name": "Anthropic | " + m,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        }
        for m in (live.SOURCE_MODEL, live.TARGET_MODEL)
    }
    manifest = {
        "kind": driver.PILOT_KIND,
        "workload": driver.WORKLOAD,
        "format_variant": workload.FORMAT_VARIANT,
        "source_files": live.sources(driver.WORKLOAD),
        "prompts": workload.prompts(),
        "generator_seed": driver.PILOT_SEED,
        "generator_count": 6,
        "schedule": driver.pilot_schedule(tasks),
        "stage_cap_usd": "1",
        "training_cap_usd": "1",
        "fixture_sha256": file_hash(args.fixture),
        "pilot_arm_models": driver.PILOT_ARMS,
        "policies": {
            a: original_study.policies()["original"] for a in driver.PILOT_ARMS
        },
        "endpoints": endpoints,
        "scoring": "strict_application_scalar_exact_v1",
        "bounded_transport_retry": True,
        "limitations": [],
    }
    write_json(args.output / "manifest.json", manifest)
    args.manifest_sha256 = args.pilot_manifest_sha256 = pilot.digest(manifest)
    by_task = {t["task_id"]: t for t in tasks}
    steps = [
        {**row, "workflow_stage": stage}
        for row in manifest["schedule"]
        for stage in live.STAGES
    ]
    requests = []

    def transport(path, key, payload=None):
        assert path == "/chat/completions"
        item = steps[len(requests)]
        assert "expected" not in json.dumps(payload)
        requests.append(deepcopy(payload))
        text = reference(by_task[item["task_id"]])[item["workflow_stage"]]
        return {
            "id": f"generation-{len(requests)}",
            "model": payload["model"],
            "provider": "Anthropic",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {"cost": "0.001", "prompt_tokens": 20, "completion_tokens": 10},
            "openrouter_metadata": {
                "requested": payload["model"],
                "attempt": 1,
                "is_byok": False,
                "endpoints": {
                    "available": [
                        {
                            "selected": True,
                            "provider": "Anthropic",
                            "model": payload["model"],
                        }
                    ]
                },
            },
        }

    monkeypatch.setattr(pilot, "request_json", transport)
    monkeypatch.setattr(
        pilot, "account", lambda key: {"usage": "0", "limit_remaining": "50"}
    )
    return args, manifest, tasks, requests


def test_real_ledger_pilot_and_replayed_promotion_costs(experiment):
    args, manifest, _, requests = experiment
    result = driver.run_pilot(args, "fake-key", manifest)
    assert result["schedule_complete"] and len(requests) == 54
    assert result["gate"]["pass"] and result["gate"]["invalid_workflows"] == 0
    assert result["stage_accounting"]["paid_stage_cost_usd"] == "0.054"
    data, finance = driver.verified_pilot(args)
    assert data["summary.json"]["gate"]["pass"]
    assert finance["known_stage_usd"] == "0.054"
    driver.run_pilot(args, "fake-key", manifest)
    assert len(requests) == 54


@pytest.mark.parametrize("mutation", ["cost", "gate", "comparison", "call", "identity"])
def test_promotion_refuses_forged_evidence(experiment, mutation):
    args, manifest, _, requests = experiment
    driver.run_pilot(args, "fake-key", manifest)
    path = args.output / "summary.json"
    data = json.loads(path.read_text())
    if mutation == "cost":
        data["stage_accounting"]["paid_stage_cost_usd"] = "0"
    elif mutation == "gate":
        data["gate"]["exact_answers_by_arm"]["target"] = 0
    elif mutation == "comparison":
        data["comparisons"][0]["score"]["em"] = 0
    elif mutation == "call":
        path = args.output / "calls.json"
        data = json.loads(path.read_text())
        data[0]["answer"] = "D01|D02|D03"
    else:
        args.pilot_manifest_sha256 = "0" * 64
    write_json(path, data)
    with pytest.raises(pilot.PilotError):
        driver.verified_pilot(args)
    assert len(requests) == 54


def test_pilot_partial_and_shorter_replay_do_not_promote(experiment):
    args, manifest, _, requests = experiment
    args.max_calls = 3
    first = driver.run_pilot(args, "fake-key", manifest)
    assert not first["schedule_complete"] and not first["gate"]["pass"]
    assert first["comparisons"] == []
    with pytest.raises(pilot.PilotError):
        driver.verified_pilot(args)
    args.max_calls = None
    driver.run_pilot(args, "fake-key", manifest)
    saved = (args.output / "summary.json").read_bytes()
    args.max_calls = 2
    with pytest.raises(pilot.PilotError, match="saved evidence"):
        driver.run_pilot(args, "fake-key", manifest)
    assert saved == (args.output / "summary.json").read_bytes() and len(requests) == 54


def test_shared_adapter_strict_score_and_invalid_agreement(experiment):
    _, manifest, tasks, _ = experiment
    task = tasks[0]
    history = reference(task)
    attention = load_module(
        "strict_adapter_attention", ROOT / "python/agentc/_attention.py"
    )
    call = live.workflow_for(
        manifest,
        task,
        "answer",
        {s: history[s] for s in ("filter", "synthesize")},
        attention,
        model=live.SOURCE_MODEL,
        run_identity="test",
        prompts=manifest["prompts"],
    )
    assert live.divergence_for(manifest, "answer", call, "M01", "M01") == 0
    assert live.divergence_for(manifest, "answer", call, "ERROR", "ERROR") == 1
    call["parameters"]["extra"]["structured_relational_contract"]["upstream_valid"] = (
        False
    )
    assert live.divergence_for(manifest, "answer", call, "M01", "M01") == 1
    bad_history = {**history, "synthesize": "ERROR"}
    assert live.score_for(
        manifest, history["answer"], task["expected"], history=bad_history
    ) == {"em": 0, "f1": 0}
    assert (
        live.divergence_for({}, "answer", {}, "ERROR", "ERROR") == 0
    )  # original QA behavior unchanged


@pytest.mark.parametrize("bad", ["unknown", "", 42])
def test_unknown_workload_rejected(bad):
    with pytest.raises(pilot.PilotError):
        live.workflow_for({"workload": bad})
    with pytest.raises(pilot.PilotError):
        live.sources(bad)


def test_gate_requires_accuracy_and_validity():
    rows = [
        {
            "arm": a,
            "valid_by_site": {s: True for s in live.STAGES},
            "score": {"em": 1, "f1": 1},
        }
        for a in driver.PILOT_ARMS
        for _ in range(6)
    ]
    assert driver.pilot_gate(rows)["pass"]
    assert not driver.pilot_gate(rows[:-1])["pass"]
    for r in rows:
        r["score"] = {"em": 0, "f1": 0}
    assert not driver.pilot_gate(rows)["pass"]
    for r in rows:
        r["score"] = {"em": 1, "f1": 1}
    rows[0]["valid_by_site"]["filter"] = False
    assert not driver.pilot_gate(rows)["pass"]


def test_strict_scoring_contract_required(experiment):
    args, manifest, _, _ = experiment
    driver.validate(args, manifest)
    manifest["scoring"] = "unchanged_normalized_raw_answer_EM_F1"
    args.manifest_sha256 = pilot.digest(manifest)
    with pytest.raises(pilot.PilotError):
        driver.validate(args, manifest)
