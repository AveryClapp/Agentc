"""No-network tests for selected-only feedback and exact-payload replay."""
import json
import unittest
from unittest.mock import Mock

from bench.openrouter_frontier import SOURCE_MODEL, make_call, policy_specs
from bench.openrouter_matrix import MODELS, PilotError
from bench.openrouter_pilot import digest
from bench.openrouter_replay import OutcomeTable, lexical_divergence, payload_for, public_task, replay_policy, shadow_sample
from tests.test_openrouter_matrix import task


class ReplayTests(unittest.TestCase):
    def test_lexical_metric_matches_production_default(self):
        self.assertEqual(lexical_divergence("", ""), 0)
        self.assertEqual(lexical_divergence("Paris", "paris"), 1)
        self.assertEqual(lexical_divergence("not Paris", "Paris"), 0.5)
        self.assertAlmostEqual(lexical_divergence("a a b", "b c"), 2 / 3)

    def test_shadow_decisions_are_outcome_independent_and_bounded(self):
        self.assertFalse(shadow_sample("s", "natural", "t", 0))
        self.assertTrue(shadow_sample("s", "natural", "t", 1))
        self.assertEqual(shadow_sample("s", "natural", "t", .02), shadow_sample("s", "natural", "t", .02))
        with self.assertRaises(PilotError):
            shadow_sample("s", "n", "t", float("nan"))

    def test_public_task_removes_expected_and_support_labels(self):
        t = task(0)
        t["meta"]["paragraphs"][0]["supporting"] = True
        public = public_task(t)
        self.assertNotIn("expected", public)
        self.assertNotIn("supporting", public["meta"]["paragraphs"][0])

    def setup_replay(self):
        models = [SOURCE_MODEL, MODELS[1][0]]
        manifest = {"catalog": {}, "endpoints": {m: {"tag": "provider"} for m in models},
                    "policy_replay": {"shadow_seed": "frozen"}}
        attention = Mock()
        attention.compute_attention_scores.return_value = ([0, 0, 1], ["question"])
        tasks = {"natural": {str(i): task(i) for i in range(2)}}
        rows = []
        for i, phase in enumerate(("calibration", "holdout")):
            for model in models:
                item = {"context": "natural", "model": model, "arm": "full", "phase": phase, "task_id": str(i)}
                call = make_call(tasks["natural"][str(i)], item, attention)
                rows.append({**item, "id": f"{i}-{model}", "request_sha256": digest(payload_for(call, manifest)),
                             "answer": "yes" if model == SOURCE_MODEL else "no", "expected": "gold secret", "em": 0, "f1": 0,
                             "usage": {"prompt_tokens": 100, "completion_tokens": 1}, "latency_ms": 10,
                             "cost_usd": "0.01", "nominal_uncached_cost_usd": "0.02"})
        native = Mock()
        native.optimize_model_catalog.return_value = "{}"
        native.optimize_observe.return_value = "opaque-observation"
        native.optimize_complete_exploration.return_value = True
        calls = []
        def plan(encoded):
            call = json.loads(encoded)
            self.assertNotIn("gold secret", encoded)
            calls.append(call)
            selected = {**call, "model": models[1]}
            candidate = {"kind": "rewritten", "call": selected, "rule": "ModelDowngrade", "agentc_observation_context": {"identity": "candidate"}}
            if len(calls) == 1:
                return json.dumps({"kind": "pass_through", "agentc_observation_context": {"identity": "reference"},
                                   "agentc_exploration_context": {"lease_token": "opaque-lease", "candidate_plan": candidate}})
            return json.dumps(candidate)
        native.optimize_plan.side_effect = plan
        policy = policy_specs()[0]
        policy["settings"]["AGENTC_OPTIMIZE_SHADOW"] = "1"
        return native, attention, manifest, rows, tasks, policy

    def test_native_gets_primary_then_leased_feedback_and_sampled_shadow_only(self):
        native, attention, manifest, rows, tasks, policy = self.setup_replay()
        result = replay_policy(native, attention, manifest, rows, tasks, policy, "natural", restart_after_calibration=True)
        self.assertEqual(native.optimize_observe.call_count, 2)  # no separate candidate observation
        self.assertEqual(native.optimize_complete_exploration.call_count, 1)
        self.assertEqual(native.optimize_record_divergence.call_count, 1)
        self.assertEqual([r["scope"] for d in result["decisions"] for r in d["revealed"]],
                         ["primary", "exploration", "primary", "shadow"])
        self.assertEqual(result["revealed_calls"], 4)
        self.assertTrue(result["restart_performed"])
        self.assertEqual(native.optimize_configure.call_count, 2)
        for observed in native.optimize_observe.call_args_list:
            self.assertNotIn("expected", json.loads(observed.args[1]))
            self.assertNotIn("f1", json.loads(observed.args[1]))
        self.assertEqual(result["decisions"][0]["observed_billed_cost_noncausal_usd"], "0.02")

    def test_table_hides_gold_and_uses_full_sample_for_identical_noop_requests(self):
        native, attention, manifest, rows, tasks, policy = self.setup_replay()
        original = rows[0]
        duplicate = {**original, "id": "earlier", "arm": "compress", "answer": "different", "cost_usd": "0.00001"}
        table = OutcomeTable([duplicate, *rows])
        call = make_call(tasks["natural"]["0"], {**original, "arm": "full"}, attention)
        view = table.reveal("natural", "0", payload_for(call, manifest), "primary")
        self.assertEqual(view["id"], original["id"])
        self.assertFalse({"expected", "em", "f1"} & view.keys())
        with self.assertRaises(PilotError):
            table.reveal("natural", "0", {"unmeasured": 1}, "primary")

    def test_missing_counterfactual_fails_lease_without_synthesizing_evidence(self):
        native, attention, manifest, rows, tasks, policy = self.setup_replay()
        rows = [r for r in rows if r["model"] == SOURCE_MODEL]
        with self.assertRaises(PilotError):
            replay_policy(native, attention, manifest, rows, tasks, policy, "natural")
        native.optimize_fail_exploration.assert_called_once_with("opaque-lease")
        native.optimize_complete_exploration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
