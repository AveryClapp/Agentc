"""Unit tests for the frozen-workload LiteLLM admission preflight."""

from __future__ import annotations

import json
import socket

import pytest

from bench.litellm_admission_preflight import (
    _blocked_network,
    _outcome_event,
    _plan_event,
    _span_summary,
)
from agentc._optimizer import Plan


def test_plan_event_retains_shape_but_not_message_content() -> None:
    call = {
        "model": "strong-model",
        "messages": [{"role": "user", "content": "private prompt"}],
        "parameters": {"max_output_tokens": 256},
        "call_site_id": "site",
    }
    plan = Plan(
        kind="rewritten",
        rule="OutputBudget",
        call={
            "model": "strong-model",
            "messages": call["messages"],
            "parameters": {"max_output_tokens": 64},
        },
    )

    event = _plan_event(call, plan, 1)

    assert event["input"]["message_count"] == 1
    assert event["rewritten"]["max_output_tokens"] == 64
    assert event["rules"] == ["OutputBudget"]
    assert "private prompt" not in json.dumps(event)


def test_outcome_event_retains_usage_and_cost() -> None:
    event = _outcome_event(
        {
            "input_tokens": 20,
            "output_tokens": 5,
            "latency_ms": 10.5,
            "cost_usd": 0.001,
            "call_site_id": "site",
            "output": "do not retain",
        },
        2,
    )

    assert event == {
        "sequence": 2,
        "input_tokens": 20,
        "output_tokens": 5,
        "latency_ms": 10.5,
        "cost_usd": 0.001,
        "call_site_id": "site",
    }


def test_span_summary_counts_actor_eligibility() -> None:
    spans = [
        {
            "status": "OK",
            "attributes": json.dumps(
                {
                    "agentc.optimization.scope": "tau2.evaluated_assistant",
                    "agentc.optimization.eligible": True,
                }
            ),
        },
        {
            "status": "OK",
            "attributes": json.dumps(
                {
                    "agentc.optimization.scope": "tau2.user_simulator",
                    "agentc.optimization.eligible": False,
                }
            ),
        },
    ]

    assert _span_summary(spans) == {
        "count": 2,
        "scope_counts": {
            "tau2.evaluated_assistant": 1,
            "tau2.user_simulator": 1,
        },
        "eligible": 1,
        "excluded": 1,
        "status_counts": {"OK": 2},
    }


def test_network_guard_blocks_socket_connections() -> None:
    with _blocked_network() as attempts:
        with pytest.raises(RuntimeError, match="network disabled"):
            socket.create_connection(("example.com", 443))

    assert attempts == ["('example.com', 443)"]
