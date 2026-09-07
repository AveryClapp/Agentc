"""Descriptive figures from an externally pinned, completion-gated analysis.

Never opens labels, dispatches, replays an acquisition, or changes the bootstrap.
Fixed workflow comparisons and native probe counts are separate measurements.
"""

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

from bench import openrouter_protected_analysis as analysis
from bench.openrouter_pilot import PilotError, digest, money

PLANS = {
    "0a12f704cebedd33a200843aadc654b791e6e844c0657130fcd37a015fd9d7a5": (
        "filter",
        "Context compression",
        "sonnet",
    ),
    "7ab3dfa273f06a586a83a3b8aeef2d1b2a700afc395b19e338779af2ac3f4365": (
        "filter",
        "Model swap",
        "haiku",
    ),
    "96d709826f8a0f628f42dd52435147d98f4a74757229fae08cda5844ce7abb7d": (
        "filter",
        "Compression + model swap",
        "haiku",
    ),
    "4af0af3e5429f8a27a7f91119ccc242d0b9e0099efd36587d6137ab16346b808": (
        "synthesize",
        "Model swap",
        "haiku",
    ),
    "4af3f0c206f7028d1d6450d89cf2066c9532ab5445b6827b934f4742c76d1718": (
        "answer",
        "State dropping + model swap",
        "haiku",
    ),
    "a2e9cd4ae3b4dd1095605314042bb1613b8f82f63d4fb01eccaaf24f5d926c51": (
        "answer",
        "Model swap",
        "haiku",
    ),
    "95f30df35584b3dba2a172322e675ee89a61515e2ab990b3d053aa990b1b5d29": (
        "answer",
        "State dropping",
        "sonnet",
    ),
}
FOOTER = "63 generated development tasks; descriptive results, not adaptive-serving efficacy."
PREVIEW_LABEL = "SYNTHETIC LAYOUT TEST — NOT EXPERIMENT RESULTS"


def require(condition, message):
    if not condition:
        raise PilotError(message)


def number(value):
    require(
        type(value) in (int, float) and math.isfinite(value), "nonfinite figure value"
    )
    return value


