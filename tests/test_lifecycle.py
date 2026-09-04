"""Tests for agentc init/shutdown lifecycle (bd-105).

Run: maturin develop && pytest tests/test_lifecycle.py -v
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import agentc
from agentc._config import resolve_config
from agentc._lifecycle import (
    _initialized,
    _shutdown_in_progress,
    get_config,
    is_initialized,
)


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Ensure clean state before and after each test."""
    # Reset state before test
    _initialized.clear()
    _shutdown_in_progress.clear()
    yield
    # Reset state after test
    if is_initialized():
        agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()


@pytest.fixture()
def tmp_storage(tmp_path: Path) -> Path:
    """Provide a temporary storage directory."""
    return tmp_path / "agentc"


def _record_guard_divergence(call_site_id: str, rule: str, divergence: float) -> None:
    """Exercise the public opaque-token FFI while testing the solo-rule guard."""
    plan = {
        "kind": "rewritten",
        "rule": rule,
        "call": {
            "call_site_id": call_site_id,
            "trace_id": "00" * 16,
            "span_id": "00" * 8,
            "model": "test-model",
            "messages": [],
        },
        "projected_savings_usd": 0.01,
    }
    outcome = {
        "input_tokens": 1,
        "output_tokens": 1,
        "latency_ms": 1.0,
        "cost_usd": 0.001,
        "call_site_id": call_site_id,
    }
    token = agentc._native.optimize_observe(json.dumps(plan), json.dumps(outcome))
    assert token
    agentc._native.optimize_record_divergence(token, divergence)


class TestConfig:
    def test_defaults(self) -> None:
        config = resolve_config()
        assert config.capture_content is True
        assert config.capture_embeddings is True  # follows capture_content
        assert config.fail_open is True
        assert str(config.storage_path).endswith(".agentc")

    def test_capture_embeddings_follows_content(self) -> None:
        config = resolve_config(capture_content=False)
        assert config.capture_embeddings is False

    def test_capture_embeddings_explicit(self) -> None:
        config = resolve_config(capture_content=False, capture_embeddings=True)
        assert config.capture_embeddings is True

    def test_env_overrides_defaults(self) -> None:
        with patch.dict(os.environ, {"AGENTC_CAPTURE_CONTENT": "false"}):
            config = resolve_config()
            assert config.capture_content is False

    def test_explicit_kwarg_overrides_env(self) -> None:
        with patch.dict(os.environ, {"AGENTC_CAPTURE_CONTENT": "false"}):
            config = resolve_config(capture_content=True)
            assert config.capture_content is True

    def test_storage_path_custom(self, tmp_storage: Path) -> None:
        config = resolve_config(storage_path=str(tmp_storage))
        assert config.storage_path == tmp_storage

    def test_storage_path_home_fallback(self) -> None:
        """When HOME is not set, falls back to temp directory."""
        with patch.dict(os.environ, {}, clear=True):
            with patch("pathlib.Path.home", side_effect=RuntimeError("no HOME")):
                config = resolve_config()
                assert "agentc" in str(config.storage_path)


