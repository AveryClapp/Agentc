"""Background writer thread and span queue.

Provides a bounded queue for span and cache-insert messages and a daemon
thread that drains them to the Rust FFI. Cache inserts ride the same queue
as spans so a single writer thread serializes all SQLite writes.
"""

from __future__ import annotations

import dataclasses
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


@dataclasses.dataclass
class CacheInsertMsg:
    """Cache-insert request enqueued by the @memoize decorator.

    Fields mirror the agentc._native.cache_insert signature. output_bytes is
    the raw LLM output serialized to bytes by the caller (the decorator).
    `embedding` is the 256 × f32 little-endian query embedding; when present
    the writer also populates the LSH band rows. `None` disables semantic
    lookup for this entry.
    """

    prompt_hash: bytes
    model: str
    parameters_hash: bytes
    call_site_id: str
    output_bytes: bytes
    input_tokens: int
    output_tokens: int
    recorded_cost_usd: float
    ttl_seconds: int
    embedding: bytes | None = None


@dataclasses.dataclass
class _FlushBarrier:
    """Sentinel that forces the writer to flush its batch and signal an event.

    Used by ``flush_blocking`` so callers — mainly tests — can wait until every
    item enqueued before the call has hit SQLite. Not part of the public API.
    """

    done: threading.Event


_queue: queue.Queue[dict[str, Any] | CacheInsertMsg | _FlushBarrier | None] = queue.Queue(
    maxsize=QUEUE_MAX_SIZE
)
_writer_thread: threading.Thread | None = None
_stop_event = threading.Event()
_total_written = 0
_total_cache_inserts = 0
_total_drops = 0
_lock = threading.Lock()


def start() -> None:
    """Start the background writer thread."""
    global _writer_thread, _total_written, _total_cache_inserts, _total_drops

    if _writer_thread is not None and _writer_thread.is_alive():
        logger.debug("Background writer already running")
        return

    _stop_event.clear()
    _total_written = 0
    _total_cache_inserts = 0
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

    logger.info(
        "Background writer stopped (%d spans, %d cache inserts, %d dropped)",
        _total_written,
        _total_cache_inserts,
        _total_drops,
    )
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
            logger.debug("Span queue full. Dropped span %s (%d total drops)", span_id, drops)


def enqueue_cache_insert(msg: CacheInsertMsg) -> None:
    """Enqueue a cache-insert request. Tail-drops on full queue.

    Fails silently if the writer is not running; the @memoize decorator is
    responsible for logging — cache inserts are best-effort.
    """
    global _total_drops
    try:
        _queue.put_nowait(msg)
    except queue.Full:
        with _lock:
            _total_drops += 1
            drops = _total_drops
        if drops % DROP_LOG_INTERVAL == 0:
            logger.warning("Writer queue full: %d items dropped", drops)


def get_stats() -> dict[str, int]:
    """Get writer statistics."""
    return {
        "total_written": _total_written,
        "total_cache_inserts": _total_cache_inserts,
        "total_drops": _total_drops,
        "queue_size": _queue.qsize(),
    }


def flush_blocking(timeout_s: float = 2.0) -> bool:
    """Flush the writer's current batch and block until the commit completes.

    Returns True on success, False on timeout or when the writer is stopped.
    Primarily useful for tests — production code relies on the 5-second
    interval + batch-size trigger and should not call this.
    """
    if _writer_thread is None or not _writer_thread.is_alive():
        return False
    barrier = _FlushBarrier(done=threading.Event())
    try:
        _queue.put(barrier, timeout=timeout_s)
    except queue.Full:
        return False
    return barrier.done.wait(timeout=timeout_s)


