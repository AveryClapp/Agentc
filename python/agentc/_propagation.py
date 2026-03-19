"""Context propagation for threads, processes, and remote services.

Provides:
- traced_executor: context-propagating wrapper for ThreadPoolExecutor
- get_trace_context / attach_trace_context: cross-process context passing
- inject_trace_headers: W3C traceparent header injection
"""

from __future__ import annotations

import contextvars
import re
import logging
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from agentc._context import SpanContext, get_current_span, set_current_span

logger = logging.getLogger("agentc")

T = TypeVar("T")

_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_16 = re.compile(r"^[0-9a-f]{16}$")


class traced_executor:
    """Context-propagating wrapper for ThreadPoolExecutor.

    Usage:
        with traced_executor(ThreadPoolExecutor(max_workers=4)) as pool:
            pool.submit(fn, *args)  # context auto-propagated
    """

    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    def __enter__(self) -> "traced_executor":
        self._executor.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._executor.__exit__(*args)

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> "Future[T]":
        """Submit fn with current context propagated to the child thread."""
        ctx = contextvars.copy_context()
        return self._executor.submit(ctx.run, fn, *args, **kwargs)

    def map(self, fn: Callable[..., T], *iterables: Any, timeout: float | None = None) -> Any:
        """Map fn across iterables with context propagation."""
        ctx = contextvars.copy_context()

        def wrapped_fn(*args: Any) -> T:
            return ctx.run(fn, *args)

        return self._executor.map(wrapped_fn, *iterables, timeout=timeout)

    def shutdown(self, wait: bool = True, **kwargs: Any) -> None:
        self._executor.shutdown(wait=wait, **kwargs)


def get_trace_context() -> dict[str, Any] | None:
    """Get current trace context for cross-process propagation.

    Returns:
        Dict with trace_id (32 hex), span_id (16 hex), trace_flags (int),
        or None if no active span.
    """
    current = get_current_span()
    if current is None:
        return None
    return {
        "trace_id": current.trace_id,
        "span_id": current.span_id,
        "trace_flags": 1,  # sampled
    }


def attach_trace_context(ctx: dict[str, Any]) -> None:
    """Attach a trace context received from a parent process.

    After calling this, subsequent spans will inherit the provided trace_id
    and use the provided span_id as parent_span_id.

    Args:
        ctx: Dict with "trace_id" (32 hex), "span_id" (16 hex), "trace_flags" (int).

    Raises:
        ValueError: If ctx is malformed (missing keys or invalid format).
    """
    from agentc._lifecycle import is_initialized

    if not is_initialized():
        logger.debug("attach_trace_context called before init() — no-op")
        return

    # Validate required keys
    if "trace_id" not in ctx:
        raise ValueError("attach_trace_context: missing required key 'trace_id'")
    if "span_id" not in ctx:
        raise ValueError("attach_trace_context: missing required key 'span_id'")

    trace_id = str(ctx["trace_id"])
    span_id = str(ctx["span_id"])

    # Validate format
    if not _HEX_32.match(trace_id):
        raise ValueError(f"attach_trace_context: invalid trace_id format (expected 32 hex chars): {trace_id!r}")
    if not _HEX_16.match(span_id):
        raise ValueError(f"attach_trace_context: invalid span_id format (expected 16 hex chars): {span_id!r}")

    set_current_span(SpanContext(span_id=span_id, trace_id=trace_id, name="attached"))
    logger.debug("Trace context attached: trace_id=%s, span_id=%s", trace_id, span_id)


def inject_trace_headers(headers: dict[str, str]) -> dict[str, str]:
    """Inject W3C traceparent header into outgoing headers.

    Args:
        headers: Existing headers dict. Modified in-place and returned.

    Returns:
        The headers dict with "traceparent" added if active span exists.
    """
    current = get_current_span()
    if current is None:
        return headers

    trace_flags = "01"  # sampled
    traceparent = f"00-{current.trace_id}-{current.span_id}-{trace_flags}"
    headers["traceparent"] = traceparent
    return headers
