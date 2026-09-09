"""Only synthetic public inputs/receipts; no real labels, provider or ledger."""

import itertools
import os
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from test_openrouter_protected_fixture import fixture as fixture  # noqa: PLC0414

from bench import openrouter_protected_forecast as f
from bench.openrouter_pilot import SCHEMA_ENDPOINTS, PilotError


def endpoint(model):
    return {
        "tag": "anthropic",
        "provider_name": "Anthropic",
        "name": "Anthropic | " + SCHEMA_ENDPOINTS[model][0],
        "context_length": 1_000_000 if model == f.MODELS[0] else 200_000,
        "supported_parameters": [
            "max_tokens",
            "temperature",
            "structured_outputs",
            "response_format",
        ],
        "pricing": {
            "prompt": "0.000003" if model == f.MODELS[0] else "0.000001",
            "completion": "0.000015" if model == f.MODELS[0] else "0.000005",
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


@pytest.fixture
def setup(fixture, monkeypatch):
    _, out, _, checkpoint, _ = fixture
    original_synthetic = f.fixture.typed.make_tasks

    def public_stage_tasks(seed, count):
        values = original_synthetic(seed, count)
        for task in values:
            task["prompt"] = (
                "Tenant TTEST; requisition R001. Apply the public approval policy."
            )
        return values

    monkeypatch.setattr(f.fixture.typed, "make_tasks", public_stage_tasks)
    bundle = f.fixture.materialize(out, checkpoint)
    enrollment = {
        "endpoints": {model: endpoint(model) for model in f.MODELS},
        "catalog": {"targets": [{"capabilities": {"structured_outputs": True}}]},
        "experiment_policy": {"output_cap": 256},
    }
    bridge = out / "synthetic-materializer"
    bridge.write_bytes(b"offline synthetic materializer sentinel")
    monkeypatch.setattr(f.materializer, "materialize", no_op)
    attention = f.load_module(
        "test_forecast_attention", f.ROOT / "python/agentc/_attention.py"
    )
    calls, intents = [], []
    indexed = {task["task_id"]: task for task in bundle["tasks"]}
    for task_id in bundle["selection"]["pilot_ids"]:
        for model in f.MODELS:
            for stage in f.typed.STAGES:
                original = f.typed.call(
                    indexed[task_id],
                    stage,
                    f._histories(indexed[task_id], stage)[0],
                    attention,
                    model=model,
                    run_identity="synthetic-pilot",
                )
                original["parameters"]["max_output_tokens"] = 256
                payload = f.live.payload_for(
                    original, {"workload": f.typed.WORKLOAD, **enrollment}
                )
                item = {
                    "phase": "pilot",
                    "arm": "S0" if model == f.MODELS[0] else "H0",
                    "task_id": task_id,
                    "workflow_stage": stage,
                    "id": f"synthetic-{len(calls)}",
                }
                usage = {
                    "prompt_tokens": (len(f.canonical(payload)) + 3) // 4,
                    "completion_tokens": 9,
                }
                calls.append(
                    {
                        **item,
                        "model": model,
                        "finish_reason": "stop",
                        "usage": usage,
                        "cost_usd": "0.01",
                        "answer": "never inspected",
                        "request_sha256": f.digest(payload),
                    }
                )
                intents.append({**item, "payload": payload, "original_call": original})
    return bundle, calls, intents, enrollment, bridge


def run(setup):
    return f.forecast(*setup)


def test_fresh_forecast_counts_rates_costs_and_reservation_are_separate(setup):
    report = run(setup)
    assert report["evidence_kind"] == "fresh_pilot"
    assert len(report["public_envelopes"]) == 69 * 3 * 2 * 2
    assert len(report["measurements"]) == 6
    assert all(len(row["samples"]) == 6 for row in report["measurements"])
    assert len(report["scenarios"]["seven_plans_twenty_completed_pairs"]["plans"]) == 7
    assert report["scenarios"]["seven_plans_twenty_completed_pairs"]["probes"] == 140
    assert report["scenarios"]["generic_sixty_attempts_per_site"]["attempts"] == 180
    assert Decimal(report["largest_pair_allowance_usd"]) == Decimal("12.015360")
    assert Decimal(report["training_usd"]) == Decimal(report["source_usd"]) + Decimal(
        report["candidate_usd"]
    )
    assert Decimal(report["training_and_HR_usd"]) == Decimal(
        report["training_usd"]
    ) + Decimal(report["HR_usd"])
    assert Decimal(report["training_plus_largest_pair_reserve_usd"]) == Decimal(
        report["training_usd"]
    ) + Decimal("12.01536")
    assert report["observed_evidence_cost_usd"] == "0.36"
    assert report["assumptions"]["provider_capacity_bound"] is False
    assert report["assumptions"]["spending_guarantee"] is False
    assert report["assumptions"]["uses_labels"] is False
    assert report["assumptions"]["cache_credit"] is False
    assert "answer" not in report["measurements"][0]["samples"][0]
    assert all(row["output_tokens"] == 256 for row in report["public_envelopes"])
    for row in report["public_envelopes"]:
        measurement = next(
            m
            for m in report["measurements"]
            if (m["model"], m["stage"]) == (row["model"], row["stage"])
        )
        expected = (
            row["payload_utf8_bytes"]
            * Decimal(measurement["max_effective_rate"])
            * Decimal("1.05")
        ).to_integral_value(rounding="ROUND_CEILING")
        assert row["estimated_input_tokens"] == int(expected)


def test_forecast_is_label_blind_and_unchanged_by_response_content(setup, monkeypatch):
    original = run(setup)
    bundle, calls, intents, enrollment, bridge = deepcopy(setup)
    for call in calls:
        call["answer"] = "different non-semantic bytes"
        call["unused_score"] = -999
    monkeypatch.setattr(
        f.typed, "score_answer", lambda *a, **k: pytest.fail("scored a task")
    )
    monkeypatch.setattr(
        f.fixture.typed, "make_tasks", lambda *a: pytest.fail("regenerated labels")
    )
    path_open = Path.open

    def guard(path, *args, **kwargs):
        assert path.name not in {"labels.json", ".env"}
        return path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard)
    changed = f.forecast(bundle, calls, intents, enrollment, bridge)
    for field in (
        "training_usd",
        "HR_usd",
        "prospective_pilot_usd",
        "public_envelopes",
        "measurements",
        "scenarios",
    ):
        assert changed[field] == original[field]
    assert (
        changed["provenance"]["calls_sha256"] != original["provenance"]["calls_sha256"]
    )


def test_largest_three_uses_json_utf8_not_characters_or_relevance(setup):
    task = deepcopy(setup[0]["tasks"][0])
    for paragraph in task["meta"]["paragraphs"]:
        paragraph["sentences"] = ["a"]
    task["meta"]["paragraphs"][0]["sentences"] = ["é" * 5]
    task["meta"]["paragraphs"][1]["sentences"] = ['"' * 6]
    task["meta"]["paragraphs"][2]["sentences"] = ["a" * 9]
    task["meta"]["paragraphs"][3]["sentences"] = ["🙂" * 4]
    history = f._histories(task, "answer")[0]
    assert f.typed.parse_filter(history["filter"]) == ("D01", "D02", "D04")
    assert f.typed.parse_synthesis(history["synthesize"]) == {
        "sku": "K999",
        "vendor": "V999",
        "qty": 999,
        "unit": 9999,
        "total": 9999999,
        "region": "US",
    }
    chosen = set(f.typed.parse_filter(history["filter"]))
    paragraphs = task["meta"]["paragraphs"]
    def size(items):
        return len(
            f.canonical(
                "\n\n".join(
                    p["title"] + "\n" + "\n".join(p["sentences"]) for p in items
                )
            )
        )

    best = size([p for p in paragraphs if p["title"] in chosen])
    assert best == max(size(items) for items in itertools.combinations(paragraphs, 3))


def test_probe_placement_uses_largest_twenty_not_first_twenty(setup):
    bundle, calls, intents, enrollment, bridge = deepcopy(setup)
    study_ids = bundle["selection"]["study_ids"]
    indexed = {task["task_id"]: task for task in bundle["tasks"]}
    for index, task_id in enumerate(study_ids):
        indexed[task_id]["meta"]["paragraphs"][0]["sentences"] = [
            "x" * (100 * (index + 1))
        ]
    bundle["provenance"]["public_sha256"] = f._file_digest(bundle["tasks"])
    report = f.forecast(bundle, calls, intents, enrollment, bridge)
    plans = report["scenarios"]["seven_plans_twenty_completed_pairs"]["plans"]
    assert all(
        set(plan["selected_cost_envelope_ids"]) == set(study_ids[-20:])
        for plan in plans
    )
    assert all(
        set(plan["selected_cost_envelope_ids"]) != set(study_ids[:20]) for plan in plans
    )


def test_invalid_upstream_can_dominate_rewritten_envelope(setup, monkeypatch):
    def drop_valid(call, rule, bridge):
        result, proof = no_op(call, rule, bridge)
        if (
            rule == "StateDrop"
            and call["parameters"]["extra"]["typed_relational_contract"][
                "upstream_valid"
            ]
        ):
            result["messages"].pop(1)
            proof["applied"] = True
        return result, proof

    monkeypatch.setattr(f.materializer, "materialize", drop_valid)
    report = run(setup)
    answers = [
        row
        for row in report["public_envelopes"]
        if row["stage"] == "answer" and row["rewrites"]
    ]
    assert all(len(row["history_variants"]) == 2 for row in answers)
    assert all(
        row["payload_utf8_bytes"]
        == max(v["payload_utf8_bytes"] for v in row["history_variants"])
        for row in answers
    )


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "model",
        "stage",
        "status",
        "cap",
        "bool_usage",
        "nonfinite_cost",
        "negative_usage",
        "id",
        "provider",
        "schema",
        "phase",
        "wrong_task",
        "extra_transport",
    ],
)
def test_bad_measurements_fail_before_any_materialization(setup, monkeypatch, change):
    bundle, calls, intents, enrollment, bridge = deepcopy(setup)
    if change == "missing":
        calls.pop()
        intents.pop()
    elif change == "duplicate":
        calls[1] = deepcopy(calls[0])
        intents[1] = deepcopy(intents[0])
    elif change == "model":
        calls[0]["model"] = "unsupported"
    elif change == "stage":
        calls[0]["workflow_stage"] = intents[0]["workflow_stage"] = "other"
    elif change == "status":
        calls[0]["finish_reason"] = "length"
    elif change == "cap":
        intents[0]["payload"]["max_tokens"] = 512
    elif change == "bool_usage":
        calls[0]["usage"]["prompt_tokens"] = True
    elif change == "nonfinite_cost":
        calls[0]["cost_usd"] = "NaN"
    elif change == "negative_usage":
        calls[0]["usage"]["completion_tokens"] = -1
    elif change == "id":
        calls[0]["id"] = "wrong"
    elif change == "provider":
        intents[0]["payload"]["provider"]["only"] = ["unknown"]
    elif change == "schema":
        intents[0]["payload"]["response_format"] = f.typed.schema_for("answer")
    elif change == "phase":
        calls[0]["phase"] = intents[0]["phase"] = "unreviewed"
    elif change == "wrong_task":
        calls[0]["task_id"] = intents[0]["task_id"] = "unselected"
    else:
        intents[0]["payload"]["plugins"] = []
    monkeypatch.setattr(
        f.materializer,
        "materialize",
        lambda *a: pytest.fail("materialized before validating evidence"),
    )
    with pytest.raises(PilotError):
        f.forecast(bundle, calls, intents, enrollment, bridge)


