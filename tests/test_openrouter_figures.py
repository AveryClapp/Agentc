from copy import deepcopy

import pytest

from bench.openrouter_figures import conditional_intervals, policy_data, render
from bench.openrouter_frontier import CONTEXTS, SOURCE_MODEL
from bench.openrouter_pilot import PilotError


def test_intervals_include_setup_cost_not_just_serving_savings():
    reference = [{"task_id": str(i), "f1": 1., "cost": 1.} for i in range(2)]
    treatment = [{"task_id": str(i), "f1": .5, "cost": .5} for i in range(2)]
    report = conditional_intervals(reference, treatment, 1., 3., "test", draws=40)
    assert report["quality_delta_95"] == [-.5, -.5]
    assert report["net_cost_reduction_95"] == pytest.approx([-1/3, -1/3])
    treatment.reverse()
    with pytest.raises(PilotError, match="pairs"):
        conditional_intervals(reference, treatment, 1, 1, "test")
    with pytest.raises(PilotError, match="draws"):
        conditional_intervals(reference, reference, 1, 1, "test", draws=0)


def test_native_cost_counts_exploration_and_static_counts_all_training_arms():
    rows, controls, trajectories = [], [], []
    for context in CONTEXTS:
        for phase in ("warmup", "calibration", "holdout"):
            for model in (SOURCE_MODEL, "cheap"):
                for arm in ("full", "compress"):
                    if phase == "warmup" and arm == "compress":
                        continue
                    rows.append({"id": f"{context}/{phase}/{model}/{arm}", "task_id": phase, "context": context,
                        "phase": phase, "model": model, "arm": arm, "f1": 1., "nominal_uncached_cost_usd": "1" if model == SOURCE_MODEL else ".5"})
        controls.append({"context": context, "name": "calibrated_fixed_model", "selected": {"model": "cheap", "arm": "full"},
            "candidates": [{"model": SOURCE_MODEL, "arm": "full"}, {"model": "cheap", "arm": "full"}]})
        trajectories.append({"context": context, "policy": "joint_default_budget", "decisions": [
            {"task_id": phase, "phase": phase, "primary_row_id": f"{context}/{phase}/{SOURCE_MODEL}/full",
             "nominal_uncached_cost_estimate_usd": "1.25"} for phase in ("warmup", "calibration", "holdout")]})
    data = policy_data({}, rows, {"controls": controls}, {"trajectories": trajectories})
    for context in CONTEXTS:
        group = {r["name"]: r for r in data if r["context"] == context}
        assert group["Original"]["baseline_total_nominal_usd"] == 3
        assert group["CC · legacy greedy"]["policy_total_nominal_usd"] == 3
        assert group["Calibrated fixed model"]["policy_setup_nominal_usd"] == 2.5
        assert group["Calibrated fixed model"]["policy_total_nominal_usd"] == 3
        assert group["Joint model+CC · 20"]["policy_total_nominal_usd"] == 3.75
        assert group["Joint model+CC · 20"]["net_cost_reduction"] == -.25


def test_render_exports_all_numeric_panels_and_refuses_overwrite(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    from matplotlib.figure import Figure
    savefig = Figure.savefig
    row_limits = []
    def checked_savefig(figure, path, *args, **kwargs):
        if path.name == "02-policy-cost-quality-ablation.svg":
            row_limits.extend(ax.get_ylim() for ax in figure.axes)
        return savefig(figure, path, *args, **kwargs)
    monkeypatch.setattr(Figure, "savefig", checked_savefig)
    models = [SOURCE_MODEL, "anthropic/claude-haiku-4.5", "google/gemini-2.5-flash-lite", "qwen/qwen3-30b-a3b-instruct-2507"]
    data = {"opportunity": [], "policies": [], "interactions": []}
    for context in CONTEXTS:
        for i, model in enumerate(models):
            for arm in ("full", "compress"):
                data["opportunity"].append({"context": context, "model": model, "arm": arm,
                    "mean_f1": .7 + .01*i, "mean_f1_95": [.65, .78], "mean_nominal_uncached_usd": .001*(i+1)})
            if model != SOURCE_MODEL:
                data["interactions"].append({"context": context, "model": model, "difference_in_differences": .01,
                    "paired_bootstrap_95": [-.03, .05]})
        for name, kind, delta in (("Original", "reference", 0), ("CC", "rewrite", .1), ("Joint", "joint", -.2)):
            data["policies"].append({"context": context, "name": name, "kind": kind, "net_cost_reduction": delta,
                "f1_delta": 0, "intervals": {"net_cost_reduction_95": [delta-.02, delta+.02], "quality_delta_95": [-.02, .02]}})
    render(deepcopy(data), tmp_path)
    assert len(list(tmp_path.glob("*.svg"))) == 3
    assert len(list(tmp_path.glob("*.png"))) == 3
    assert all(p.stat().st_size > 1000 for p in tmp_path.iterdir())
    assert row_limits == [(2.5, -.5)] * 4
    with pytest.raises(PilotError, match="overwrite"):
        render(data, tmp_path)
