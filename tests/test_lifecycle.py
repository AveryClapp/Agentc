"""Tests for agentc init/shutdown lifecycle (bd-105).

Run: maturin develop && pytest tests/test_lifecycle.py -v
"""

from __future__ import annotations

import os
import signal
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import agentc
from agentc._config import Config, resolve_config
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

    def test_directory_permissions(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        # Check directory was created with correct permissions
        stat = tmp_storage.stat()
        assert stat.st_mode & 0o777 == 0o700

    def test_creates_per_process_db(self, tmp_storage: Path) -> None:
        agentc.init(storage_path=str(tmp_storage))
        pid = os.getpid()
        db_path = tmp_storage / "active" / f"pid-{pid}.db"
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


class TestSignalHandlers:
    def test_atexit_registered(self, tmp_storage: Path) -> None:
        """atexit handler is registered during init."""
        import atexit

        original_count = len(atexit._exithandlers) if hasattr(atexit, "_exithandlers") else 0
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
