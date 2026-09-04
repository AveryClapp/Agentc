"""No-network paired analysis and artifact integrity regression tests."""
from __future__ import annotations

import copy
import unittest

from bench.openrouter_analysis import analyze, gold_tokens_present, validate, wilson
from bench.openrouter_matrix import score
from bench.openrouter_pilot import PilotError, digest


def fixture():
    tasks, schedule = [], []
    for i, phase in enumerate(["warmup", "calibration"] + ["holdout"] * 4):
        tasks.append({"task_id": str(i), "expected": "gold"})
        for arm in (["full"] if phase == "warmup" else ["full", "compress"]):
            schedule.append({"task_id": str(i), "phase": phase, "model": "model",
                             "provider_tag": "tag", "arm": arm})
    manifest = {"paper_evidence": False, "kind": "exploratory_factorial", "schedule": schedule,
                "warmup_tasks": 1, "calibration_tasks": 1, "holdout_tasks": 4,
                "endpoints": {"model": {"tag": "tag", "provider_name": "Provider"}},
                "limitations": ["exploratory"]}
    stage = "matrix-v1-" + digest(manifest)[:20]
    rows = []
    # All four paired outcomes: pass/pass, pass/fail, fail/pass, fail/fail.
    successes = {("2", "full"), ("2", "compress"), ("3", "full"), ("4", "compress")}
    for i, item in enumerate(schedule):
        answer = "gold" if (item["task_id"], item["arm"]) in successes else "not gold"
        cost = "0.01" if item["arm"] == "full" else "0.006"
        rows.append({**item, "id": stage + f"-{i:04d}", "stage": stage, "generation_id": f"gen-{i}",
                     "provider": "Provider", "paper_evidence": False,
                     "answer": answer, "expected": "gold", **score(answer, "gold"),
                     "cost_usd": cost, "usage": {"cost": cost, "prompt_tokens": 100 if item["arm"] == "full" else 60,
                                                "completion_tokens": 3},
                     "finish_reason": "stop",
                     "native_plan": {"kind": "pass_through" if item["arm"] == "full" else "rewritten"}})
    return manifest, rows, tasks


class AnalysisTests(unittest.TestCase):
    def test_paired_transitions_and_all_billed_phases(self):
        manifest, rows, tasks = fixture()
        before = copy.deepcopy(rows)
        report = analyze(manifest, rows, tasks)
        holdout = next(r for r in report["paired"] if r["phase"] == "holdout")
        self.assertEqual(holdout["strict_em_transitions"], {"both_pass": 1, "loss": 1, "gain": 1, "both_fail": 1})
        self.assertEqual(holdout["loss_task_ids"], ["3"])
        self.assertEqual(holdout["gain_task_ids"], ["4"])
        self.assertEqual(holdout["strict_em_delta"], 0)
        self.assertAlmostEqual(holdout["input_token_reduction"], 0.4)
        self.assertAlmostEqual(holdout["cost_reduction"], 0.4)
        self.assertEqual(report["phase_cost_usd"], {"warmup": "0.01", "calibration": "0.016", "holdout": "0.064"})
        self.assertEqual(report["total_matrix_cost_usd"], "0.090")
        self.assertEqual(holdout["full"]["nonexact_with_gold_tokens_diagnostic_only"], 2)
        self.assertFalse(report["paper_evidence"])
        self.assertTrue(report["post_hoc_analysis"])
        self.assertEqual(rows, before)

    def test_no_baseline_success_means_undefined_not_zero_risk(self):
        report = analyze(*fixture())
        calibration = next(r for r in report["paired"] if r["phase"] == "calibration")
        self.assertIsNone(calibration["loss_given_full_pass_wilson_95"])

    def test_wilson_small_samples_do_not_certify_safety(self):
        self.assertIsNone(wilson(0, 0))
        self.assertAlmostEqual(wilson(0, 12)[1], 0.242494006655, places=9)
        self.assertAlmostEqual(wilson(1, 2)[0], 0.094531205734, places=9)
        with self.assertRaises(PilotError):
            wilson(3, 2)

    def test_gold_presence_is_only_a_token_boundary_diagnostic(self):
        self.assertTrue(gold_tokens_present("not The Eiffel Tower", "Eiffel Tower"))
        self.assertFalse(gold_tokens_present("cart", "art"))
        self.assertFalse(gold_tokens_present("anything", ""))
        self.assertFalse(gold_tokens_present("alpha gamma beta", "alpha beta"))

    def test_partial_duplicate_or_reordered_rows_rejected(self):
        for mutation in (lambda r: r.pop(), lambda r: r.append(copy.deepcopy(r[0])),
                         lambda r: r.reverse()):
            manifest, rows, tasks = fixture()
            mutation(rows)
            with self.assertRaises(PilotError):
                validate(manifest, rows, tasks)

    def test_modified_scores_labels_costs_or_attribution_rejected(self):
        for field, value in [("em", 1), ("f1", float("nan")), ("expected", "new gold"),
                             ("provider", "Wrong"), ("provider_tag", "wrong"),
                             ("stage", "wrong"), ("id", "wrong"), ("cost_usd", "99"),
                             ("generation_id", "gen-1"), ("paper_evidence", True),
                             ("native_plan", {"kind": "rewritten"})]:
            with self.subTest(field=field):
                manifest, rows, tasks = fixture()
                rows[0][field] = value
                with self.assertRaises(PilotError):
                    validate(manifest, rows, tasks)

    def test_invalid_token_accounting_rejected(self):
        for value in (-1, True, 1.5, "1"):
            manifest, rows, tasks = fixture()
            rows[0]["usage"]["prompt_tokens"] = value
            with self.assertRaises(PilotError):
                validate(manifest, rows, tasks)

    def test_fixture_duplicate_or_missing_identity_rejected(self):
        for mutation in (lambda t: t.append(t[0]), lambda t: t.pop()):
            manifest, rows, tasks = fixture()
            mutation(tasks)
            with self.assertRaises(PilotError):
                validate(manifest, rows, tasks)

    def test_phase_count_mismatch_and_cross_phase_overlap_rejected(self):
        for overlap in (False, True):
            manifest, rows, tasks = fixture()
            if overlap:
                manifest["schedule"][1]["task_id"] = "0"
                rows[1]["task_id"] = "0"
                manifest["schedule"][2]["task_id"] = "0"
                rows[2]["task_id"] = "0"
            else:
                manifest["holdout_tasks"] = 3
            stage = "matrix-v1-" + digest(manifest)[:20]
            for i, row in enumerate(rows):
                row.update(stage=stage, id=stage + f"-{i:04d}")
            with self.assertRaises(PilotError):
                validate(manifest, rows, tasks)


if __name__ == "__main__":
    unittest.main()