def build_data(report):
    """Validate plot scope and reconcile plotted quantities to whole-task rows."""
    require(report["kind"] == analysis.KIND, "wrong protected analysis kind")
    require(
        report["analysis_protocol"] == analysis.PROTOCOL, "analysis protocol changed"
    )
    require(
        report["analysis_protocol_sha256"] == digest(analysis.PROTOCOL),
        "protocol hash changed",
    )
    require(report["manifest_sha256"] == analysis.MANIFEST_SHA256, "wrong acquisition")
    require(
        report["manifest_file_sha256"] == analysis.MANIFEST_FILE_SHA256,
        "wrong manifest bytes",
    )
    for name in ("schedule_complete", "labels_opened"):
        require(
            report[name] is True, "incomplete or unscored analysis cannot be plotted"
        )
    for name in (
        "adaptive_savings_claim",
        "native_primary_promotion",
        "efficacy_claim",
        "paper_evidence",
        "pilot_accuracy_reported",
    ):
        require(report[name] is False, "figure scope cannot imply adaptive efficacy")
    exposure = report["fixture_exposure"]
    require(
        exposure["unseen_input_claim"] is False and exposure["resampling"] is False,
        "development exposure disclosure required",
    )
    rows = report["paired_tasks"]
    require(report["paired_task_count"] == len(rows) == 63, "all 63 clusters required")
    require(len({row["task_id"] for row in rows}) == 63, "duplicate task cluster")
    values = {}
    totals = {}
    for arm in ("source", "HR"):
        quality = [number(row["cells"][arm]["exact_accuracy"]) for row in rows]
        require(all(value in (0, 1) for value in quality), "accuracy is not binary")
        amounts = [money(row["cells"][arm]["workflow_cost_usd"]) for row in rows]
        require(all(value >= 0 for value in amounts), "negative workflow cost")
        require(
            report["accuracy_totals"][arm] == {"correct": sum(quality), "tasks": 63},
            "accuracy total differs from clusters",
        )
        values[arm + "_accuracy"] = sum(quality) / 63
        totals[arm] = sum(amounts, Decimal(0))
    differences = [
        row["cells"]["HR"]["exact_accuracy"] - row["cells"]["source"]["exact_accuracy"]
        for row in rows
    ]
    require(
        all(
            row["HR_minus_source_accuracy"] == delta
            for row, delta in zip(rows, differences)
        ),
        "paired quality difference changed",
    )
    values["HR_minus_source_accuracy"] = sum(differences) / 63
    transitions = dict(
        Counter(
            "HR_gain" if d > 0 else "HR_loss" if d < 0 else "tie" for d in differences
        )
    )
    require(report["quality_transitions"] == transitions, "quality transitions changed")
    intervals = {}
    for key, mean in values.items():
        item = report["descriptive_paired_intervals"][key]
        require(
            math.isclose(number(item["mean"]), mean, abs_tol=1e-12),
            "interval mean differs from clusters",
        )
        low, high = map(number, item["descriptive_95_percentile_interval"])
        require(
            (-1 if key.startswith("HR_minus") else 0) <= low <= high <= 1,
            "invalid accuracy interval",
        )
        intervals[key] = {"mean": mean, "low": low, "high": high}
    costs = report["costs"]
    require(
        money(costs["source_workflows_usd"]) == totals["source"]
        and money(costs["fixed_HR_workflows_usd"]) == totals["HR"],
        "workflow charges differ from clusters",
    )
    parts = {
        "Source workflows": totals["source"],
        "Native probes": money(costs["native_probes_usd"]),
        "Fixed HR workflows": totals["HR"],
        "Pilot": money(costs["pilot_usd"]),
    }
    require(all(value >= 0 for value in parts.values()), "negative acquisition cost")
    require(
        sum(parts.values(), Decimal(0)) == money(costs["all_acquisition_usd"]),
        "acquisition charges omitted",
    )
    require(
        totals["source"] + parts["Native probes"]
        == money(costs["source_learning_block_usd"]),
        "learning block excludes probes",
    )
    native = report["native_learning"]
    require(
        native["current_guard_or_admission_assessed"] is False
        and native["sample_floor_is_not_acceptance"] is True,
        "sample counts are not admission",
    )
    profiles = native["profiles"]
    require(
        len(profiles) == len(PLANS)
        and {p["key"]["execution_plan_id"] for p in profiles} == set(PLANS),
        "exact observed plan inventory changed",
    )
    inventory = {}
    for profile in profiles:
        key = profile["key"]["execution_plan_id"]
        stage, label, model = PLANS[key]
        require(
            profile["workflow_stage"] == stage
            and profile["runtime_version"]["target_model_id"]
            == f"anthropic/claude-{model}-4.5",
            "plan attribution changed",
        )
        n, disagreements = profile["paired_observations"], profile["disagreements"]
        require(
            type(n) is int
            and type(disagreements) is int
            and 0 <= disagreements <= n <= 20,
            "invalid paired counts",
        )
        require(
            profile["evidence_target"] == 20
            and profile["count_floor_met"] is (n == 20)
            and profile["pairs_short_of_floor"] == 20 - n,
            "evidence floor changed",
        )
        inventory[key] = {
            "label": f"{stage.capitalize()}: {label}",
            "pairs": n,
            "disagreements": disagreements,
        }
    counts = report["call_counts"]
    require(
        counts
        == {
            "pilot": 36,
            "source": 189,
            "HR": 189,
            "probe": sum(p["pairs"] for p in inventory.values()),
        },
        "call inventory changed",
    )
    return {
        "intervals": intervals,
        "transitions": transitions,
        "costs": {k: str(v) for k, v in parts.items()},
        "all_acquisition_usd": costs["all_acquisition_usd"],
        "profiles": [inventory[key] for key in PLANS],
        "scope": FOOTER,
    }


