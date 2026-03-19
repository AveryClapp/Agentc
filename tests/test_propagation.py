"""Tests for context propagation (bd-398).

Run: maturin develop && pytest tests/test_propagation.py -v
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import agentc
from agentc._context import SpanContext, get_current_span, set_current_span
from agentc._lifecycle import _initialized, _shutdown_in_progress
from agentc._propagation import (
    attach_trace_context,
    get_trace_context,
    inject_trace_headers,
    traced_executor,
)


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    _initialized.clear()
    _shutdown_in_progress.clear()
    set_current_span(None)
    yield
    if agentc.is_initialized():
        agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()
    set_current_span(None)
    import agentc._span as span_mod
    span_mod._logged_not_initialized = False


@pytest.fixture()
def tmp_storage(tmp_path: Path) -> Path:
    return tmp_path / "agentc"


@pytest.fixture()
def initialized(tmp_storage: Path) -> Path:
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(tmp_storage))
    return tmp_storage


class TestTracedExecutor:
    def test_propagates_context(self, initialized: Path) -> None:
        """Child thread span has correct parent via traced_executor."""
        parent_ctx = SpanContext(span_id="abcd123456789012", trace_id="abcd1234567890abcdef567890abcdef", name="parent")
        set_current_span(parent_ctx)

        results: list[dict[str, str | None]] = []

        def worker() -> dict[str, str | None]:
            ctx = get_current_span()
            return {
                "trace_id": ctx.trace_id if ctx else None,
                "span_id": ctx.span_id if ctx else None,
                "name": ctx.name if ctx else None,
            }

        with traced_executor(ThreadPoolExecutor(max_workers=2)) as pool:
            future = pool.submit(worker)
            result = future.result()
            results.append(result)

        set_current_span(None)

        assert results[0]["trace_id"] == "abcd1234567890abcdef567890abcdef"
        assert results[0]["span_id"] == "abcd123456789012"
        assert results[0]["name"] == "parent"

    def test_four_concurrent_tasks_share_trace(self, initialized: Path) -> None:
        parent_ctx = SpanContext(span_id="abcd123456789012", trace_id="abcd1234567890abcdef567890abcdef", name="parent")
        set_current_span(parent_ctx)

        def worker(i: int) -> str | None:
            ctx = get_current_span()
            return ctx.trace_id if ctx else None

        with traced_executor(ThreadPoolExecutor(max_workers=4)) as pool:
            futures = [pool.submit(worker, i) for i in range(4)]
            trace_ids = [f.result() for f in futures]

        set_current_span(None)

        assert all(tid == "abcd1234567890abcdef567890abcdef" for tid in trace_ids)

    def test_bare_executor_disconnected(self, initialized: Path) -> None:
        """Without traced_executor, threads don't inherit context."""
        parent_ctx = SpanContext(span_id="abcd123456789012", trace_id="abcd1234567890abcdef567890abcdef", name="parent")
        set_current_span(parent_ctx)

        def worker() -> str | None:
            ctx = get_current_span()
            return ctx.trace_id if ctx else None

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            result = future.result()

        set_current_span(None)

        # Without traced_executor, child thread has no context
        assert result is None

    def test_map_propagates_context(self, initialized: Path) -> None:
        parent_ctx = SpanContext(span_id="abcd123456789012", trace_id="abcd1234567890abcdef567890abcdef", name="parent")
        set_current_span(parent_ctx)

        def worker(i: int) -> str | None:
            ctx = get_current_span()
            return ctx.trace_id if ctx else None

        with traced_executor(ThreadPoolExecutor(max_workers=2)) as pool:
            results = list(pool.map(worker, range(4)))

        set_current_span(None)

        assert all(r == "abcd1234567890abcdef567890abcdef" for r in results)


