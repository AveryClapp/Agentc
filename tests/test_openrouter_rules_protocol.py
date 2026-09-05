import json
import os
from copy import deepcopy
from unittest.mock import Mock

import pytest

from bench.openrouter_frontier import SOURCE_MODEL
from bench.openrouter_pilot import PilotError
from bench.openrouter_rules_protocol import NON_ROUTING_RULES, STAGES, activation, preflight, prompt_constants, protocol_policies, workflow_call


def task():
    return {"task_id": "public-id", "prompt": "Public question?", "expected": "GOLD_SECRET",
        "meta": {"gold_answer": "GOLD_SECRET", "paragraphs": [{"title": "Title", "sentences": ["Public passage."], "supporting": True}]}}


def attention():
    instance = Mock()
    instance.compute_attention_scores.side_effect = lambda messages, _: ([.5]*len(messages), ["Public"])
    return instance


def test_workflow_separates_sites_keeps_question_and_uses_only_own_history():
    history = {}
    calls = []
    for stage in STAGES:
        call = workflow_call(task(), stage, history, attention())
        calls.append(call)
        assert "GOLD_SECRET" not in json.dumps(call)
        assert "supporting" not in json.dumps(call)
        assert call["messages"][-1]["content"] == "Question: Public question?"
        assert call["input_deps"][-1]["kind"] == "user_input"
        assert call["input_deps"] == call["parameters"]["extra"]["message_deps"]
        history[stage] = stage + " own output"
    assert len({c["call_site_id"] for c in calls}) == 3
    assert len({c["trace_id"] for c in calls}) == 1
    assert len({c["span_id"] for c in calls}) == 3
    assert calls[1]["messages"][1]["content"] == "filter own output"
    assert calls[1]["parameters"]["extra"]["window_state_reads"] == ["filter_result"]
    assert calls[2]["parameters"]["extra"]["window_state_reads"] == ["synthesis"]
    assert calls[2]["input_deps"][1] == {"kind": "state", "key": "filter_result"}
    assert calls[2]["messages"][2]["content"] == "synthesize own output"
    routed = workflow_call(task(), "filter", {}, attention(), model="cheap")
    assert routed["call_site_id"] == calls[0]["call_site_id"]


@pytest.mark.parametrize("stage,history", [("answer", {}), ("filter", {"filter": "future"}), ("bad", {}), ("synthesize", {"filter": 4})])
def test_missing_or_future_state_fails_closed(stage, history):
    with pytest.raises(PilotError):
        workflow_call(task(), stage, history, attention())


def test_literal_prompts_are_read_without_executing_old_agent(tmp_path):
    source = tmp_path / "agent.py"
    source.write_text('raise RuntimeError("must not execute")\nFILTER_SYSTEM="filter"\nSYNTH_SYSTEM="synth"\nANSWER_SYSTEM="answer"\n')
    assert prompt_constants(source)["FILTER_SYSTEM"] == "filter"
    source.write_text('FILTER_SYSTEM=evil()')
    with pytest.raises(ValueError):
        prompt_constants(source)


def test_arm_controls_preserve_native_budgets_and_exclude_broken_families():
    policies = {p["name"]: p["settings"] for p in protocol_policies()}
    assert policies["historical_rules"]["AGENTC_EVAL_PLANNER_MODE"] == "current_greedy"
    assert policies["historical_rules"]["AGENTC_COMPOSE"] == "0"
    assert policies["guarded_rules"]["AGENTC_COMPOSE"] == "1"
    assert policies["routing_only"]["AGENTC_ENABLED_RULES"] == "ModelDowngrade"
    assert set(policies["joint"]["AGENTC_ENABLED_RULES"].split(",")) == {*NON_ROUTING_RULES, "ModelDowngrade"}
    assert all("AGENTC_SHADOW_DIVERGENCE_BUDGET" not in p for p in policies.values())


def test_selected_cap_is_not_reported_as_measured_token_savings():
    call = workflow_call(task(), "filter", {}, attention())
    selected = deepcopy(call)
    selected["parameters"]["max_output_tokens"] = 64
    record = activation(call, {"kind": "rewritten", "rule": "OutputBudget", "call": selected})
    assert record["output_cap_changed"] and not record["messages_changed"]
    assert record["measured_token_savings"] is None
    assert record["executed_on_provider"] is False


def test_preflight_restores_environment_and_uses_isolated_stores(monkeypatch):
    monkeypatch.setenv("AGENTC_UNRELATED_TEST_FLAG", "preserve")
    before = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    native = Mock()
    native.optimize_observe.return_value = "token"
    native.optimize_record_divergence.return_value = None  # production void API
    def plan(encoded):
        call = json.loads(encoded)
        assert "GOLD_SECRET" not in encoded
        if os.environ["AGENTC_OPTIMIZE"] == "0":
            return json.dumps({"kind": "pass_through"})
        selected = deepcopy(call)
        selected["parameters"]["max_output_tokens"] = 64
        return json.dumps({"kind": "rewritten", "rule": "OutputBudget", "call": selected})
    native.optimize_plan.side_effect = plan
    report = preflight(native, attention(), {}, task(), repetitions=3)
    assert report["provider_calls"] == 0 and report["quality_claim"] is None
    assert len(report["decisions"]) == 5*3*3
    assert native.optimize_record_divergence.call_count == 4*3*3
    assert native.optimize_reset.call_count == 5
    stores = [call.args[0] for call in native.optimize_configure.call_args_list]
    assert len(set(stores)) == 5
    assert before == {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
