"""No-network matched-repeat protocol and real-Ledger integration checks."""

from copy import copy, deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import shutil

import pytest

from bench import openrouter_typed_repeat as repeat
from bench import openrouter_typed_factorial as study
from bench.openrouter_pilot import PilotError, digest
from test_openrouter_typed_factorial import experiment as experiment


def report():
    rows = []
    for i in range(study.COUNT):
        cells = {
            arm: {
                "em": 1.0,
                "cost_usd": "0.001",
                "nominal_uncached_cost_usd": "0.002",
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_input_tokens": 0,
                "cache_unknown_calls": 0,
                "observational_latency_ms": 100,
                "valid_by_stage": {s: True for s in study.live.STAGES},
            }
            for arm in study.ARMS
        }
        rows.append(
            {"task_id": f"synthetic-{i}", "cells": cells, **study.contrasts(cells)}
        )
    return {
        "paired_tasks": rows,
        "schedule_complete": True,
        "paper_evidence": False,
        "native_admission_tested": False,
        "efficacy_claim": False,
        "finance": {
            "snapshot_financially_complete": True,
            "failed_provider_results": [],
            "additional_attempt_records": [],
            "retained_uncertainty_usd": "0",
            "pending_upper_bound_usd": "0",
            "known_stage_usd": str(Decimal("0.004") * study.COUNT),
        },
    }


def test_task_clusters_transitions_costs_and_pilot_counted_once():
    first, second = report(), report()
    first["paired_tasks"][0]["cells"]["S0"]["em"] = 0.0
    first["paired_tasks"][0].update(study.contrasts(first["paired_tasks"][0]["cells"]))
    second["paired_tasks"][1]["cells"]["S0"]["em"] = 0.0
    second["paired_tasks"][1].update(
        study.contrasts(second["paired_tasks"][1]["cells"])
    )
    value = repeat.compare(first, second, "0.1")
    assert value["independent_tasks"] == 32
    assert len(value["paired_tasks"]) == 32
    cell = value["cells"][0]
    assert cell["primary_exact"] == cell["repeat_exact"] == 31
    assert cell["quality_transitions_00_01_10_11"] == {
        "00": 0,
        "01": 1,
        "10": 1,
        "11": 30,
    }
    assert cell["quality_disagreements"] == 2
    assert value["both_matrices_known_usd"] == "0.256"
    assert value["matrices_plus_feasibility_known_usd"] == "0.356"
    assert cell["primary_resources"]["nominal_uncached_cost_usd"] == "0.064"
    assert value["adaptive_policy_tested"] is False
    assert (
        value["descriptive_task_cluster_interactions"]["quality_interaction_change"][
            "mean"
        ]
        == 0
    )


@pytest.mark.parametrize(
    "change",
    [
        "partial",
        "finance",
        "hold",
        "failed",
        "duplicate",
        "order",
        "cell",
        "contrast",
        "native",
        "claim",
        "nonbinary",
    ],
)
def test_comparison_rejects_invalid_or_partial_evidence(change):
    first, second = report(), report()
    if change == "partial":
        second["paired_tasks"].pop()
    elif change == "finance":
        second["finance"]["snapshot_financially_complete"] = False
    elif change == "hold":
        second["finance"]["retained_uncertainty_usd"] = "1"
    elif change == "failed":
        second["finance"]["failed_provider_results"] = ["failed"]
    elif change == "duplicate":
        first["paired_tasks"][1]["task_id"] = first["paired_tasks"][0]["task_id"]
    elif change == "order":
        second["paired_tasks"].reverse()
    elif change == "cell":
        second["paired_tasks"][0]["cells"].pop("HR")
    elif change == "contrast":
        second["paired_tasks"][0]["quality_interaction"] = 0.5
    elif change == "native":
        second["native_admission_tested"] = True
    elif change == "claim":
        second["efficacy_claim"] = True
    else:
        second["paired_tasks"][0]["cells"]["HR"]["em"] = True
    with pytest.raises(PilotError):
        repeat.compare(first, second, "0.1")


