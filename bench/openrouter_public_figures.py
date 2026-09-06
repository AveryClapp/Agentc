"""Two offline figures from an externally audited, byte-hash-pinned public report.

This renderer checks report consistency and artifact integrity, not the ledger.
The supplied analysis SHA must come from an independent completed audit. No key,
ledger, labels, provider requests, or bootstrap computation are used here.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from bench import openrouter_public_factorial as study
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_pilot import PilotError, canonical, digest, money

VERSION = "public-model-sd-figures-v1"
ARMS = study.FOUR_ARMS
LABELS = {
    "S0": "Sonnet · original",
    "SR": "Sonnet · SD",
    "H0": "Haiku · original",
    "HR": "Haiku · SD",
}
MONEY = ("cost_usd", "nominal_uncached_cost_usd")
COUNTS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_unknown_calls")
RESOURCES = (*MONEY, *COUNTS, "observational_latency_ms")
METRICS = ("em", "f1", *RESOURCES)
SCOPE = "Fixed model × StateDrop · native ContextCompress applied 0/64 times"
CAUTION = "32 public training tasks, one realization per cell · not adaptive-policy or native-admission proof"
INTERVAL_NOTE = "Canonical 95% task-cluster descriptive intervals; zero width does not imply certainty."


def _require(condition, message):
    if not condition:
        raise PilotError(message)


def _number(value):
    _require(
        type(value) in (int, float) and math.isfinite(value),
        "nonfinite or nonnumeric report value",
    )
    return value


def _count(value):
    _require(type(value) is int and value >= 0, "invalid report count")
    return value


def _hash(value):
    _require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
        "invalid artifact hash",
    )
    return value


def _close(actual, expected):
    return math.isclose(_number(actual), expected, rel_tol=1e-12, abs_tol=1e-12)


def _totals(cells):
    return {
        key: str(sum((money(cell[key]) for cell in cells), Decimal(0)))
        if key in MONEY
        else sum(cell[key] for cell in cells)
        for key in RESOURCES
    }


def _same_resources(actual, expected):
    _require(set(actual) == set(RESOURCES), "resource field coverage changed")
    for key in RESOURCES:
        if key in MONEY:
            _require(
                money(actual[key]) == money(expected[key]),
                "resource money total changed",
            )
        elif key in COUNTS:
            _require(
                _count(actual[key]) == expected[key], "resource token total changed"
            )
        else:
            _require(
                _close(actual[key], expected[key]), "resource latency total changed"
            )


def build_data(manifest, report):
    """Pure consistency check; file/hash authority is supplied by load_data."""
    try:
        return _build_data(manifest, report)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise PilotError("malformed public figure evidence") from exc


def _build_data(manifest, report):
    _require(
        manifest["kind"] == report["kind"] == study.KIND
        and manifest["phase"] == "primary"
        and report["manifest_sha256"] == digest(manifest)
        and manifest["native_execution_allowed"] is False
        and manifest["paper_evidence"] is False
        and report["schedule_complete"] is True
        and report["labels_read"] is True
        and all(
            report[key] is False
            for key in (
                "paper_evidence",
                "efficacy_claim",
                "adaptive_policy_tested",
                "native_admission_tested",
                "individual_cc_sd_identified",
            )
        )
        and canonical(manifest["arms"]) == canonical(study.ARMS)
        and canonical(manifest["scope_decision"]) == canonical(study.SCOPE_DECISION)
        and report["scheduled_factorial_factors"] == ["model", "rules_bundle"]
        and report["operational_interpretation"]
        == study.SCOPE_DECISION["operational_intervention"],
        "figures require complete fixed public model-by-SD evidence",
    )
    _require(
        set(report["artifact_hashes"]) == set(study.PHASE_FILES),
        "phase artifact coverage changed",
    )
    for value in report["artifact_hashes"].values():
        _hash(value)
    _require(bool(manifest["source_files"]), "missing frozen sources")
    for value in manifest["source_files"].values():
        _hash(value)
    rows = report["paired_tasks"]
    schedule = manifest["schedule"]
    ids = list(dict.fromkeys(item["task_id"] for item in schedule))
    _require(
        len(ids) == len(rows) == study.PRIMARY_COUNT
        and len(schedule) == study.PRIMARY_COUNT * len(ARMS)
        and [row["task_id"] for row in rows] == ids
        and all(isinstance(task_id, str) and task_id for task_id in ids)
        and {(item["task_id"], item["arm"]) for item in schedule}
        == {(task_id, arm) for task_id in ids for arm in ARMS}
        and all(item["phase"] == "primary" for item in schedule),
        "missing, duplicated, reordered, or nonprimary task cells",
    )
    vectors, all_cells, invalid_calls = [], [], 0
    for row in rows:
        _require(set(row["cells"]) == set(ARMS), "four-cell coverage changed")
        for cell in row["cells"].values():
            _require(
                _number(cell["em"]) in (0, 1) and 0 <= _number(cell["f1"]) <= 1,
                "invalid quality metric",
            )
            valid = cell["valid_by_stage"]
            _require(
                set(valid) == set(study.live.STAGES)
                and all(type(value) is bool for value in valid.values()),
                "invalid stage status",
            )
            statuses = [valid[stage] for stage in study.live.STAGES]
            _require(
                statuses == sorted(statuses, reverse=True),
                "invalid upstream path was repaired",
            )
            _require(
                cell["invalid_path"] is (not all(statuses)),
                "invalid-path flag differs from stages",
            )
            _require(
                not cell["invalid_path"] or cell["em"] == cell["f1"] == 0,
                "invalid path credited as correct",
            )
            _require(
                cell["em"] != 1 or cell["f1"] == 1, "exact match must have unit F1"
            )
            invalid_calls += sum(not value for value in statuses)
            for key in MONEY:
                _require(
                    isinstance(cell[key], str),
                    "cost must retain decimal representation",
                )
                money(cell[key])
            for key in COUNTS:
                _count(cell[key])
            _require(
                cell["cached_input_tokens"] <= cell["input_tokens"]
                and cell["cache_unknown_calls"] <= 3
                and _number(cell["observational_latency_ms"]) >= 0,
                "invalid resource accounting",
            )
            all_cells.append(cell)
        _require(
            canonical(row["contrasts"]) == canonical(study.contrasts(row["cells"])),
            "paired contrast changed",
        )
        vector = dict(row["contrasts"])
        vector.update(
            {
                arm + "/" + key: float(cell[key])
                for arm, cell in row["cells"].items()
                for key in METRICS
            }
        )
        vectors.append(vector)
    _require(
        _count(report["application_invalid_calls"]) == invalid_calls,
        "invalid-call total changed",
    )
    totals = _totals(all_cells)
    _same_resources(report["resources"], totals)
    finance = report["finance"]
    _require(
        finance["snapshot_financially_complete"] is True,
        "incomplete financial snapshot",
    )
    for key in (
        "artifact_ids_missing_from_ledger",
        "unincorporated_completed_ids",
        "unresolved_stage_calls",
        "failed_provider_results",
        "additional_attempt_records",
    ):
        _require(
            finance[key] == [], "financial snapshot has unmatched or failed attempts"
        )
    _require(
        money(finance["retained_uncertainty_usd"])
        == money(finance["pending_upper_bound_usd"])
        == 0,
        "stage has unresolved financial exposure",
    )
    _require(
        money(finance["known_stage_usd"])
        == money(finance["conservative_stage_commitment_usd"])
        == money(totals["cost_usd"]),
        "stage charge differs from cell totals",
    )
    campaign = sum(
        (
            money(finance[key])
            for key in (
                "campaign_known_usd",
                "campaign_retained_uncertainty_usd",
                "campaign_pending_upper_bound_usd",
            )
        ),
        Decimal(0),
    )
    _require(
        campaign == money(finance["campaign_conservative_commitment_usd"])
        and money(finance["campaign_known_usd"]) >= money(totals["cost_usd"]),
        "campaign financial totals changed",
    )
    pilot = money(manifest["pilot_evidence"]["known_cost_usd"])
    _require(
        money(report["pilot_inclusive_known_usd"]) == pilot + money(totals["cost_usd"]),
        "pilot-inclusive charge changed",
    )
    coverage = report["materialization_coverage"]
    _require(
        set(coverage)
        <= {"none/False", "ContextCompress/False", "StateDrop/True", "StateDrop/False"},
        "unexpected applied transformation",
    )
    _require(
        all(_count(value) >= 0 for value in coverage.values())
        and coverage.get("none/False") == 256
        and coverage.get("ContextCompress/False") == 64
        and coverage.get("StateDrop/True", 0) + coverage.get("StateDrop/False", 0)
        == 64,
        "zero-CC materialization coverage changed",
    )
    _require(
        report["applied_transformations"]
        == (["StateDrop"] if coverage.get("StateDrop/True", 0) else []),
        "applied transformation list changed",
    )
    abstentions = report["materialization_abstentions"]
    _require(
        all(_count(value) >= 0 for value in abstentions.values())
        and sum(abstentions.values()) == 384 - coverage.get("StateDrop/True", 0),
        "abstention count changed",
    )
    intervals = report["descriptive_task_cluster_intervals"]
    undefined = [
        key for key in vectors[0] if any(vector[key] is None for vector in vectors)
    ]
    _require(
        report["undefined_log_contrasts"] == undefined
        and set(intervals) == set(vectors[0]) - set(undefined),
        "canonical interval coverage changed",
    )
    for key, interval in intervals.items():
        values = [vector[key] for vector in vectors]
        _require(
            set(interval) == {"mean", "descriptive_95_percentile_interval"},
            "interval fields changed",
        )
        bounds = interval["descriptive_95_percentile_interval"]
        _require(
            len(bounds) == 2
            and _number(bounds[0]) <= _number(bounds[1])
            and min(values) - 1e-12 <= bounds[0] <= bounds[1] <= max(values) + 1e-12
            and _close(interval["mean"], sum(values) / len(values)),
            "canonical interval is inconsistent with task values",
        )
    cells = [
        {
            "arm": arm,
            "label": LABELS[arm],
            "tasks": 32,
            "invalid_paths": sum(row["cells"][arm]["invalid_path"] for row in rows),
            "totals": _totals([row["cells"][arm] for row in rows]),
            "metrics": {key: deepcopy(intervals[arm + "/" + key]) for key in METRICS},
        }
        for arm in ARMS
    ]
    return {
        "kind": VERSION,
        "manifest_sha256": digest(manifest),
        "analysis_canonical_sha256": digest(report),
        "renderer_source_sha256": file_hash(Path(__file__)),
        "source_files": deepcopy(manifest["source_files"]),
        "artifact_hashes": deepcopy(report["artifact_hashes"]),
        "scope": SCOPE,
        "adaptive_policy_tested": False,
        "native_admission_tested": False,
        "paper_evidence": False,
        "cells": cells,
        "paired_tasks": deepcopy(rows),
        "canonical_intervals": deepcopy(intervals),
        "undefined_log_contrasts": undefined,
        "finance": deepcopy(finance),
        "pilot_inclusive_known_usd": report["pilot_inclusive_known_usd"],
        "materialization_coverage": deepcopy(coverage),
        "limitations": deepcopy(report["limitations"]),
        "trust_basis": "External independently audited report hash; renderer checks integrity, not the ledger.",
    }


def load_data(directory, *, analysis_sha256, manifest_sha256):
    """Read pinned report/manifest and hash receipts as opaque bytes, never labels."""
    directory = Path(directory)
    _require(
        file_hash(directory / "analysis.json") == _hash(analysis_sha256),
        "analysis differs from external audit hash",
    )
    report = study.read(directory / "analysis.json")
    manifest = study.read(directory / "manifest.json")
    _require(
        digest(manifest) == _hash(manifest_sha256),
        "manifest differs from external audit hash",
    )
    data = build_data(manifest, report)
    _require(
        canonical(manifest["source_files"]) == canonical(study.sources()),
        "frozen acquisition source changed",
    )
    for name in study.PHASE_FILES:
        _require(
            file_hash(directory / name) == report["artifact_hashes"][name],
            "raw phase artifact changed",
        )
    data["analysis_raw_sha256"] = analysis_sha256
    return data


def render(data, directory):
    """Render validated display data to a new directory; never overwrite evidence."""
    _hash(data["analysis_raw_sha256"])
    _require(
        data["kind"] == VERSION
        and data["renderer_source_sha256"] == file_hash(Path(__file__)),
        "display data comes from a different renderer version",
    )
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    directory = Path(directory)
    _require(not directory.exists(), "refusing to overwrite figure directory")
    directory.mkdir(parents=True)
    write_json(directory / "plot_data.json", data, immutable=True)
    settings = {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.edgecolor": "#839094",
        "axes.labelcolor": "#26383c",
        "text.color": "#26383c",
        "xtick.color": "#52676b",
        "ytick.color": "#52676b",
        "svg.hashsalt": VERSION,
        "savefig.facecolor": "white",
    }
    files = {}

    def save(fig, stem):
        for suffix in ("svg", "png"):
            path = directory / (stem + "." + suffix)
            fig.savefig(
                path,
                dpi=220,
                bbox_inches="tight",
                metadata={"Date": None} if suffix == "svg" else None,
            )
            files[path.name] = file_hash(path)
        plt.close(fig)

    with plt.rc_context(settings):
        max_cost = max(
            max(
                cell["metrics"]["cost_usd"]["mean"],
                cell["metrics"]["cost_usd"]["descriptive_95_percentile_interval"][1],
            )
            for cell in data["cells"]
        )
        fig, axes = plt.subplots(
            1, 2, figsize=(11, 5.0), sharex=True, sharey=True, layout="constrained"
        )
        for axis, metric, title in zip(axes, ("em", "f1"), ("Exact match", "Token F1")):
            coincident = defaultdict(list)
            for index, cell in enumerate(data["cells"]):
                cost, quality = (cell["metrics"][key] for key in ("cost_usd", metric))
                x, y = cost["mean"], 100 * quality["mean"]
                coincident[x, y].append(cell["arm"])
                xlo, xhi = cost["descriptive_95_percentile_interval"]
                ylo, yhi = [
                    100 * value
                    for value in quality["descriptive_95_percentile_interval"]
                ]
                color = "#5378a8" if index < 2 else "#1c6b70"
                marker = "o" if index % 2 == 0 else "s"
                # Draw interval endpoints directly; percentile intervals need not
                # contain the sample mean. Never clip them to the point estimate.
                axis.plot([xlo, xhi], [y, y], color=color, lw=1.2, alpha=0.8)
                axis.plot([x, x], [ylo, yhi], color=color, lw=1.2, alpha=0.8)
                axis.plot(
                    x,
                    y,
                    marker=marker,
                    color=color,
                    markerfacecolor="white" if index % 2 == 0 else color,
                    markersize=8,
                    label=cell["label"],
                )
            for (x, y), arms in coincident.items():
                if len(arms) > 1:
                    align_right = x > max_cost / 2
                    axis.annotate(
                        "/".join(arms) + " coincide",
                        (x, y),
                        xytext=(-8 if align_right else 8, -15 if y > 50 else 15),
                        textcoords="offset points",
                        ha="right" if align_right else "left",
                        fontsize=8,
                    )
            axis.set_title(title, loc="left", fontsize=12)
            axis.set_xlabel("Mean paid cost per three-call workflow (USD)")
            axis.set_ylabel(title + " (%)")
            axis.set_ylim(-3, 103)
            axis.set_yticks([0, 25, 50, 75, 100])
            axis.grid(color="#e3e8e9", lw=0.6)
            axis.set_axisbelow(True)
        axes[0].set_xlim(-0.03 * (max_cost or 0.001), (max_cost or 0.001) * 1.1)
        axes[0].legend(loc="lower right", fontsize=9, frameon=True)
        fig.suptitle(SCOPE, fontsize=13)
        fig.supxlabel(
            CAUTION
            + "\n"
            + INTERVAL_NOTE
            + "\nAll three calls charged; per-cell costs exclude the separately charged feasibility pilot.",
            fontsize=8,
        )
        save(fig, "01-cost-quality")

        rows = data["paired_tasks"]
        fig, axes = plt.subplots(
            1, 2, figsize=(8.4, 10.8), sharey=True, layout="constrained"
        )
        for axis, metric, title in zip(
            axes, ("em", "f1"), ("Exact match · 0 or 1", "Token F1 · 0 to 1")
        ):
            values = np.array(
                [[row["cells"][arm][metric] for arm in ARMS] for row in rows]
            )
            axis.imshow(
                values,
                aspect="auto",
                interpolation="nearest",
                cmap="Blues",
                vmin=0,
                vmax=1,
            )
            for i, row in enumerate(rows):
                for j, arm in enumerate(ARMS):
                    cell = row["cells"][arm]
                    value = cell[metric]
                    label = (
                        "X"
                        if cell["invalid_path"]
                        else (str(int(value)) if metric == "em" else f"{value:.2f}")
                    )
                    axis.text(
                        j,
                        i,
                        label,
                        ha="center",
                        va="center",
                        fontsize=8,
                        color="white" if value > 0.65 else "#26383c",
                    )
            axis.set_xticks(range(4), ARMS)
            axis.set_yticks(range(32), [f"T{i + 1:02d}" for i in range(32)], fontsize=8)
            axis.tick_params(axis="both", length=0)
            axis.set_title(title, loc="left", fontsize=11)
        fig.suptitle(
            "All 32 tasks in frozen order · no outcome-based sorting", fontsize=13
        )
        fig.supxlabel(
            "X = invalid application path, scored 0; valid wrong answers remain 0.\nT01–T32 IDs and exact unrounded values are retained in plot_data.json.\nS = Sonnet; H = Haiku; 0 = original; R = SD (CC applied 0/64).\n"
            + CAUTION,
            fontsize=8,
        )
        save(fig, "02-all-paired-tasks")
    provenance = {
        "kind": VERSION,
        "renderer_source_sha256": file_hash(Path(__file__)),
        "analysis_raw_sha256": data["analysis_raw_sha256"],
        "manifest_sha256": data["manifest_sha256"],
        "artifact_hashes": data["artifact_hashes"],
        "source_files": data["source_files"],
        "plot_data_raw_sha256": file_hash(directory / "plot_data.json"),
        "matplotlib_version": matplotlib.__version__,
        "numpy_version": np.__version__,
        "formats": ["svg", "png"],
        "files": files,
        "trust_basis": data["trust_basis"],
        "bootstrap_recomputed": False,
    }
    write_json(directory / "rendering.json", provenance, immutable=True)
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-output", type=Path, required=True)
    parser.add_argument("--analysis-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--figure-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = load_data(
            args.study_output,
            analysis_sha256=args.analysis_sha256,
            manifest_sha256=args.manifest_sha256,
        )
        render(data, args.figure_output)
        print(
            json.dumps(
                {
                    "figures": 2,
                    "paper_evidence": False,
                    "output": str(args.figure_output),
                }
            )
        )
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError):
        print(
            "Figures stopped: incomplete or changed audited evidence; no raw diagnostics."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
