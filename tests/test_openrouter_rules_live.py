import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bench import openrouter_rules_live as live
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_pilot import PilotError, digest


class FakeLedger:
    def __init__(self):
        self.results = {}
        self.requests = []

    def call(self, key, call_id, stage, cap, payload, metadata):
        fingerprint = digest([payload, metadata, stage])
        if call_id in self.results:
            assert self.results[call_id]["fingerprint"] == fingerprint
            return self.results[call_id]
        self.requests.append((payload, metadata))
        assert "PRIVATE_GOLD" not in json.dumps([payload, metadata])
        # Distinct generated outputs let tests identify each arm's own history.
        answer = metadata["arm"] + ":" + metadata["workflow_stage"] + ":" + metadata["task_id"]
        cost = "0.0001" if payload["model"] == live.TARGET_MODEL else "0.001"
        result = {"id": call_id, "stage": stage, "fingerprint": fingerprint,
            "answer": answer, "model": payload["model"], "provider": "Anthropic",
            "cost_usd": cost, "usage": {"prompt_tokens": 20, "completion_tokens": 10, "cost": cost},
            "latency_ms": 10, "finish_reason": "stop", "generation_id": "generation-" + call_id,
            "paper_evidence": False}
        self.results[call_id] = result
        return result

    def summary(self):
        return {"completed_calls": len(self.results), "unresolved_calls": []}


class FakeNative:
    def __init__(self):
        self.stores = {}
        self.storage = None
        self.completions = []
        self.observations = []
        self.tokens = 0
        self.change_decision = False

    def optimize_configure(self, storage, catalog_json):
        self.storage = storage
        self.stores.setdefault(storage, {})
        self.catalog = catalog_json

    def optimize_model_catalog(self):
        return self.catalog

    def optimize_plan(self, encoded):
        assert "PRIVATE_GOLD" not in encoded
        call = json.loads(encoded)
        site = call["call_site_id"]
        count = self.stores[self.storage].get(site, 0)
        self.stores[self.storage][site] = count + 1
        self.tokens += 1
        plan = {"kind": "pass_through", "agentc_observation_context": {"opaque": self.tokens}}
        if os.environ["AGENTC_OPTIMIZE"] == "0" or (count < 3 and not self.change_decision):
            return json.dumps(plan)
        rules = os.environ["AGENTC_ENABLED_RULES"]
        candidate = deepcopy(call)
        if "ModelDowngrade" in rules:
            candidate["model"] = live.TARGET_MODEL
        else:
            candidate["parameters"]["max_output_tokens"] = 64
        selected = {"kind": "rewritten", "rule": "ModelDowngrade" if "ModelDowngrade" in rules else "OutputBudget", "call": candidate}
        if os.environ["AGENTC_EVAL_PLANNER_MODE"] == "current_greedy":
            return json.dumps({**selected, "agentc_observation_context": {"opaque": self.tokens}})
        plan["agentc_exploration_context"] = {"candidate_plan": selected, "lease_token": "lease-" + str(self.tokens)}
        return json.dumps(plan)

    def optimize_observe(self, encoded, outcome):
        assert "PRIVATE_GOLD" not in encoded + outcome
        self.observations.append(json.loads(outcome))
        return "observation-" + str(self.tokens)

    def optimize_complete_exploration(self, lease, outcome, divergence):
        self.completions.append((lease, json.loads(outcome), divergence))
        return True

    def optimize_record_divergence(self, token, divergence):
        return None

    def optimize_flush(self):
        pass

    def optimize_reset(self):
        self.storage = None


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    tasks = [{"task_id": str(i), "prompt": "Public question " + str(i), "expected": "PRIVATE_GOLD_" + str(i),
        "meta": {"paragraphs": [{"title": "Public", "sentences": ["Public passage."], "supporting": True}]}} for i in range(8)]
    fixture = tmp_path / "fixture.json"
    write_json(fixture, tasks)
    output = tmp_path / "output"
    eps = {m: {"tag": "anthropic", "provider_name": "Anthropic", "name": "Anthropic | " + m,
        "pricing": {"prompt": "0.000001" if m == live.TARGET_MODEL else "0.000003", "completion": "0.000005"}} for m in (live.SOURCE_MODEL, live.TARGET_MODEL)}
    manifest = {"kind": "live_six_arm_development", "source_files": {}, "native_sha256": file_hash(fixture),
        "runtime": {}, "fixture_sha256": file_hash(fixture), "created_at": datetime.now(timezone.utc).isoformat(),
        "maximum_reconstruction_age_seconds": 7200, "schedule": live.schedule_for([t["task_id"] for t in tasks], set(), 1, 4),
        "stage_cap_usd": "1", "endpoints": eps, "catalog": {}, "policies": {a: live.policy_settings(a) for a in live.ARMS},
        "prompts": live.prompt_constants(), "shadow_seed": "test", "risk_margin": .02, "limitations": []}
    write_json(output / "manifest.json", manifest)
    args = SimpleNamespace(output=output, fixture=fixture, native=fixture, ledger=tmp_path / "ledger", max_calls=None)
    ledger, native = FakeLedger(), FakeNative()
    attention = Mock()
    attention.compute_attention_scores.side_effect = lambda messages, _: ([.5]*len(messages), ["Public"])
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    monkeypatch.setattr(live, "sources", lambda: {})
    monkeypatch.setattr(live, "runtime_sources", lambda: {})
    monkeypatch.setattr(live, "Ledger", lambda *args: ledger)
    monkeypatch.setattr(live, "load_module", lambda *args, **kwargs: native if kwargs.get("native") else attention)
    return args, manifest, tasks, ledger, native


