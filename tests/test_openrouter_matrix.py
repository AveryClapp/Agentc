"""No-network checks for the frozen factorial pilot protocol."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bench.openrouter_matrix import (
    MODELS, PilotError, analyze, make_schedule, messages_for, native_call, run, score, write_json,
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


@contextmanager
def fake_run(*, real_ledger=False):
    """Exercise actual run/checkpoint logic with no native extension or network."""
    with ExitStack() as stack:
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        args = SimpleNamespace(output=directory / "output", fixture=directory / "fixture.json",
                               native=directory / "native", ledger=directory / "ledger", max_calls=None)
        tasks = [task(0), task(1)]
        schedule = [{"task_id": t["task_id"], "phase": "warmup", "model": MODELS[0][0],
                     "provider_tag": MODELS[0][1], "arm": "full"} for t in tasks]
        manifest = {"source_files": {}, "native_sha256": "hash", "fixture_sha256": "hash",
                    "settings": {}, "catalog": {}, "stage_cap_usd": "5", "schedule": schedule,
                    "limitations": [], "endpoints": {MODELS[0][0]: {"provider_name": "Provider", "name": "Provider | model"}}}
        write_json(args.fixture, tasks)
        write_json(args.output / "manifest.json", manifest)
        native = Mock()
        native.optimize_model_catalog.return_value = "{}"
        native.optimize_observe.return_value = "observation-token"
        plan = {"kind": "pass_through", "agentc_observation_context": {"identity": "fixed"}, "diagnostic_ms": 1}
        native.optimize_plan.side_effect = lambda _: json.dumps(plan)
        attention = Mock()
        attention.compute_attention_scores.return_value = ([0, 0, 1], ["question"])
        ledger = Mock()
        def result(key, call_id, stage, cap, payload, metadata):
            return {"id": call_id, "stage": stage, "model": payload["model"], "provider": "Provider",
                    "answer": "gold secret", "cost_usd": "0.01", "latency_ms": 1,
                    "usage": {"prompt_tokens": 100, "completion_tokens": 2, "cost": "0.01"},
                    "finish_reason": "stop", "paper_evidence": False}
        ledger.call.side_effect = result
        ledger.summary.return_value = {}
        if real_ledger:
            stack.enter_context(patch("bench.openrouter_pilot.account", return_value={"usage": "0"}))
            def response(path, key, payload):
                return {"id": "provider-generation", "model": payload["model"], "provider": "Provider",
                        "choices": [{"message": {"content": "gold secret"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 2, "cost": "0.0001"},
                        "openrouter_metadata": {"requested": payload["model"], "attempt": 1, "is_byok": False,
                            "endpoints": {"available": [{"provider": "Provider", "model": "model", "selected": True}]}}}
            transport = stack.enter_context(patch("bench.openrouter_pilot.request_json", side_effect=response))
            args.test_transport = transport
        else:
            stack.enter_context(patch("bench.openrouter_matrix.Ledger", return_value=ledger))
        stack.enter_context(patch("bench.openrouter_matrix.source_hashes", return_value={}))
        stack.enter_context(patch("bench.openrouter_matrix.file_hash", return_value="hash"))
        stack.enter_context(patch("bench.openrouter_matrix.load_module",
                                  side_effect=lambda name, path, **kwargs: native if kwargs.get("native") else attention))
        stack.enter_context(redirect_stdout(StringIO()))
        yield args, native, plan


class ResumeEvidenceTests(unittest.TestCase):
    def test_provider_error_stops_before_native_observation_and_downstream_dispatch(self):
        with fake_run(real_ledger=True) as (args, native, plan):
            original = args.test_transport.side_effect
            def failed_response(*call_args):
                value = original(*call_args)
                value["choices"][0].update(finish_reason="error", error={"code": 503})
                value["usage"]["cost"] = 0
                return value
            args.test_transport.side_effect = failed_response
            with self.assertRaisesRegex(PilotError, "did not complete"):
                run(args, "fake-key")
            native.optimize_observe.assert_not_called()
            self.assertEqual(args.test_transport.call_count, 1)
            events = [json.loads(line) for line in args.ledger.read_text().splitlines()]
            self.assertEqual([e["event"] for e in events], ["origin", "reserve", "response"])

    def test_complete_replay_repairs_summary_after_interrupted_checkpoint(self):
        with fake_run(real_ledger=True) as (args, native, plan):
            def interrupted_write(path, value, **kwargs):
                if path.name == "summary.json" and value["completed_calls"] == 2:
                    raise OSError("interrupted between result and summary replacement")
                write_json(path, value, **kwargs)
            with patch("bench.openrouter_matrix.write_json", side_effect=interrupted_write):
                with self.assertRaises(OSError):
                    run(args, "fake-key")
            before = (args.output / "results.json").read_bytes()
            self.assertEqual(len(json.loads(before)), 2)
            self.assertEqual(json.loads((args.output / "summary.json").read_text())["completed_calls"], 1)
            run(args, "fake-key")
            self.assertEqual((args.output / "results.json").read_bytes(), before)
            self.assertEqual(json.loads((args.output / "summary.json").read_text())["completed_calls"], 2)
            self.assertEqual(args.test_transport.call_count, 2)

    def test_real_ledger_fresh_and_cached_rows_preserve_identical_evidence(self):
        with fake_run(real_ledger=True) as (args, native, plan):
            args.max_calls = 1
            run(args, "fake-key")
            original = json.loads((args.output / "results.json").read_text())[0]
            self.assertNotIn("at", original)
            self.assertEqual(args.test_transport.call_count, 1)
            args.max_calls = None
            run(args, "fake-key")
            rows = json.loads((args.output / "results.json").read_text())
            self.assertEqual(rows[0], original)
            self.assertEqual(len(rows), 2)
            self.assertEqual(args.test_transport.call_count, 2)
            before = (args.output / "results.json").read_bytes()
            args.max_calls = 1
            run(args, "fake-key")
            self.assertEqual((args.output / "results.json").read_bytes(), before)
            self.assertEqual(args.test_transport.call_count, 2)

    def test_legacy_ledger_timestamp_is_preserved_not_compared_as_evidence(self):
        with fake_run(real_ledger=True) as (args, native, plan):
            args.max_calls = 1
            run(args, "fake-key")
            rows = json.loads((args.output / "results.json").read_text())
            rows[0]["at"] = "legacy-ledger-insertion-time"
            write_json(args.output / "results.json", rows)
            args.max_calls = None
            run(args, "fake-key")
            extended = json.loads((args.output / "results.json").read_text())
            self.assertEqual(extended[0], rows[0])
            self.assertEqual(args.test_transport.call_count, 2)

    def test_short_replay_does_not_truncate_longer_artifacts(self):
        with fake_run() as (args, native, plan):
            run(args, "fake-key")
            before = {name: (args.output / name).read_bytes() for name in ("results.json", "summary.json")}
            args.max_calls = 1
            self.assertEqual(run(args, "fake-key")["completed_calls"], 1)
            for name, content in before.items():
                self.assertEqual((args.output / name).read_bytes(), content)

    def test_interrupted_replay_preserves_complete_artifact(self):
        with fake_run() as (args, native, plan):
            run(args, "fake-key")
            before = (args.output / "results.json").read_bytes()
            native.optimize_plan.side_effect = [json.dumps(plan), PilotError("interrupted")]
            with self.assertRaises(PilotError):
                run(args, "fake-key")
            self.assertEqual((args.output / "results.json").read_bytes(), before)

    def test_extension_retains_original_prefix_plans(self):
        with fake_run() as (args, native, plan):
            args.max_calls = 1
            run(args, "fake-key")
            original = json.loads((args.output / "results.json").read_text())[0]
            plan["diagnostic_ms"] = 99
            args.max_calls = None
            run(args, "fake-key")
            rows = json.loads((args.output / "results.json").read_text())
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0], original)
            self.assertEqual(rows[1]["native_plan"]["diagnostic_ms"], 99)

    def test_changed_plan_identity_does_not_replace_evidence(self):
        with fake_run() as (args, native, plan):
            run(args, "fake-key")
            before = (args.output / "results.json").read_bytes()
            plan["agentc_observation_context"]["identity"] = "changed"
            with self.assertRaisesRegex(PilotError, "evidence preserved"):
                run(args, "fake-key")
            self.assertEqual((args.output / "results.json").read_bytes(), before)

    def test_wrong_schedule_rejected_before_native_or_provider_calls(self):
        with fake_run() as (args, native, plan):
            run(args, "fake-key")
            rows = json.loads((args.output / "results.json").read_text())
            rows[1]["task_id"] = "unknown"
            write_json(args.output / "results.json", rows)
            native.reset_mock()
            with patch("bench.openrouter_matrix.Ledger") as ledger:
                with self.assertRaises(PilotError):
                    run(args, "fake-key")
                ledger.assert_not_called()
            native.optimize_configure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
