"""Synthetic plot-contract checks; fixtures are not experimental observations."""

from copy import deepcopy

import pytest

from bench import openrouter_typed_factorial as study
from bench import openrouter_typed_factorial_figures as figures
from bench.openrouter_pilot import PilotError, digest


def inputs():
    manifest = {
        "kind": study.KIND,
        "schedule": [
            {"task_id": f"synthetic-test-{i}", "arm": a}
            for i in range(32)
            for a in study.ARMS
        ],
    }
    pairs = []
    for i in range(32):
        cells = {}
        for a, cost in zip(study.ARMS, ["0.01", "0.005", "0.002", "0.001"]):
            cells[a] = {
                "em": float(a != "H0" or i % 8 != 0),
                "cost_usd": cost,
                "nominal_uncached_cost_usd": cost,
                "valid_by_stage": {s: True for s in study.live.STAGES},
                "cached_input_tokens": 0,
                "cache_unknown_calls": 0,
            }
        pairs.append(
            {"task_id": f"synthetic-test-{i}", "cells": cells, **study.contrasts(cells)}
        )
    report = {
        "manifest_sha256": digest(manifest),
        "schedule_complete": True,
        "efficacy_claim": False,
        "native_admission_tested": False,
        "paper_evidence": False,
        "paired_tasks": pairs,
        "finance": {
            "snapshot_financially_complete": True,
            "failed_provider_results": [],
            "additional_attempt_records": [],
            "retained_uncertainty_usd": "0",
            "pending_upper_bound_usd": "0",
        },
        "materializations": {"ContextCompress": 64, "StateDrop": 64},
        "materialization_abstentions": {"no_rule": 256},
        "limitations": ["SYNTHETIC UNIT TEST ONLY; not provider measurements."],
    }
    return manifest, report


def test_derived_values_preserve_every_cell_and_task():
    manifest, report = inputs()
    before = deepcopy(report)
    data = figures.build_data(manifest, report)
    assert report == before
    assert [c["arm"] for c in data["cells"]] == list(study.ARMS)
    assert [c["exact"] for c in data["cells"]] == [32, 32, 28, 32]
    assert [c["mean_paid_usd"] for c in data["cells"]] == pytest.approx(
        [0.01, 0.005, 0.002, 0.001]
    )
    assert data["quality_interval"]["mean"] == pytest.approx(12.5)
    assert data["log_cost_interval"]["mean"] == pytest.approx(0)
    assert data["paired_tasks"] == report["paired_tasks"]
    assert data["analysis_sha256"] == digest(report)
    assert data["paper_evidence"] is False


@pytest.mark.parametrize(
    "change",
    [
        "partial",
        "native",
        "claim",
        "hash",
        "finance",
        "holds",
        "pending",
        "failed",
        "extra-attempt",
        "duplicate",
        "order",
        "cell",
        "interaction",
        "nonbinary",
    ],
)
def test_refuse_partial_relabelled_or_inconsistent_plots(change):
    manifest, report = inputs()
    if change == "partial":
        report["paired_tasks"].pop()
    elif change == "native":
        report["native_admission_tested"] = True
    elif change == "claim":
        report["efficacy_claim"] = True
    elif change == "hash":
        report["manifest_sha256"] = "other"
    elif change == "finance":
        report["finance"]["snapshot_financially_complete"] = False
    elif change == "holds":
        report["finance"]["retained_uncertainty_usd"] = "1"
    elif change == "pending":
        report["finance"]["pending_upper_bound_usd"] = "1"
    elif change == "failed":
        report["finance"]["failed_provider_results"] = ["failure"]
    elif change == "extra-attempt":
        report["finance"]["additional_attempt_records"] = ["failure"]
    elif change == "duplicate":
        report["paired_tasks"][1]["task_id"] = report["paired_tasks"][0]["task_id"]
    elif change == "order":
        report["paired_tasks"].reverse()
    elif change == "cell":
        report["paired_tasks"][0]["cells"]["invented"] = report["paired_tasks"][0][
            "cells"
        ]["S0"]
    elif change == "interaction":
        report["paired_tasks"][0]["quality_interaction"] = 10
    else:
        report["paired_tasks"][0]["cells"]["S0"]["em"] = 0.5
    with pytest.raises(PilotError):
        figures.build_data(manifest, report)


def test_zero_cost_rows_are_explicit_not_offset():
    manifest, report = inputs()
    report["paired_tasks"][0]["cells"]["H0"]["cost_usd"] = "0"
    report["paired_tasks"][0].update(
        study.contrasts(report["paired_tasks"][0]["cells"])
    )
    data = figures.build_data(manifest, report)
    assert data["log_cost_tasks"] == 31 and data["zero_cost_tasks"] == 1
    assert data["paired_tasks"][0]["log_cost_ratio_interaction"] is None


def test_render_all_dimensions_and_refuse_overwrite(tmp_path):
    pytest.importorskip("matplotlib")
    data = figures.build_data(*inputs())
    report = figures.render(data, tmp_path)
    assert report["formats"] == ["svg", "png"]
    assert len(list(tmp_path.glob("*.svg"))) == len(list(tmp_path.glob("*.png"))) == 3
    for path in tmp_path.glob("*.svg"):
        text = path.read_text()
        assert "<svg" in text and "NaN" not in text
    assert all(path.stat().st_size > 1000 for path in tmp_path.iterdir())
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(PilotError, match="overwrite"):
        figures.render(data, tmp_path)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_plot_cli_reconstructs_before_loading_cached_analysis(monkeypatch, tmp_path):
    # A cached file is not authority. Failure to reconstruct must precede rendering.
    manifest, _ = inputs()
    import json

    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(figures, "load_key", lambda path: "fake")

    def rejected(*args):
        raise PilotError("incomplete evidence")

    monkeypatch.setattr(study, "analyze", rejected)
    monkeypatch.setattr(
        figures, "render", lambda *a: pytest.fail("rendered unverified data")
    )
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "figures",
            "--env-file",
            "fake",
            "--ledger",
            "fake",
            "--output",
            str(tmp_path),
            "--pilot",
            "fake",
            "--bridge",
            "fake",
            "--figure-output",
            str(tmp_path / "figures"),
            "--manifest-sha256",
            "fake",
            "--pilot-manifest-sha256",
            "fake",
        ],
    )
    assert figures.main() == 1
    assert not (tmp_path / "figures").exists()


@pytest.mark.parametrize("zero_cost_tasks", [0, 1, 32])
def test_interaction_svg_discloses_degenerate_intervals_and_subset_n(
    tmp_path, zero_cost_tasks
):
    pytest.importorskip("matplotlib")
    manifest, report = inputs()
    for index, row in enumerate(report["paired_tasks"]):
        for cell in row["cells"].values():
            cell["em"] = 1.0
        if index < zero_cost_tasks:
            row["cells"]["H0"]["cost_usd"] = "0"
        row.update(study.contrasts(row["cells"]))
    data = figures.build_data(manifest, report)
    assert data["quality_interval"]["descriptive_95_percentile_interval"] == [0, 0]
    figures.render(data, tmp_path)
    text = (tmp_path / "02-paired-interactions.svg").read_text()
    assert "n = 32 tasks" in text
    assert f"n = {32 - zero_cost_tasks} tasks" in text
    assert f"zero-cost tasks excluded = {zero_cost_tasks}" in text
    assert "One observed realization per cell" in text
    assert "zero-width intervals do not imply certainty" in text
    assert "not an adaptive-policy or safety certificate" in text
