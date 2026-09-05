"""Data-bound exploratory figures: CC opportunity, policies, and interactions.

These figures describe the complete ContextCompress-only acquisition. They
must never be presented as the requested future full-rule factorial study.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from bench.openrouter_frontier import CONTEXTS, SOURCE_MODEL, load_tasks
from bench.openrouter_frontier_analysis import calibrate, total_cost
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_mechanisms import mean_interval
from bench.openrouter_pilot import PilotError, digest, money
from bench.openrouter_replay import validate_matrix

NAMES = {"anthropic/claude-sonnet-4.5": "Sonnet 4.5", "anthropic/claude-haiku-4.5": "Haiku 4.5",
    "google/gemini-2.5-flash-lite": "Gemini Flash Lite", "qwen/qwen3-30b-a3b-instruct-2507": "Qwen3 30B"}
POLICY_NAMES = {"routing_default_budget": "Routing · 20", "routing_expanded_budget": "Routing · 160",
    "rewrite_default_budget": "Guarded CC · 20", "rewrite_expanded_budget": "Guarded CC · 160",
    "joint_default_budget": "Joint model+CC · 20", "joint_expanded_budget": "Joint model+CC · 160"}


def conditional_intervals(reference, treatment, reference_setup, treatment_setup, seed, draws=2000):
    if draws < 40:
        raise PilotError("at least40 bootstrap draws required")
    if not reference or len(reference) != len(treatment) or any(a["task_id"] != b["task_id"] for a, b in zip(reference, treatment)):
        raise PilotError("figure intervals require ordered question pairs")
    rng = random.Random(seed)
    quality, savings = [], []
    for _ in range(draws):
        indices = [rng.randrange(len(reference)) for _ in reference]
        quality.append(sum(treatment[i]["f1"] - reference[i]["f1"] for i in indices) / len(indices))
        baseline = reference_setup + sum(reference[i]["cost"] for i in indices)
        if baseline <= 0:
            raise PilotError("positive baseline cost required")
        savings.append(1 - (treatment_setup + sum(treatment[i]["cost"] for i in indices)) / baseline)
    def interval(values):
        values.sort()
        return [values[int(.025 * (draws - 1))], values[int(.975 * (draws - 1))]]
    return {"quality_delta_95": interval(quality), "net_cost_reduction_95": interval(savings), "draws": draws,
        "scope": "descriptive conditional on fixed calibration and realized trajectory; does not rerun adaptive policy or estimate tuning/provider replication uncertainty"}


def policy_data(manifest, rows, lock, replay):
    by_id = {r["id"]: r for r in rows}
    output = []
    for context in CONTEXTS:
        group = [r for r in rows if r["context"] == context]
        ref = [r for r in group if (r["model"], r["arm"]) == (SOURCE_MODEL, "full")]
        baseline_setup = float(total_cost([r for r in ref if r["phase"] != "holdout"], "nominal_uncached_cost_usd"))
        reference = sorted([{"task_id": r["task_id"], "f1": r["f1"], "cost": float(money(r["nominal_uncached_cost_usd"]))}
            for r in ref if r["phase"] == "holdout"], key=lambda r: r["task_id"])
        baseline_total = baseline_setup + sum(r["cost"] for r in reference)

        def add(name, treatment, setup, kind):
            treatment = sorted(treatment, key=lambda r: r["task_id"])
            intervals = conditional_intervals(reference, treatment, baseline_setup, setup, digest(["figure-policy-v1", context, name]))
            output.append({"context": context, "name": name, "kind": kind, "questions": len(reference),
                "baseline_setup_nominal_usd": baseline_setup, "policy_setup_nominal_usd": setup,
                "baseline_total_nominal_usd": baseline_total, "policy_total_nominal_usd": setup + sum(r["cost"] for r in treatment),
                "net_cost_reduction": 1 - (setup + sum(r["cost"] for r in treatment)) / baseline_total,
                "f1_delta": sum(b["f1"] - a["f1"] for a, b in zip(reference, treatment)) / len(reference), "intervals": intervals})

        add("Original", reference, baseline_setup, "reference")
        old = [r for r in group if r["model"] == SOURCE_MODEL and (r["arm"] == "compress" or r["phase"] == "warmup")]
        add("CC · legacy greedy", [{"task_id": r["task_id"], "f1": r["f1"], "cost": float(money(r["nominal_uncached_cost_usd"]))}
            for r in old if r["phase"] == "holdout"], float(total_cost([r for r in old if r["phase"] != "holdout"], "nominal_uncached_cost_usd")), "rewrite")
        for control in [c for c in lock["controls"] if c["context"] == context]:
            selected = control["selected"]
            candidates = {(c["model"], c["arm"]) for c in control["candidates"]}
            setup = [r for r in group if (r["phase"] == "warmup" and r["model"] == SOURCE_MODEL)
                or (r["phase"] == "calibration" and (r["model"], r["arm"]) in candidates)]
            chosen = [r for r in group if r["phase"] == "holdout" and (r["model"], r["arm"]) == (selected["model"], selected["arm"])]
            add("Calibrated fixed model" if control["name"] == "calibrated_fixed_model" else "Calibrated static model+CC",
                [{"task_id": r["task_id"], "f1": r["f1"], "cost": float(money(r["nominal_uncached_cost_usd"]))} for r in chosen],
                float(total_cost(setup, "nominal_uncached_cost_usd")), "static")
        for trajectory in [t for t in replay["trajectories"] if t["context"] == context]:
            decisions = trajectory["decisions"]
            chosen = [{"task_id": d["task_id"], "f1": by_id[d["primary_row_id"]]["f1"],
                "cost": float(money(d["nominal_uncached_cost_estimate_usd"]))} for d in decisions if d["phase"] == "holdout"]
            setup = sum(float(money(d["nominal_uncached_cost_estimate_usd"])) for d in decisions if d["phase"] != "holdout")
            add(POLICY_NAMES[trajectory["policy"]], chosen, setup, trajectory["policy"].split("_")[0])
    return output


def validate_trajectories(manifest, rows, replay):
    """Reject partial/relabeled policy reports before a persuasive plot exists."""
    specs = {p["name"]: p["settings"] for p in manifest["policy_replay"]["specs"]}
    expected = {(name, context) for name in specs for context in CONTEXTS}
    actual = [(t["policy"], t["context"]) for t in replay["trajectories"]]
    if len(actual) != len(expected) or set(actual) != expected or set(specs) != set(POLICY_NAMES):
        raise PilotError("figure replay needs exact unique frozen policy/context coverage")
    by_id = {r["id"]: r for r in rows}
    for trajectory in replay["trajectories"]:
        name, context = trajectory["policy"], trajectory["context"]
        if trajectory["settings"] != specs[name]:
            raise PilotError("figure replay settings differ from frozen policy")
        budget = "160" if "expanded" in name else "20"
        if specs[name]["AGENTC_OPTIMIZE_EXPLORATION_CALLS_PER_SITE_24H"] != budget:
            raise PilotError("figure policy budget label differs from frozen setting")
        expected_tasks = list(dict.fromkeys((r["task_id"], r["phase"]) for r in manifest["schedule"] if r["context"] == context))
        if [(d["task_id"], d["phase"]) for d in trajectory["decisions"]] != expected_tasks:
            raise PilotError("figure replay task/phase chronology is incomplete or changed")
        for decision in trajectory["decisions"]:
            row = by_id.get(decision["primary_row_id"])
            if row is None or (row["context"], row["task_id"], row["phase"]) != (context, decision["task_id"], decision["phase"]):
                raise PilotError("figure replay primary outcome belongs to another task")


def activation_data(rows):
    full = {(r["context"], r["model"], r["task_id"]): r for r in rows if r["phase"] == "holdout" and r["arm"] == "full"}
    output = []
    for context, model in sorted({(r["context"], r["model"]) for r in full.values()}):
        candidates = [r for r in rows if r["phase"] == "holdout" and (r["context"], r["model"], r["arm"]) == (context, model, "compress")]
        output.append({"context": context, "model": model, "questions": len(candidates),
            "native_rewritten": sum(r["native_plan"]["kind"] == "rewritten" for r in candidates),
            "identical_payload_pairs": sum(r["request_sha256"] == full[(context, model, r["task_id"])]["request_sha256"] for r in candidates)})
    return output


def build_data(manifest, rows, lock, replay, mechanisms):
    for report, field in ((replay, "consumed_rows_sha256"), (mechanisms, "results_sha256")):
        if report["manifest_sha256"] != digest(manifest) or report[field] != digest(rows) or report["paper_evidence"] is not False:
            raise PilotError("figure inputs do not share complete matrix provenance")
    if (replay["calibration_only"] or replay["restart_after_calibration"] or calibrate(manifest, rows) != lock
            or replay.get("evaluation_kind") != "offline_selected_feedback_replay"
            or replay.get("replay_source_sha256") != file_hash(Path(__file__).with_name("openrouter_replay.py"))):
        raise PilotError("figure inputs need original complete replay and frozen static lock")
    validate_trajectories(manifest, rows, replay)
    opportunity = []
    for context in CONTEXTS:
        for model in sorted(manifest["endpoints"]):
            for arm in ("full", "compress"):
                values = [r for r in rows if r["phase"] == "holdout" and (r["context"], r["model"], r["arm"]) == (context, model, arm)]
                opportunity.append({"context": context, "model": model, "arm": arm, "questions": len(values),
                    "mean_f1": sum(r["f1"] for r in values) / len(values),
                    "mean_f1_95": mean_interval([r["f1"] for r in values], digest(["opportunity-v1", context, model, arm])),
                    "mean_nominal_uncached_usd": float(total_cost(values, "nominal_uncached_cost_usd")) / len(values)})
    return {"paper_evidence": False, "scope": "ContextCompress-only exploratory study, not full-rule factorial",
        "manifest_sha256": digest(manifest), "results_sha256": digest(rows), "replay_sha256": digest(replay),
        "mechanisms_sha256": digest(mechanisms), "plot_source_sha256": file_hash(Path(__file__)),
        "opportunity": opportunity, "activation": activation_data(rows),
        "policies": policy_data(manifest, rows, lock, replay), "interactions": mechanisms["interactions"],
        "limitations": ["Natural and extended panels reuse the same questions; they are not independent datasets.",
            "Identical-payload pairs measure provider repeat variability, not a rewrite effect; see activation counts.",
            "Nominal costs reprice observed tokens without implicit cache discounts; these are not deployment bills.",
            "Native policy totals include all measured primary/exploration/shadow decisions; static totals pay all candidate calibration calls.",
            "Unadjusted descriptive intervals are conditional on the realized trajectory; they are not a safety or noninferiority certificate.",
            "The requested full-rule/LOO study needs separate live acquisition; no unexercised rule is assigned a measured zero effect."]}


def render(data, directory):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = ("#1c6b70", "#5378a8", "#a56636", "#89679b")
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.titlesize": 12,
        "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": "#839094",
        "axes.labelcolor": "#26383c", "text.color": "#26383c", "xtick.color": "#52676b", "ytick.color": "#52676b",
        "svg.hashsalt": "agentc-frontier-v1", "savefig.facecolor": "white"})
    directory.mkdir(parents=True, exist_ok=True)
    def save(fig, stem):
        for suffix in ("svg", "png"):
            path = directory / f"{stem}.{suffix}"
            if path.exists():
                raise PilotError("refusing to overwrite an existing figure")
            fig.savefig(path, dpi=220, bbox_inches="tight", metadata={"Date": None} if suffix == "svg" else None)
        plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(11.8, 4.3), sharex=True, sharey=True, layout="constrained")
    models = sorted({r["model"] for r in data["opportunity"]})
    for ax, context in zip(axes, CONTEXTS):
        for index, model in enumerate(models):
            points = [next(r for r in data["opportunity"] if (r["context"], r["model"], r["arm"]) == (context, model, arm)) for arm in ("full", "compress")]
            ax.plot([p["mean_nominal_uncached_usd"] for p in points], [100*p["mean_f1"] for p in points], color=colors[index], lw=1.5)
            for point, marker in zip(points, ("o", "s")):
                value, ci = 100*point["mean_f1"], [100*x for x in point["mean_f1_95"]]
                ax.errorbar(point["mean_nominal_uncached_usd"], value, yerr=[[max(0, value-ci[0])], [max(0, ci[1]-value)]],
                    fmt=marker, color=colors[index], markersize=6, capsize=2, lw=.8,
                    markerfacecolor="white" if marker == "o" else colors[index], label=NAMES.get(model, model) if marker == "o" else None)
        ax.set_xscale("log")
        ax.set_xlabel("Nominal uncached cost per answer (USD, log scale)")
        activation = [r for r in data["activation"] if r["context"] == context]
        noops = sum(r["identical_payload_pairs"] for r in activation)
        pairs = sum(r["questions"] for r in activation)
        ax.set_title(context.capitalize() + f" context · {noops}/{pairs} unchanged pairs", loc="left", fontweight="bold", fontsize=11)
        ax.grid(axis="y", color="#d9e1e2", lw=.6)
    axes[0].set_ylabel("Answer token F1 (%)")
    axes[0].legend(frameon=False, fontsize=8, loc="lower center")
    fig.suptitle("Model × rewrite opportunity  |  ○ Full prompt    ■ ContextCompress", fontsize=13)
    question_counts = sorted({r["questions"] for r in data["activation"]})
    sample_label = str(question_counts[0]) if len(question_counts) == 1 else "/".join(map(str, question_counts))
    fig.supxlabel(f"{sample_label} shared questions per context × {len(models)} models · descriptive 95% intervals\nUnchanged prompts measure repeat variability · no setup or deployment-cost claim", fontsize=9)
    save(fig, "01-model-rewrite-opportunity")

    fig, axes = plt.subplots(2, 2, figsize=(13, 9), layout="constrained", sharex="col")
    kind_color = {"reference": "#859398", "static": "#a56636", "routing": colors[1], "rewrite": colors[0], "joint": colors[3]}
    for row, context in enumerate(CONTEXTS):
        records = [r for r in data["policies"] if r["context"] == context]
        for y, record in enumerate(records):
            c = kind_color[record["kind"]]
            for col, field, bounds in ((0, "net_cost_reduction", "net_cost_reduction_95"), (1, "f1_delta", "quality_delta_95")):
                value = 100*record[field]
                lo, hi = [100*v for v in record["intervals"][bounds]]
                if col == 0:
                    axes[row, col].barh(y, value, color=c, alpha=.22, height=.6)
                axes[row, col].errorbar(value, y, xerr=[[max(0, value-lo)], [max(0, hi-value)]], fmt="o", color=c, capsize=2, markersize=4)
        for col, ax in enumerate(axes[row]):
            ax.axvline(0, color="#667a7e", lw=.8)
            ax.set_yticks(range(len(records)), [r["name"] for r in records] if col == 0 else [])
            # Bar extents and point-only autoscaling differ; identical explicit
            # row limits keep every quality interval aligned with its method.
            ax.set_ylim(len(records) - .5, -.5)
            ax.grid(axis="x", color="#e3e8e9", lw=.6)
            ax.set_title(context.capitalize() + (" · net savings" if col == 0 else " · quality change"), loc="left", fontweight="bold")
        axes[row, 1].axvline(-2, color="#a56636", lw=.8, ls=":")
    axes[1, 0].set_xlabel("Setup-inclusive nominal cost reduction vs original (%)")
    axes[1, 1].set_xlabel("Held-out F1 change vs original (percentage points)")
    fig.suptitle("Does selection earn back its cost?  |  ContextCompress-only ablation", fontsize=14)
    fig.supxlabel("20 / 160 = candidate-call budget · all profiling and comparison calls charged · dotted line = −2 pp reference margin, not a certified bound", fontsize=9)
    save(fig, "02-policy-cost-quality-ablation")

    fig, ax = plt.subplots(figsize=(9, 4.5), layout="constrained")
    labels = []
    for y, record in enumerate(data["interactions"]):
        value = 100*record["difference_in_differences"]
        low, high = [100*x for x in record["paired_bootstrap_95"]]
        color = colors[0] if record["context"] == "natural" else colors[1]
        ax.errorbar(value, y, xerr=[[max(0, value-low)], [max(0, high-value)]], fmt="o", color=color, capsize=3, markersize=6)
        labels.append(NAMES.get(record["model"], record["model"]) + " · " + record["context"])
    ax.set_yticks(range(len(labels)), labels)
    ax.invert_yaxis()
    ax.axvline(0, color="#667a7e", lw=1)
    ax.grid(axis="x", color="#d9e1e2", lw=.6)
    ax.set_xlabel("Model-specific compression effect minus Sonnet's effect (F1 percentage points)")
    ax.set_title("Does the same rewrite behave differently across models?", loc="left", fontweight="bold")
    fig.supxlabel("Four paired outcomes · unadjusted 95% intervals\nIdentical-payload arms measure repeat variability, not an exercised rewrite (see opportunity panel counts)", fontsize=9)
    save(fig, "03-model-rewrite-interactions")
    return {"matplotlib_version": matplotlib.__version__, "formats": ["svg", "png"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "natural", "extended", "replay", "mechanisms", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads((args.artifacts / "manifest.json").read_text())
        if manifest["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}:
            raise PilotError("figure fixtures differ from frozen acquisition")
        rows = validate_matrix(manifest, json.loads((args.artifacts / "results.json").read_text()), load_tasks(args.natural, args.extended), calibration_only=False)
        lock = json.loads((args.artifacts / "static_calibration_lock.json").read_text())
        replay, mechanisms = json.loads(args.replay.read_text()), json.loads(args.mechanisms.read_text())
        data = build_data(manifest, rows, lock, replay, mechanisms)
        write_json(args.output / "plot_data.json", data, immutable=True)
        rendering = render(data, args.output)
        write_json(args.output / "rendering.json", {**rendering, "paper_evidence": False, "plot_data_sha256": digest(data),
            "files": {p.name: file_hash(p) for p in sorted(args.output.iterdir()) if p.suffix in (".png", ".svg")}}, immutable=True)
        print(json.dumps({"output": str(args.output), "figures": 3, "paper_evidence": False}))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Figure generation stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