class TestGetTraceContext:
    def test_returns_dict(self, initialized: Path) -> None:
        set_current_span(SpanContext(span_id="abcd567890abcdef", trace_id="abcd1234567890abcdef567890abcdef", name="test"))
        ctx = get_trace_context()
        set_current_span(None)

        assert ctx is not None
        assert ctx["trace_id"] == "abcd1234567890abcdef567890abcdef"
        assert ctx["span_id"] == "abcd567890abcdef"
        assert ctx["trace_flags"] == 1

    def test_format_valid(self, initialized: Path) -> None:
        set_current_span(SpanContext(span_id="abcd567890abcdef", trace_id="abcd1234567890abcdef567890abcdef", name="test"))
        ctx = get_trace_context()
        set_current_span(None)

        assert ctx is not None
        assert re.match(r"^[0-9a-f]{32}$", ctx["trace_id"])
        assert re.match(r"^[0-9a-f]{16}$", ctx["span_id"])

    def test_returns_none_without_span(self) -> None:
        assert get_trace_context() is None


class TestAttachTraceContext:
    def test_attaches_valid_context(self, initialized: Path) -> None:
        ctx = {
            "trace_id": "aaaabbbbccccdddd1111222233334444",
            "span_id": "eeee5555ffff6666",
            "trace_flags": 1,
        }
        attach_trace_context(ctx)

        current = get_current_span()
        assert current is not None
        assert current.trace_id == "aaaabbbbccccdddd1111222233334444"
        assert current.span_id == "eeee5555ffff6666"

    def test_missing_trace_id_raises(self, initialized: Path) -> None:
        with pytest.raises(ValueError, match="trace_id"):
            attach_trace_context({"span_id": "eeee5555ffff6666"})

    def test_missing_span_id_raises(self, initialized: Path) -> None:
        with pytest.raises(ValueError, match="span_id"):
            attach_trace_context({"trace_id": "aaaabbbbccccdddd1111222233334444"})

    def test_invalid_trace_id_raises(self, initialized: Path) -> None:
        with pytest.raises(ValueError, match="invalid trace_id format"):
            attach_trace_context({"trace_id": "too-short", "span_id": "eeee5555ffff6666"})

    def test_invalid_span_id_raises(self, initialized: Path) -> None:
        with pytest.raises(ValueError, match="invalid span_id format"):
            attach_trace_context({"trace_id": "aaaabbbbccccdddd1111222233334444", "span_id": "short"})

    def test_noop_before_init(self) -> None:
        """No-op if init() hasn't been called."""
        ctx = {
            "trace_id": "aaaabbbbccccdddd1111222233334444",
            "span_id": "eeee5555ffff6666",
            "trace_flags": 1,
        }
        attach_trace_context(ctx)  # Should not raise
        assert get_current_span() is None


class TestInjectTraceHeaders:
    def test_injects_traceparent(self, initialized: Path) -> None:
        set_current_span(SpanContext(span_id="abcd567890abcdef", trace_id="abcd1234567890abcdef567890abcdef", name="test"))
        headers: dict[str, str] = {}
        result = inject_trace_headers(headers)
        set_current_span(None)

        assert "traceparent" in result
        assert result["traceparent"] == "00-abcd1234567890abcdef567890abcdef-abcd567890abcdef-01"

    def test_preserves_existing_headers(self, initialized: Path) -> None:
        set_current_span(SpanContext(span_id="abcd567890abcdef", trace_id="abcd1234567890abcdef567890abcdef", name="test"))
        headers = {"Authorization": "Bearer xyz"}
        result = inject_trace_headers(headers)
        set_current_span(None)

        assert result["Authorization"] == "Bearer xyz"
        assert "traceparent" in result

    def test_w3c_format(self, initialized: Path) -> None:
        set_current_span(SpanContext(span_id="abcd567890abcdef", trace_id="abcd1234567890abcdef567890abcdef", name="test"))
        headers: dict[str, str] = {}
        inject_trace_headers(headers)
        set_current_span(None)

        traceparent = headers["traceparent"]
        parts = traceparent.split("-")
        assert len(parts) == 4
        assert parts[0] == "00"  # version
        assert len(parts[1]) == 32  # trace_id
        assert len(parts[2]) == 16  # span_id
        assert len(parts[3]) == 2  # flags

    def test_noop_without_span(self) -> None:
        headers: dict[str, str] = {"existing": "value"}
        result = inject_trace_headers(headers)
        assert "traceparent" not in result
        assert result["existing"] == "value"
