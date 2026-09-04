"""No-network checks of development-only contract experiment controls."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bench.openrouter_contract import PREFIX, messages, prepare, summarize
from bench.openrouter_matrix import MODELS, PilotError, messages_for, write_json
from tests.test_openrouter_matrix import task


class ContractTests(unittest.TestCase):
    def test_contract_changes_last_instruction_not_context_or_gold(self):
        value = task(0)
        self.assertEqual(messages(value, "legacy"), messages_for(value))
        rewritten = messages(value, "reinforced")
        self.assertEqual(rewritten[:-1], messages_for(value)[:-1])
        self.assertEqual(rewritten[-1]["content"], PREFIX + "Question: Question 0?")
        self.assertNotIn("gold secret", str(rewritten))
        with self.assertRaises(PilotError):
            messages(value, "unknown")

    def test_prepare_uses_only_exposed_calibration_and_balances_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            args = SimpleNamespace(output=p / "out", previous=p / "previous.json", fixture=p / "fixture.json")
            write_json(args.fixture, [task(i) for i in range(20)])
            write_json(args.previous, {"schedule": [
                {"task_id": str(i), "phase": "calibration" if i < 8 else "holdout"} for i in range(20)]})
            with patch("bench.openrouter_contract.endpoints", return_value={}), patch("bench.openrouter_contract.sources", return_value={}):
                result = prepare(args, "fake")
                manifest = json.loads((args.output / "manifest.json").read_text())
                self.assertEqual(result["scheduled_calls"], 96)
                selected = {r["task_id"] for r in manifest["schedule"]}
                self.assertEqual(len(selected), 6)
                self.assertTrue(selected.issubset({str(i) for i in range(8)}))
                for task_id in selected:
                    cells = {(r["model"], r["contract"], r["max_tokens"]) for r in manifest["schedule"] if r["task_id"] == task_id}
                    self.assertEqual(cells, {(m, c, n) for m, _ in MODELS for c in ("legacy", "reinforced") for n in (128, 512)})
                with self.assertRaises(PilotError):
                    prepare(args, "fake")

    def test_summary_does_not_claim_semantic_or_paper_accuracy(self):
        report = summarize([], {"schedule": [], "limitations": []})
        self.assertFalse(report["paper_evidence"])
        self.assertEqual(report["cost_usd"], "0")


if __name__ == "__main__":
    unittest.main()
