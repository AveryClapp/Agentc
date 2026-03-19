"""Background writer thread and span queue.

Provides a bounded queue for span collection and a daemon thread that drains
it to the Rust write_span FFI function.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

logger = logging.getLogger("agentc")

QUEUE_MAX_SIZE = 1000
FLUSH_BATCH_SIZE = 100
FLUSH_INTERVAL_S = 5.0
MERGE_SPAN_THRESHOLD = 10_000
MERGE_INTERVAL_S = 30 * 60  # 30 minutes
DROP_LOG_INTERVAL = 100

_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=QUEUE_MAX_SIZE)
_writer_thread: threading.Thread | None = None
_stop_event = threading.Event()
_total_written = 0
_total_drops = 0
_lock = threading.Lock()


def start() -> None:
    """Start the background writer thread."""
    global _writer_thread, _total_written, _total_drops

    if _writer_thread is not None and _writer_thread.is_alive():
        logger.debug("Background writer already running")
        return

    _stop_event.clear()
    _total_written = 0
    _total_drops = 0

    # Clear any stale items
    while not _queue.empty():
        try:
            _queue.get_nowait()
        except queue.Empty:
            break

    _writer_thread = threading.Thread(target=_writer_loop, name="agentc-writer", daemon=True)
    _writer_thread.start()
    logger.debug("Background writer started (queue_size=%d)", QUEUE_MAX_SIZE)


def stop(timeout_ms: int = 5000) -> None:
    """Stop the background writer thread and drain remaining items."""
    global _writer_thread

    if _writer_thread is None or not _writer_thread.is_alive():
        return

    # Signal stop
    _stop_event.set()
    # Send sentinel
    try:
        _queue.put_nowait(None)
    except queue.Full:
        pass

    _writer_thread.join(timeout=timeout_ms / 1000)
    if _writer_thread.is_alive():
        logger.warning("Background writer did not stop within %dms", timeout_ms)

    logger.info("Background writer stopped (%d spans written, %d dropped)", _total_written, _total_drops)
    _writer_thread = None


def enqueue(span_dict: dict[str, Any]) -> None:
    """Enqueue a span for background writing. Tail-drops on full queue."""
    global _total_drops

    try:
        _queue.put_nowait(span_dict)
    except queue.Full:
        with _lock:
            _total_drops += 1
            drops = _total_drops
        if drops % DROP_LOG_INTERVAL == 0:
            logger.warning("Span queue full: %d spans dropped (logged every %d)", drops, DROP_LOG_INTERVAL)
        else:
            span_id = span_dict.get("span_id", "?")
            logger.warning("Span queue full. Dropped span %s (%d total drops)", span_id, drops)


def get_stats() -> dict[str, int]:
    """Get writer statistics."""
    return {
        "total_written": _total_written,
        "total_drops": _total_drops,
        "queue_size": _queue.qsize(),
    }


def _writer_loop() -> None:
    """Main loop for the background writer thread."""
    global _total_written

    from agentc._native import write_span

    batch: list[dict[str, Any]] = []
    last_flush = time.monotonic()
    spans_since_merge = 0
    last_merge = time.monotonic()

    while not _stop_event.is_set():
        try:
            item = _queue.get(timeout=0.1)
        except queue.Empty:
            # Check flush interval
            if batch and (time.monotonic() - last_flush) >= FLUSH_INTERVAL_S:
                _flush_batch(write_span, batch)
                _total_written += len(batch)
                spans_since_merge += len(batch)
                batch.clear()
                last_flush = time.monotonic()
            continue

        if item is None:
            # Sentinel — drain remaining and exit
            break

        batch.append(item)

        # Flush on batch size
        if len(batch) >= FLUSH_BATCH_SIZE:
            _flush_batch(write_span, batch)
            _total_written += len(batch)
            spans_since_merge += len(batch)
            batch.clear()
            last_flush = time.monotonic()

        # Check periodic merge
        elapsed = time.monotonic() - last_merge
        if spans_since_merge >= MERGE_SPAN_THRESHOLD or elapsed >= MERGE_INTERVAL_S:
            reason = f"{spans_since_merge} spans" if spans_since_merge >= MERGE_SPAN_THRESHOLD else f"{elapsed/60:.0f}min elapsed"
            logger.info("Periodic merge triggered (reason: %s)", reason)
            # TODO(VelvetHammer, bd-2zc): Actual merge protocol
            spans_since_merge = 0
            last_merge = time.monotonic()

    # Drain remaining items
    while True:
        try:
            item = _queue.get_nowait()
            if item is not None:
                batch.append(item)
        except queue.Empty:
            break

    if batch:
        _flush_batch(write_span, batch)
        _total_written += len(batch)

    logger.debug("Writer loop exited (%d total written)", _total_written)


def _flush_batch(write_fn: Any, batch: list[dict[str, Any]]) -> None:
    """Write a batch of spans via the Rust FFI."""
    for span_dict in batch:
        try:
            write_fn(span_dict)
        except Exception:
            logger.debug("Failed to write span %s", span_dict.get("span_id", "?"), exc_info=True)
    logger.debug("Flushed %d spans to disk", len(batch))
