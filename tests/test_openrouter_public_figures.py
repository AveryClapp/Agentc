"""Entirely synthetic display-contract tests; not experimental observations."""

import json
import sys
from copy import deepcopy
from decimal import Decimal

import pytest

from bench import openrouter_public_figures as figures
from bench.openrouter_pilot import PilotError, digest

study = figures.study


def inputs(*, all_unit=False, all_zero_cost=False, invalid=False):
    manifest = {
        "kind": study.KIND,
        "phase": "primary",
        "native_execution_allowed": False,
        "paper_evidence": False,
        "arms": deepcopy(study.ARMS),
        "scope_decision": deepcopy(study.SCOPE_DECISION),
        "source_files": {"synthetic/source.py": "1" * 64},
        "pilot_evidence": {"known_cost_usd": "0.1"},
        "schedule": [
            {"task_id": f"synthetic-{i:02d}", "arm": arm, "phase": "primary"}
            for i in range(32)
            for arm in figures.ARMS
        ],
    }
    rows = []
    for i in range(32):
        cells = {}
        for index, arm in enumerate(figures.ARMS):
            em = float(all_unit or (i + index) % 7 != 0)
            bad = invalid and i == 0 and index == 0
            cost = "0" if all_zero_cost else str(Decimal("0.001") * (4 - index))
            cells[arm] = {
                "em": 0.0 if bad else em,
                "f1": 0.0 if bad else max(em, 0.4),
                "valid_by_stage": {stage: not bad for stage in study.live.STAGES},
                "invalid_path": bad,
                "cost_usd": cost,
                "nominal_uncached_cost_usd": cost,
                "input_tokens": 1200,
                "output_tokens": 100,
                "cached_input_tokens": 0,
                "cache_unknown_calls": 3,
                "observational_latency_ms": 120.5,
            }
        rows.append(
            {
                "task_id": f"synthetic-{i:02d}",
                "cells": cells,
                "contrasts": study.contrasts(cells),
            }
        )
    vectors = []
    for row in rows:
        vector = dict(row["contrasts"])
        vector.update(
            {
                arm + "/" + key: float(cell[key])
                for arm, cell in row["cells"].items()
                for key in figures.METRICS
            }
        )
        vectors.append(vector)
    undefined = [
        key for key in vectors[0] if any(vector[key] is None for vector in vectors)
    ]
    intervals = {}
    for key in vectors[0]:
        if key not in undefined:
            values = [vector[key] for vector in vectors]
            # Synthetic intervals for validation only. No sampling of real data.
            intervals[key] = {
                "mean": sum(values) / 32,
                "descriptive_95_percentile_interval": [min(values), max(values)],
            }
    all_cells = [cell for row in rows for cell in row["cells"].values()]
    totals = {
        key: str(sum((Decimal(cell[key]) for cell in all_cells), Decimal(0)))
        if key in figures.MONEY
        else sum(cell[key] for cell in all_cells)
        for key in figures.RESOURCES
    }
    cost = totals["cost_usd"]
    report = {
        "kind": study.KIND,
        "manifest_sha256": digest(manifest),
        "artifact_hashes": {name: "2" * 64 for name in study.PHASE_FILES},
        "schedule_complete": True,
        "labels_read": True,
        "paper_evidence": False,
        "efficacy_claim": False,
        "adaptive_policy_tested": False,
        "native_admission_tested": False,
        "individual_cc_sd_identified": False,
        "scheduled_factorial_factors": ["model", "rules_bundle"],
        "operational_interpretation": study.SCOPE_DECISION["operational_intervention"],
        "paired_tasks": rows,
        "resources": totals,
        "descriptive_task_cluster_intervals": intervals,
        "undefined_log_contrasts": undefined,
        "application_invalid_calls": 3 if invalid else 0,
        "applied_transformations": ["StateDrop"],
        "materialization_coverage": {
            "none/False": 256,
            "ContextCompress/False": 64,
            "StateDrop/True": 64,
        },
        "materialization_abstentions": {"not_applicable": 64, "no_rule": 256},
        "pilot_inclusive_known_usd": str(Decimal(cost) + Decimal("0.1")),
        "finance": {
            "snapshot_financially_complete": True,
            **{
                key: []
                for key in (
                    "artifact_ids_missing_from_ledger",
                    "unincorporated_completed_ids",
                    "unresolved_stage_calls",
                    "failed_provider_results",
                    "additional_attempt_records",
                )
            },
            "retained_uncertainty_usd": "0",
            "pending_upper_bound_usd": "0",
            "known_stage_usd": cost,
            "conservative_stage_commitment_usd": cost,
            "campaign_known_usd": "20",
            "campaign_retained_uncertainty_usd": "6",
            "campaign_pending_upper_bound_usd": "0",
            "campaign_conservative_commitment_usd": "26",
        },
        "limitations": ["SYNTHETIC TEST ONLY; not provider evidence."],
    }
    return manifest, report


