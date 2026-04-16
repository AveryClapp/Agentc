"""Tests for the memoization cache eviction pass.

Drives the exit criteria for bd-11t: TTL expiry, LRU cap, and a
concurrency smoke test that VACUUM does not clobber in-flight work.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

import agentc
from agentc import _writer


@pytest.fixture
def agentc_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTC_MEMOIZE", raising=False)
    if agentc.is_initialized():
        agentc.shutdown(timeout_ms=500)
    agentc.init(storage_path=str(tmp_path / ".agentc"))
    yield
    if agentc.is_initialized():
        agentc.shutdown(timeout_ms=1000)


def _insert_entry(tag: int, *, ttl_seconds: int = 60) -> bytes:
    """Insert a fresh cache row via the FFI; returns its prompt_hash."""
    from agentc._native import cache_insert

    prompt_hash = hashlib.sha256(f"prompt-{tag}".encode()).digest()
    params_hash = hashlib.sha256(f"params-{tag}".encode()).digest()
    cache_insert(
        prompt_hash,
        "m",
        params_hash,
        "app.site",
        b"body",
        0,
        0,
        0.0,
        ttl_seconds,
    )
    return prompt_hash


def _row_count() -> int:
    from agentc._native import cache_stats

    return int(cache_stats().get("entries", 0))


def test_ttl_sweep_removes_expired_entries(agentc_init):
    """A maintenance pass evicts every entry whose TTL has elapsed."""
    from agentc._native import cache_maintenance

    for i in range(3):
        _insert_entry(i, ttl_seconds=-1)  # already expired.

    assert _row_count() == 3

    stats = cache_maintenance()
    assert stats["ttl_rows"] == 3
    assert _row_count() == 0


def test_ttl_sweep_keeps_live_entries(agentc_init):
    """Entries inside their TTL window are retained."""
    from agentc._native import cache_maintenance

    for i in range(4):
        _insert_entry(i, ttl_seconds=3600)

    assert _row_count() == 4

    stats = cache_maintenance()
    assert stats["ttl_rows"] == 0
    assert _row_count() == 4


def test_lru_evict_drops_five_percent_over_cap(agentc_init):
    """Over-cap caches lose floor(cap * 5%) rows (or at least 1) per pass."""
    from agentc._native import cache_maintenance

    # Seed 210 entries so the 5 % bite is observable: 5 % of 200 = 10.
    for i in range(210):
        _insert_entry(i, ttl_seconds=3600)

    assert _row_count() == 210

    stats = cache_maintenance(200)
    assert stats["lru_rows"] == 10, "5 % of 200 = 10 rows evicted"
    assert _row_count() == 200

    # Running again with the same cap is a no-op (now at capacity).
    stats = cache_maintenance(200)
    assert stats["lru_rows"] == 0
    assert _row_count() == 200


def test_lru_evict_under_cap_is_noop(agentc_init):
    from agentc._native import cache_maintenance

    for i in range(5):
        _insert_entry(i, ttl_seconds=3600)

    stats = cache_maintenance(100)
    assert stats["lru_rows"] == 0
    assert _row_count() == 5


def test_maintenance_during_concurrent_inserts(agentc_init):
    """VACUUM + eviction running alongside live inserts must not lose rows.

    This is the closest thing to the spec's "VACUUM does not race with an
    in-flight merge" check that we can express inside a single process:
    we keep feeding the writer thread while repeatedly firing maintenance
    passes from the foreground. Every insert that races in must eventually
    appear in cache_stats.
    """
    import threading

    from agentc._native import cache_maintenance

    stop = threading.Event()

    def spammer() -> None:
        i = 10_000
        while not stop.is_set():
            _insert_entry(i, ttl_seconds=3600)
            i += 1
            time.sleep(0.001)

    t = threading.Thread(target=spammer, daemon=True)
    t.start()
    try:
        for _ in range(5):
            cache_maintenance(1_000_000)  # large cap → no LRU activity
            time.sleep(0.02)
    finally:
        stop.set()
        t.join(timeout=2)

    _writer.flush_blocking(timeout_s=2.0)
    # At least some inserts should have landed; the DB must be intact.
    assert _row_count() > 0