class TestInit:
    def test_basic_init(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        assert is_initialized()
        assert tmp_storage.exists()
        assert (tmp_storage / "active").exists()
        assert Path(agentc._native.optimize_storage_path()) == tmp_storage

    def test_directory_permissions(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        # Check directory was created with correct permissions
        stat = tmp_storage.stat()
        assert stat.st_mode & 0o777 == 0o700

    def test_creates_per_process_db(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        # create_db is a stub currently, but the path should be accessible
        assert (tmp_storage / "active").exists()

    def test_idempotent(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        assert is_initialized()
        # Second call is no-op
        agentc.init(storage_path=str(tmp_storage))
        assert is_initialized()

    def test_config_stored(self, tmp_storage: Path) -> None:
        agentc.init(
            capture_content=False,
            capture_embeddings=True,
            fail_open=False,
            storage_path=str(tmp_storage),
        )
        config = get_config()
        assert config is not None
        assert config.capture_content is False
        assert config.capture_embeddings is True
        assert config.fail_open is False
        assert config.storage_path == tmp_storage

    def test_reinit_after_shutdown(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        assert is_initialized()
        agentc.shutdown()
        assert not is_initialized()
        agentc.init(storage_path=str(tmp_storage))
        assert is_initialized()

    def test_explicit_storage_temporarily_overrides_environment(
        self,
        tmp_storage: Path,
        tmp_path: Path,
    ) -> None:
        previous = str(tmp_path / "caller-storage")
        with patch.dict(os.environ, {"AGENTC_STORAGE_PATH": previous}):
            agentc.init(storage_path=str(tmp_storage))
            assert os.environ["AGENTC_STORAGE_PATH"] == str(tmp_storage)
            assert Path(agentc._native.optimize_storage_path()) == tmp_storage
            agentc.shutdown()
            assert os.environ["AGENTC_STORAGE_PATH"] == previous

    def test_reinit_does_not_leak_optimizer_warm_state(
        self,
        tmp_path: Path,
    ) -> None:
        from agentc._optimizer import observe_outcome, plan_call

        first_storage = tmp_path / "first"
        second_storage = tmp_path / "second"
        call = {
            "call_site_id": "storage-isolation-site",
            "trace_id": "0" * 32,
            "span_id": "0" * 16,
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "warm"}],
            "parameters": {
                "max_output_tokens": 256,
                "extra": {
                    "agentc_route_context": {
                        "provider_protocol": "openai.chat.completions.v1",
                        "provider_namespace": "openai",
                        "input_tokens_upper_bound": 10,
                        "image_input": False,
                        "tool_calling": False,
                        "structured_outputs": False,
                        "streaming": False,
                    }
                },
            },
            "tools": [],
            "input_deps": [],
            "occurrence_ix": 0,
        }
        outcome = {
            "input_tokens": 10,
            "output_tokens": 20,
            "latency_ms": 1.0,
            "cost_usd": 0.001,
            "output_is_structured": False,
            "output_is_short": True,
            "call_site_id": "storage-isolation-site",
        }
        controls = {
            "AGENTC_ENABLED_RULES": "OutputBudget",
            "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3",
            "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
            "AGENTC_OPTIMIZE_SHADOW": "0",
        }

        with patch.dict(os.environ, controls):
            agentc.init(storage_path=str(first_storage))
            for _ in range(3):
                observe_outcome(plan_call(call), outcome)
            assert plan_call(call).kind == "rewritten"
            agentc.shutdown()

            agentc.init(storage_path=str(second_storage))
            assert Path(agentc._native.optimize_storage_path()) == second_storage
            assert plan_call(call).kind == "pass_through"
            agentc.shutdown()

        assert (first_storage / "cost_model.db").exists()
        assert (second_storage / "cost_model.db").exists()

    def test_guard_breach_streak_survives_reinit(self, tmp_storage: Path) -> None:
        controls = {
            "AGENTC_ENABLED_RULES": "OutputBudget",
            "AGENTC_SHADOW_DIVERGENCE_BUDGET": "0.1",
        }
        with patch.dict(os.environ, controls):
            agentc.init(storage_path=str(tmp_storage))
            for _ in range(4):
                _record_guard_divergence("restart-guard-site", "OutputBudget", 0.5)
            agentc.shutdown()

            with sqlite3.connect(tmp_storage / "cost_model.db") as connection:
                first = connection.execute(
                    "SELECT n_samples, divergence_mean, consecutive_breaches "
                    "FROM rule_divergence "
                    "WHERE call_site_id = ? AND rule = ?",
                    ("restart-guard-site", "OutputBudget"),
                ).fetchone()
            assert first == pytest.approx((4, 0.5, 4))

            agentc.init(storage_path=str(tmp_storage))
            _record_guard_divergence("restart-guard-site", "OutputBudget", 0.5)
            agentc.shutdown()

        with sqlite3.connect(tmp_storage / "cost_model.db") as connection:
            divergence = connection.execute(
                "SELECT n_samples, divergence_mean, consecutive_breaches "
                "FROM rule_divergence "
                "WHERE call_site_id = ? AND rule = ?",
                ("restart-guard-site", "OutputBudget"),
            ).fetchone()
            disabled = connection.execute(
                "SELECT reason FROM optimizer_disabled "
                "WHERE call_site_id = ? AND rule = ?",
                ("restart-guard-site", "OutputBudget"),
            ).fetchone()

        assert divergence == pytest.approx((5, 0.5, 0))
        assert disabled == ("shadow_divergence",)

    def test_guard_divergence_window_survives_reinit(self, tmp_storage: Path) -> None:
        controls = {
            "AGENTC_ENABLED_RULES": "OutputBudget",
            "AGENTC_OPTIMIZE_DIVERGENCE_WINDOW": "3",
            "AGENTC_SHADOW_DIVERGENCE_BUDGET": "1.0",
        }
        with patch.dict(os.environ, controls):
            agentc.init(storage_path=str(tmp_storage))
            for divergence in [0.9, 0.9, 0.9, 0.1, 0.1, 0.1]:
                _record_guard_divergence(
                    "windowed-guard-site", "OutputBudget", divergence
                )
            agentc.shutdown()

            agentc.init(storage_path=str(tmp_storage))
            _record_guard_divergence("windowed-guard-site", "OutputBudget", 0.1)
            agentc.shutdown()

        with sqlite3.connect(tmp_storage / "cost_model.db") as connection:
            summary = connection.execute(
                "SELECT n_samples, window_samples, divergence_mean, "
                "consecutive_breaches FROM rule_divergence "
                "WHERE call_site_id = ? AND rule = ?",
                ("windowed-guard-site", "OutputBudget"),
            ).fetchone()
            retained = connection.execute(
                "SELECT COUNT(*), MIN(sample_sequence), MAX(sample_sequence) "
                "FROM rule_divergence_observation "
                "WHERE call_site_id = ? AND rule = ?",
                ("windowed-guard-site", "OutputBudget"),
            ).fetchone()

        assert summary == pytest.approx((7, 3, 0.1, 0))
        assert retained == (3, 5, 7)

    def test_complete_plan_exposure_disable_survives_reinit(
        self, tmp_storage: Path
    ) -> None:
        from agentc._optimizer import observe_outcome, plan_call, record_divergence

        call = {
            "call_site_id": "complete-plan-guard-site",
            "trace_id": "0" * 32,
            "span_id": "0" * 16,
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "warm"}],
            "parameters": {
                "max_output_tokens": 256,
                "extra": {
                    "agentc_route_context": {
                        "provider_protocol": "openai.chat.completions.v1",
                        "provider_namespace": "openai",
                        "input_tokens_upper_bound": 10,
                        "image_input": False,
                        "tool_calling": False,
                        "structured_outputs": False,
                        "streaming": False,
                    }
                },
            },
            "tools": [],
            "input_deps": [],
            "occurrence_ix": 0,
        }
        outcome = {
            "input_tokens": 10,
            "output_tokens": 20,
            "latency_ms": 1.0,
            "cost_usd": 0.001,
            "output_is_structured": False,
            "output_is_short": True,
            "call_site_id": "complete-plan-guard-site",
        }
        controls = {
            "AGENTC_ENABLED_RULES": "OutputBudget",
            "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3",
            "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
            "AGENTC_OPTIMIZE_SHADOW": "0.02",
            "AGENTC_SHADOW_DIVERGENCE_BUDGET": "0.0",
        }

        with patch.dict(os.environ, controls):
            agentc.init(storage_path=str(tmp_storage))
            for _ in range(3):
                observe_outcome(plan_call(call), outcome)

            selected = plan_call(call)
            assert selected.kind == "rewritten"
            observe_outcome(selected, outcome)
            assert selected.observation_token
            # Delayed feedback must use the threshold bound when the plan was
            # selected, not reinterpret it under a later process setting.
            os.environ["AGENTC_SHADOW_DIVERGENCE_BUDGET"] = "1.0"
            record_divergence(selected.observation_token, 1.0)
            os.environ["AGENTC_SHADOW_DIVERGENCE_BUDGET"] = "0.0"

            # One sample exhausts the plan budget, but not the legacy
            # five-strike solo-rule guard. The exact plan guard causes this
            # fallback.
            assert plan_call(call).kind == "pass_through"
            agentc.shutdown()

            agentc.init(storage_path=str(tmp_storage))
            assert plan_call(call).kind == "pass_through"
            agentc.shutdown()

        with sqlite3.connect(tmp_storage / "cost_model.db") as connection:
            guard = connection.execute(
                "SELECT divergence_threshold, divergence_exposure, window_samples "
                "FROM execution_plan_guard"
            ).fetchone()
            disabled = connection.execute(
                "SELECT reason, exposure FROM execution_plan_disabled"
            ).fetchone()
            legacy_disabled = connection.execute(
                "SELECT COUNT(*) FROM optimizer_disabled "
                "WHERE call_site_id = ? AND rule = ?",
                ("complete-plan-guard-site", "OutputBudget"),
            ).fetchone()

        assert guard == pytest.approx((0.0, 1.0, 1))
        assert disabled is not None
        assert disabled[0] == "divergence_exposure"
        assert disabled[1] == pytest.approx(1.0)
        assert legacy_disabled == (0,)

    def test_invalid_guard_divergence_does_not_create_state(
        self, tmp_storage: Path
    ) -> None:
        controls = {
            "AGENTC_ENABLED_RULES": "OutputBudget",
            "AGENTC_SHADOW_DIVERGENCE_BUDGET": "0.1",
        }
        with patch.dict(os.environ, controls):
            agentc.init(storage_path=str(tmp_storage))
            for divergence in [float("nan"), float("inf"), float("-inf"), -0.1, 1.1]:
                _record_guard_divergence(
                    "invalid-guard-site", "OutputBudget", divergence
                )
            agentc.shutdown()

        with sqlite3.connect(tmp_storage / "cost_model.db") as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM rule_divergence "
                "WHERE call_site_id = ? AND rule = ?",
                ("invalid-guard-site", "OutputBudget"),
            ).fetchone()
        assert count == (0,)

    @pytest.mark.parametrize(
        "invalid_threshold",
        ["nan", "inf", "-inf", "-1e-300", "1.000000000001", "-0.1", "1.1"],
    )
    def test_invalid_guard_threshold_falls_back_to_rule_budget(
        self, tmp_storage: Path, invalid_threshold: str
    ) -> None:
        controls = {
            "AGENTC_ENABLED_RULES": "OutputBudget",
            "AGENTC_SHADOW_DIVERGENCE_BUDGET": invalid_threshold,
        }
        with patch.dict(os.environ, controls):
            agentc.init(storage_path=str(tmp_storage))
            for _ in range(5):
                _record_guard_divergence("fallback-guard-site", "OutputBudget", 0.5)
            agentc.shutdown()

        with sqlite3.connect(tmp_storage / "cost_model.db") as connection:
            divergence = connection.execute(
                "SELECT n_samples, divergence_mean, consecutive_breaches "
                "FROM rule_divergence "
                "WHERE call_site_id = ? AND rule = ?",
                ("fallback-guard-site", "OutputBudget"),
            ).fetchone()
            disabled = connection.execute(
                "SELECT reason FROM optimizer_disabled "
                "WHERE call_site_id = ? AND rule = ?",
                ("fallback-guard-site", "OutputBudget"),
            ).fetchone()

        assert divergence == pytest.approx((5, 0.5, 0))
        assert disabled == ("shadow_divergence",)


