"""Real append-only Ledger with fake transport/account; no credentials/network."""

from copy import deepcopy
from decimal import Decimal
import json
import sys
from types import SimpleNamespace

import pytest

from bench import openrouter_pilot as pilot
from bench import openrouter_shape_control as control
from bench import structured_relational_workload as workload
from bench.openrouter_matrix import ROOT, write_json


def fake_endpoints():
    return {
        model: {
            "tag": "anthropic",
            "provider_name": "Anthropic",
            "name": "Anthropic | " + model,
            "supported_parameters": ["max_tokens", "temperature"],
            "context_length": 200000,
            "max_completion_tokens": 64000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        }
        for model in control.MODELS
    }


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    args = SimpleNamespace(
        output=tmp_path / "control",
        ledger=tmp_path / "ledger.jsonl",
        max_calls=None,
        previous=ROOT
        / "bench/repro/openrouter-structured-format-v2-pilot-2026-09-05/manifest.json",
        fixture=tmp_path / "fixture.json",
    )
    write_json(args.fixture, workload.make_tasks(control.SEED, 6))
    monkeypatch.setattr(control, "endpoints", lambda _: fake_endpoints())
    prepared = control.prepare(args, "FAKE-TEST-KEY")
    args.manifest_sha256 = prepared["manifest_sha256"]
    manifest = json.loads((args.output / "manifest.json").read_text())
    requests, state = (
        [],
        {
            "fail_at": None,
            "failure_kind": "filter",
            "duplicate_at": None,
            "invalid_at": None,
            "wrong_provider_at": None,
            "missing_generation_at": None,
        },
    )
    truth = control.expected_selection(manifest["task"])

    def transport(path, key, payload=None):
        assert path == "/chat/completions"
        index = len(requests)
        item = manifest["schedule"][index]
        assert payload == item["payload"]
        assert "expected" not in payload
        requests.append(deepcopy(payload))
        text = control.LITERAL if item["condition"] == "literal" else truth
        if index == state["invalid_at"]:
            text = "Narration: " + text
        response = {
            "id": f"generation-{index if index == state['duplicate_at'] else index + 1}",
            "model": payload["model"],
            "provider": "Anthropic",
            "service_tier": "default",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "end_turn",
                    "message": {
                        "role": "assistant",
                        "content": text,
                        "reasoning": None,
                    },
                }
            ],
            "usage": {
                "cost": "0.001",
                "prompt_tokens": 100,
                "completion_tokens": 11,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
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
        if index == state["missing_generation_at"]:
            response["id"] = None
        if index == state["wrong_provider_at"]:
            response["openrouter_metadata"]["endpoints"]["available"][0]["provider"] = (
                "Other"
            )
        if index == state["fail_at"]:
            if state["failure_kind"] == "transport":
                raise pilot.ProviderFailure(
                    "SECRET-ERROR-BODY", {"kind": "transport_error", "retryable": True}
                )
            response["choices"][0]["finish_reason"] = "content_filter"
            response["choices"][0]["native_finish_reason"] = "refusal"
            response["choices"][0]["message"]["content"] = None
        return response

    monkeypatch.setattr(pilot, "request_json", transport)
    monkeypatch.setattr(
        pilot, "account", lambda _: {"usage": "0", "limit_remaining": "50"}
    )
    return args, manifest, requests, state


def read_rows(args):
    path = args.output / "calls.json"
    return json.loads(path.read_text()) if path.exists() else []


def test_full_real_ledger_run_and_exact_replay(experiment):
    args, manifest, requests, _ = experiment
    report = control.run(args, "FAKE-TEST-KEY")
    assert len(requests) == 10 and report["schedule_complete"]
    assert len(report["comparisons"]) == 10
    assert all(r["strict_valid"] and r["exact"] for r in report["comparisons"])
    assert report["stage_accounting"]["known_stage_usd"] == "0.010"
    assert report["stage_accounting"]["retained_uncertainty_usd"] == "0"
    assert report["efficacy_claim"] is False
    rows = read_rows(args)
    assert len({r["generation_id"] for r in rows}) == 10
    assert all("metadata" not in r and "key_id" not in r for r in rows)
    assert all(r["response_diagnostics"]["reasoning_is_null"] for r in rows)
    assert all(
        r["response_diagnostics"]["native_finish_reason"] == "end_turn" for r in rows
    )
    before = (args.output / "calls.json").read_bytes()
    replay = control.run(args, "FAKE-TEST-KEY")
    assert replay == report and len(requests) == 10
    assert (args.output / "calls.json").read_bytes() == before
    events = control.snapshot(pilot.Ledger(args.ledger, "FAKE-TEST-KEY"))
    assert control.verified_rows(events, manifest)[0] == rows


def test_factorial_has_only_intended_shape_scope_differences(experiment):
    _, manifest, _, _ = experiment
    schedule = manifest["schedule"]
    assert len(schedule) == 10
    assert [x["condition"] for x in schedule[:2]] == ["literal", "literal"]
    assert (
        len(
            {
                (x["model"], x["shape"], x["scope"])
                for x in schedule
                if x["condition"] == "selection"
            }
        )
        == 8
    )
    for model in control.MODELS:
        cells = {
            (x["shape"], x["scope"]): x["payload"]
            for x in schedule
            if x["model"] == model and x["condition"] == "selection"
        }
        for scope in ("full", "local"):
            single, multi = cells["single", scope], cells["multi", scope]
            assert single["messages"][0] == multi["messages"][0]
            assert single["messages"][1]["content"] == "\n\n".join(
                m["content"] for m in multi["messages"][1:]
            )
            assert {k: v for k, v in single.items() if k != "messages"} == {
                k: v for k, v in multi.items() if k != "messages"
            }
            assert len(multi["messages"]) == 22 and len(single["messages"]) == 2
        full, local = (
            cells["multi", "full"]["messages"],
            cells["multi", "local"]["messages"],
        )
        assert full[:-1] == local[:-1]
        assert (
            "Approval table" in full[-1]["content"]
            and "Approval table" not in local[-1]["content"]
        )
        assert (
            full[-1]["content"].split("Output requirement: ")[1]
            == local[-1]["content"].split("Output requirement: ")[1]
        )
    assert "expected" not in manifest["task"]
    assert workload.ERROR_INPUT not in json.dumps(schedule)


def test_partial_has_no_comparison_and_shorter_resume_preserves_artifacts(experiment):
    args, _, requests, _ = experiment
    args.max_calls = 4
    report = control.run(args, "FAKE-TEST-KEY")
    assert (
        len(requests) == 4
        and not report["schedule_complete"]
        and report["comparisons"] == []
    )
    assert report["stage_accounting"]["known_stage_usd"] == "0.004"
    saved = {
        name: (args.output / name).read_bytes()
        for name in ("calls.json", "summary.json")
    }
    args.max_calls = 3
    with pytest.raises(pilot.PilotError, match="precedes saved"):
        control.run(args, "FAKE-TEST-KEY")
    assert all(
        (args.output / name).read_bytes() == data for name, data in saved.items()
    )
    args.max_calls = None
    assert (
        control.run(args, "FAKE-TEST-KEY")["schedule_complete"] and len(requests) == 10
    )


@pytest.mark.parametrize(
    "change",
    ["external_sha", "sources", "payload", "cap", "retry", "task", "schedule_order"],
)
def test_frozen_controls_fail_before_dispatch(experiment, change):
    args, manifest, requests, _ = experiment
    if change == "external_sha":
        args.manifest_sha256 = "0" * 64
    else:
        if change == "sources":
            manifest["source_files"]["bench/openrouter_shape_control.py"] = "0" * 64
        elif change == "payload":
            manifest["schedule"][0]["payload"]["temperature"] = 1
        elif change == "cap":
            manifest["stage_cap_usd"] = "5"
        elif change == "retry":
            manifest["retry_policy"] = "once"
        elif change == "task":
            manifest["task"]["prompt"] += " changed"
        elif change == "schedule_order":
            manifest["schedule"].reverse()
        write_json(args.output / "manifest.json", manifest)
        args.manifest_sha256 = pilot.digest(manifest)
    with pytest.raises(pilot.PilotError):
        control.run(args, "FAKE-TEST-KEY")
    assert requests == [] and not args.ledger.exists()


@pytest.mark.parametrize(
    "field",
    [
        "answer",
        "cost_usd",
        "request_sha256",
        "schedule_index",
        "generation_id",
        "response_diagnostics",
    ],
)
def test_tampered_result_prefix_fails_before_fresh_dispatch(experiment, field):
    args, _, requests, _ = experiment
    args.max_calls = 2
    control.run(args, "FAKE-TEST-KEY")
    rows = read_rows(args)
    rows[1][field] = "TAMPERED"
    write_json(args.output / "calls.json", rows)
    args.max_calls = None
    with pytest.raises(pilot.PilotError, match="saved control rows"):
        control.run(args, "FAKE-TEST-KEY")
    assert len(requests) == 2


def test_crash_after_paid_ledger_before_artifact_recovers_without_second_dispatch(
    experiment, monkeypatch
):
    args, _, requests, _ = experiment
    real_write = control.write_json
    failed = False

    def write(path, value, **kwargs):
        nonlocal failed
        if path.name == "calls.json" and not failed:
            failed = True
            raise OSError("synthetic disk failure")
        return real_write(path, value, **kwargs)

    monkeypatch.setattr(control, "write_json", write)
    with pytest.raises(OSError):
        control.run(args, "FAKE-TEST-KEY")
    assert len(requests) == 1 and not (args.output / "calls.json").exists()
    report = json.loads((args.output / "summary.json").read_text())
    assert report["stage_accounting"]["known_stage_usd"] == "0.001"
    assert report["completed_calls"] == 0
    assert report["stage_accounting"]["artifact_billed_subtotal_usd"] == "0"
    assert len(report["stage_accounting"]["unincorporated_completed_ids"]) == 1
    assert not report["schedule_complete"]
    monkeypatch.setattr(control, "write_json", real_write)
    assert control.run(args, "FAKE-TEST-KEY")["schedule_complete"]
    assert len(requests) == 10


@pytest.mark.parametrize("kind", ["filter", "transport"])
def test_failure_costs_holds_no_observation_and_no_retry(experiment, kind):
    args, _, requests, state = experiment
    state.update(fail_at=2, failure_kind=kind)
    with pytest.raises(pilot.PilotError):
        control.run(args, "FAKE-TEST-KEY")
    assert len(requests) == 3 and len(read_rows(args)) == 2
    report = json.loads((args.output / "summary.json").read_text())
    cost = report["stage_accounting"]
    bound = pilot.upper_cost(requests[2])
    assert money(cost["known_stage_usd"]) == Decimal(
        "0.003" if kind == "filter" else "0.002"
    )
    assert money(cost["retained_uncertainty_usd"]) == bound - (
        Decimal("0.001") if kind == "filter" else 0
    )
    assert money(cost["conservative_stage_commitment_usd"]) == Decimal("0.002") + bound
    assert (
        not report["schedule_complete"]
        and report["comparisons"] == []
        and cost["failed_attempts"] == 1
    )
    state["fail_at"] = None
    with pytest.raises(pilot.PilotError, match="retries are not authorized"):
        control.run(args, "FAKE-TEST-KEY")
    assert len(requests) == 3


money = pilot.money


@pytest.mark.parametrize(
    "field", ["duplicate_at", "missing_generation_at", "wrong_provider_at"]
)
def test_invalid_fresh_attribution_stops_but_preserves_paid_cost(experiment, field):
    args, _, requests, state = experiment
    state[field] = 1
    with pytest.raises(pilot.PilotError):
        control.run(args, "FAKE-TEST-KEY")
    assert len(requests) == 2 and len(read_rows(args)) == 1
    report = json.loads((args.output / "summary.json").read_text())
    assert money(report["stage_accounting"]["known_stage_usd"]) == Decimal("0.002")
    assert report["comparisons"] == [] and not report["schedule_complete"]
    with pytest.raises(pilot.PilotError):
        control.run(args, "FAKE-TEST-KEY")
    assert len(requests) == 2


def test_invalid_format_is_scored_only_after_all_ten_not_normalized(experiment):
    args, _, requests, state = experiment
    state["invalid_at"] = 4
    report = control.run(args, "FAKE-TEST-KEY")
    assert len(requests) == 10 and report["schedule_complete"]
    assert sum(r["strict_valid"] for r in report["comparisons"]) == 9
    assert report["comparisons"][4]["exact"] is False


def test_complete_comparisons_do_not_feed_expected_into_dispatch(
    experiment, monkeypatch
):
    args, _, requests, _ = experiment
    args.max_calls = 9
    monkeypatch.setattr(
        control,
        "expected_selection",
        lambda _: (_ for _ in ()).throw(AssertionError("evaluator accessed early")),
    )
    report = control.run(args, "FAKE-TEST-KEY")
    assert len(requests) == 9 and report["comparisons"] == []


def test_prepare_immutable_and_wrong_fixture_before_catalog(experiment, monkeypatch):
    args, _, requests, _ = experiment
    monkeypatch.setattr(
        control,
        "endpoints",
        lambda _: (_ for _ in ()).throw(AssertionError("catalog fetched")),
    )
    with pytest.raises(pilot.PilotError, match="already frozen"):
        control.prepare(args, "FAKE-TEST-KEY")
    args.output = args.output.parent / "different"
    write_json(args.fixture, workload.make_tasks(9, 6))
    with pytest.raises(pilot.PilotError, match="first exposed"):
        control.prepare(args, "FAKE-TEST-KEY")
    assert requests == []


def test_cli_does_not_print_provider_exception_or_secret(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "control",
            "run",
            "--env-file",
            str(tmp_path / "unused"),
            "--ledger",
            str(tmp_path / "unused-ledger"),
            "--output",
            str(tmp_path),
            "--manifest-sha256",
            "0" * 64,
        ],
    )
    monkeypatch.setattr(control, "load_key", lambda _: "FAKE-TEST-KEY")
    monkeypatch.setattr(
        control,
        "run",
        lambda *_: (_ for _ in ()).throw(pilot.PilotError("SECRET-ERROR-BODY")),
    )
    assert control.main() == 1
    output = capsys.readouterr().out
    assert "SECRET" not in output and "FAKE-TEST-KEY" not in output
