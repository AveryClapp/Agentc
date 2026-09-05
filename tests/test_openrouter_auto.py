from argparse import Namespace
from contextlib import contextmanager
from copy import deepcopy
import json

import pytest

from bench import openrouter_auto as auto
from bench.openrouter_pilot import PilotError


def contract():
    endpoint = {"provider_name": "Anthropic", "name": "Anthropic | endpoint-model", "tag": "anthropic",
                "pricing": {"prompt": ".000003", "completion": ".000015"}}
    manifest = {"endpoints": {auto.SOURCE_MODEL: endpoint}, "allowed_models": [auto.SOURCE_MODEL],
                "provider_only": ["anthropic"], "contract": "reinforced"}
    result = {"model": auto.SOURCE_MODEL, "provider": "Anthropic", "cost_usd": ".01", "usage": {"cost": .01},
        "router_metadata": {"requested": "openrouter/auto", "strategy": "auto", "attempt": 1, "is_byok": False,
            "endpoints": {"available": [{"model": "endpoint-model", "provider": "Anthropic", "selected": True}]}}}
    return manifest, result


def test_request_keeps_bounded_default_service_controls():
    manifest, _ = contract()
    task = {"prompt": "Who?", "meta": {"paragraphs": [{"title": "P", "sentences": ["Ada."]}]}}
    request = auto.request_for(manifest, task)
    assert request["model"] == "openrouter/auto"
    assert request["plugins"] == [{"id": "auto-router", "allowed_models": [auto.SOURCE_MODEL]}]
    assert request["provider"]["only"] == ["anthropic"]
    assert request["provider"]["allow_fallbacks"] is False
    assert request["provider"]["data_collection"] == "deny"
    assert request["max_tokens"] == 512
    assert "session_id" not in request
    assert "cost_tier" not in request["plugins"][0]


@pytest.mark.parametrize("mutation", ["model", "provider", "endpoint", "duplicate", "attempt", "byok", "strategy", "requested", "billing"])
def test_auto_attribution_rejects_mismatch_before_next_call(mutation):
    manifest, result = contract()
    assert auto.validate_dispatch(result, manifest) == manifest["endpoints"][auto.SOURCE_MODEL]
    if mutation == "model": result["model"] = "outside/pool"
    elif mutation == "provider": result["provider"] = "Other"
    elif mutation == "endpoint": result["router_metadata"]["endpoints"]["available"][0]["model"] = "Other"
    elif mutation == "duplicate": result["router_metadata"]["endpoints"]["available"] *= 2
    elif mutation == "attempt": result["router_metadata"]["attempt"] = 2
    elif mutation == "byok": result["router_metadata"]["is_byok"] = True
    elif mutation == "strategy": result["router_metadata"]["strategy"] = "fallback"
    elif mutation == "requested": result["router_metadata"]["requested"] = auto.SOURCE_MODEL
    elif mutation == "billing": result["cost_usd"] = ".02"
    with pytest.raises(PilotError):
        auto.validate_dispatch(result, manifest)


def test_service_tier_reads_durable_response_and_does_not_invent_missing_value():
    class Ledger:
        responses = [{"event": "response", "id": "a", "response": {}}]
        @contextmanager
        def locked(self): yield None
        def read(self, handle): return self.responses
    ledger = Ledger()
    assert auto.reported_service_tier(ledger, "a") is None
    ledger.responses[0]["response"]["service_tier"] = "default"
    assert auto.reported_service_tier(ledger, "a") == "default"
    ledger.responses[0]["response"]["service_tier"] = "priority"
    with pytest.raises(PilotError, match="non-default"):
        auto.reported_service_tier(ledger, "a")
    with pytest.raises(PilotError, match="exactly one"):
        auto.reported_service_tier(ledger, "unknown")


def test_prepare_uses_real_frontier_metric_schema_and_source_only_schedule(tmp_path, monkeypatch):
    actual = json.loads((auto.ROOT / "bench/repro/openrouter-frontier-2026-09-04/manifest.json").read_text())
    manifest, _ = contract()
    actual["endpoints"] = manifest["endpoints"]
    actual["schedule"] = [{"task_id": "a", "phase": "warmup", "context": "natural", "model": auto.SOURCE_MODEL, "arm": arm}
                          for arm in ("full", "compress")]
    natural, extended = tmp_path / "natural.json", tmp_path / "extended.json"
    natural.write_text("[]")
    extended.write_text("[]")
    actual["fixtures"] = {"natural": auto.file_hash(natural), "extended": auto.file_hash(extended)}
    frontier = tmp_path / "frontier"
    frontier.mkdir()
    (frontier / "manifest.json").write_text(json.dumps(actual))
    monkeypatch.setattr(auto, "endpoints", lambda key: deepcopy(manifest["endpoints"]))
    monkeypatch.setattr(auto, "load_tasks", lambda *args: {"natural": {"a": {"prompt": "Who?", "expected": "Ada", "meta": {"paragraphs": []}}}})
    args = Namespace(frontier=frontier, natural=natural, extended=extended, output=tmp_path / "auto")
    result = auto.prepare(args, "TEST_NOT_A_CREDENTIAL")
    assert result["scheduled_calls"] == 1
    frozen = json.loads((args.output / "manifest.json").read_text())
    assert frozen["scoring"]["primary"] == actual["primary_quality_metric"]
    assert frozen["stage_cap_usd"] == "5"
    assert frozen["optimizer"] == "none"

    class FakeLedger:
        results = {}
        responses = []
        dispatches = 0
        def __init__(self, *args): pass
        @contextmanager
        def locked(self): yield None
        def read(self, handle): return self.responses
        def summary(self): return {"completed_calls": len(self.results), "unresolved_calls": []}
        def call(self, key, call_id, stage, cap, payload, metadata):
            if call_id in self.results:
                return {**self.results[call_id], "at": "cached-timestamp"}
            type(self).dispatches += 1
            _, response = contract()
            response.update(id=call_id, stage=stage, event="result", answer="Ada", finish_reason="stop",
                            generation_id="fake-generation", metadata=metadata, paper_evidence=False)
            response["usage"].update(prompt_tokens=10, completion_tokens=1)
            self.results[call_id] = response
            self.responses.append({"event": "response", "id": call_id, "response": {"service_tier": "default"}})
            return response
    monkeypatch.setattr(auto, "Ledger", FakeLedger)
    args.ledger = tmp_path / "unused-ledger"
    args.max_calls = None
    assert auto.run(args, "TEST_NOT_A_CREDENTIAL")["completed_calls"] == 1
    saved = (args.output / "results.json").read_bytes()
    assert auto.run(args, "TEST_NOT_A_CREDENTIAL")["completed_calls"] == 1
    assert FakeLedger.dispatches == 1
    args.max_calls = 0
    assert auto.run(args, "TEST_NOT_A_CREDENTIAL")["completed_calls"] == 0
    assert (args.output / "results.json").read_bytes() == saved
