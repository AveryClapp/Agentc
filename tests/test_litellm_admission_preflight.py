"""Unit tests for the frozen-workload LiteLLM admission preflight."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bench.litellm_admission_preflight import (
    _blocked_network,
    _configure_optimizer_environment,
    _outcome_event,
    _plan_event,
    _semantic_response_digest,
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


def test_optimizer_environment_uses_the_fresh_preflight_store() -> None:
    storage_path = Path("/tmp/isolated-agentc-preflight")
    with patch.dict(os.environ, {}, clear=True):
        _configure_optimizer_environment(storage_path)

        assert os.environ["AGENTC_STORAGE_PATH"] == str(storage_path)
        assert os.environ["AGENTC_OPTIMIZE"] == "1"
        assert os.environ["AGENTC_ENABLED_RULES"] == "OutputBudget"
        assert os.environ["OPENAI_API_KEY"] == "offline-preflight-no-network"


def test_semantic_response_digest_excludes_volatile_transport_fields() -> None:
    first = SimpleNamespace(
        role="assistant",
        content="stable answer",
        tool_calls=None,
        raw_data={"id": "random-1", "created": 1},
        generation_time_seconds=0.1,
    )
    second = SimpleNamespace(
        role="assistant",
        content="stable answer",
        tool_calls=None,
        raw_data={"id": "random-2", "created": 2},
        generation_time_seconds=9.9,
    )
    changed = SimpleNamespace(
        role="assistant",
        content="different answer",
        tool_calls=None,
    )

    assert _semantic_response_digest(first) == _semantic_response_digest(second)
    assert _semantic_response_digest(first) != _semantic_response_digest(changed)
