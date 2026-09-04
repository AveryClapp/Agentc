"""No-network checks for the matched context acquisition protocol."""
import copy
import tempfile
import unittest
from pathlib import Path

from bench.openrouter_frontier import CAP, CONTEXTS, load_tasks, make_call, policy_specs, schedule_for, summarize
from bench.openrouter_matrix import MODELS, PilotError, write_json
from bench.openrouter_pilot import digest
from tests.test_openrouter_matrix import task


class FrontierTests(unittest.TestCase):
    def test_schedule_has_matched_conditions_and_disjoint_phases(self):
        excluded = {str(i) for i in range(23)}
        schedule = schedule_for([str(i) for i in range(500)], excluded, 20, 160)
        self.assertEqual(len(schedule), 24 + 20 * 16 + 160 * 16)
        seen = {}
        for row in schedule:
            self.assertNotIn(row["task_id"], excluded)
            self.assertEqual(seen.setdefault(row["task_id"], row["phase"]), row["phase"])
        for task_id, phase in seen.items():
            arms = {(r["context"], r["model"], r["arm"]) for r in schedule if r["task_id"] == task_id}
            expected = {(c, m, a) for c in CONTEXTS for m, _ in MODELS
                        for a in (["full"] if phase == "warmup" else ["full", "compress"])}
            self.assertEqual(arms, expected)
        self.assertEqual(schedule_for(list(reversed([str(i) for i in range(500)])), excluded, 20, 160), schedule)

    def test_inadequate_universe_rejected(self):
        for ids, excluded in [(["x"] * 200, set()), ([str(i) for i in range(183)], {"1"})]:
            with self.assertRaises(PilotError):
                schedule_for(ids, excluded, 20, 160)

    def test_context_pair_retains_original_passages(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            natural = [task(0), task(1)]
            extended = copy.deepcopy(natural)
            for t in extended:
                t["meta"]["paragraphs"].append({"title": "Extra", "sentences": ["Distractor."]})
            write_json(p / "natural.json", natural)
            write_json(p / "extended.json", extended)
            pair = load_tasks(p / "natural.json", p / "extended.json")
            self.assertEqual(set(pair), set(CONTEXTS))
            for mutate in (lambda r: r[0].update(expected="changed"),
                           lambda r: r[0]["meta"]["paragraphs"].pop(0),
                           lambda r: r.pop()):
                changed = copy.deepcopy(extended)
                mutate(changed)
                write_json(p / "extended.json", changed)
                with self.assertRaises(PilotError):
                    load_tasks(p / "natural.json", p / "extended.json")

    def test_call_excludes_gold_and_uses_reinforced_question_for_attention(self):
        class Attention:
            def compute_attention_scores(self, messages, trace):
                self.last = messages[-1]["content"]
                return [0.0] * (len(messages) - 1) + [1.0], ["question"]
        attention = Attention()
        item = {"context": "natural", "model": MODELS[0][0], "arm": "compress"}
        t = task(0)
        call = make_call(t, item, attention)
        self.assertIn("Answer format:", attention.last)
        self.assertTrue(attention.last.endswith("Question: Question 0?"))
        self.assertEqual(call["parameters"]["max_output_tokens"], CAP)
        self.assertEqual(call["input_deps"][-1]["kind"], "user_input")
        self.assertNotIn("gold secret", str(call))
        t["expected"] = "changed gold"
        self.assertEqual(digest(make_call(t, item, attention)), digest(call))
        full = make_call(t, {**item, "arm": "full"}, attention)
        self.assertEqual(full["messages"], call["messages"])
        self.assertEqual(full["parameters"]["extra"]["attention_scores"], [])
        routed = make_call(t, {**item, "model": MODELS[1][0]}, attention, source_model=MODELS[0][0])
        self.assertEqual(routed["call_site_id"], call["call_site_id"])
        self.assertEqual(routed["model"], call["model"])

    def test_policy_ablation_keeps_native_guarded_path(self):
        policies = policy_specs()
        self.assertEqual(len(policies), 6)
        for policy in policies:
            env = policy["settings"]
            self.assertEqual(env["AGENTC_EVAL_PLANNER_MODE"], "joint_guarded")
            self.assertEqual(env["AGENTC_COMPOSE"], "1")
            self.assertEqual(env["AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE"], "20")
            self.assertEqual(env["AGENTC_OPTIMIZE_DIVERGENCE_EXPOSURE_BUDGET"], "1")
            self.assertEqual(env["AGENTC_OPTIMIZE_EXPLORATION_CALLS_PER_SITE_24H"],
                             "160" if "expanded" in policy["name"] else "20")

    def test_summary_separates_paid_and_nominal_cache_cost(self):
        row = {"phase": "calibration", "context": "natural", "model": "m", "arm": "full",
               "em": 1, "f1": 1, "cost_usd": "0.001", "nominal_uncached_cost_usd": "0.01",
               "cached_input_tokens": 900, "usage": {"prompt_tokens": 1000, "completion_tokens": 2},
               "native_plan": {"kind": "pass_through"}, "answer": "answer", "finish_reason": "stop"}
        report = summarize([row], {"schedule": [1], "limitations": []})
        self.assertEqual(report["cost_usd"], "0.001")
        self.assertEqual(report["aggregates"][0]["nominal_uncached_cost_usd"], "0.01")
        self.assertEqual(report["aggregates"][0]["cached_input_tokens"], 900)
        self.assertFalse(report["paper_evidence"])
        row["cached_input_tokens"] = None
        report = summarize([row], {"schedule": [1], "limitations": []})
        self.assertEqual(report["aggregates"][0]["cache_accounting_missing_calls"], 1)


if __name__ == "__main__":
    unittest.main()
