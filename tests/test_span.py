"""Tests for @trace decorator and span() context manager (bd-1ya).

Run: maturin develop && pytest tests/test_span.py -v
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import agentc
from agentc._context import SpanContext, get_current_span, set_current_span
from agentc._lifecycle import _initialized, _shutdown_in_progress, get_config
from contextlib import contextmanager
from collections.abc import Generator

from agentc._span import _generate_span_id, _generate_trace_id, _logged_not_initialized


@contextmanager
def capture_all_spans(written: list[dict[str, Any]]) -> Generator[None, None, None]:
    """Capture both root spans (direct write) and child spans (enqueued)."""
    with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):
        with patch("agentc._span._enqueue_span", side_effect=lambda d: written.append(d)):
            yield


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Ensure clean state before and after each test."""
    import agentc._span as span_mod

    _initialized.clear()
    _shutdown_in_progress.clear()
    set_current_span(None)
    span_mod._logged_not_initialized = False
    yield
    if agentc.is_initialized():
        agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()
    set_current_span(None)
    span_mod._logged_not_initialized = False


@pytest.fixture()
def tmp_storage(tmp_path: Path) -> Path:
    return tmp_path / "agentc"


@pytest.fixture()
def initialized(tmp_storage: Path) -> Path:
    """Initialize agentc for tests that need it."""
    agentc.init(storage_path=str(tmp_storage))
    return tmp_storage


class TestIdGeneration:
    def test_span_id_format(self) -> None:
        sid = _generate_span_id()
        assert len(sid) == 16
        int(sid, 16)  # valid hex

    def test_trace_id_format(self) -> None:
        tid = _generate_trace_id()
        assert len(tid) == 32
        int(tid, 16)  # valid hex

    def test_span_ids_unique(self) -> None:
        ids = {_generate_span_id() for _ in range(100)}
        assert len(ids) == 100

    def test_trace_ids_unique(self) -> None:
        ids = {_generate_trace_id() for _ in range(100)}
        assert len(ids) == 100


class TestTraceDecorator:
    def test_creates_root_span(self, initialized: Path) -> None:
        """@trace creates root span with kind=invoke_agent."""
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):

            @agentc.trace(name="my-agent")
            def my_func() -> str:
                return "result"

            result = my_func()

        assert result == "result"
        assert len(written) == 1
        span = written[0]
        assert span["kind"] == "invoke_agent"
        assert span["name"] == "my-agent"
        assert span["status"] == "OK"
        assert "parent_span_id" not in span
        assert len(span["span_id"]) == 16
        assert len(span["trace_id"]) == 32

    def test_name_defaults_to_qualname(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):

            @agentc.trace()
            def some_function() -> None:
                pass

            some_function()

        assert written[0]["name"].endswith("some_function")

    def test_sets_agent_name_attribute(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):

            @agentc.trace(name="my-agent")
            def agent_fn() -> None:
                pass

            agent_fn()

        attrs = json.loads(written[0]["attributes"])
        assert attrs["gen_ai.agent.name"] == "my-agent"

    def test_agent_id_default(self, initialized: Path) -> None:
        """Default agent_id is module.qualname."""
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):

            @agentc.trace(name="test")
            def my_agent() -> None:
                pass

            my_agent()

        attrs = json.loads(written[0]["attributes"])
        assert attrs["gen_ai.agent.id"].endswith("my_agent")

    def test_agent_id_custom(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):

            @agentc.trace(name="test", agent_id="custom-id")
            def my_agent() -> None:
                pass

            my_agent()

        attrs = json.loads(written[0]["attributes"])
        assert attrs["gen_ai.agent.id"] == "custom-id"

    def test_async_function(self, initialized: Path) -> None:
        """@trace on async function returns coroutine, span created."""
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):

            @agentc.trace(name="async-agent")
            async def async_fn() -> str:
                return "async-result"

            result = asyncio.run(async_fn())

        assert result == "async-result"
        assert len(written) == 1
        assert written[0]["kind"] == "invoke_agent"
        assert written[0]["name"] == "async-agent"

    def test_nested_trace_creates_child(self, initialized: Path) -> None:
        """Nested @trace creates child span inheriting trace_id."""
        written: list[dict[str, Any]] = []

        with capture_all_spans(written):

            @agentc.trace(name="outer")
            def outer() -> str:
                return inner()

            @agentc.trace(name="inner")
            def inner() -> str:
                return "done"

            outer()

        assert len(written) == 2
        # Inner finishes first
        inner_span = next(s for s in written if s["name"] == "inner")
        outer_span = next(s for s in written if s["name"] == "outer")
        # Same trace_id
        assert inner_span["trace_id"] == outer_span["trace_id"]
        # Inner has parent
        assert inner_span.get("parent_span_id") == outer_span["span_id"]
        # Outer is root
        assert "parent_span_id" not in outer_span

    def test_exception_sets_error_status(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):

            @agentc.trace(name="failing")
            def failing() -> None:
                raise ValueError("boom")

            with pytest.raises(ValueError, match="boom"):
                failing()

        assert len(written) == 1
        assert written[0]["status"] == "ERROR"
        attrs = json.loads(written[0]["attributes"])
        assert attrs["error.type"] == "ValueError"
        assert attrs["error.message"] == "boom"

    def test_preserves_return_value(self, initialized: Path) -> None:
        with patch("agentc._span._write_root_span"):

            @agentc.trace(name="test")
            def returns_42() -> int:
                return 42

            assert returns_42() == 42

    def test_timestamps_set(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):

            @agentc.trace(name="test")
            def noop() -> None:
                pass

            noop()

        s = written[0]
        assert s["start_time"] > 0
        assert s["end_time"] >= s["start_time"]


