"""Static controls cannot select on heldout quality; comparisons preserve pairs."""
from copy import deepcopy

import pytest

from bench.openrouter_frontier import CONTEXTS, SOURCE_MODEL
from bench.openrouter_frontier_analysis import analyze, calibrate, pair_summary, paired_interval
from bench.openrouter_pilot import PilotError


def matrix():
    manifest = {"endpoints": {SOURCE_MODEL: {}, "cheap": {}}, "calibration_tasks": 2,
                "policy_replay": {"risk_margin": .02}, "limitations": []}
    rows = []
    for context in CONTEXTS:
        for phase, n in (("warmup", 3), ("calibration", 2), ("holdout", 2)):
            for i in range(n):
                for model in manifest["endpoints"]:
                    for arm in (("full",) if phase == "warmup" else ("full", "compress")):
                        # Calibration selects cheap/full; a cheaper rewrite fails quality.
                        quality = .5 if model == "cheap" and arm == "compress" else 1.
                        price = "1" if model == SOURCE_MODEL else ("0.2" if arm == "full" else "0.1")
                        rows.append({"id": f"{context}/{phase}/{i}/{model}/{arm}", "context": context,
                            "phase": phase, "task_id": f"{phase}-{i}", "model": model, "arm": arm,
                            "f1": quality, "em": float(quality == 1), "cost_usd": price,
                            "nominal_uncached_cost_usd": price, "usage": {"prompt_tokens": 10},
                            "native_plan": {"kind": "rewritten" if arm == "compress" else "pass_through"},
                            "request_sha256": f"{context}/{phase}/{i}/{model}/{arm}"})
    return manifest, rows


def test_static_lock_ignores_heldout_and_observed_cache_prices():
    manifest, rows = matrix()
    lock = calibrate(manifest, rows)
    assert all(c["selected"]["model"] == "cheap" and c["selected"]["arm"] == "full" for c in lock["controls"])
    changed = deepcopy(rows)
    for row in changed:
        if row["phase"] == "holdout":
            row["f1"] = 0
            row["em"] = 0
    assert calibrate(manifest, changed) == lock
    for row in changed:
        row["cost_usd"] = "1000" if row["model"] == "cheap" else "0.0001"
    assert [c["selected"] for c in calibrate(manifest, changed)["controls"]] == [c["selected"] for c in lock["controls"]]


def test_static_controls_charge_all_calibration_candidates_and_preserve_losses():
    manifest, rows = matrix()
    lock = calibrate(manifest, rows)
    for row in rows:
        if row["phase"] == "holdout" and row["model"] == "cheap":
            row.update(f1=0., em=0.)
    report = analyze(manifest, rows, lock)
    for control in report["calibration_selected_controls"]:
        expected_setup = 7 if control["name"] == "calibrated_fixed_model" else 11
        assert control["setup_calls"] == expected_setup
        assert float(control["total_nominal_uncached_cost_estimate_usd"]) == pytest.approx(5.8 if expected_setup == 7 else 8.)
        assert control["f1_delta"] == -1
        assert control["strict_em_losses"] == 2
        assert control["selected"]["model"] == "cheap"


def test_changed_calibration_lock_and_incomplete_cells_rejected():
    manifest, rows = matrix()
    lock = calibrate(manifest, rows)
    changed = deepcopy(lock)
    changed["controls"][0]["selected"]["model"] = SOURCE_MODEL
    with pytest.raises(PilotError, match="lock changed"):
        analyze(manifest, rows, changed)
    with pytest.raises(PilotError, match="complete calibration"):
        calibrate(manifest, [r for r in rows if r["phase"] != "calibration" or r["task_id"] != "calibration-0"])


def test_question_pairs_not_unpaired_rows_and_intervals_reproducible():
    _, rows = matrix()
    full = [r for r in rows if r["phase"] == "holdout" and r["context"] == CONTEXTS[0] and r["model"] == SOURCE_MODEL and r["arm"] == "full"]
    treatment = deepcopy(full)
    for row in treatment:
        row["nominal_uncached_cost_usd"] = "0.5"
    interval = paired_interval(full, treatment, seed="test", draws=50)
    assert interval["quality_f1_delta_95"] == [0., 0.]
    assert interval["nominal_uncached_cost_reduction_95"] == [.5, .5]
    assert paired_interval(full, treatment, seed="test", draws=50) == interval
    with pytest.raises(PilotError, match="question-matched"):
        paired_interval(full, list(reversed(treatment)), seed="test")


def test_net_mean_does_not_hide_individual_harm():
    _, rows = matrix()
    full = [r for r in rows if r["phase"] == "holdout" and r["context"] == CONTEXTS[0] and r["model"] == SOURCE_MODEL and r["arm"] == "full"]
    full[0].update(f1=0., em=0.)
    treatment = deepcopy(full)
    treatment[0].update(f1=1., em=1.)
    treatment[1].update(f1=0., em=0.)
    report = pair_summary(full, treatment, "test")
    assert report["f1_delta"] == 0
    assert report["any_f1_loss_count"] == 1
    assert report["mean_positive_f1_loss"] == .5
    assert report["strict_em_losses"] == report["strict_em_gains"] == 1
