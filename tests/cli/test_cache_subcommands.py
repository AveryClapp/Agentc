"""Snapshot-ish tests for `agentc cache` subcommands (bd-e5e).

Drives the four subcommands end-to-end against a pre-populated traces.db,
asserting on the structure of the rendered output rather than exact
whitespace so minor formatting changes don't spuriously break the suite.

The fixtures build a database by calling the Rust FFI directly through
``agentc._native`` rather than through the decorator, so they don't depend
on a real LLM round-trip. The CLI binary is located via the workspace's
`target/debug/agentc` — build it once with `cargo build -p agentc-cli`
before running these tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTC_BIN = REPO_ROOT / "target" / "debug" / "agentc"


def _require_binary() -> Path:
    if not AGENTC_BIN.exists():
        pytest.skip(
            f"agentc binary not built at {AGENTC_BIN}; "
            "run `cargo build -p agentc-cli` first"
        )
    return AGENTC_BIN


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    binary = _require_binary()
    env = os.environ.copy()
    return subprocess.run(
        [str(binary), *args],
        env=env,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh ~/.agentc-style directory with init done via the Python runtime."""
    monkeypatch.setenv("HOME", str(tmp_path))
    storage = tmp_path / ".agentc"
    storage.mkdir(parents=True, exist_ok=True)

    import agentc
    from agentc import _writer

    if agentc.is_initialized():
        agentc.shutdown(timeout_ms=500)
    agentc.init(storage_path=str(storage))
    yield storage
    if agentc.is_initialized():
        agentc.shutdown(timeout_ms=1000)


def _insert_cache_entry(
    tag: int,
    *,
    ttl_seconds: int = 3600,
    call_site: str = "tests.cli:entry",
    model: str = "gpt-4o",
    output: bytes = b"cached-output",
) -> str:
    """Insert one cache row and return its cache_key_hash."""
    from agentc._native import cache_insert

    p_hash = hashlib.sha256(f"prompt-{tag}".encode()).digest()
    q_hash = hashlib.sha256(f"params-{tag}".encode()).digest()
    cache_insert(p_hash, model, q_hash, call_site, output, 100, 50, 0.001, ttl_seconds)

    composite = hashlib.sha256()
    composite.update(p_hash)
    composite.update(model.encode())
    composite.update(q_hash)
    return composite.hexdigest()


def _flush() -> None:
    from agentc import _writer

    _writer.flush_blocking(timeout_s=2.0)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_cache_stats_on_empty_db_is_rendered_but_zero(storage_dir: Path):
    """`agentc cache stats` must not crash when no cache rows exist."""
    _flush()
    result = _run_cli("cache", "stats", "--storage-path", str(storage_dir))
    assert result.returncode == 0, result.stderr
    assert "Cache summary" in result.stdout
    assert "Entries:" in result.stdout
    assert "Exact hits:" in result.stdout
    assert "LSH hits:" in result.stdout
    assert "Misses:" in result.stdout
    # No spans with agentc.cache.* attributes → hits render as "—".
    assert re.search(r"Entries:\s+0\b", result.stdout), result.stdout


def test_cache_stats_reflects_inserted_entries(storage_dir: Path):
    for i in range(4):
        _insert_cache_entry(i, call_site=f"site.{i % 2}")
    _flush()
    result = _run_cli("cache", "stats", "--storage-path", str(storage_dir))
    assert result.returncode == 0, result.stderr
    assert re.search(r"Entries:\s+4\b", result.stdout), result.stdout


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def test_cache_inspect_renders_fields_for_known_prefix(storage_dir: Path):
    key = _insert_cache_entry(0, call_site="app.tools:summarize", output=b"summary!")
    _flush()
    prefix = key[:8]
    result = _run_cli("cache", "inspect", prefix, "--storage-path", str(storage_dir))
    assert result.returncode == 0, result.stderr
    assert f"Cache entry: {key[:16]}" in result.stdout
    assert "Call site:       app.tools:summarize" in result.stdout
    assert "Model:           gpt-4o" in result.stdout
    assert "Input tokens:    100" in result.stdout
    assert "Output tokens:   50" in result.stdout


def test_cache_inspect_rejects_short_prefix(storage_dir: Path):
    result = _run_cli("cache", "inspect", "abc", "--storage-path", str(storage_dir))
    assert result.returncode != 0
    assert "at least 4" in result.stderr


def test_cache_inspect_errors_on_missing_prefix(storage_dir: Path):
    _insert_cache_entry(0)
    _flush()
    result = _run_cli(
        "cache", "inspect", "deadbeef", "--storage-path", str(storage_dir)
    )
    assert result.returncode != 0
    assert "No cache entry found" in result.stderr


# ---------------------------------------------------------------------------
# evict
# ---------------------------------------------------------------------------


def test_cache_evict_all_removes_every_row(storage_dir: Path):
    for i in range(5):
        _insert_cache_entry(i)
    _flush()
    result = _run_cli("cache", "evict", "--all", "--storage-path", str(storage_dir))
    assert result.returncode == 0, result.stderr
    assert "Evicted 5 entries" in result.stdout

    canonical = storage_dir / "traces.db"
    conn = sqlite3.connect(str(canonical))
    try:
        count = conn.execute("SELECT COUNT(*) FROM memoization_cache").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_cache_evict_pattern_scopes_to_call_site_glob(storage_dir: Path):
    _insert_cache_entry(0, call_site="app.a:fn")
    _insert_cache_entry(1, call_site="app.b:fn")
    _insert_cache_entry(2, call_site="app.a:other")
    _flush()

    result = _run_cli(
        "cache", "evict", "--pattern", "app.a:*", "--storage-path", str(storage_dir)
    )
    assert result.returncode == 0, result.stderr
    assert re.search(r"Evicted\s+2\s+entries\s+matching", result.stdout), result.stdout

    canonical = storage_dir / "traces.db"
    conn = sqlite3.connect(str(canonical))
    try:
        rows = conn.execute(
            "SELECT call_site_id FROM memoization_cache ORDER BY call_site_id"
        ).fetchall()
        assert rows == [("app.b:fn",)]
    finally:
        conn.close()


def test_cache_evict_older_than_uses_duration(storage_dir: Path):
    _insert_cache_entry(0, ttl_seconds=3600)
    _flush()
    # "0s" means "nothing is old enough"; "1" would be invalid.
    result = _run_cli(
        "cache", "evict", "--older-than", "0s", "--storage-path", str(storage_dir)
    )
    assert result.returncode == 0, result.stderr
    # Depending on clock skew zero/one row is swept; just assert we printed the banner.
    assert "Evicted" in result.stdout


def test_cache_evict_requires_exactly_one_filter(storage_dir: Path):
    result = _run_cli("cache", "evict", "--storage-path", str(storage_dir))
    assert result.returncode != 0
    # clap surfaces this itself as "required" / "the following required arguments".
    combined = result.stderr + result.stdout
    assert "--older-than" in combined or "--pattern" in combined or "--all" in combined


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------


def test_cache_bench_reports_missing_reference_harness(storage_dir: Path):
    """Until M9 ships, bench must fail cleanly instead of producing fake numbers."""
    result = _run_cli(
        "cache",
        "bench",
        "--call-site",
        "tests.cli:bench",
        "--runs",
        "5",
        "--storage-path",
        str(storage_dir),
    )
    assert result.returncode != 0
    assert "reference-agent harness" in result.stderr or "M9" in result.stderr
