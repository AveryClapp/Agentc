"""Native lifecycle regression tests; set AGENTC_NATIVE_LIBRARY to the build."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from bench.openrouter_matrix import ROOT, load_module, native_call


@unittest.skipUnless(os.environ.get("AGENTC_NATIVE_LIBRARY"), "native build not specified")
class CatalogConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ["AGENTC_NATIVE_LIBRARY"]
        loader = importlib.machinery.ExtensionFileLoader("_native", path)
        spec = importlib.util.spec_from_file_location("_native", path, loader=loader)
        cls.native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.native)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.native.optimize_reset)
        self.first = str(Path(self.directory.name) / "first")
        self.native.optimize_configure(self.first)
        self.default = json.loads(self.native.optimize_model_catalog())

    def test_explicit_namespace_and_restart(self):
        custom = json.loads(json.dumps(self.default))
        for target in custom["targets"]:
            target["provider_namespace"] = "pilot-" + target["provider_namespace"]
        self.native.optimize_configure(self.first, catalog_json=json.dumps(custom))
        self.assertEqual(json.loads(self.native.optimize_model_catalog()), custom)
        self.native.optimize_reset()
        self.native.optimize_configure(self.first, catalog_json=json.dumps(custom))
        self.assertEqual(json.loads(self.native.optimize_model_catalog()), custom)
        self.native.optimize_configure(self.first)
        self.assertEqual(json.loads(self.native.optimize_model_catalog()), self.default)

    def test_invalid_snapshot_preserves_existing_state(self):
        second = str(Path(self.directory.name) / "must-not-exist")
        for value in ("{", "{}", json.dumps({**self.default, "targets": []})):
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                self.native.optimize_configure(second, catalog_json=value)
            self.assertEqual(self.native.optimize_storage_path(), self.first)
            self.assertEqual(json.loads(self.native.optimize_model_catalog()), self.default)
            self.assertFalse(Path(second).exists())

    def test_openrouter_namespace_generates_a_real_native_rewrite(self):
        # Unit-test-only observations in this test's disposable store. Paid
        # experiments never use this store or these synthetic outcomes.
        from unittest.mock import patch
        custom = json.loads(json.dumps(self.default))
        custom["targets"] = custom["targets"][:1]
        target = custom["targets"][0]
        target.update(adapter_protocol="openrouter.chat.completions.v1",
                      provider_namespace="openrouter", model_id="test/model", aliases=[])
        settings = {"AGENTC_ENABLED_RULES": "ContextCompress", "AGENTC_OPTIMIZE": "1",
                    "AGENTC_EVAL_PLANNER_MODE": "current_greedy", "AGENTC_COMPOSE": "0",
                    "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3", "AGENTC_OPTIMIZE_EXPLORATION": "0",
                    "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "100"}
        with patch.dict(os.environ, settings):
            self.native.optimize_configure(self.first, catalog_json=json.dumps(custom))
            attention = load_module("catalog_test_attention", ROOT / "python/agentc/_attention.py")
            task = {"task_id": "unit-test", "prompt": "Where is Paris?", "meta": {"paragraphs": [
                {"title": "Relevant", "sentences": ["Paris is in France."]},
                *[{"title": "Distractor", "sentences": ["unrelated " * 1000]} for _ in range(3)],
            ]}}
            item = {"model": "test/model", "arm": "full"}
            call = native_call(task, item, attention)
            outcome = {"input_tokens": 5000, "output_tokens": 1, "latency_ms": 1000,
                       "cost_usd": 0.01, "output_is_structured": False, "output_is_short": True,
                       "call_site_id": call["call_site_id"]}
            for _ in range(3):
                plan = self.native.optimize_plan(json.dumps(call))
                self.assertIn("agentc_observation_context", json.loads(plan))
                self.assertTrue(self.native.optimize_observe(plan, json.dumps(outcome)))
            item["arm"] = "compress"
            call = native_call(task, item, attention)
            plan = json.loads(self.native.optimize_plan(json.dumps(call)))
            self.assertEqual(plan["kind"], "rewritten")
            self.assertEqual(plan["rule"], "ContextCompress")
            self.assertLess(len(plan["call"]["messages"]), len(call["messages"]))
            self.assertEqual(plan["call"]["messages"][-1], call["messages"][-1])
            self.assertIn("agentc_observation_context", plan)


if __name__ == "__main__":
    unittest.main()