@pytest.mark.parametrize(
    "field",
    ["response_formats", "endpoints", "source_files", "arms", "schedule", "created_at"],
)
def test_same_setup_requires_every_field_except_later_time(field):
    first = frozen_primary()
    second = deepcopy(first)
    second["created_at"] = (
        datetime.fromisoformat(first["created_at"]) + timedelta(seconds=1)
    ).isoformat()
    repeat.same_setup(first, second)
    second[field] = first[field] if field == "created_at" else "changed"
    with pytest.raises(PilotError):
        repeat.same_setup(first, second)


@pytest.fixture
def prepared_repeat(experiment, tmp_path, monkeypatch):
    args, manifest, ledger, sent = experiment
    # Extend the fake feasibility evidence before any primary call is made.
    evidence = {**manifest["pilot_evidence"], "known_cost_usd": "0.1"}
    monkeypatch.setattr(study, "pilot_evidence", lambda _: evidence)
    manifest["pilot_evidence"] = evidence
    (args.output / "manifest.json").write_text(json.dumps(manifest))
    args.manifest_sha256 = digest(manifest)
    study.run(args, "fake", manifest)
    args.primary = args.output
    args.primary_manifest_sha256 = args.manifest_sha256
    args.output = tmp_path / "repeat"
    result = repeat.prepare(args, "fake")
    args.manifest_sha256 = result["manifest_sha256"]
    args.repeat_protocol_sha256 = result["repeat_protocol_sha256"]
    return args, ledger, sent


def test_real_ledger_repeat_reconstructs_and_cannot_repeat_twice(
    prepared_repeat, tmp_path
):
    args, ledger, sent = prepared_repeat
    copied = copy(args)
    copied.output = tmp_path / "copied"
    shutil.copytree(args.output, copied.output)
    assert len(sent) == 12
    assert repeat.run(args, "fake") == {
        "completed_calls": 12,
        "schedule_complete": True,
    }
    assert len(sent) == 24
    manifest = json.loads((args.output / "manifest.json").read_text())
    study.analyze(args, "fake", manifest)
    second = repeat.read(args.output / "analysis.json")
    _, first = repeat.reconstruct_primary(args, "fake")
    value = repeat.compare(first, second, "0.1")
    assert value["independent_tasks"] == 1
    assert repeat.analyze(args, "fake")["complete_matrices"] == 2
    assert ledger.summary()["spent_usd"] == "0.024"
    before = ledger.path.read_bytes()
    with pytest.raises(PilotError, match="one repeat"):
        repeat.run(copied, "fake")
    assert ledger.path.read_bytes() == before and len(sent) == 24


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "primary-file",
        "protocol-hash",
        "manifest-hash",
        "primary-hash",
        "ledger",
        "bridge",
        "key",
    ],
)
def test_tampering_fails_before_dispatch(prepared_repeat, tmp_path, monkeypatch, field):
    args, ledger, sent = prepared_repeat
    key = "fake"
    if field == "source":
        original = repeat.file_hash
        monkeypatch.setattr(
            repeat,
            "file_hash",
            lambda path: (
                "other" if path.name == "openrouter_typed_repeat.py" else original(path)
            ),
        )
    elif field == "primary-file":
        (args.primary / "calls.json").write_text("[]")
    elif field == "protocol-hash":
        args.repeat_protocol_sha256 = "changed"
    elif field == "manifest-hash":
        args.manifest_sha256 = "changed"
    elif field == "primary-hash":
        args.primary_manifest_sha256 = "changed"
    elif field == "ledger":
        copied = tmp_path / "different-ledger.jsonl"
        shutil.copyfile(ledger.path, copied)
        args.ledger = copied
    elif field == "bridge":
        args.bridge.write_text("changed")
    else:
        key = "wrong"
    before = ledger.path.read_bytes()
    with pytest.raises(PilotError):
        repeat.run(args, key)
    assert ledger.path.read_bytes() == before and len(sent) == 12


def test_crash_after_repeat_claim_never_dispatches_again(prepared_repeat, monkeypatch):
    args, ledger, sent = prepared_repeat
    before = ledger.path.read_bytes()

    def crash(*_):
        raise PilotError("before first attempt")

    monkeypatch.setattr(study, "run", crash)
    with pytest.raises(PilotError, match="before first"):
        repeat.run(args, "fake")
    assert repeat.repeat_claim(args).exists()
    with pytest.raises(PilotError, match="one repeat"):
        repeat.run(args, "fake")
    assert len(sent) == 12 and ledger.path.read_bytes() == before