def reference(setup):
    _, calls, intents, enrollment, _ = deepcopy(setup)
    manifest = {
        "workload": f.typed.WORKLOAD,
        "prompts": f.typed.prompts(),
        **enrollment,
    }
    repeats_c, repeats_i = [], []
    for call, intent in zip(calls, intents, strict=True):
        if call["model"] == f.MODELS[0]:
            c, i = deepcopy(call), deepcopy(intent)
            c["id"] = i["id"] = c["id"] + "-repeat"
            c["arm"] = i["arm"] = "repeat"
            c["usage"]["prompt_tokens"] *= 2
            repeats_c.append(c)
            repeats_i.append(i)
    calls += repeats_c
    intents += repeats_i
    raw_intents = [
        {
            k: row[k]
            for k in ("phase", "arm", "task_id", "workflow_stage", "original_call")
        }
        for row in intents
    ]
    hashes = {
        name: f._file_digest(value)
        for name, value in (
            ("manifest.json", manifest),
            ("calls.json", calls),
            ("intents.json", raw_intents),
        )
    }
    return manifest, calls, raw_intents, hashes


def test_all_historical_repeats_are_retained_with_external_raw_pins(setup):
    manifest, calls, intents, hashes = reference(setup)
    normalized = f.normalize_reference(manifest, calls, intents, artifact_hashes=hashes)
    assert len(normalized["calls"]) == len(normalized["intents"]) == 54
    report = f.prepilot_forecast(
        setup[0],
        normalized["calls"],
        normalized["intents"],
        setup[3],
        setup[4],
        reference_manifest_sha256=normalized["reference_manifest_sha256"],
        reference_artifact_hashes=hashes,
    )
    assert report["evidence_kind"] == "historical_pre_pilot"
    for row in report["measurements"]:
        assert len(row["samples"]) == (12 if row["model"] == f.MODELS[0] else 6)
        if row["model"] == f.MODELS[0]:
            assert Decimal(row["max_effective_rate"]) >= Decimal("0.5")
    assert report["provenance"]["reference"]["artifact_hashes"] == hashes
    assert report["prospective_pilot_usd"] != report["observed_evidence_cost_usd"]


