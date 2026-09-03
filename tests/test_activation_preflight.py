"""Focused tests for the offline optimizer activation preflight."""

from __future__ import annotations

from pathlib import Path

from bench.activation_preflight import (
    _message_shape,
    _offline_completion,
    _offline_env,
    _shape_stats,
    _summarize_plan_events,
)


def test_offline_completion_is_deterministic_and_openai_shaped() -> None:
    kwargs = {
        "model": "gpt-4o-mini-2024-07-18",
        "messages": [{"role": "user", "content": "Where is the answer?"}],
    }
    first = _offline_completion(kwargs, 1)
    second = _offline_completion(kwargs, 2)

    assert first.model == kwargs["model"]
    assert first.choices[0].message.content == second.choices[0].message.content
    assert first.id != second.id
    assert first.usage.prompt_tokens > 0
    assert first.usage.completion_tokens > 0


def test_message_shape_hashes_content_without_returning_it() -> None:
    messages = [
        {"role": "system", "content": "secret system text"},
        {"role": "user", "content": "secret question"},
    ]
    count, prompt_bytes, digest = _message_shape(messages)

    assert count == 2
    assert prompt_bytes == len("secret system text".encode()) + len(
        "secret question".encode()
    )
    assert len(digest) == 16
    assert "secret" not in digest


def test_offline_env_overrides_live_provider_configuration(tmp_path: Path) -> None:
    env = _offline_env(
        {
            "OPENAI_API_KEY": "real-openai-key",
            "ANTHROPIC_API_KEY": "real-anthropic-key",
            "TOGETHER_API_KEY": "real-together-key",
            "BENCH_OPENAI_BASE_URL": "https://provider.example/v1",
        },
        storage_dir=tmp_path,
        tasks=8,
        model="test-model",
        hot_threshold=3,
    )

    assert env["OPENAI_API_KEY"] == "offline-preflight-no-network"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["TOGETHER_API_KEY"] == ""
    assert env["BENCH_OPENAI_BASE_URL"] == ""
    assert env["AGENTC_OPTIMIZE"] == "1"
    assert env["AGENTC_OPTIMIZE_SHADOW"] == "0"
    assert env["BENCH_MAX_TASKS"] == "8"


def test_shape_stats_use_observed_order_statistics() -> None:
    assert _shape_stats([]) == {
        "min": None,
        "median": None,
        "p95": None,
        "max": None,
    }
    assert _shape_stats([40, 10, 30, 20]) == {
        "min": 10,
        "median": 20,
        "p95": 40,
        "max": 40,
    }


def test_summary_counts_full_composed_rule_set_after_warmup() -> None:
    events = [
        {
            "call_site_id": "site-a",
            "call_site_ordinal": 1,
            "plan_kind": "pass_through",
            "rules": [],
            "input": {"prompt_bytes": 100, "message_count": 2},
        },
        {
            "call_site_id": "site-a",
            "call_site_ordinal": 2,
            "plan_kind": "pass_through",
            "rules": [],
            "input": {"prompt_bytes": 200, "message_count": 3},
        },
        {
            "call_site_id": "site-a",
            "call_site_ordinal": 3,
            "plan_kind": "pass_through",
            "rules": [],
            "input": {"prompt_bytes": 300, "message_count": 4},
        },
        {
            "call_site_id": "site-a",
            "call_site_ordinal": 4,
            "plan_kind": "composed",
            "rules": ["ContextCompress", "OutputBudget"],
            "input": {"prompt_bytes": 400, "message_count": 5},
        },
        {
            "call_site_id": "site-b",
            "call_site_ordinal": 1,
            "plan_kind": "pass_through",
            "rules": [],
            "input": {"prompt_bytes": 50, "message_count": 2},
        },
    ]

    summary = _summarize_plan_events(events, hot_threshold=3)

    assert summary["optimizer_decisions"] == 5
    assert summary["call_site_count"] == 2
    assert summary["warmup_decisions"] == 4
    assert summary["post_warmup_decisions"] == 1
    assert summary["post_warmup_activations"] == 1
    assert summary["post_warmup_activation_rate"] == 1.0
    assert summary["rule_activation_counts"] == {
        "ContextCompress": 1,
        "OutputBudget": 1,
    }
