"""Repository-native figures for a complete, raw-ledger-verified fixed factorial.

Not adaptive planner performance. The CLI reruns evidence reconstruction before
plotting; prefixes and fabricated success/financial flags are not figure inputs.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from bench import openrouter_typed_factorial as study
from bench.openrouter_joint_analysis import paired_bootstrap
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_pilot import PilotError, digest, load_key, money

LABELS = {
    "S0": "Sonnet\nOriginal",
    "SR": "Sonnet\nCC + SD",
    "H0": "Haiku\nOriginal",
    "HR": "Haiku\nCC + SD",
}
FOOTNOTE = "32 generated development tasks · fixed configurations, not adaptive policy · descriptive intervals, not a safety certificate"


def build_data(manifest, report):
    """Deterministic display data; CLI must first regenerate report from receipts."""
    rows = report["paired_tasks"]
    ids = list(dict.fromkeys(item["task_id"] for item in manifest["schedule"]))
    finance = report["finance"]
    if (
        manifest["kind"] != study.KIND
        or report["manifest_sha256"] != digest(manifest)
        or report.get("schedule_complete") is not True
        or report.get("efficacy_claim") is not False
        or report.get("native_admission_tested") is not False
        or report.get("paper_evidence") is not False
        or finance.get("snapshot_financially_complete") is not True
        or finance.get("failed_provider_results")
        or finance.get("additional_attempt_records")
        or money(finance["retained_uncertainty_usd"]) != 0
        or money(finance["pending_upper_bound_usd"]) != 0
        or len(rows) != study.COUNT
        or len(ids) != study.COUNT
        or [r["task_id"] for r in rows] != ids
        or len(set(ids)) != len(ids)
    ):
        raise PilotError(
            "figures require the complete independently reconstructable factorial"
        )
    for row in rows:
        if set(row["cells"]) != set(study.ARMS) or any(
            row[k] != v for k, v in study.contrasts(row["cells"]).items()
        ):
            raise PilotError("paired cells or interaction values changed")
        for cell in row["cells"].values():
            if type(cell["em"]) not in (int, float) or cell["em"] not in (0, 1):
                raise PilotError("exact-quality value is not binary")
            money(cell["cost_usd"])
            money(cell["nominal_uncached_cost_usd"])
    cells = []
    for arm in study.ARMS:
        selected = [r["cells"][arm] for r in rows]
        intervals = paired_bootstrap(
            [
                {"cost_usd": float(c["cost_usd"]), "em": float(c["em"])}
                for c in selected
            ],
            seed=717,
        )
        cells.append(
            {
                "arm": arm,
                "label": LABELS[arm],
                "tasks": len(selected),
                "exact": sum(int(c["em"]) for c in selected),
                "valid_workflows": sum(
                    all(c["valid_by_stage"].values()) for c in selected
                ),
                "mean_paid_usd": sum(float(c["cost_usd"]) for c in selected)
                / len(selected),
                "mean_nominal_uncached_usd": sum(
                    float(c["nominal_uncached_cost_usd"]) for c in selected
                )
                / len(selected),
                "mean_paid_95": intervals["cost_usd"][
                    "descriptive_95_percentile_interval"
                ],
                "cached_input_tokens": sum(c["cached_input_tokens"] for c in selected),
                "cache_unknown_calls": sum(c["cache_unknown_calls"] for c in selected),
            }
        )
    vectors = [{"quality_pp": 100 * r["quality_interaction"]} for r in rows]
    log_rows = [r for r in rows if r["log_cost_ratio_interaction"] is not None]
    log_vectors = [
        {"log_cost_ratio": r["log_cost_ratio_interaction"]} for r in log_rows
    ]
    return {
        "manifest_sha256": digest(manifest),
        "analysis_sha256": digest(report),
        "plot_source_sha256": file_hash(Path(__file__)),
        "paper_evidence": False,
        "scope": "fixed model x CC/SD opportunity; not native policy admission",
        "cells": cells,
        "paired_tasks": rows,
        "finance": finance,
        "quality_interval": paired_bootstrap(vectors, seed=717)["quality_pp"],
        "log_cost_interval": paired_bootstrap(log_vectors, seed=717)["log_cost_ratio"]
        if log_vectors
        else None,
        "log_cost_tasks": len(log_rows),
        "zero_cost_tasks": len(rows) - len(log_rows),
        "materializations": report["materializations"],
        "abstentions": report["materialization_abstentions"],
        "limitations": report["limitations"]
        + [
            "Per-cell costs exclude the separately charged feasibility pilot; this is not deployment economics.",
            "Small-sample bootstrap intervals condition on these single observed realizations; degenerate intervals do not imply certainty.",
        ],
    }


def render(data, directory):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import ListedColormap

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": "#839094",
            "axes.labelcolor": "#26383c",
            "text.color": "#26383c",
            "xtick.color": "#52676b",
            "ytick.color": "#52676b",
            "svg.hashsalt": "agentc-typed-factorial-v1",
            "savefig.facecolor": "white",
        }
    )
    directory.mkdir(parents=True, exist_ok=True)
    colors = ["#5378a8", "#5378a8", "#1c6b70", "#1c6b70"]

    def save(fig, stem):
        for suffix in ("svg", "png"):
            path = directory / (stem + "." + suffix)
            if path.exists():
                raise PilotError("refusing to overwrite a figure")
            fig.savefig(
                path,
                dpi=220,
                bbox_inches="tight",
                metadata={"Date": None} if suffix == "svg" else None,
            )
        plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), layout="constrained")
    for i, cell in enumerate(data["cells"]):
        value = cell["mean_paid_usd"]
        lo, hi = cell["mean_paid_95"]
        axes[0].bar(
            i, value, color=colors[i], alpha=0.35 if i % 2 == 0 else 0.9, width=0.6
        )
        axes[0].errorbar(
            i,
            value,
            yerr=[[max(0, value - lo)], [max(0, hi - value)]],
            fmt="none",
            ecolor=colors[i],
            capsize=4,
        )
        axes[0].annotate(
            f"${value:.4f}",
            (i, max(value, hi)),
            xytext=(0, 7),
            textcoords="offset points",
            ha="center",
            fontsize=9,
        )
        accuracy = 100 * cell["exact"] / cell["tasks"]
        axes[1].bar(
            i, accuracy, color=colors[i], alpha=0.35 if i % 2 == 0 else 0.9, width=0.6
        )
        axes[1].annotate(
            f"{cell['exact']}/{cell['tasks']}",
            (i, accuracy),
            xytext=(0, 6),
            textcoords="offset points",
            ha="center",
        )
    for ax in axes:
        ax.set_xticks(range(4), [c["label"] for c in data["cells"]])
        ax.grid(axis="y", color="#e3e8e9", lw=0.6)
        ax.set_axisbelow(True)
    axes[0].set_ylim(0, max(c["mean_paid_95"][1] for c in data["cells"]) * 1.3 or 1)
    axes[0].set_ylabel("Mean paid cost per workflow (USD)")
    axes[1].set_ylim(0, 114)
    axes[1].set_yticks([0, 25, 50, 75, 100])
    axes[1].set_ylabel("Exact final answers (%)")
    fig.suptitle("Model × input rewrites: fixed-configuration ablation", fontsize=14)
    fig.supxlabel(
        FOOTNOTE
        + "\nCC = ContextCompress at filter; SD = StateDrop at answer · all three calls per workflow charged",
        fontsize=8,
    )
    save(fig, "01-cost-quality")

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.7), layout="constrained")
    configs = [
        (
            "quality_interaction",
            "quality_interval",
            100,
            "Quality interaction (percentage points)",
        ),
        (
            "log_cost_ratio_interaction",
            "log_cost_interval",
            1,
            "Relative cost interaction (log ratio)",
        ),
    ]
    for ax, (field, interval, scale, label) in zip(axes, configs):
        values = [
            r[field] * scale for r in data["paired_tasks"] if r[field] is not None
        ]
        ax.set_title(
            f"n = {len(values)} tasks"
            + (
                f"; zero-cost tasks excluded = {data['zero_cost_tasks']}"
                if field == "log_cost_ratio_interaction"
                else ""
            ),
            fontsize=9,
        )
        if values:
            jitter = np.linspace(-0.16, 0.16, len(values))
            ax.scatter(values, jitter, color="#5378a8", s=22, alpha=0.6, linewidths=0)
            summary = data[interval]
            center = summary["mean"]
            lo, hi = summary["descriptive_95_percentile_interval"]
            ax.errorbar(
                center,
                0.4,
                xerr=[[max(0, center - lo)], [max(0, hi - center)]],
                fmt="D",
                color="#1c6b70",
                markersize=7,
                capsize=5,
                lw=2,
            )
            ax.annotate(
                f"mean {center:.3g}  ·  95% [{lo:.3g}, {hi:.3g}]",
                (center, 0.4),
                xytext=(0, 13),
                textcoords="offset points",
                ha="center",
                fontsize=9,
            )
            span = max(max(values) - min(values), abs(center), 0.1 if scale == 1 else 5)
            ax.set_xlim(
                min(min(values), lo, 0) - 0.35 * span,
                max(max(values), hi, 0) + 0.35 * span,
            )
        else:
            ax.text(
                0.5,
                0.5,
                "No strictly positive-cost pairs",
                transform=ax.transAxes,
                ha="center",
            )
        ax.axvline(0, color="#839094", lw=1, ls="--")
        ax.set_yticks([])
        ax.set_ylim(-0.28, 0.75)
        ax.set_xlabel(label)
        ax.grid(axis="x", color="#e3e8e9", lw=0.6)
    fig.suptitle("Does rewriting affect the two models differently?", fontsize=14)
    fig.supxlabel(
        "Each dot is one task; diamond is the mean with a paired descriptive bootstrap interval\n(HR − H0) − (SR − S0) for quality; log(HR/H0) − log(SR/S0) for cost · zero is no interaction\nOne observed realization per cell: zero-width intervals do not imply certainty\nGenerated development tasks, not an adaptive-policy or safety certificate · price multiplication alone is not learned synergy",
        fontsize=8,
    )
    save(fig, "02-paired-interactions")

    rows = data["paired_tasks"]
    quality = np.array([[r["cells"][a]["em"] for a in study.ARMS] for r in rows])
    relative = np.array(
        [
            [
                float(r["cells"][a]["cost_usd"]) / float(r["cells"]["S0"]["cost_usd"])
                if float(r["cells"]["S0"]["cost_usd"]) > 0
                else np.nan
                for a in study.ARMS
            ]
            for r in rows
        ]
    )
    fig, axes = plt.subplots(1, 2, figsize=(8.3, 10), sharey=True, layout="constrained")
    axes[0].imshow(
        quality,
        aspect="auto",
        interpolation="nearest",
        cmap=ListedColormap(["#f2dfca", "#1c6b70"]),
        vmin=0,
        vmax=1,
    )
    image = axes[1].imshow(
        relative,
        aspect="auto",
        interpolation="nearest",
        cmap="Blues",
        vmin=0,
        vmax=max(1, float(np.nanmax(relative))) if np.any(np.isfinite(relative)) else 1,
    )
    for i, row in enumerate(rows):
        for j, arm in enumerate(study.ARMS):
            correct = row["cells"][arm]["em"] == 1
            axes[0].text(
                j,
                i,
                "1" if correct else "0",
                ha="center",
                va="center",
                color="white" if correct else "#6f4423",
                fontsize=8,
            )
            value = relative[i, j]
            axes[1].text(
                j,
                i,
                f"{value:.2f}×" if math.isfinite(value) else "n/a",
                ha="center",
                va="center",
                fontsize=7,
                color="white"
                if math.isfinite(value) and value > image.norm.vmax * 0.65
                else "#26383c",
            )
    for ax in axes:
        ax.set_xticks(range(4), list(study.ARMS))
        ax.tick_params(axis="both", length=0)
    axes[0].set_yticks(
        range(len(rows)), [f"T{i + 1:02d}" for i in range(len(rows))], fontsize=8
    )
    axes[0].set_title("Exact answer · 1 correct, 0 wrong", loc="left", fontsize=11)
    axes[1].set_title(
        "Paid cost relative to S0 · lower is cheaper", loc="left", fontsize=11
    )
    fig.suptitle("Every paired task, without outcome-based sorting", fontsize=13)
    fig.supxlabel(
        "T01–T32 follow the frozen fixture order; complete IDs are in plot_data.json\nS = Sonnet; H = Haiku; 0 = original; R = CC + SD · no per-task oracle policy",
        fontsize=8,
    )
    save(fig, "03-all-paired-tasks")
    return {"matplotlib_version": matplotlib.__version__, "formats": ["svg", "png"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("env-file", "ledger", "output", "pilot", "bridge", "figure-output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--pilot-manifest-sha256", required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads((args.output / "manifest.json").read_text())
        # Analysis performs no provider request; the key only verifies the
        # original ledger identity. Never trust an edited cached analysis file.
        study.analyze(args, load_key(args.env_file), manifest)
        report = json.loads((args.output / "analysis.json").read_text())
        data = build_data(manifest, report)
        write_json(args.figure_output / "plot_data.json", data, immutable=True)
        rendering = render(data, args.figure_output)
        write_json(
            args.figure_output / "rendering.json",
            {
                **rendering,
                "plot_data_sha256": digest(data),
                "files": {
                    p.name: file_hash(p)
                    for p in sorted(args.figure_output.iterdir())
                    if p.suffix in (".png", ".svg")
                },
            },
            immutable=True,
        )
        print(
            json.dumps(
                {
                    "figures": 3,
                    "paper_evidence": False,
                    "output": str(args.figure_output),
                }
            )
        )
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError):
        print(
            "Figure generation stopped: incomplete or changed evidence; no raw diagnostics."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
