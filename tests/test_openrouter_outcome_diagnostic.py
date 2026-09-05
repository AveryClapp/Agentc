"""No-network diagnostic contract and real-ledger replay tests."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bench import openrouter_outcome_diagnostic as diag
from bench import openrouter_pilot as pilot
from bench.openrouter_matrix import file_hash, write_json


@pytest.fixture
def example(tmp_path, monkeypatch):
    task = {
        "task_id": "used-training",
        "prompt": "Public question?",
        "meta": {"paragraphs": [{"title": "Title", "sentences": ["Public facts"]}]},
        "expected": "PRIVATE_GOLD",
    }
    endpoint = {
        "tag": "anthropic",
        "provider_name": "Anthropic",
        "name": "Anthropic | endpoint-sonnet",
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
    }
    calls, decisions = [], []
    for i, site in enumerate(diag.STAGES):
        primary = f"primary-{site}"
        candidate = f"candidate-{site}"
        payload = pilot.make_request(
            diag.live.SOURCE_MODEL,
            ["anthropic"],
            [{"role": "user", "content": "exact-original-" + site}],
            max_tokens=512,
        )
        signature = {
            "kind": "pass_through",
            "primary": payload,
            "candidate": {**payload, "model": diag.live.TARGET_MODEL},
            "candidate_identity": {"key": {"execution_plan_id": site}},
            "candidate_rules": ["ModelDowngrade"],
        }
        decisions.append(
            {
                "phase": "training",
                "arm": "joint",
                "task_id": task["task_id"],
                "workflow_stage": site,
                "semantic_plan": signature,
                "divergence_feedback": 0 if i == 2 else 0.5,
                "primary_id": primary,
                "incurred_ids": [primary, candidate],
            }
        )
        calls.extend(
            [
                {"id": primary, "answer": "old-source-" + site},
                {"id": candidate, "answer": "old-candidate-" + site},
            ]
        )
    data = {
        "calls.json": calls,
        "decisions.json": decisions,
        "summary.json": {"cost_usd": "1"},
    }
    pairs = diag.pairs_for(data)
    args = SimpleNamespace(
        output=tmp_path / "output",
        fixture=tmp_path / "fixture.json",
        ledger=tmp_path / "ledger.jsonl",
        previous=tmp_path / "previous",
        max_calls=None,
    )
    write_json(
        args.fixture,
        [task, {**task, "task_id": "UNTOUCHED_HELDOUT", "expected": "HELDOUT_GOLD"}],
    )
    for name in diag.FILES:
        write_json(args.previous / name, data.get(name, {}))
    manifest = {
        "kind": "paired_source_suffix_development_v1",
        "pairs": pairs,
        "schedule": diag.schedule_for(pairs),
        "stage_cap_usd": diag.CAP,
        "source_model": diag.live.SOURCE_MODEL,
        "endpoints": {diag.live.SOURCE_MODEL: endpoint},
        "prompts": diag.live.prompt_constants(),
        "limitations": [],
        "fixture_sha256": file_hash(args.fixture),
        "source_files": diag.sources(),
        "previous_manifest_sha256": diag.PREVIOUS_HASH,
        "previous_files": {n: file_hash(args.previous / n) for n in diag.FILES},
    }
    args.manifest_sha256 = pilot.digest(manifest)
    requests = []

    def transport(path, key, payload=None):
        assert path == "/chat/completions"
        assert "PRIVATE_GOLD" not in json.dumps(
            payload
        ) and "HELDOUT_GOLD" not in json.dumps(payload)
        requests.append(deepcopy(payload))
        return {
            "id": f"gen-{len(requests)}",
            "model": payload["model"],
            "provider": "Anthropic",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": f"new-output-{len(requests)}",
                    },
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
                            "model": "endpoint-sonnet",
                        }
                    ]
                },
            },
        }

    monkeypatch.setattr(pilot, "request_json", transport)
    monkeypatch.setattr(
        pilot, "account", lambda key: {"usage": "0", "limit_remaining": "50"}
    )
    attention = Mock()
    attention.compute_attention_scores.side_effect = lambda messages, _: (
        [0.5] * len(messages),
        [],
    )
    ledger = pilot.Ledger(args.ledger, "fake-not-a-key")
    return args, manifest, data, ledger, attention, requests


def test_all_pairs_and_zero_disagreement_included(example):
    _, manifest, _, _, _, _ = example
    assert len(manifest["pairs"]) == 3
    assert len(manifest["schedule"]) == 7 + 4 + 1
    assert sum(p["lexical_divergence"] == 0 for p in manifest["pairs"]) == 1
    assert len({pilot.digest(i) for i in manifest["schedule"]}) == 12
    assert manifest["schedule"] == diag.schedule_for(manifest["pairs"])


def test_paired_suffix_has_own_history_exact_repeat_and_source_only(example):
    args, manifest, data, ledger, attention, requests = example
    report = diag.acquire(args, manifest, data, ledger, "fake-not-a-key", attention)
    assert report["schedule_complete"] and len(requests) == 12
    assert report["incremental_paid_usd"] == "0.012"
    assert report["archive_and_diagnostic_known_usd"] == "1.012"
    assert not report["efficacy_claim"]
    rows = json.loads((args.output / "calls.json").read_text())
    pair_map = {p["pair_id"]: p for p in manifest["pairs"]}
    branch_rows = {}
    for item, payload, row in zip(manifest["schedule"], requests, rows):
        assert payload["model"] == diag.live.SOURCE_MODEL
        pair = pair_map[item["pair_id"]]
        if (
            item["branch"] == "repeat"
            and item["workflow_stage"] == pair["workflow_stage"]
        ):
            assert payload == pair["reference_payload"]
        else:
            history = deepcopy(pair["history"])
            if item["branch"] != "repeat":
                history[pair["workflow_stage"]] = pair[item["branch"] + "_output"]
            history.update(branch_rows.get((item["pair_id"], item["branch"]), {}))
            assert payload["messages"][1]["content"] == history["filter"]
            if item["workflow_stage"] == "answer":
                assert payload["messages"][2]["content"] == history["synthesize"]
        branch_rows.setdefault((item["pair_id"], item["branch"]), {})[
            item["workflow_stage"]
        ] = row["answer"]
    summary = json.loads((args.output / "summary.json").read_text())
    assert len(summary["paired_comparisons"]) == 3
    assert all(
        r["task_id"] != "UNTOUCHED_HELDOUT" for r in summary["paired_comparisons"]
    )


def test_resume_real_ledger_never_repeats_paid_calls_and_partial_suppresses(example):
    args, manifest, data, ledger, attention, requests = example
    args.max_calls = 2
    first = diag.acquire(args, manifest, data, ledger, "fake-not-a-key", attention)
    assert not first["schedule_complete"] and len(requests) == 2
    assert (
        json.loads((args.output / "summary.json").read_text())["paired_comparisons"]
        == []
    )
    args.max_calls = None
    diag.acquire(args, manifest, data, ledger, "fake-not-a-key", attention)
    assert len(requests) == 12
    diag.acquire(args, manifest, data, ledger, "fake-not-a-key", attention)
    assert len(requests) == 12


def test_corrupted_saved_answer_stops_before_next_dispatch(example):
    args, manifest, data, ledger, attention, requests = example
    args.max_calls = 2
    diag.acquire(args, manifest, data, ledger, "fake-not-a-key", attention)
    path = args.output / "calls.json"
    rows = json.loads(path.read_text())
    rows[0]["answer"] = "tampered"
    write_json(path, rows)
    args.max_calls = None
    with pytest.raises(pilot.PilotError, match="immutable ledger"):
        diag.acquire(args, manifest, data, ledger, "fake-not-a-key", attention)
    assert len(requests) == 2


def test_shorter_replay_preserves_completed_summary(example):
    args, manifest, data, ledger, attention, requests = example
    diag.acquire(args, manifest, data, ledger, "fake-not-a-key", attention)
    path = args.output / "summary.json"
    completed = path.read_bytes()
    args.max_calls = 1
    with pytest.raises(pilot.PilotError, match="precedes saved evidence"):
        diag.acquire(args, manifest, data, ledger, "fake-not-a-key", attention)
    assert len(requests) == 12 and path.read_bytes() == completed


@pytest.mark.parametrize("cap", [0, -1, True, 1.5])
def test_invalid_prefix_cap_rejected_before_dispatch(example, cap):
    args, manifest, data, ledger, attention, requests = example
    args.max_calls = cap
    with pytest.raises(pilot.PilotError, match="cap"):
        diag.acquire(args, manifest, data, ledger, "fake-not-a-key", attention)
    assert requests == []


@pytest.mark.parametrize(
    "field",
    [
        "stage_cap_usd",
        "source_files",
        "schedule",
        "pairs",
        "fixture_sha256",
        "previous_manifest_sha256",
    ],
)
def test_manifest_mutation_rejected(example, field):
    args, manifest, data, _, _, _ = example
    diag.validate_manifest(manifest, args, data)
    manifest[field] = "changed"
    with pytest.raises(pilot.PilotError, match="frozen manifest"):
        diag.validate_manifest(manifest, args, data)


def test_no_probe_without_real_feedback(example):
    _, _, data, _, _, _ = example
    data["decisions.json"][0]["divergence_feedback"] = None
    with pytest.raises(pilot.PilotError, match="completed exact source-paired"):
        diag.pairs_for(data)


def test_heldout_never_selected(example):
    _, _, data, _, _, _ = example
    for d in data["decisions.json"]:
        d["phase"] = "heldout"
    with pytest.raises(pilot.PilotError, match="missing or duplicated"):
        diag.pairs_for(data)


def test_real_failure_retry_is_charged_and_not_recorded_as_output(example, monkeypatch):
    args, manifest, data, ledger, attention, requests = example
    successful = pilot.request_json
    attempted = []

    def fail_once(path, key, payload=None):
        attempted.append(1)
        if len(attempted) == 1:
            raise pilot.ProviderFailure(
                "fake timeout", {"kind": "timeout", "retryable": True}
            )
        return successful(path, key, payload)

    monkeypatch.setattr(pilot, "request_json", fail_once)
    monkeypatch.setattr(diag.time, "sleep", lambda seconds: None)
    # Advance only the fake retry clock; no real sleeps/provider requests.
    clock = iter(range(100, 10000, 10))
    monkeypatch.setattr(pilot.time, "time", lambda: next(clock))
    report = diag.acquire(args, manifest, data, ledger, "fake-not-a-key", attention)
    assert len(requests) == 12 and len(attempted) == 13
    assert pilot.money(report["incremental_retained_uncertainty_usd"]) > 0
    assert len(json.loads((args.output / "calls.json").read_text())) == 12