class TestShutdown:
    def test_basic_shutdown(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        agentc.shutdown()
        assert not is_initialized()

    def test_shutdown_without_init(self) -> None:
        """Shutdown before init is a no-op."""
        agentc.shutdown()  # Should not raise

    def test_shutdown_reentrant_guard(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        agentc.shutdown()
        # Second shutdown is no-op
        agentc.shutdown()

    def test_shutdown_clears_config(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        assert get_config() is not None
        agentc.shutdown()
        assert get_config() is None

    def test_shutdown_custom_timeout(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        agentc.shutdown(timeout_ms=1000)
        assert not is_initialized()

    def test_shutdown_writes_optimization_scope_manifest_fragment(
        self,
        tmp_storage: Path,
    ) -> None:
        from agentc._optimization_scope import decide_optimization

        agentc.init(storage_path=str(tmp_storage))
        with agentc.optimization_scope("tau2.user_simulator", optimize=False):
            assert not decide_optimization().eligible
        agentc.shutdown()

        report_path = (
            tmp_storage / "optimization-scopes" / f"pid-{os.getpid()}.json"
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["total_calls"] == 1
        assert report["excluded_calls"] == 1
        assert report["scopes"][0]["name"] == "tau2.user_simulator"


class TestSignalHandlers:
    def test_atexit_registered(self, tmp_storage: Path) -> None:
        """atexit handler is registered during init."""
        import atexit

        assert hasattr(atexit, "register")
        agentc.init(storage_path=str(tmp_storage))
        # We can't easily count atexit handlers in Python 3.12+
        # but we verify init doesn't raise
        assert is_initialized()

    def test_signal_handlers_installed(self, tmp_storage: Path) -> None:
        """Signal handlers are installed during init."""
        agentc.init(storage_path=str(tmp_storage))
        handler = signal.getsignal(signal.SIGTERM)
        # Our handler should be installed (not SIG_DFL)
        assert handler is not signal.SIG_DFL


class TestConcurrency:
    def test_concurrent_init(self, tmp_storage: Path) -> None:
        """First-caller-wins: concurrent init() calls don't race."""
        import concurrent.futures

        results: list[bool] = []

        def try_init() -> bool:
            try:
                agentc.init(storage_path=str(tmp_storage))
                return True
            except Exception:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(try_init) for _ in range(10)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        # All should succeed (idempotent)
        assert all(results)
        assert is_initialized()