def render(data, directory, *, preview=False):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    directory = Path(directory)
    require(not directory.exists(), "figure directory already exists")
    directory.mkdir(parents=True)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": "#8d9b9e",
            "text.color": "#24383d",
            "axes.labelcolor": "#24383d",
            "svg.hashsalt": "agentc-protected-v1",
            "savefig.facecolor": "white",
        }
    )
    teal, orange, blue, gray = "#1c6b70", "#b36d35", "#5378a8", "#86999e"

    def save(fig, stem):
        fig.text(0.02, 0.015, FOOTER, fontsize=8, color="#52676b")
        if preview:
            fig.text(0.02, 0.96, PREVIEW_LABEL, fontsize=9, color=orange, weight="bold")
        for extension in ("svg", "png"):
            fig.savefig(
                directory / f"{stem}.{extension}",
                dpi=220,
                metadata={"Date": None} if extension == "svg" else None,
            )
        plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.1))
    fig.subplots_adjust(left=0.16, right=0.97, bottom=0.25, top=0.85, wspace=0.6)
    for y, (key, label, color) in enumerate(
        (
            ("source_accuracy", "Source: Sonnet", blue),
            ("HR_accuracy", "Fixed Haiku + rules", teal),
        )
    ):
        row = data["intervals"][key]
        axes[0].plot([100 * row["low"], 100 * row["high"]], [y, y], color=color, lw=3)
        axes[0].scatter([100 * row["mean"]], [y], color=color, s=60, zorder=3)
        axes[0].annotate(
            f"{row['mean'] * 100:.1f}%",
            (row["mean"] * 100, y),
            xytext=(0, 13),
            textcoords="offset points",
            ha="center",
        )
    axes[0].set(
        yticks=[0, 1],
        yticklabels=["Source: Sonnet", "Fixed Haiku + rules"],
        ylim=(-0.6, 1.7),
        xlim=(-2, 105),
        xlabel="Exact workflow accuracy (%)",
        title="A   Fixed workflow comparison",
    )
    row = data["intervals"]["HR_minus_source_accuracy"]
    axes[1].axvline(0, color=gray, lw=1)
    axes[1].plot([100 * row["low"], 100 * row["high"]], [0, 0], color=teal, lw=3)
    axes[1].scatter([100 * row["mean"]], [0], color=teal, s=60, zorder=3)
    axes[1].set(
        yticks=[],
        ylim=(-0.7, 0.7),
        xlabel="HR minus source (percentage points)",
        title="B   Paired accuracy difference",
    )
    extent = max(5, abs(row["low"] * 100), abs(row["high"] * 100)) * 1.3
    axes[1].set_xlim(-extent, extent)
    axes[1].text(
        0.5,
        0.85,
        f"{100 * row['mean']:+.2f} pp   [{100 * row['low']:+.2f}, {100 * row['high']:+.2f}]",
        transform=axes[1].transAxes,
        ha="center",
        bbox={"facecolor": "white", "edgecolor": "none", "pad": 2},
    )
    fig.text(
        0.5,
        0.12,
        "Whole-task paired bootstrap; 10,000 replicates; descriptive 95% intervals",
        ha="center",
        fontsize=9,
    )
    save(fig, "01-fixed-workflow-quality")

    fig, ax = plt.subplots(figsize=(9, 3.7))
    fig.subplots_adjust(left=0.08, right=0.97, bottom=0.32, top=0.65)
    fig.suptitle(
        f"Complete acquisition: ${float(data['all_acquisition_usd']):.6f}", y=0.89
    )
    left = 0
    for (label, value), color in zip(data["costs"].items(), (blue, orange, teal, gray)):
        amount = float(value)
        ax.barh(
            [0],
            [amount],
            left=left,
            color=color,
            label=f"{label}: ${amount:.6f}",
            height=0.5,
        )
        left += amount
    ax.set(
        yticks=[],
        xlabel="Actual acquisition charges (USD)",
        xlim=(0, max(left * 1.03, 0.01)),
    )
    ax.legend(
        loc="lower left", bbox_to_anchor=(0, 1.05), ncol=2, frameon=False, fontsize=9
    )
    fig.text(
        0.5,
        0.14,
        "Includes pilot and probes; this total is not an adaptive deployment-cost estimate.",
        ha="center",
        fontsize=9,
    )
    save(fig, "02-all-acquisition-costs")

    fig, ax = plt.subplots(figsize=(11, 4.9))
    fig.subplots_adjust(left=0.39, right=0.94, bottom=0.24, top=0.83)
    for y, row in enumerate(data["profiles"]):
        same = row["pairs"] - row["disagreements"]
        ax.barh(y, same, color=teal, height=0.65)
        ax.barh(y, row["disagreements"], left=same, color=orange, height=0.65)
        ax.text(
            20.4,
            y,
            f"{row['pairs']} pairs / {row['disagreements']} disagreements",
            va="center",
            fontsize=9,
        )
    ax.axvline(20, color=gray, lw=1, linestyle="--")
    ax.set(
        yticks=range(len(data["profiles"])),
        yticklabels=[p["label"] for p in data["profiles"]],
        xlim=(0, 35),
        xticks=[0, 5, 10, 15, 20],
        xlabel="Observed exact-plan comparisons",
        title="Native learning: counts are not admission",
    )
    ax.invert_yaxis()
    fig.text(
        0.5,
        0.12,
        "20 is the sample floor, not a safety certificate. Plans were probed on different calls.",
        ha="center",
        fontsize=9,
    )
    save(fig, "03-native-plan-learning")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--analysis-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--synthetic-preview", action="store_true")
    args = parser.parse_args()
    raw = args.analysis.read_bytes()
    require(
        hashlib.sha256(raw).hexdigest() == args.analysis_sha256,
        "external analysis hash differs",
    )
    report = analysis._json(raw)
    data = build_data(report)
    render(data, args.output, preview=args.synthetic_preview)
    (args.output / "plot-data.json").write_text(
        json.dumps(
            {
                "analysis_sha256": args.analysis_sha256,
                "synthetic_preview": args.synthetic_preview,
                **data,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    (args.output / "rendering.json").write_text(
        json.dumps(
            {
                "figure_source_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "analysis_sha256": args.analysis_sha256,
                "synthetic_preview": args.synthetic_preview,
                "files": {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(args.output.iterdir())
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(
        json.dumps(
            {"figures": 3, "analysis_sha256": args.analysis_sha256, "scope": FOOTER}
        )
    )


if __name__ == "__main__":
    main()
