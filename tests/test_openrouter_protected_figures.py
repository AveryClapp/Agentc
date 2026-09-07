"""Synthetic summaries only; no acquisition files, providers or gold labels."""

from copy import deepcopy
import hashlib
import json

import pytest

from bench import openrouter_protected_analysis as analysis
from bench import openrouter_protected_figures as figures
from bench.openrouter_pilot import PilotError, digest


@pytest.fixture
def report():
    result = {
        "kind": analysis.KIND,
        "analysis_protocol": deepcopy(analysis.PROTOCOL),
        "analysis_protocol_sha256": digest(analysis.PROTOCOL),
        "manifest_sha256": analysis.MANIFEST_SHA256,
        "manifest_file_sha256": analysis.MANIFEST_FILE_SHA256,
        "schedule_complete": True,
        "labels_opened": True,
        "fixture_exposure": {"unseen_input_claim": False, "resampling": False},
        "paired_task_count": 63,
        "paired_tasks": [
            {
                "task_id": f"synthetic-{i}",
                "cells": {
                    "source": {"exact_accuracy": 1, "workflow_cost_usd": ".01"},
                    "HR": {"exact_accuracy": int(i >= 3), "workflow_cost_usd": ".001"},
                },
                "HR_minus_source_accuracy": -int(i < 3),
            }
            for i in range(63)
        ],
        "accuracy_totals": {
            "source": {"correct": 63, "tasks": 63},
            "HR": {"correct": 60, "tasks": 63},
        },
        "quality_transitions": {"HR_loss": 3, "tie": 60},
        "descriptive_paired_intervals": {
            "source_accuracy": {
                "mean": 1,
                "descriptive_95_percentile_interval": [1, 1],
            },
            "HR_accuracy": {
                "mean": 60 / 63,
                "descriptive_95_percentile_interval": [0.85, 1],
            },
            "HR_minus_source_accuracy": {
                "mean": -3 / 63,
                "descriptive_95_percentile_interval": [-0.15, 0],
            },
        },
        "costs": {
            "source_workflows_usd": ".63",
            "fixed_HR_workflows_usd": ".063",
            "native_probes_usd": ".1",
            "pilot_usd": ".02",
            "all_acquisition_usd": ".813",
            "source_learning_block_usd": ".73",
        },
        "native_learning": {
            "profiles": [],
            "current_guard_or_admission_assessed": False,
            "sample_floor_is_not_acceptance": True,
        },
        "call_counts": {"pilot": 36, "source": 189, "HR": 189, "probe": 130},
    }
    for key in (
        "adaptive_savings_claim",
        "native_primary_promotion",
        "efficacy_claim",
        "paper_evidence",
        "pilot_accuracy_reported",
    ):
        result[key] = False
    for i, (key, (stage, _, model)) in enumerate(figures.PLANS.items()):
        n = 20 if i < 5 else 16 if i == 5 else 14
        result["native_learning"]["profiles"].append(
            {
                "key": {"execution_plan_id": key},
                "workflow_stage": stage,
                "runtime_version": {"target_model_id": f"anthropic/claude-{model}-4.5"},
                "paired_observations": n,
                "disagreements": 0 if i < 5 else 2,
                "evidence_target": 20,
                "count_floor_met": n == 20,
                "pairs_short_of_floor": 20 - n,
            }
        )
    return result