def test_partial_primary_rejected_before_new_manifest(
    experiment, monkeypatch, tmp_path
):
    args, manifest, ledger, sent = experiment
    args.primary = args.output
    args.primary_manifest_sha256 = digest(manifest)
    args.output = tmp_path / "repeat"
    monkeypatch.setattr(study, "analyze", lambda *_: None)
    partial = report()
    partial["schedule_complete"] = False
    (args.primary / "analysis.json").write_text(json.dumps(partial))
    with pytest.raises(PilotError, match="complete verified"):
        repeat.prepare(args, "fake")
    assert not args.output.exists() and not sent


def test_different_fresh_manifest_cannot_bypass_one_repeat(prepared_repeat, tmp_path):
    args, ledger, sent = prepared_repeat
    other = copy(args)
    other.output = tmp_path / "second-fresh-repeat"
    frozen = repeat.prepare(other, "fake")
    other.manifest_sha256 = frozen["manifest_sha256"]
    other.repeat_protocol_sha256 = frozen["repeat_protocol_sha256"]
    assert other.manifest_sha256 != args.manifest_sha256
    repeat.run(args, "fake")
    before = ledger.path.read_bytes()
    with pytest.raises(PilotError, match="one repeat"):
        repeat.run(other, "fake")
    assert ledger.path.read_bytes() == before and len(sent) == 24


def test_repeat_provider_failure_keeps_allowance_and_cannot_restart(
    prepared_repeat, monkeypatch
):
    args, ledger, sent = prepared_repeat
    from bench import openrouter_pilot as pilot

    original = pilot.request_json

    def network(path, key, payload=None):
        if payload is not None:
            raise PilotError("synthetic timeout")
        return original(path, key, payload)

    monkeypatch.setattr(pilot, "request_json", network)
    with pytest.raises(PilotError):
        repeat.run(args, "fake")
    state = ledger.summary()
    assert repeat.money(state["spent_usd"]) == repeat.money("0.012")
    assert repeat.money(state["conservative_committed_usd"]) > repeat.money("0.012")
    before = ledger.path.read_bytes()
    with pytest.raises(PilotError):
        repeat.run(args, "fake")
    assert ledger.path.read_bytes() == before and len(sent) == 12


def test_catalog_refresh_allows_only_declared_consistent_observation_times(
    prepared_repeat,
):
    args, _, _ = prepared_repeat
    primary = repeat.read(args.primary / "manifest.json")
    repeated = repeat.read(args.output / "manifest.json")
    repeat.same_setup(primary, repeated)
    repeated["catalog"]["targets"][0]["price"]["input_per_million_tokens_usd"] += 1
    with pytest.raises(PilotError, match="inconsistent"):
        repeat.same_setup(primary, repeated)
    repeated = repeat.read(args.output / "manifest.json")
    repeated["catalog"]["targets"][0]["provenance"]["observed_at_utc"] = (
        "2026-01-01T00:00:00Z"
    )
    with pytest.raises(PilotError, match="inconsistent"):
        repeat.same_setup(primary, repeated)


def frozen_primary():
    return repeat.read(
        study.ROOT / "bench/repro/openrouter-typed-factorial-2026-09-05/manifest.json"
    )


def refreshed_observation(primary):
    result = deepcopy(primary)
    for endpoint in result["endpoints"].values():
        for field in repeat.ROLLING_TELEMETRY:
            if isinstance(endpoint[field], dict):
                endpoint[field] = {"synthetic_rolling_observation": 123}
            else:
                endpoint[field] = 99.5
    result["catalog"] = study.feasibility.catalog_for(result, result["endpoints"])
    result["created_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=1)
    ).isoformat()
    return result