class TestTraceWithoutInit:
    def test_noop_without_init(self) -> None:
        """Without init(), @trace is a no-op."""

        @agentc.trace(name="test")
        def my_func() -> str:
            return "works"

        assert my_func() == "works"

    def test_async_noop_without_init(self) -> None:

        @agentc.trace(name="test")
        async def async_fn() -> str:
            return "works"

        assert asyncio.run(async_fn()) == "works"

    def test_logs_debug_once(self) -> None:
        import agentc._span as span_mod

        assert not span_mod._logged_not_initialized

        @agentc.trace(name="test")
        def noop() -> None:
            pass

        noop()
        assert span_mod._logged_not_initialized


class TestSpanContextManager:
    def test_creates_child_inside_trace(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with capture_all_spans(written):

            @agentc.trace(name="parent")
            def parent() -> None:
                with agentc.span("tool-call", kind="execute_tool"):
                    pass

            parent()

        assert len(written) == 2
        tool_span = next(s for s in written if s["name"] == "tool-call")
        parent_span = next(s for s in written if s["name"] == "parent")
        assert tool_span["kind"] == "execute_tool"
        assert tool_span.get("parent_span_id") == parent_span["span_id"]
        assert tool_span["trace_id"] == parent_span["trace_id"]

    def test_creates_root_outside_trace(self, initialized: Path) -> None:
        """span() outside @trace creates root span with new trace_id."""
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):
            with agentc.span("standalone", kind="chat"):
                pass

        assert len(written) == 1
        assert written[0]["name"] == "standalone"
        assert written[0]["kind"] == "chat"
        assert "parent_span_id" not in written[0]

    def test_kind_chat(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):
            with agentc.span("llm-call", kind="chat"):
                pass

        assert written[0]["kind"] == "chat"

    def test_kind_execute_tool(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):
            with agentc.span("tool"):
                pass

        assert written[0]["kind"] == "execute_tool"

    def test_exception_in_span(self, initialized: Path) -> None:
        """Exception sets status=ERROR, preserves error info, re-raises."""
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):
            with pytest.raises(RuntimeError, match="fail"):
                with agentc.span("failing"):
                    raise RuntimeError("fail")

        assert len(written) == 1
        assert written[0]["status"] == "ERROR"
        attrs = json.loads(written[0]["attributes"])
        assert attrs["error.type"] == "RuntimeError"
        assert attrs["error.message"] == "fail"

    def test_noop_without_init(self) -> None:
        """Without init(), span() is a passthrough."""
        with agentc.span("test") as ctx:
            assert ctx.name == "test"
            # No crash, passthrough

    def test_yields_span_context(self, initialized: Path) -> None:
        with patch("agentc._span._write_root_span"):
            with agentc.span("test") as ctx:
                assert isinstance(ctx, SpanContext)
                assert ctx.name == "test"
                assert len(ctx.span_id) == 16
                assert len(ctx.trace_id) == 32