def test_data_preserves_all_cells_intervals_and_invalids_without_bootstrap(monkeypatch):
    manifest, report = inputs(invalid=True)
    before = deepcopy((manifest, report))
    monkeypatch.setattr(
        study.analysis, "paired_bootstrap", lambda *a, **k: pytest.fail("rebootstrap")
    )
    data = figures.build_data(manifest, report)
    assert (manifest, report) == before
    assert data["paired_tasks"] == report["paired_tasks"]
    assert data["canonical_intervals"] == report["descriptive_task_cluster_intervals"]
    assert [cell["arm"] for cell in data["cells"]] == list(figures.ARMS)
    assert data["cells"][0]["invalid_paths"] == 1
    assert data["finance"]["campaign_retained_uncertainty_usd"] == "6"
    assert data["paper_evidence"] is False
    assert "not the ledger" in data["trust_basis"]


def test_percentile_interval_need_not_contain_sample_mean():
    manifest, report = inputs()
    summary = report["descriptive_task_cluster_intervals"]["S0/em"]
    summary["descriptive_95_percentile_interval"] = [1.0, 1.0]
    assert summary["mean"] < 1
    data = figures.build_data(manifest, report)
    assert data["cells"][0]["metrics"]["em"] == summary


@pytest.mark.parametrize(
    "change",
    [
        "partial",
        "duplicate",
        "task_order",
        "missing_cell",
        "duplicate_schedule",
        "schedule_phase",
        "hash",
        "sources",
        "artifacts",
        "finance",
        "missing_ledger",
        "extra_result",
        "pending_id",
        "failed",
        "extra_attempt",
        "hold",
        "pending",
        "stage_cost",
        "campaign",
        "pilot_cost",
        "resource_cost",
        "resource_tokens",
        "resource_latency",
        "bool_em",
        "fraction_em",
        "f1_range",
        "unit_em_partial_f1",
        "nan",
        "infinity",
        "negative_cost",
        "bool_token",
        "cached",
        "cache_unknown",
        "invalid_flag",
        "invalid_credit",
        "invalid_repair",
        "invalid_total",
        "stage_keys",
        "stage_bool",
        "contrast",
        "interval_mean",
        "interval_range",
        "interval_reverse",
        "interval_nan",
        "interval_keys",
        "interval_missing",
        "undefined",
        "native",
        "adaptive",
        "claim",
        "scope",
        "cc_applied",
        "materialization_count",
        "abstentions",
    ],
)
def test_reject_changed_incomplete_or_malformed_evidence(change):
    manifest, report = inputs()
    cell = report["paired_tasks"][0]["cells"]["S0"]
    interval = report["descriptive_task_cluster_intervals"]["S0/em"]
    if change == "partial":
        report["paired_tasks"].pop()
    elif change == "duplicate":
        report["paired_tasks"][1]["task_id"] = "synthetic-00"
    elif change == "task_order":
        report["paired_tasks"].reverse()
    elif change == "missing_cell":
        del report["paired_tasks"][0]["cells"]["H0"]
    elif change == "duplicate_schedule":
        manifest["schedule"][1] = deepcopy(manifest["schedule"][0])
    elif change == "schedule_phase":
        manifest["schedule"][0]["phase"] = "pilot"
    elif change == "hash":
        report["manifest_sha256"] = "f" * 64
    elif change == "sources":
        manifest["source_files"] = {}
    elif change == "artifacts":
        report["artifact_hashes"].pop("calls.json")
    elif change == "finance":
        report["finance"]["snapshot_financially_complete"] = False
    elif change in {
        "missing_ledger",
        "extra_result",
        "pending_id",
        "failed",
        "extra_attempt",
    }:
        key = {
            "missing_ledger": "artifact_ids_missing_from_ledger",
            "extra_result": "unincorporated_completed_ids",
            "pending_id": "unresolved_stage_calls",
            "failed": "failed_provider_results",
            "extra_attempt": "additional_attempt_records",
        }[change]
        report["finance"][key] = ["unmatched"]
    elif change in {"hold", "pending", "stage_cost", "campaign"}:
        key = {
            "hold": "retained_uncertainty_usd",
            "pending": "pending_upper_bound_usd",
            "stage_cost": "known_stage_usd",
            "campaign": "campaign_conservative_commitment_usd",
        }[change]
        report["finance"][key] = "1"
    elif change == "pilot_cost":
        report["pilot_inclusive_known_usd"] = "1"
    elif change.startswith("resource_"):
        key = {
            "resource_cost": "cost_usd",
            "resource_tokens": "input_tokens",
            "resource_latency": "observational_latency_ms",
        }[change]
        report["resources"][key] = 999
    elif change == "bool_em":
        cell["em"] = True
    elif change == "fraction_em":
        cell["em"] = 0.5
    elif change == "f1_range":
        cell["f1"] = 1.1
    elif change == "unit_em_partial_f1":
        cell.update(em=1, f1=0.5)
    elif change == "nan":
        cell["f1"] = float("nan")
    elif change == "infinity":
        cell["observational_latency_ms"] = float("inf")
    elif change == "negative_cost":
        cell["cost_usd"] = "-0.1"
    elif change == "bool_token":
        cell["input_tokens"] = True
    elif change == "cached":
        cell["cached_input_tokens"] = 10000
    elif change == "cache_unknown":
        cell["cache_unknown_calls"] = 4
    elif change == "invalid_flag":
        cell["invalid_path"] = True
    elif change == "invalid_credit":
        cell.update(
            invalid_path=True,
            valid_by_stage={s: False for s in study.live.STAGES},
            em=1,
            f1=1,
        )
    elif change == "invalid_repair":
        cell["valid_by_stage"]["filter"] = False
    elif change == "invalid_total":
        report["application_invalid_calls"] = 1
    elif change == "stage_keys":
        cell["valid_by_stage"]["extra"] = True
    elif change == "stage_bool":
        cell["valid_by_stage"]["filter"] = 1
    elif change == "contrast":
        report["paired_tasks"][0]["contrasts"]["model/em"] = 99
    elif change == "interval_mean":
        interval["mean"] = 0
    elif change == "interval_range":
        interval["descriptive_95_percentile_interval"] = [-1, 2]
    elif change == "interval_reverse":
        interval["descriptive_95_percentile_interval"] = [1, 0]
    elif change == "interval_nan":
        interval["descriptive_95_percentile_interval"][0] = float("nan")
    elif change == "interval_keys":
        interval["invented"] = True
    elif change == "interval_missing":
        report["descriptive_task_cluster_intervals"].pop("S0/em")
    elif change == "undefined":
        report["undefined_log_contrasts"] = ["model/em"]
    elif change == "native":
        report["native_admission_tested"] = True
    elif change == "adaptive":
        report["adaptive_policy_tested"] = True
    elif change == "claim":
        report["efficacy_claim"] = True
    elif change == "scope":
        manifest["scope_decision"]["individual_cc_identified"] = True
    elif change == "cc_applied":
        report["applied_transformations"].append("ContextCompress")
    elif change == "materialization_count":
        report["materialization_coverage"]["ContextCompress/False"] = 63
    elif change == "abstentions":
        report["materialization_abstentions"]["no_rule"] = 0
    if change != "hash":
        report["manifest_sha256"] = digest(manifest)
    with pytest.raises(PilotError):
        figures.build_data(manifest, report)