def test_real_snapshot_telemetry_refresh_preserves_raw_observations():
    primary = frozen_primary()
    repeated = refreshed_observation(primary)
    before = deepcopy((primary, repeated))
    repeat.same_setup(primary, repeated)
    assert (primary, repeated) == before
    versions = repeat.observation_versions(primary, repeated)
    assert versions["primary"] != versions["repeat"]
    assert versions["repeat"]["endpoint_snapshot_sha256"] == digest(
        repeated["endpoints"]
    )
    assert "not immutable-weight identities" in " ".join(repeat.LIMITATIONS)


@pytest.mark.parametrize(
    "field",
    [
        "pricing",
        "status",
        "model_id",
        "name",
        "tag",
        "supported_parameters",
        "supports_implicit_caching",
        "unknown_field",
        "context_length",
    ],
)
def test_stable_endpoint_changes_rejected_even_with_valid_derived_catalog(field):
    primary = frozen_primary()
    repeated = refreshed_observation(primary)
    endpoint = repeated["endpoints"][study.live.SOURCE_MODEL]
    if field == "pricing":
        endpoint[field]["prompt"] = "0.000004"
    elif field == "status":
        endpoint[field] = 1
    elif field == "supported_parameters":
        endpoint[field].append("invented")
    elif field == "supports_implicit_caching":
        endpoint[field] = True
    elif field == "context_length":
        endpoint[field] += 1
    else:
        endpoint[field] = "changed"
    repeated["catalog"] = study.feasibility.catalog_for(repeated, repeated["endpoints"])
    repeated["created_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(PilotError):
        repeat.same_setup(primary, repeated)


@pytest.mark.parametrize(
    "path",
    [
        ("catalog_version",),
        ("price_table_version",),
        ("targets", 0, "model_version"),
        ("targets", 0, "price", "table_version"),
        ("targets", 0, "provenance", "catalog_version"),
        ("targets", 0, "context_window_tokens"),
        ("targets", 0, "max_output_tokens"),
        ("targets", 0, "price", "source_url"),
        ("targets", 0, "provenance", "source_url"),
    ],
)
def test_forged_catalog_derivations_rejected(path):
    primary = frozen_primary()
    repeated = refreshed_observation(primary)
    value = repeated["catalog"]
    for part in path[:-1]:
        value = value[part]
    value[path[-1]] = "forged"
    with pytest.raises(PilotError, match="inconsistent"):
        repeat.same_setup(primary, repeated)


@pytest.mark.parametrize("field", ["capabilities", "adapter_protocol", "revision_kind"])
def test_template_derived_catalog_facts_stay_exact(field):
    primary = frozen_primary()
    repeated = refreshed_observation(primary)
    target = repeated["catalog"]["targets"][0]
    if field == "capabilities":
        target[field]["structured_outputs"] = False
    else:
        target[field] = "changed"
    # Constructor preserves these template fields; normalization must not erase them.
    repeated["catalog"] = study.feasibility.catalog_for(repeated, repeated["endpoints"])
    repeated["created_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(PilotError):
        repeat.same_setup(primary, repeated)


def test_unknown_endpoint_field_rejected_even_if_present_in_both_snapshots():
    primary = frozen_primary()
    primary["endpoints"][study.live.SOURCE_MODEL]["unknown_field"] = "unreviewed"
    primary["catalog"] = study.feasibility.catalog_for(primary, primary["endpoints"])
    primary["created_at"] = datetime.now(timezone.utc).isoformat()
    repeated = refreshed_observation(primary)
    with pytest.raises(PilotError, match="unknown"):
        repeat.same_setup(primary, repeated)


@pytest.mark.parametrize("field", ["status", "capability", "catalog_price"])
def test_boolean_numeric_substitution_is_not_exact_setup(field):
    primary = frozen_primary()
    repeated = refreshed_observation(primary)
    if field == "status":
        repeated["endpoints"][study.live.SOURCE_MODEL]["status"] = False
        repeated["catalog"] = study.feasibility.catalog_for(
            repeated, repeated["endpoints"]
        )
    elif field == "capability":
        repeated["catalog"]["targets"][0]["capabilities"]["streaming"] = 0
    else:
        target = next(
            t
            for t in repeated["catalog"]["targets"]
            if t["model_id"] == study.live.TARGET_MODEL
        )
        target["price"]["input_per_million_tokens_usd"] = True
    repeated["created_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(PilotError):
        repeat.same_setup(primary, repeated)