def test_schedule_has_disjoint_calibration_counterbalanced_six_arms():
    schedule = live.schedule_for([str(i) for i in range(30)], {"0", "1"}, 4, 8)
    assert not {"0", "1"} & {r["task_id"] for r in schedule}
    cal = {r["task_id"] for r in schedule if r["phase"] == "calibration"}
    development = {r["task_id"] for r in schedule if r["phase"] != "calibration"}
    assert len(cal) == 4 and len(development) == 8 and not cal & development
    for task_id in development:
        assert {r["arm"] for r in schedule if r["task_id"] == task_id} == set(live.ARMS)
    assert schedule == live.schedule_for(list(reversed([str(i) for i in range(30)])), {"0", "1"}, 4, 8)


@pytest.mark.parametrize("ids,cal,dev", [(["x", "x"], 1, 4), (["x"], 0, 4), (["x"], 1, 3), (["x"], 1, 4)])
def test_invalid_schedule_rejected(ids, cal, dev):
    with pytest.raises(PilotError):
        live.schedule_for(ids, set(), cal, dev)


def test_live_arms_use_only_own_histories_and_charge_all_calls(experiment):
    args, manifest, tasks, ledger, native = experiment
    report = live.run(args, "fake-key")
    assert report["completed_decisions"] == len(manifest["schedule"])*3
    assert report["completed_calls"] > report["completed_decisions"]
    assert len(native.completions) == 4*3  # guarded, routing, sequential, joint
    for payload, meta in ledger.requests:
        if meta["workflow_stage"] != "filter":
            assert payload["messages"][1]["content"] == meta["arm"] + ":filter:" + meta["task_id"]
        if meta["workflow_stage"] == "answer":
            assert payload["messages"][2]["content"] == meta["arm"] + ":synthesize:" + meta["task_id"]
    sequential = next(r for r in report["reports"] if r["arm"] == "sequential")
    assert sequential["calls_by_scope"]["calibration"] == 6
    assert list(sequential["primary_model_counts"]) == [live.TARGET_MODEL]
    assert len(native.stores) == 6
    assert json.loads((args.output / "static-selection.json").read_text())["calibration_only"]


def test_resume_paid_prefix_without_redispatch_or_warm_store_reuse(experiment):
    args, manifest, tasks, ledger, native = experiment
    args.max_calls = 1
    first = live.run(args, "fake-key")
    assert first["completed_calls"] == 1
    args.max_calls = None
    result = live.run(args, "fake-key")
    count = len(ledger.requests)
    saved = (args.output / "decisions.json").read_bytes()
    final = live.run(args, "fake-key")
    assert len(ledger.requests) == count
    assert result["calls_sha256"] == final["calls_sha256"]
    assert saved == (args.output / "decisions.json").read_bytes()
    assert len(native.stores) == 12  # two full reconstructions, never same warm DB