def test_complete_data_keeps_full_cost_and_probe_scope(report):
    data = figures.build_data(report)
    assert data["costs"] == {
        "Source workflows": "0.63",
        "Native probes": "0.1",
        "Fixed HR workflows": "0.063",
        "Pilot": "0.02",
    }
    assert data["all_acquisition_usd"] == ".813"
    assert data["transitions"] == {"HR_loss": 3, "tie": 60}
    assert sum(p["pairs"] for p in data["profiles"]) == 130
    assert "not adaptive" in data["scope"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("schedule_complete", False),
        ("labels_opened", False),
        ("paper_evidence", True),
        ("efficacy_claim", True),
        ("native_primary_promotion", True),
        ("adaptive_savings_claim", True),
        ("paired_task_count", 62),
    ],
)
def test_scope_changes_reject(report, key, value):
    report[key] = value
    with pytest.raises(PilotError):
        figures.build_data(report)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["paired_tasks"].pop(),
        lambda r: r["paired_tasks"][0].update(task_id="synthetic-1"),
        lambda r: r["paired_tasks"][0]["cells"]["source"].update(exact_accuracy=True),
        lambda r: r["costs"].update(all_acquisition_usd=".713"),
        lambda r: r["costs"].update(source_workflows_usd=".1"),
        lambda r: r["costs"].update(source_learning_block_usd=".63"),
        lambda r: r["fixture_exposure"].update(unseen_input_claim=True),
        lambda r: r["analysis_protocol"].update(bootstrap_seed=12),
        lambda r: r["descriptive_paired_intervals"]["HR_accuracy"].update(
            mean=float("nan")
        ),
        lambda r: r["descriptive_paired_intervals"]["HR_accuracy"].update(
            descriptive_95_percentile_interval=[1, 0.9]
        ),
        lambda r: r["native_learning"].update(current_guard_or_admission_assessed=True),
        lambda r: r["native_learning"]["profiles"][0].update(disagreements=21),
        lambda r: r["native_learning"]["profiles"][0].update(workflow_stage="answer"),
        lambda r: r["native_learning"]["profiles"][0].update(count_floor_met=False),
        lambda r: r["native_learning"]["profiles"].pop(),
        lambda r: r["call_counts"].update(probe=129),
    ],
)
def test_mixed_or_incomplete_quantities_reject(report, mutation):
    mutation(report)
    with pytest.raises(PilotError):
        figures.build_data(report)


def test_render_outputs_are_scoped_and_never_overwrite(report, tmp_path):
    output = tmp_path / "figures"
    data = figures.build_data(report)
    figures.render(data, output, preview=True)
    assert len(list(output.glob("*.svg"))) == len(list(output.glob("*.png"))) == 3
    for path in output.glob("*.svg"):
        assert figures.FOOTER in path.read_text()
        assert figures.PREVIEW_LABEL in path.read_text()
    with pytest.raises(PilotError, match="already exists"):
        figures.render(data, output)


def test_difference_annotation_masks_reference_line(report, tmp_path, monkeypatch):
    from matplotlib.axes import Axes

    original = Axes.text
    annotations = []

    def text(self, x, y, value, *args, **kwargs):
        artist = original(self, x, y, value, *args, **kwargs)
        if " pp   [" in str(value):
            annotations.append(artist)
        return artist

    monkeypatch.setattr(Axes, "text", text)
    figures.render(figures.build_data(report), tmp_path / "figures", preview=True)
    assert len(annotations) == 1
    patch = annotations[0].get_bbox_patch()
    assert patch is not None and patch.get_facecolor() == (1, 1, 1, 1)


def test_cli_external_pin_checked_before_render(report, tmp_path, monkeypatch):
    path = tmp_path / "approved-analysis.json"
    raw = json.dumps(report).encode()
    path.write_bytes(raw)
    output = tmp_path / "figures"
    monkeypatch.setattr(
        "sys.argv",
        [
            "figures",
            "--analysis",
            str(path),
            "--analysis-sha256",
            "0" * 64,
            "--output",
            str(output),
            "--synthetic-preview",
        ],
    )
    with pytest.raises(PilotError, match="external analysis hash"):
        figures.main()
    assert not output.exists()
    monkeypatch.setattr(
        "sys.argv",
        [
            "figures",
            "--analysis",
            str(path),
            "--analysis-sha256",
            hashlib.sha256(raw).hexdigest(),
            "--output",
            str(output),
            "--synthetic-preview",
        ],
    )
    figures.main()
    saved = json.loads((output / "plot-data.json").read_text())
    assert saved["analysis_sha256"] == hashlib.sha256(raw).hexdigest()
    assert saved["transitions"] == {"HR_loss": 3, "tie": 60}
    assert saved["synthetic_preview"] is True
    provenance = json.loads((output / "rendering.json").read_text())
    assert len(provenance["figure_source_sha256"]) == 64
    assert provenance["synthetic_preview"] is True
    assert len(provenance["files"]) == 7