def frozen_synthetic(tmp_path, monkeypatch, **kwargs):
    manifest, report = inputs(**kwargs)
    for name in study.PHASE_FILES:
        value = (
            manifest if name == "manifest.json" else {"synthetic_opaque_receipt": name}
        )
        (tmp_path / name).write_text(json.dumps(value))
        report["artifact_hashes"][name] = figures.file_hash(tmp_path / name)
    (tmp_path / "analysis.json").write_text(json.dumps(report))
    monkeypatch.setattr(study, "sources", lambda: deepcopy(manifest["source_files"]))
    return {
        "analysis_sha256": figures.file_hash(tmp_path / "analysis.json"),
        "manifest_sha256": digest(manifest),
    }


def test_load_pins_raw_report_sources_and_all_artifacts_without_labels(
    tmp_path, monkeypatch
):
    hashes = frozen_synthetic(tmp_path, monkeypatch)

    def forbidden(*a, **k):
        pytest.fail("renderer called audit/ledger/labels/bootstrap")

    for name in ("audit", "analyze", "load_bundle", "load_key", "endpoint_snapshot"):
        monkeypatch.setattr(study, name, forbidden)
    monkeypatch.setattr(study.analysis, "paired_bootstrap", forbidden)
    data = figures.load_data(tmp_path, **hashes)
    assert data["analysis_raw_sha256"] == hashes["analysis_sha256"]
    assert not (tmp_path / "labels.json").exists()


@pytest.mark.parametrize("target", ["analysis.json", *study.PHASE_FILES, "source"])
def test_file_or_source_tamper_blocks_render(tmp_path, monkeypatch, target):
    hashes = frozen_synthetic(tmp_path, monkeypatch)
    if target == "source":
        monkeypatch.setattr(study, "sources", lambda: {"other.py": "1" * 64})
    else:
        path = tmp_path / target
        path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(PilotError):
        figures.load_data(tmp_path, **hashes)


