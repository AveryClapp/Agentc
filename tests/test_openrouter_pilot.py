"""No-network tests of paid-call budget, crash, and resume behavior."""
from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from bench.openrouter_pilot import Ledger, PilotError, load_key, make_request, money


def response():
    return {"id": "test-generation", "model": "test/model", "provider": "Test",
            "usage": {"cost": 0.001, "prompt_tokens": 15, "completion_tokens": 1},
            "choices": [{"message": {"content": "4"}, "finish_reason": "stop"}]}


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ledger = Ledger(self.root / "ledger.jsonl", "fake-key")
        self.payload = make_request("test/model", ["test-provider"],
                                    [{"role": "user", "content": "2+2?"}], max_tokens=16)

    def call(self, **kwargs):
        args = dict(key="fake-key", call_id="call-1", stage="test", stage_cap=Decimal("1"),
                    payload=self.payload, metadata={"source": "test"})
        args.update(kwargs)
        return self.ledger.call(**args)

    def test_key_parsing_never_evaluates_shell(self):
        path = self.root / ".env"
        path.write_text("UNRELATED=value\nexport OPENROUTER_API_KEY='fake-$(exit)'\n")
        self.assertEqual(load_key(path), "fake-$(exit)")
        path.write_text("OPENROUTER_API_KEY=a\nOPENROUTER_API_KEY=b\n")
        with self.assertRaises(PilotError):
            load_key(path)

    def test_invalid_costs_rejected(self):
        for value in (None, True, -1, "nan", "Infinity", "garbage"):
            with self.subTest(value=value), self.assertRaises(PilotError):
                money(value)

    @patch("bench.openrouter_pilot.account", return_value={"usage": 0})
    @patch("bench.openrouter_pilot.request_json", return_value=response())
    def test_success_replays_without_second_request(self, request, account):
        first = self.call()
        second = self.call()
        self.assertEqual(first["generation_id"], second["generation_id"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(account.call_count, 1)
        self.assertEqual(self.ledger.summary()["spent_usd"], "0.001")
        self.assertEqual(self.ledger.summary()["unresolved_calls"], [])
        self.assertNotIn("fake-key", self.ledger.path.read_text())

    @patch("bench.openrouter_pilot.account", return_value={"usage": 0})
    @patch("bench.openrouter_pilot.request_json", return_value=response())
    def test_changed_resume_inputs_fail(self, request, account):
        self.call()
        with self.assertRaises(PilotError):
            self.call(metadata={"source": "changed"})
        self.assertEqual(request.call_count, 1)

    @patch("bench.openrouter_pilot.account", return_value={"usage": 0})
    @patch("bench.openrouter_pilot.request_json", side_effect=PilotError("lost response"))
    def test_lost_response_blocks_further_calls(self, request, account):
        with self.assertRaises(PilotError):
            self.call()
        with self.assertRaises(PilotError):
            self.call(call_id="call-2")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.ledger.summary()["unresolved_calls"], ["call-1"])

    @patch("bench.openrouter_pilot.account", return_value={"usage": 0})
    @patch("bench.openrouter_pilot.request_json")
    def test_missing_cost_is_persisted_and_blocks(self, request, account):
        bad = response()
        bad["usage"].pop("cost")
        request.return_value = bad
        with self.assertRaises(PilotError):
            self.call()
        self.assertIn('"event":"response"', self.ledger.path.read_text())
        with self.assertRaises(PilotError):
            self.call(call_id="another")
        self.assertEqual(request.call_count, 1)

    @patch("bench.openrouter_pilot.account")
    @patch("bench.openrouter_pilot.request_json")
    def test_reserve_checks_stage_limit_before_network(self, request, account):
        with self.assertRaises(PilotError):
            self.call(stage_cap=Decimal("0.000001"))
        request.assert_not_called()
        account.assert_not_called()

    @patch("bench.openrouter_pilot.account")
    @patch("bench.openrouter_pilot.request_json")
    def test_cumulative_limit_across_stages(self, request, account):
        with self.ledger.locked() as handle:
            self.ledger.append(handle, {"event": "result", "id": "old",
                                       "stage": "old-stage", "cost_usd": "49.999"})
        with self.assertRaises(PilotError):
            self.call(stage_cap=Decimal("50"))
        request.assert_not_called()
        account.assert_not_called()

    @patch("bench.openrouter_pilot.account", return_value={"usage": 51})
    @patch("bench.openrouter_pilot.request_json")
    def test_external_account_spend_also_counts(self, request, account):
        with self.ledger.locked() as handle:
            self.ledger.append(handle, {"event": "origin", "usage_usd": "0"})
        with self.assertRaises(PilotError):
            self.call()
        request.assert_not_called()

    @patch("bench.openrouter_pilot.account")
    def test_corrupt_ledger_never_resets_budget(self, account):
        self.ledger.path.write_text('{"event":')
        with self.assertRaises(PilotError):
            self.call()
        account.assert_not_called()

    def test_other_key_cannot_reuse_ledger(self):
        with self.ledger.locked() as handle:
            self.ledger.append(handle, {"event": "origin", "usage_usd": "0"})
        with self.assertRaises(PilotError):
            Ledger(self.ledger.path, "different-key").summary()

    @patch("bench.openrouter_pilot.account")
    def test_price_cap_cannot_be_changed(self, account):
        self.payload["provider"].pop("max_price")
        with self.assertRaises(PilotError):
            self.call()
        account.assert_not_called()

    def test_fixed_route_cannot_have_fallbacks(self):
        with self.assertRaises(PilotError):
            make_request("test/model", ["a", "b"], self.payload["messages"])
        self.assertFalse(self.payload["provider"]["allow_fallbacks"])
        self.assertEqual(self.payload["transforms"], [])

    def test_auto_requires_explicit_model_pool(self):
        with self.assertRaises(PilotError):
            make_request("openrouter/auto", ["test"], self.payload["messages"])

    @patch("bench.openrouter_pilot.account", return_value={"usage": 0})
    @patch("bench.openrouter_pilot.request_json")
    def test_fixed_model_mismatch_blocks(self, request, account):
        bad = response()
        bad["model"] = "other/model"
        request.return_value = bad
        with self.assertRaisesRegex(PilotError, "different model"):
            self.call()
        self.assertEqual(self.ledger.summary()["unresolved_calls"], ["call-1"])

    @patch("bench.openrouter_pilot.account", return_value={"usage": 0})
    @patch("bench.openrouter_pilot.request_json", return_value=response())
    def test_endpoint_contract_requires_returned_metadata(self, request, account):
        with self.assertRaisesRegex(PilotError, "dispatch contract"):
            self.call(metadata={"dispatch_contract": {
                "provider_name": "Test", "endpoint_model": "test/model"}})

    @patch("bench.openrouter_pilot.account")
    def test_different_dispatch_key_rejected_before_network(self, account):
        with self.assertRaises(PilotError):
            self.call(key="different-key")
        account.assert_not_called()


if __name__ == "__main__":
    unittest.main()
