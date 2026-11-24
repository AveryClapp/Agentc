"""Signals S1-S5 and response normalization."""

from __future__ import annotations

import os

import pytest
from litellm import ModelResponse

from agentc.change_safety.normalize import action_signature, normalize_response
from agentc.change_safety.records import NormalizedResponse, ToolAction
from agentc.change_safety.signals import (
    action_disagreement,
    compute_signals,
    lexical_divergence,
    normalized_divergence,
    termination_divergence,
)


def text(value: str, tokens: int = 10, finish: str = "stop") -> NormalizedResponse:
    return NormalizedResponse(kind="text", text=value, output_tokens=tokens, finish_reason=finish)


def tools(*calls: tuple[str, str], tokens: int = 10) -> NormalizedResponse:
    return NormalizedResponse(
        kind="tool_calls",
        tool_calls=tuple(ToolAction(n, a) for n, a in calls),
        output_tokens=tokens,
        finish_reason="tool_calls",
    )


def test_lexical_and_normalized_match_production_guard():
    from agentc._patches._optimizer_glue import _text_divergence

    a, b = "The account is Open.", "the account is open"
    for mode, fn in (("lexical", lexical_divergence), ("normalized", normalized_divergence)):
        os.environ["AGENTC_SHADOW_DIVERGENCE_MODE"] = mode
        try:
            assert fn(a, b) == pytest.approx(_text_divergence(a, b))
        finally:
            del os.environ["AGENTC_SHADOW_DIVERGENCE_MODE"]


def test_action_disagreement_grades_arguments():
    a = tools(("transfer", '{"amount":10,"to":"x"}'))
    assert action_disagreement(a, a)[0] == 0.0
    b = tools(("transfer", '{"amount":20,"to":"x"}'))
    assert action_disagreement(a, b)[0] == pytest.approx(0.5)
    assert action_disagreement(a, tools(("lookup", "{}")))[0] == 1.0
    assert action_disagreement(a, text("hi"))[0] == 1.0
    assert action_disagreement(text("hi"), text("bye"))[0] == 0.0


def test_action_disagreement_pairs_repeated_tools_in_order():
    a = tools(("get", '{"id":1}'), ("get", '{"id":2}'))
    b = tools(("get", '{"id":1}'), ("get", '{"id":3}'))
    assert action_disagreement(a, b)[0] == pytest.approx(0.5)


def test_termination_and_length():
    assert termination_divergence(text("ok"), text("bye ###STOP###"))[0] == 1.0
    assert termination_divergence(text("a"), tools(("transfer_to_human_agents", "{}")))[0] == 1.0
    assert termination_divergence(text("a", 10), text("a", 10, finish="length"))[0] == 1.0
    assert termination_divergence(text("a", 10), text("b", 10))[0] == 0.0
    value, parts = termination_divergence(text("a", 3), text("b", 15))
    assert value == pytest.approx(1.0) and parts["output_token_log2_ratio"] == pytest.approx(2.0)


def test_structured_outputs_score_zero_or_one_for_s1_to_s3():
    a = tools(("get", '{"id":1}'))
    same = compute_signals(a, a, embed=False)
    diff = compute_signals(a, tools(("get", '{"id":2}')), embed=False)
    assert (same.S1, same.S2, same.S4) == (0.0, 0.0, 0.0)
    assert (diff.S1, diff.S2) == (1.0, 1.0) and diff.S4 == pytest.approx(1.0)


def test_errors_and_missing_forks_are_none_not_agreement():
    error = NormalizedResponse(kind="error", error="boom")
    for fork in (None, error):
        values = compute_signals(text("a"), fork)
        assert all(values.get(s) is None for s in ("S1", "S2", "S3", "S4", "S5"))


def test_normalize_litellm_response_drops_call_ids():
    def response(call_id: str) -> ModelResponse:
        return ModelResponse(
            choices=[{
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": call_id, "type": "function",
                                    "function": {"name": "get", "arguments": '{"b": 1, "a": 2}'}}],
                },
            }],
            usage={"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107,
                   "prompt_tokens_details": {"cached_tokens": 64}},
        )

    a, b = normalize_response(response("call_1")), normalize_response(response("call_2"))
    assert a == b
    assert a.tool_calls[0].arguments_json == '{"a":2,"b":1}'
    assert (a.input_tokens, a.output_tokens, a.cached_input_tokens) == (100, 7, 64)
    assert action_signature(a) == '[["get","{\\"a\\":2,\\"b\\":1}"]]'