@pytest.mark.parametrize(
    "change", ["hash", "request", "order", "model", "prompt", "missing"]
)
def test_reference_normalization_rejects_forged_or_incomplete_pins(setup, change):
    manifest, calls, intents, hashes = reference(setup)
    if change == "hash":
        hashes["calls.json"] = "0" * 64
    else:
        if change == "request":
            calls[0]["request_sha256"] = "0" * 64
        elif change == "order":
            intents.reverse()
        elif change == "model":
            calls[0]["model"] = f.MODELS[1]
        elif change == "prompt":
            manifest["prompts"]["FILTER_SYSTEM"] = "changed"
        else:
            intents.pop()
        hashes = {
            name: f._file_digest(value)
            for name, value in (
                ("manifest.json", manifest),
                ("calls.json", calls),
                ("intents.json", intents),
            )
        }
    with pytest.raises(PilotError):
        f.normalize_reference(manifest, calls, intents, artifact_hashes=hashes)


def test_frozen_tier_pricing_is_used_and_inputs_are_unchanged(setup):
    before = deepcopy(setup)
    baseline = run(setup)
    changed = deepcopy(setup)
    for model in f.MODELS:
        changed[3]["endpoints"][model]["pricing"]["overrides"] = [
            {"min_prompt_tokens": 0, "prompt": "0.000006", "completion": "0.000030"}
        ]
    tiered = run(changed)
    assert Decimal(tiered["training_usd"]) > Decimal(baseline["training_usd"])
    assert setup == before