def _writer_loop() -> None:
    """Main loop for the background writer thread."""
    global _total_written, _total_cache_inserts

    from agentc._native import write_span

    batch: list[dict[str, Any] | CacheInsertMsg] = []
    last_flush = time.monotonic()
    spans_since_merge = 0
    last_merge = time.monotonic()

    while not _stop_event.is_set():
        try:
            item = _queue.get(timeout=0.1)
        except queue.Empty:
            if batch and (time.monotonic() - last_flush) >= FLUSH_INTERVAL_S:
                span_count, cache_count = _counts(batch)
                _flush_batch(write_span, batch)
                _total_written += span_count
                _total_cache_inserts += cache_count
                spans_since_merge += span_count
                batch.clear()
                last_flush = time.monotonic()
            continue

        if item is None:
            break

        if isinstance(item, _FlushBarrier):
            if batch:
                span_count, cache_count = _counts(batch)
                _flush_batch(write_span, batch)
                _total_written += span_count
                _total_cache_inserts += cache_count
                spans_since_merge += span_count
                batch.clear()
                last_flush = time.monotonic()
            item.done.set()
            continue

        batch.append(item)

        if len(batch) >= FLUSH_BATCH_SIZE:
            span_count, cache_count = _counts(batch)
            _flush_batch(write_span, batch)
            _total_written += span_count
            _total_cache_inserts += cache_count
            spans_since_merge += span_count
            batch.clear()
            last_flush = time.monotonic()

        elapsed = time.monotonic() - last_merge
        if spans_since_merge >= MERGE_SPAN_THRESHOLD or elapsed >= MERGE_INTERVAL_S:
            reason = f"{spans_since_merge} spans" if spans_since_merge >= MERGE_SPAN_THRESHOLD else f"{elapsed/60:.0f}min elapsed"
            logger.info("Periodic merge triggered (reason: %s)", reason)
            _trigger_merge()
            spans_since_merge = 0
            last_merge = time.monotonic()

    while True:
        try:
            item = _queue.get_nowait()
        except queue.Empty:
            break
        if item is None:
            continue
        if isinstance(item, _FlushBarrier):
            item.done.set()
            continue
        batch.append(item)

    if batch:
        span_count, cache_count = _counts(batch)
        _flush_batch(write_span, batch)
        _total_written += span_count
        _total_cache_inserts += cache_count

    logger.debug(
        "Writer loop exited (%d spans, %d cache inserts written)",
        _total_written,
        _total_cache_inserts,
    )


def _counts(batch: list[dict[str, Any] | CacheInsertMsg]) -> tuple[int, int]:
    spans = sum(1 for it in batch if not isinstance(it, CacheInsertMsg))
    cache_inserts = len(batch) - spans
    return spans, cache_inserts


def _flush_batch(
    write_fn: Any,
    batch: list[dict[str, Any] | CacheInsertMsg],
) -> None:
    """Write a batch of mixed items via the Rust FFI.

    Catches BaseException (not just Exception) because PyO3 PanicException
    inherits from BaseException. A Rust panic must not crash the writer thread.
    """
    cache_insert_fn = None
    for item in batch:
        if isinstance(item, CacheInsertMsg):
            if cache_insert_fn is None:
                try:
                    from agentc._native import cache_insert as cache_insert_fn  # type: ignore[no-redef]
                except ImportError:
                    logger.debug("cache_insert FFI unavailable; dropping cache messages")
                    continue
            try:
                cache_insert_fn(  # type: ignore[misc]
                    item.prompt_hash,
                    item.model,
                    item.parameters_hash,
                    item.call_site_id,
                    item.output_bytes,
                    item.input_tokens,
                    item.output_tokens,
                    item.recorded_cost_usd,
                    item.ttl_seconds,
                    item.embedding,
                )
            except BaseException:
                logger.debug(
                    "Failed to insert cache entry for %s", item.call_site_id, exc_info=True
                )
        else:
            try:
                write_fn(item)
            except BaseException:
                logger.debug("Failed to write span %s", item.get("span_id", "?"), exc_info=True)
    logger.debug("Flushed %d items", len(batch))


def _trigger_merge() -> None:
    """Fold pending per-process DBs into the canonical store.

    Runs inline on the writer thread. The underlying Rust call acquires a
    cross-process flock and releases the GIL during IO. Failures here are
    non-fatal — the writer keeps running and the next tick retries.
    """
    from agentc._native import merge_all_pending

    try:
        stats = merge_all_pending()
    except BaseException:
        logger.debug("Periodic merge failed", exc_info=True)
        return

    spans = stats.get("spans_merged", 0) if isinstance(stats, dict) else 0
    if spans > 0:
        logger.info("Merged %d spans into canonical store", spans)