def test_resume_partial_exploration_keeps_billing_and_feedback_exact(experiment):
    args, manifest, tasks, ledger, native = experiment
    full = live.run(args, "fake-key")
    calls = json.loads((args.output / "calls.json").read_text())
    decisions = json.loads((args.output / "decisions.json").read_text())
    probe = next(i for i, r in enumerate(calls) if r["scope"] == "exploration")
    # Simulate artifact interruption after paid primary but before probe and
    # decision save. Immutable ledger remains authoritative for billed results.
    paid_primary = calls[probe - 1]["id"]
    decision_index = next(i for i, d in enumerate(decisions) if d["primary_id"] == paid_primary)
    write_json(args.output / "calls.json", calls[:probe])
    write_json(args.output / "decisions.json", decisions[:decision_index])
    count = len(ledger.requests)
    resumed = live.run(args, "fake-key")
    assert len(ledger.requests) == count
    assert resumed["calls_sha256"] == full["calls_sha256"]


def test_changed_native_plan_blocks_before_another_provider_call(experiment):
    args, manifest, tasks, ledger, native = experiment
    live.run(args, "fake-key")
    count = len(ledger.requests)
    native.change_decision = True
    with pytest.raises(PilotError, match="native decision changed"):
        live.run(args, "fake-key")
    assert len(ledger.requests) == count


def test_gold_change_cannot_change_native_feedback_but_changes_evaluator(experiment):
    args, manifest, tasks, ledger, native = experiment
    live.run(args, "fake-key")
    decisions = json.loads((args.output / "decisions.json").read_text())
    calls = json.loads((args.output / "calls.json").read_text())
    public_records = json.dumps([calls, decisions])
    assert "PRIVATE_GOLD" not in public_records
    selection = live.static_selection(decisions, calls, {t["task_id"]: t for t in tasks}, manifest)
    assert selection["model"] == live.TARGET_MODEL


def test_environment_restored_after_limit_and_expiry_blocks_dispatch(experiment, monkeypatch):
    args, manifest, tasks, ledger, native = experiment
    monkeypatch.setenv("AGENTC_TEST_SENTINEL", "preserve")
    before = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    args.max_calls = 7
    live.run(args, "fake-key")
    assert before == {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    manifest["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    write_json(args.output / "manifest.json", manifest)
    with pytest.raises(PilotError, match="window expired"):
        live.run(args, "fake-key")
    assert len(ledger.requests) == 7


def test_frozen_source_change_blocks_dispatch(experiment, monkeypatch):
    args, manifest, tasks, ledger, native = experiment
    monkeypatch.setattr(live, "sources", lambda: {"changed": "source"})
    with pytest.raises(PilotError, match="frozen workflow"):
        live.run(args, "fake-key")
    assert not ledger.requests


def test_uncovered_cap_fails_closed(experiment):
    args, manifest, tasks, ledger, native = experiment
    call = {"model": live.SOURCE_MODEL, "parameters": {"max_output_tokens": 513, "temperature": 0},
            "messages": [{"role": "user", "content": "hello"}]}
    with pytest.raises(PilotError):
        live.payload_for(call, manifest)


@pytest.mark.parametrize("journal", ["calls.json", "decisions.json", "intents.json"])
def test_extra_journal_suffix_fails_before_dispatch(experiment, journal):
    args, manifest, tasks, ledger, native = experiment
    live.run(args, "fake-key")
    count = len(ledger.requests)
    path = args.output / journal
    rows = json.loads(path.read_text())
    write_json(path, rows + [rows[-1]])
    with pytest.raises(PilotError, match="prefix"):
        live.run(args, "fake-key")
    assert len(ledger.requests) == count


def test_duplicate_call_identity_rejected_before_dispatch(experiment):
    args, manifest, tasks, ledger, native = experiment
    live.run(args, "fake-key")
    count = len(ledger.requests)
    path = args.output / "calls.json"
    rows = json.loads(path.read_text())
    rows[1]["id"] = rows[0]["id"]
    write_json(path, rows)
    with pytest.raises(PilotError, match="prefix"):
        live.run(args, "fake-key")
    assert len(ledger.requests) == count


def test_short_prefix_on_complete_journal_is_not_labeled_reconstructed(experiment):
    args, manifest, tasks, ledger, native = experiment
    full = live.run(args, "fake-key")
    assert full["schedule_complete"]
    args.max_calls = 1
    prefix = live.run(args, "fake-key")
    assert not prefix["schedule_complete"]
    assert prefix["reconstructed_calls"] == 1
    assert prefix["completed_calls"] == full["completed_calls"]
