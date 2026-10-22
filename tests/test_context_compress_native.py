"""Exercise conversation retention through the real native planner and SDK adapter."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentc import _native
from agentc._patches._optimizer_glue import (
    apply_call_mutations_anthropic,
    apply_call_mutations_openai,
    build_call_dict_anthropic,
    build_call_dict_openai,
)


@pytest.fixture(autouse=True)
def isolated_optimizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_OPTIMIZE", "1")
    monkeypatch.setenv("AGENTC_COMPOSE", "1")
    monkeypatch.setenv("AGENTC_ENABLED_RULES", "ContextCompress")
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "current_greedy")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "0")
    monkeypatch.setenv("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "50")
    _native.optimize_configure(str(tmp_path / ".agentc"))
    yield
    _native.optimize_reset()


def call_for(kwargs: dict, scores: list[float], provider: str = "openai") -> dict:
    builder = (
        build_call_dict_anthropic if provider == "anthropic" else build_call_dict_openai
    )
    call = builder(
        kwargs,
        call_site_id="native.exchange.retention",
        trace_id_hex="02" * 16,
        span_id_hex="03" * 8,
    )
    # The retention contract must hold even when the attention proxy has no
    # vocabulary signal for an action or a short/empty observation.
    call["parameters"]["extra"]["attention_scores"] = scores
    call["parameters"]["extra"]["follow_on_tokens"] = []
    return call


def warm(call: dict) -> None:
    for _ in range(3):
        plan = _native.optimize_plan(json.dumps(call))
        assert json.loads(plan)["kind"] == "pass_through"
        _native.optimize_observe(
            plan,
            json.dumps(
                {
                    "call_site_id": call["call_site_id"],
                    "model": call["model"],
                    "input_tokens": 4000,
                    "output_tokens": 100,
                    "latency_ms": 100,
                    "cost_usd": 0.01,
                }
            ),
        )


def request(messages: list[dict]) -> dict:
    return {
        "model": "gpt-4o-2024-11-20",
        "messages": messages,
        "temperature": 0,
        "max_tokens": 512,
        "stop": ["STOP"],
        "extra_headers": {"X-Request-ID": "retention-test"},
    }


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_opaque_header_cannot_admit_compression_even_after_warmup(
    provider: str,
) -> None:
    kwargs = request(
        [
            {"role": "system", "content": "agent instructions"},
            {"role": "user", "content": "original task"},
            {"role": "assistant", "content": "old command"},
            {"role": "user", "content": "obsolete output " * 1000},
        ]
    )
    kwargs["extra_headers"] = {"X-Request-Context": "opaque"}
    if provider == "anthropic":
        kwargs["model"] = "claude-sonnet-4-5"
        kwargs["system"] = kwargs["messages"].pop(0)["content"]
        kwargs["stop_sequences"] = kwargs.pop("stop")
    call = call_for(kwargs, [0.0] * 4, provider)
    before = copy.deepcopy(kwargs)
    warm(call)
    plan = json.loads(_native.optimize_plan(json.dumps(call)))
    assert plan["kind"] == "pass_through"
    assert "agentc_observation_context" not in plan
    assert kwargs == before


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_native_compression_keeps_current_action_and_empty_observation(
    provider: str,
) -> None:
    kwargs = request(
        [
            {"role": "system", "content": "agent instructions"},
            {"role": "user", "content": "original task"},
            {"role": "assistant", "content": "old command"},
            {"role": "user", "content": "obsolete output " * 1000},
            {"role": "assistant", "content": "current command"},
            {"role": "user", "content": ""},
        ]
    )
    if provider == "anthropic":
        kwargs["model"] = "claude-sonnet-4-5"
        kwargs["system"] = kwargs["messages"].pop(0)["content"]
        kwargs["stop_sequences"] = kwargs.pop("stop")
    before = copy.deepcopy(kwargs)
    call = call_for(kwargs, [0.0] * 6, provider)
    warm(call)
    plan = json.loads(_native.optimize_plan(json.dumps(call)))
    assert plan["kind"] == "rewritten"
    assert plan["rule"] == "ContextCompress"
    apply = (
        apply_call_mutations_anthropic
        if provider == "anthropic"
        else apply_call_mutations_openai
    )
    dispatched = apply(kwargs, plan["call"])
    expected = copy.deepcopy(before)
    indices = (0, 3, 4) if provider == "anthropic" else (0, 1, 4, 5)
    expected["messages"] = [before["messages"][i] for i in indices]
    assert dispatched == expected
    assert kwargs == before


def test_native_compression_keeps_old_exchanges_whole_for_all_score_masks() -> None:
    messages = [
        {"role": "system", "content": "agent instructions"},
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "old command"},
        {"role": "user", "content": "obsolete output " * 1000},
        {"role": "assistant", "content": "second command"},
        {"role": "user", "content": "second observation"},
        {"role": "assistant", "content": "current command"},
        {"role": "user", "content": "current observation"},
    ]
    kwargs = request(messages)
    warm(call_for(kwargs, [0.0] * 8))
    for mask in range(256):
        call = call_for(kwargs, [float(bool(mask & (1 << i))) for i in range(8)])
        plan = json.loads(_native.optimize_plan(json.dumps(call)))
        assert plan["kind"] in {"pass_through", "rewritten"}, mask
        selected = (
            apply_call_mutations_openai(kwargs, plan["call"])["messages"]
            if plan["kind"] == "rewritten"
            else messages
        )
        assert messages[0] in selected and messages[1] in selected, mask
        assert messages[6] in selected and messages[7] in selected, mask
        for action, observation in ((2, 3), (4, 5)):
            assert (messages[action] in selected) == (
                messages[observation] in selected
            ), mask


def test_native_compression_abstains_on_current_exchange_only() -> None:
    kwargs = request(
        [
            {"role": "user", "content": "original task"},
            {"role": "assistant", "content": "current command " * 1000},
            {"role": "user", "content": ""},
        ]
    )
    call = call_for(kwargs, [0.0] * 3)
    warm(call)
    assert json.loads(_native.optimize_plan(json.dumps(call)))["kind"] == "pass_through"


def test_native_compression_still_removes_unrelated_prefix_context() -> None:
    kwargs = request(
        [
            {"role": "system", "content": "agent instructions"},
            {"role": "user", "content": "original task"},
            {"role": "user", "content": "irrelevant document " * 1000},
            {"role": "assistant", "content": "current command"},
            {"role": "user", "content": "current result"},
        ]
    )
    call = call_for(kwargs, [1.0, 0.0, 0.0, 0.0, 0.0])
    warm(call)
    plan = json.loads(_native.optimize_plan(json.dumps(call)))
    assert plan["kind"] == "rewritten"
    assert apply_call_mutations_openai(kwargs, plan["call"])["messages"] == [
        kwargs["messages"][i] for i in (0, 1, 3, 4)
    ]


def test_native_compression_abstains_for_opaque_tool_call_history() -> None:
    kwargs = request(
        [
            {"role": "user", "content": "original task"},
            {"role": "user", "content": "irrelevant document " * 1000},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        ]
    )
    before = copy.deepcopy(kwargs)
    call = call_for(kwargs, [0.0] * 4)
    warm(call)
    assert json.loads(_native.optimize_plan(json.dumps(call)))["kind"] == "pass_through"
    assert kwargs == before
