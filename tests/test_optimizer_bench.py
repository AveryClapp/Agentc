"""Unit tests for benchmark process configuration and provenance."""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bench.optimizer_bench import (  # noqa: E402
    _effective_env,
    _has_live_credentials,
    _read_shadow_divergence,
)


def test_effective_env_loads_dotenv_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENAI_API_KEY=file-key\n")

    env = _effective_env(dotenv_path=dotenv)

    assert env["OPENAI_API_KEY"] == "file-key"
    assert _has_live_credentials(env) is True


def test_effective_env_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "shell-key")
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENAI_API_KEY=file-key\n")

    env = _effective_env(
        {"OPENAI_API_KEY": "run-key"},
        dotenv_path=dotenv,
    )

    assert env["OPENAI_API_KEY"] == "run-key"


@pytest.mark.parametrize(
    ("base_url", "key_name"),
    [
        ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
        ("https://api-inference.huggingface.co/v1", "HF_TOKEN"),
        ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    ],
)
def test_provider_specific_credentials_are_live(
    base_url: str,
    key_name: str,
) -> None:
    env = {
        "BENCH_OPENAI_BASE_URL": base_url,
        key_name: "provider-key",
    }

    assert _has_live_credentials(env) is True


def test_missing_or_empty_credentials_are_stub() -> None:
    assert _has_live_credentials({}) is False
    assert _has_live_credentials({"OPENAI_API_KEY": ""}) is False


def test_shadow_divergence_weights_sites_by_retained_window(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "cost_model.db") as connection:
        connection.execute(
            "CREATE TABLE rule_divergence ("
            "call_site_id TEXT NOT NULL, rule TEXT NOT NULL, "
            "n_samples INTEGER NOT NULL, window_samples INTEGER NOT NULL, "
            "divergence_mean REAL NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO rule_divergence VALUES (?, 'OutputBudget', ?, ?, ?)",
            [
                ("old-high-volume", 1_000, 2, 0.0),
                ("new-low-volume", 2, 2, 1.0),
            ],
        )

    [summary] = _read_shadow_divergence(tmp_path)

    assert summary.rule == "OutputBudget"
    assert summary.n_samples == 4
    assert summary.divergence_mean == pytest.approx(0.5)


def test_guard_eval_records_explicit_sampling_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GE_AGENT", "bench.agents.long_context_qa")
    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "1")
    monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "normalized")
    monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_BUDGET", "0.2")
    module = importlib.import_module("bench.run_guard_eval")
    module = importlib.reload(module)

    assert module._CSV_COLUMNS[-5:] == [
        "shadow_rate",
        "divergence_mode",
        "configured_divergence_budget",
        "shadow_calls_in_cost_totals",
        "agentc_git_commit",
    ]
    assert module.SHADOW_RATE == 1.0
    assert module.DIVERGENCE_MODE == "normalized"
    assert module.CONFIGURED_DIVERGENCE_BUDGET == "0.2"


def test_guard_eval_records_effective_defaults_for_invalid_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GE_AGENT", "bench.agents.long_context_qa")
    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "nan")
    monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "unknown")
    monkeypatch.delenv("AGENTC_SHADOW_DIVERGENCE_BUDGET", raising=False)
    module = importlib.import_module("bench.run_guard_eval")
    module = importlib.reload(module)

    assert module.SHADOW_RATE == 0.02
    assert module.DIVERGENCE_MODE == "lexical"
    assert module.CONFIGURED_DIVERGENCE_BUDGET == "rule_default"