class TestContextVarIsolation:
    def test_thread_isolation(self, initialized: Path) -> None:
        """ContextVar provides per-thread isolation."""
        import concurrent.futures

        results: list[str | None] = []

        def worker(name: str) -> str | None:
            set_current_span(SpanContext(span_id="x", trace_id="y", name=name))
            import time

            time.sleep(0.01)  # force interleaving
            ctx = get_current_span()
            return ctx.name if ctx else None

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(worker, f"thread-{i}") for i in range(4)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        # Each thread should see its own name (not another thread's)
        assert len(results) == 4
        assert set(results) == {f"thread-{i}" for i in range(4)}

    def test_asyncio_task_isolation(self, initialized: Path) -> None:
        """ContextVar provides per-asyncio-task isolation."""

        async def main() -> list[str | None]:
            results: list[str | None] = []

            async def worker(name: str) -> str | None:
                set_current_span(SpanContext(span_id="x", trace_id="y", name=name))
                await asyncio.sleep(0.01)
                ctx = get_current_span()
                return ctx.name if ctx else None

            tasks = [asyncio.create_task(worker(f"task-{i}")) for i in range(4)]
            for t in tasks:
                results.append(await t)
            return results

        results = asyncio.run(main())
        assert len(results) == 4
        assert set(results) == {f"task-{i}" for i in range(4)}

    def test_context_restored_after_trace(self, initialized: Path) -> None:
        """ContextVar restored to previous value after @trace completes."""
        assert get_current_span() is None

        with patch("agentc._span._write_root_span"):

            @agentc.trace(name="test")
            def traced_fn() -> None:
                assert get_current_span() is not None

            traced_fn()

        assert get_current_span() is None


class TestRootSpanBypass:
    def test_root_span_writes_directly(self, initialized: Path) -> None:
        """Root spans call _write_root_span directly."""
        write_mock = MagicMock()

        with patch("agentc._span._write_root_span", write_mock):

            @agentc.trace(name="root")
            def root_fn() -> None:
                pass

            root_fn()

        write_mock.assert_called_once()
        span_dict = write_mock.call_args[0][0]
        assert "parent_span_id" not in span_dict


class TestIntegration:
    def test_three_level_span_tree(self, initialized: Path) -> None:
        """@trace → span("tool") → span("chat") → 3-level tree."""
        written: list[dict[str, Any]] = []

        with capture_all_spans(written):

            @agentc.trace(name="agent")
            def agent() -> None:
                with agentc.span("tool-call", kind="execute_tool"):
                    with agentc.span("llm-call", kind="chat"):
                        pass

            agent()

        assert len(written) == 3
        agent_span = next(s for s in written if s["name"] == "agent")
        tool_span = next(s for s in written if s["name"] == "tool-call")
        llm_span = next(s for s in written if s["name"] == "llm-call")

        # Same trace
        assert agent_span["trace_id"] == tool_span["trace_id"] == llm_span["trace_id"]
        # Parent chain
        assert "parent_span_id" not in agent_span
        assert tool_span["parent_span_id"] == agent_span["span_id"]
        assert llm_span["parent_span_id"] == tool_span["span_id"]

    def test_exception_in_nested_span(self, initialized: Path) -> None:
        """Exception in inner span: outer still completes, inner has ERROR."""
        written: list[dict[str, Any]] = []

        with capture_all_spans(written):

            @agentc.trace(name="outer")
            def outer() -> str:
                try:
                    with agentc.span("inner"):
                        raise ValueError("inner-error")
                except ValueError:
                    pass
                return "ok"

            result = outer()

        assert result == "ok"
        assert len(written) == 2
        inner = next(s for s in written if s["name"] == "inner")
        outer_s = next(s for s in written if s["name"] == "outer")
        assert inner["status"] == "ERROR"
        assert outer_s["status"] == "OK"

    def test_concurrent_traces(self, initialized: Path) -> None:
        """10 concurrent @trace calls → 10 independent traces."""
        import concurrent.futures

        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):

            @agentc.trace(name="worker")
            def worker(i: int) -> int:
                return i

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(worker, i) for i in range(10)]
                for f in concurrent.futures.as_completed(futures):
                    f.result()

        assert len(written) == 10
        trace_ids = {s["trace_id"] for s in written}
        assert len(trace_ids) == 10  # all unique

    def test_async_trace_with_span(self, initialized: Path) -> None:
        """Async @trace with span() inside → correct tree."""
        written: list[dict[str, Any]] = []

        with capture_all_spans(written):

            @agentc.trace(name="async-agent")
            async def async_agent() -> str:
                with agentc.span("tool", kind="execute_tool"):
                    pass
                return "done"

            result = asyncio.run(async_agent())

        assert result == "done"
        assert len(written) == 2
        agent_span = next(s for s in written if s["name"] == "async-agent")
        tool_span = next(s for s in written if s["name"] == "tool")
        assert tool_span["parent_span_id"] == agent_span["span_id"]