def test_prospective_cap_is_not_changed_by_historical_output_length(setup):
    manifest, calls, intents, hashes = reference(setup)
    for row in calls:
        row["usage"]["completion_tokens"] = 256
    hashes["calls.json"] = f._file_digest(calls)
    normalized = f.normalize_reference(manifest, calls, intents, artifact_hashes=hashes)
    report = f.prepilot_forecast(
        setup[0],
        normalized["calls"],
        normalized["intents"],
        setup[3],
        setup[4],
        reference_manifest_sha256=normalized["reference_manifest_sha256"],
        reference_artifact_hashes=hashes,
    )
    assert all(row["output_tokens"] == 256 for row in report["public_envelopes"])


def test_materializer_drift_rejected(setup, monkeypatch):
    actual_envelopes = f._envelopes

    def changed(*args):
        values = actual_envelopes(*args)
        setup[-1].write_bytes(b"different synthetic binary")
        return values

    monkeypatch.setattr(f, "_envelopes", changed)
    with pytest.raises(PilotError, match="materializer changed"):
        run(setup)


@pytest.mark.parametrize("bad", [None, "z" * 64, "0" * 63, True])
def test_pre_pilot_requires_real_sha256_pins_before_materializing(
    setup, monkeypatch, bad
):
    monkeypatch.setattr(
        f, "_envelopes", lambda *a: pytest.fail("used unpinned evidence")
    )
    with pytest.raises(PilotError, match="reference pins"):
        f.prepilot_forecast(
            *setup,
            reference_manifest_sha256=bad,
            reference_artifact_hashes={
                name: "0" * 64
                for name in ("manifest.json", "calls.json", "intents.json")
            },
        )


def test_real_frozen_materializer_no_build_no_network(setup, monkeypatch):
    path = os.environ.get("AGENTC_TYPED_MATERIALIZER")
    if not path:
        pytest.skip(
            "set AGENTC_TYPED_MATERIALIZER to existing immutable reviewed binary"
        )
    from bench.openrouter_typed_factorial import materialize

    # setup patches the shared module; recover the original from a fresh source load.
    original = f.load_module(
        "forecast_real_materializer", f.ROOT / "bench/openrouter_typed_factorial.py"
    ).materialize
    monkeypatch.setattr(f.materializer, "materialize", original)
    assert materialize is not original
    values = list(setup)
    values[-1] = Path(path)
    report = f.forecast(*values)
    assert report["provenance"]["materializer_sha256"] == f.file_hash(Path(path))
    assert any(
        v["materialization"] and v["materialization"]["applied"]
        for row in report["public_envelopes"]
        for v in row["history_variants"]
    )
