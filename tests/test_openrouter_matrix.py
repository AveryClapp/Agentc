"""No-network checks for the frozen factorial pilot protocol."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bench.openrouter_matrix import (
    MODELS, PilotError, analyze, make_schedule, messages_for, native_call, score, write_json,
)


def task(index):
    return {"task_id": str(index), "prompt": f"Question {index}?", "expected": "gold secret",
            "meta": {"paragraphs": [{"title": "Public", "sentences": ["A supplied passage."]}]}}


class MatrixTests(unittest.TestCase):
    def test_schedule_is_balanced_disjoint_and_label_independent(self):
        tasks = [task(index) for index in range(24)]
        schedule = make_schedule(tasks, 12, 8)
        self.assertEqual(len(schedule), 12 + 20 * 8)
        phases = {phase: {r["task_id"] for r in schedule if r["phase"] == phase}
                  for phase in ("warmup", "calibration", "holdout")}
        self.assertEqual([len(v) for v in phases.values()], [3, 8, 12])
        self.assertFalse(phases["warmup"] & phases["calibration"])
        self.assertFalse(phases["holdout"] & phases["calibration"])
        self.assertFalse(phases["warmup"] & phases["holdout"])
        for identity in phases["calibration"] | phases["holdout"]:
            arms = {(r["model"], r["arm"]) for r in schedule if r["task_id"] == identity}
            self.assertEqual(arms, {(model, arm) for model, _ in MODELS for arm in ("full", "compress")})
        for value in tasks:
            value["expected"] = "different label"
        self.assertEqual(make_schedule(list(reversed(tasks)), 12, 8), schedule)

    def test_duplicate_and_insufficient_fixtures_rejected(self):
        for tasks in ([task(0)], [task(0)] * 30):
            with self.assertRaises(PilotError):
                make_schedule(tasks, 12, 8)

    def test_exact_match_does_not_accept_gold_substring(self):
        self.assertEqual(score("The Eiffel Tower.", "Eiffel Tower")["em"], 1)
        self.assertEqual(score("not Eiffel Tower", "Eiffel Tower")["em"], 0)
        self.assertEqual(score("yes", "no"), {"em": 0, "f1": 0})
        self.assertAlmostEqual(score("red blue", "red green")["f1"], 0.5)

    def test_question_is_last_and_labels_do_not_enter_call(self):
        item = {"model": MODELS[0][0], "arm": "compress"}
        class Attention:
            def compute_attention_scores(self, messages, trace):
                return [0.0] * (len(messages) - 1) + [1.0], ["question"]
        value = task(1)
        call = native_call(value, item, Attention())
        self.assertEqual(call["messages"], messages_for(value))
        self.assertEqual(call["messages"][-1]["content"], "Question: Question 1?")
        self.assertNotIn("gold secret", str(call))
        self.assertEqual(call["parameters"]["extra"]["agentc_route_context"]["provider_namespace"], "openrouter")

    def test_frozen_artifact_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            write_json(path, {"frozen": 1}, immutable=True)
            write_json(path, {"frozen": 1}, immutable=True)
            with self.assertRaises(PilotError):
                write_json(path, {"frozen": 2}, immutable=True)

    def test_empty_summary_never_claims_evidence(self):
        summary = analyze([], {"schedule": [1], "limitations": ["exploratory"]})
        self.assertFalse(summary["paper_evidence"])
        self.assertEqual(summary["cost_usd"], "0")
        self.assertEqual(summary["completed_calls"], 0)


if __name__ == "__main__":
    unittest.main()
