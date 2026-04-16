"""End-to-end tests for the @memoize decorator.

Covers the three exit-criteria scenarios from bd-dek:

- Decorator round-trip: a wrapped function returns the cached value on the
  second call (lookup_hit).
- Fail-open on corruption: injecting a bad DB path or corrupting the store
  causes a miss, never an exception (corruption_fail_open).
- Writer survives FFI panic: a forced panic in ``cache_insert`` does not
  take down the background writer thread (insert_panic).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import agentc
from agentc import _memoize, _writer


@pytest.fixture
def agentc_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Initialize agentc with an isolated storage dir; shutdown after the test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTC_MEMOIZE", raising=False)
    monkeypatch.delenv("AGENTC_MEMOIZE_SIMILARITY", raising=False)
    monkeypatch.delenv("AGENTC_MEMOIZE_TTL", raising=False)

    # agentc is a process singleton; force re-init under the patched HOME.
    if agentc.is_initialized():
        agentc.shutdown(timeout_ms=500)
    agentc.init(storage_path=str(tmp_path / ".agentc"))
    yield
    if agentc.is_initialized():
        agentc.shutdown(timeout_ms=1000)


def _wait_for_writer_drain(deadline_s: float = 2.0) -> None:
    """Force the writer to flush its batch and block until the commit lands."""
    _writer.flush_blocking(timeout_s=deadline_s)


def test_decorator_round_trip_hits_cache(agentc_init):
    """Second call with the same arg returns the cached value, skipping the body."""
    calls = {"n": 0}

    @agentc.memoize(ttl=60, similarity=1.0)
    def summarize(text: str) -> str:
        calls["n"] += 1
        return f"summary: {text}"

    first = summarize("alpha")
    assert first == "summary: alpha"
    assert calls["n"] == 1

    _wait_for_writer_drain()

    second = summarize("alpha")
    assert second == "summary: alpha"
    assert calls["n"] == 1, "cache hit should skip the function body"


def test_decorator_returns_distinct_values_for_distinct_inputs(agentc_init):
    @agentc.memoize(ttl=60, similarity=1.0)
    def echo(text: str) -> str:
        return f"got: {text}"

    assert echo("a") == "got: a"
    _wait_for_writer_drain()
    assert echo("b") == "got: b"
    _wait_for_writer_drain()
    # Hits for both.
    assert echo("a") == "got: a"
    assert echo("b") == "got: b"


def test_env_override_disables_memoize(agentc_init, monkeypatch: pytest.MonkeyPatch):
    """AGENTC_MEMOIZE=0 re-evaluated at decoration time makes every call a miss."""
    monkeypatch.setenv("AGENTC_MEMOIZE", "0")

    calls = {"n": 0}

    @agentc.memoize(ttl=60, similarity=1.0)
    def work(text: str) -> str:
        calls["n"] += 1
        return text

    work("x")
    _wait_for_writer_drain()
    work("x")
    assert calls["n"] == 2, "env override must disable caching"


def test_enabled_predicate_gates_cache(agentc_init):
    calls = {"n": 0}

    @agentc.memoize(ttl=60, similarity=1.0, enabled=lambda text: text != "skip")
    def work(text: str) -> str:
        calls["n"] += 1
        return text.upper()

    work("skip")
    work("skip")
    assert calls["n"] == 2

    work("keep")
    _wait_for_writer_drain()
    work("keep")
    assert calls["n"] == 3


def test_corruption_fail_open_lookup(agentc_init):
    """An exception in cache_lookup must surface as a miss, never propagate."""
    calls = {"n": 0}

    @agentc.memoize(ttl=60, similarity=1.0)
    def work(text: str) -> str:
        calls["n"] += 1
        return text

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected DB corruption")

    # Patch the symbol `cache_lookup` in `_memoize`'s module namespace via
    # sys.modules since it is imported lazily inside the wrapper.
    with patch("agentc._native.cache_lookup", side_effect=boom):
        # Two calls — both should execute the body, not raise.
        work("x")
        work("x")

    assert calls["n"] == 2


def test_corruption_fail_open_insert(agentc_init):
    """An exception thrown by enqueue must not leak out of the wrapper."""
    calls = {"n": 0}

    @agentc.memoize(ttl=60, similarity=1.0)
    def work(text: str) -> str:
        calls["n"] += 1
        return text

    def explode(_msg):
        raise RuntimeError("injected writer failure")

    with patch.object(_memoize, "enqueue_cache_insert", side_effect=explode):
        result = work("boom")

    assert result == "boom"
    assert calls["n"] == 1


def test_writer_survives_ffi_panic(agentc_init):
    """A panicking cache_insert must not kill the writer thread."""
    writer_thread = _writer._writer_thread
    assert writer_thread is not None and writer_thread.is_alive()

    @agentc.memoize(ttl=60, similarity=1.0)
    def work(text: str) -> str:
        return text.upper()

    # Inject a native-side panic via a fake cache_insert that raises
    # BaseException (matches how PyO3 PanicException propagates).
    class _FakePanic(BaseException):
        pass

    def panic_insert(*_args, **_kwargs):
        raise _FakePanic("simulated Rust panic")

    with patch("agentc._native.cache_insert", side_effect=panic_insert):
        work("a")
        _wait_for_writer_drain()
        work("b")
        _wait_for_writer_drain()
        work("c")
        _wait_for_writer_drain()

    # After the panics the writer thread must still be alive and processing.
    assert _writer._writer_thread is writer_thread
    assert writer_thread.is_alive(), "writer thread must survive FFI panic"

    # After the patch is lifted, fresh inserts must still land.
    calls = {"n": 0}

    @agentc.memoize(ttl=60, similarity=1.0)
    def after(text: str) -> str:
        calls["n"] += 1
        return text

    after("post-panic")
    _wait_for_writer_drain()
    after("post-panic")
    assert calls["n"] == 1, "writer must resume serving cache inserts after panic"


def test_cache_invalidate_all_returns_clean_miss(agentc_init):
    calls = {"n": 0}

    @agentc.memoize(ttl=60, similarity=1.0)
    def work(text: str) -> str:
        calls["n"] += 1
        return text

    work("a")
    _wait_for_writer_drain()
    work("a")
    assert calls["n"] == 1

    agentc.cache_invalidate_all()
    work("a")
    assert calls["n"] == 2


def test_cache_invalidate_pattern_scopes_to_call_site(agentc_init):
    # Distinct prompts so each call produces a separate cache row — the cache
    # key is (prompt, model, params); call_site_id is only an annotation.
    @agentc.memoize(ttl=60, similarity=1.0, call_site_id="site.one")
    def one(text: str) -> str:
        return f"one:{text}"

    @agentc.memoize(ttl=60, similarity=1.0, call_site_id="site.two")
    def two(text: str) -> str:
        return f"two:{text}"

    one("first-prompt")
    two("second-prompt")
    _wait_for_writer_drain()

    removed = agentc.cache_invalidate("site.one")
    assert removed >= 1, "site.one's entry should exist and be removed"

    remaining = agentc.cache_invalidate("site.two")
    assert remaining >= 1, "site.two's entry should survive site.one's invalidation"
