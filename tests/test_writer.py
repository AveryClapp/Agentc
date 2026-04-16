"""Tests for background writer thread + queue (bd-2k4).

Run: maturin develop && pytest tests/test_writer.py -v
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import agentc
from agentc._context import set_current_span
from agentc._lifecycle import _initialized, _shutdown_in_progress
from agentc._writer import (
    QUEUE_MAX_SIZE,
    _queue,
    enqueue,
    get_stats,
    start,
    stop,
)


def _make_span(span_id: str = "abc1234567890123") -> dict[str, Any]:
    return {
        "span_id": span_id,
        "trace_id": "def45678901234567890123456789012",
        "name": "test-span",
        "kind": "chat",
        "start_time": 1234567890000000,
    }


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    _initialized.clear()
    _shutdown_in_progress.clear()
    set_current_span(None)
    # Ensure writer is stopped
    stop(timeout_ms=1000)
    # Clear queue
    while not _queue.empty():
        try:
            _queue.get_nowait()
        except Exception:
            break
    yield
    stop(timeout_ms=1000)
    while not _queue.empty():
        try:
            _queue.get_nowait()
        except Exception:
            break
    if agentc.is_initialized():
        agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()


@pytest.fixture()
def tmp_storage(tmp_path: Path) -> Path:
    return tmp_path / "agentc"


class TestQueueBehavior:
    def test_queue_bounded(self) -> None:
        assert _queue.maxsize == QUEUE_MAX_SIZE

    def test_enqueue_span(self) -> None:
        span = _make_span()
        enqueue(span)
        assert _queue.qsize() == 1
        item = _queue.get_nowait()
        assert item is not None
        assert item["span_id"] == "abc1234567890123"

    def test_tail_drop_on_full(self) -> None:
        """Queue full → new span dropped (tail-drop)."""
        import agentc._writer as w

        w._total_drops = 0
        # Fill the queue
        for i in range(QUEUE_MAX_SIZE):
            _queue.put_nowait(_make_span(f"span{i:016d}"))
        assert _queue.full()

        # This should be dropped
        enqueue(_make_span("dropped000000000"))
        assert w._total_drops == 1
        # Queue still has the original items (not the dropped one)
        assert _queue.qsize() == QUEUE_MAX_SIZE


class TestWriterThread:
    def test_is_daemon(self) -> None:
        start()
        import agentc._writer as w

        assert w._writer_thread is not None
        assert w._writer_thread.daemon is True
        stop()

    def test_drains_queue(self) -> None:
        """Writer drains queue and calls write_span."""
        written: list[dict[str, Any]] = []

        with patch("agentc._writer._flush_batch") as mock_flush:
            mock_flush.side_effect = lambda fn, batch: written.extend(batch)

            start()
            for i in range(5):
                enqueue(_make_span(f"span{i:016d}"))
            time.sleep(0.3)  # Let writer process
            stop(timeout_ms=2000)

        assert len(written) == 5

    def test_stops_cleanly(self) -> None:
        start()
        stop(timeout_ms=2000)
        import agentc._writer as w

        assert w._writer_thread is None or not w._writer_thread.is_alive()

    def test_flush_on_stop_drains_remaining(self) -> None:
        """All queued items are written on stop()."""
        written: list[dict[str, Any]] = []

        with patch("agentc._writer._flush_batch") as mock_flush:
            mock_flush.side_effect = lambda fn, batch: written.extend(batch)

            start()
            for i in range(50):
                enqueue(_make_span(f"span{i:016d}"))
            stop(timeout_ms=5000)

        assert len(written) == 50


class TestRootSpanBypass:
    def test_root_span_writes_directly(self, tmp_storage: Path) -> None:
        """Root spans bypass queue and write directly."""
        written: list[dict[str, Any]] = []

        with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):
            with patch("agentc._lifecycle._apply_patches"):
                agentc.init(storage_path=str(tmp_storage))

            @agentc.trace(name="root-agent")
            def agent() -> str:
                return "done"

            agent()

        # Root span written directly (not queued)
        assert len(written) >= 1
        root_spans = [s for s in written if s.get("name") == "root-agent"]
        assert len(root_spans) == 1
        assert "parent_span_id" not in root_spans[0]

    def test_child_span_enqueued(self, tmp_storage: Path) -> None:
        """Non-root spans go through the queue."""
        enqueued: list[dict[str, Any]] = []

        with patch("agentc._span._enqueue_span", side_effect=lambda d: enqueued.append(d)):
            with patch("agentc._span._write_root_span"):
                with patch("agentc._lifecycle._apply_patches"):
                    agentc.init(storage_path=str(tmp_storage))

                @agentc.trace(name="parent")
                def parent() -> None:
                    with agentc.span("child", kind="execute_tool"):
                        pass

                parent()

        # Child span should be enqueued (not written directly)
        child_spans = [s for s in enqueued if s.get("name") == "child"]
        assert len(child_spans) == 1
        assert "parent_span_id" in child_spans[0]


class TestStats:
    def test_stats_initial(self) -> None:
        import agentc._writer as w

        w._total_written = 0
        w._total_drops = 0
        stats = get_stats()
        assert stats["total_written"] == 0
        assert stats["total_drops"] == 0


class TestMergeTrigger:
    def test_trigger_merge_calls_native(self) -> None:
        """_trigger_merge invokes merge_all_pending and tolerates zero results."""
        from agentc._writer import _trigger_merge

        mock_merge = MagicMock(return_value={
            "spans_merged": 0,
            "input_content_merged": 0,
            "output_content_merged": 0,
        })
        with patch("agentc._native.merge_all_pending", mock_merge):
            _trigger_merge()

        mock_merge.assert_called_once()

    def test_trigger_merge_swallows_errors(self) -> None:
        """Rust-side merge failure must not propagate out of the writer thread."""
        from agentc._writer import _trigger_merge

        mock_merge = MagicMock(side_effect=RuntimeError("boom"))
        with patch("agentc._native.merge_all_pending", mock_merge):
            _trigger_merge()  # Must not raise

    def test_trigger_merge_handles_non_dict(self) -> None:
        """Defensive against a future FFI shape change returning None."""
        from agentc._writer import _trigger_merge

        with patch("agentc._native.merge_all_pending", MagicMock(return_value=None)):
            _trigger_merge()  # Must not raise