@pytest.mark.parametrize(
    "all_unit,all_zero_cost,invalid",
    [(False, False, True), (True, False, False), (True, True, False)],
)
def test_render_two_figures_all_units_zero_cost_and_invalid(
    tmp_path, monkeypatch, all_unit, all_zero_cost, invalid
):
    pytest.importorskip("matplotlib")
    hashes = frozen_synthetic(
        tmp_path,
        monkeypatch,
        all_unit=all_unit,
        all_zero_cost=all_zero_cost,
        invalid=invalid,
    )
    data = figures.load_data(tmp_path, **hashes)
    output = tmp_path / "figures"
    provenance = figures.render(data, output)
    assert provenance["bootstrap_recomputed"] is False
    assert provenance["plot_data_raw_sha256"] == figures.file_hash(
        output / "plot_data.json"
    )
    assert len(provenance["files"]) == 4
    first = (output / "01-cost-quality.svg").read_text()
    second = (output / "02-all-paired-tasks.svg").read_text()
    assert "zero width does not imply certainty" in first
    assert "ContextCompress applied 0/64" in first
    assert "not adaptive-policy or native-admission proof" in first
    assert "All 32 tasks in frozen order" in second and "T32" in second
    assert "X = invalid application path" in second
    assert "NaN" not in first + second
    if all_zero_cost:
        assert "S0/SR/H0/HR coincide" in first
    if invalid:
        assert "<!-- X -->" in second
    for name, expected in provenance["files"].items():
        assert figures.file_hash(output / name) == expected
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(PilotError, match="overwrite"):
        figures.render(data, output)
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


def test_cli_hash_failure_precedes_any_output(tmp_path, monkeypatch):
    hashes = frozen_synthetic(tmp_path, monkeypatch)
    output = tmp_path / "figures"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "figures",
            "--study-output",
            str(tmp_path),
            "--analysis-sha256",
            "f" * 64,
            "--manifest-sha256",
            hashes["manifest_sha256"],
            "--figure-output",
            str(output),
        ],
    )
    monkeypatch.setattr(
        figures, "render", lambda *a: pytest.fail("rendered untrusted report")
    )
    assert figures.main() == 1
    assert not output.exists()


def test_render_refuses_changed_renderer_before_output(tmp_path, monkeypatch):
    hashes = frozen_synthetic(tmp_path, monkeypatch)
    data = figures.load_data(tmp_path, **hashes)
    data["renderer_source_sha256"] = "f" * 64
    with pytest.raises(PilotError, match="renderer version"):
        figures.render(data, tmp_path / "figures")
    assert not (tmp_path / "figures").exists()


def test_same_report_renders_identical_bytes(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    hashes = frozen_synthetic(tmp_path, monkeypatch)
    data = figures.load_data(tmp_path, **hashes)
    figures.render(data, tmp_path / "first")
    figures.render(data, tmp_path / "second")
    assert {p.name: p.read_bytes() for p in (tmp_path / "first").iterdir()} == {
        p.name: p.read_bytes() for p in (tmp_path / "second").iterdir()
    }


def test_cli_success_uses_the_same_hash_gate(tmp_path, monkeypatch):
    hashes = frozen_synthetic(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "figures",
            "--study-output",
            str(tmp_path),
            "--analysis-sha256",
            hashes["analysis_sha256"],
            "--manifest-sha256",
            hashes["manifest_sha256"],
            "--figure-output",
            str(tmp_path / "figures"),
        ],
    )
    seen = []
    monkeypatch.setattr(figures, "render", lambda data, path: seen.append(data))
    assert figures.main() == 0
    assert seen[0]["analysis_raw_sha256"] == hashes["analysis_sha256"]


def test_read_rejects_duplicate_keys_with_matching_external_hash(tmp_path, monkeypatch):
    hashes = frozen_synthetic(tmp_path, monkeypatch)
    path = tmp_path / "analysis.json"
    path.write_text('{"duplicate": 1, "duplicate": 2}')
    hashes["analysis_sha256"] = figures.file_hash(path)
    with pytest.raises(PilotError, match="duplicate"):
        figures.load_data(tmp_path, **hashes)


def test_every_number_and_label_is_synthetic():
    manifest, report = inputs()
    assert all(
        item["task_id"].startswith("synthetic-") for item in manifest["schedule"]
    )
    assert "SYNTHETIC" in report["limitations"][0]
    assert not hasattr(figures, "load_key")
    assert not hasattr(figures, "Ledger")
