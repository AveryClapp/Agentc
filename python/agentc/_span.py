"""@trace decorator and span() context manager implementation.

Provides the explicit span API for user code instrumentation.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Generator, TypeVar

from agentc._context import SpanContext, get_current_span, set_current_span

logger = logging.getLogger("agentc")

F = TypeVar("F", bound=Callable[..., Any])

# Track whether we've logged the "not initialized" warning
_logged_not_initialized = False


def _generate_span_id() -> str:
    """Generate a 16-char hex span ID."""
    return uuid.uuid4().hex[:16]


def _generate_trace_id() -> str:
    """Generate a 32-char hex trace ID."""
    return uuid.uuid4().hex[:32]


def _now_us() -> int:
    """Current wall-clock time as Unix microseconds."""
    return time.time_ns() // 1000


def _is_initialized() -> bool:
    """Check initialization without circular import."""
    from agentc._lifecycle import is_initialized

    return is_initialized()


def _get_fail_open() -> bool:
    """Get fail_open setting from config."""
    from agentc._lifecycle import get_config

    config = get_config()
    return config.fail_open if config is not None else True


def _write_root_span(span_dict: dict[str, Any]) -> None:
    """Write a root span directly via _native (bypass queue)."""
    from agentc._native import write_span

    write_span(span_dict)


def _enqueue_span(span_dict: dict[str, Any]) -> None:
    """Enqueue a non-root span for background writing."""
    from agentc._writer import enqueue

    enqueue(span_dict)


def _build_span_dict(
    *,
    span_id: str,
    trace_id: str,
    name: str,
    kind: str,
    start_time: int,
    parent_span_id: str | None = None,
    end_time: int | None = None,
    status: str = "OK",
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a span dict for write_span."""
    import json

    d: dict[str, Any] = {
        "span_id": span_id,
        "trace_id": trace_id,
        "name": name,
        "kind": kind,
        "start_time": start_time,
    }
    if parent_span_id is not None:
        d["parent_span_id"] = parent_span_id
    if end_time is not None:
        d["end_time"] = end_time
    d["status"] = status
    if attributes:
        d["attributes"] = json.dumps(attributes)
    return d


def _log_not_initialized() -> None:
    """Log a single DEBUG message on first use without init()."""
    global _logged_not_initialized
    if not _logged_not_initialized:
        logger.debug("agentc.init() not called — instrumentation disabled.")
        _logged_not_initialized = True


def trace(
    name: str | None = None,
    *,
    agent_id: str | None = None,
) -> Callable[[F], F]:
    """Decorator to mark a function as a traced agent boundary.

    Creates a root span (or child span if nested) with kind="invoke_agent".

    Args:
        name: Agent name. Defaults to function's __qualname__.
        agent_id: Override the default agent ID.
    """

    def decorator(func: F) -> F:
        # Reject async generators at decoration time
        if asyncio.iscoroutinefunction(func) and _is_async_generator(func):
            raise TypeError(
                f"@trace cannot decorate async generators: {func.__qualname__}"
            )

        resolved_name = name if name is not None else func.__qualname__
        resolved_agent_id = (
            agent_id
            if agent_id is not None
            else f"{func.__module__}.{func.__qualname__}"
        )

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if not _is_initialized():
                    _log_not_initialized()
                    return await func(*args, **kwargs)

                try:
                    return await _run_traced_async(
                        func, args, kwargs, resolved_name, resolved_agent_id
                    )
                except BaseException:
                    if _get_fail_open():
                        logger.debug(
                            "Trace error (fail_open), running function directly",
                            exc_info=True,
                        )
                        return await func(*args, **kwargs)
                    raise

            return async_wrapper  # type: ignore[return-value]
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                if not _is_initialized():
                    _log_not_initialized()
                    return func(*args, **kwargs)

                try:
                    return _run_traced_sync(
                        func, args, kwargs, resolved_name, resolved_agent_id
                    )
                except BaseException:
                    if _get_fail_open():
                        logger.debug(
                            "Trace error (fail_open), running function directly",
                            exc_info=True,
                        )
                        return func(*args, **kwargs)
                    raise

            return sync_wrapper  # type: ignore[return-value]

    return decorator


def _is_async_generator(func: Any) -> bool:
    """Check if a function is an async generator function."""
    import inspect

    return inspect.isasyncgenfunction(func)


def _run_traced_sync(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    name: str,
    agent_id: str,
) -> Any:
    """Execute a sync function with trace span wrapping."""
    parent = get_current_span()
    span_id = _generate_span_id()
    trace_id = parent.trace_id if parent is not None else _generate_trace_id()
    parent_span_id = parent.span_id if parent is not None else None
    start_time = _now_us()

    attributes: dict[str, Any] = {
        "gen_ai.agent.name": name,
        "gen_ai.agent.id": agent_id,
    }

    ctx = SpanContext(span_id=span_id, trace_id=trace_id, name=name)
    prev = get_current_span()
    set_current_span(ctx)

    logger.debug(
        "Span started: %s (kind=invoke_agent, trace_id=%s, span_id=%s)",
        name,
        trace_id,
        span_id,
    )

    status = "OK"
    try:
        result = func(*args, **kwargs)
        return result
    except BaseException as exc:
        status = "ERROR"
        attributes["error.type"] = type(exc).__name__
        attributes["error.message"] = str(exc)
        logger.error("Exception in span '%s': %s: %s", name, type(exc).__name__, exc)
        raise
    finally:
        end_time = _now_us()
        set_current_span(prev)

        span_dict = _build_span_dict(
            span_id=span_id,
            trace_id=trace_id,
            name=name,
            kind="invoke_agent",
            start_time=start_time,
            parent_span_id=parent_span_id,
            end_time=end_time,
            status=status,
            attributes=attributes,
        )

        duration_ms = (end_time - start_time) / 1000
        logger.debug("Span ended: %s (%.1fms, status=%s)", name, duration_ms, status)

        try:
            if parent_span_id is None:
                logger.debug("Root span bypass: writing %s directly", span_id)
                _write_root_span(span_dict)
            else:
                _enqueue_span(span_dict)
        except BaseException:
            if _get_fail_open():
                logger.debug("Failed to write span (suppressed)", exc_info=True)
            else:
                raise


async def _run_traced_async(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    name: str,
    agent_id: str,
) -> Any:
    """Execute an async function with trace span wrapping."""
    parent = get_current_span()
    span_id = _generate_span_id()
    trace_id = parent.trace_id if parent is not None else _generate_trace_id()
    parent_span_id = parent.span_id if parent is not None else None
    start_time = _now_us()

    attributes: dict[str, Any] = {
        "gen_ai.agent.name": name,
        "gen_ai.agent.id": agent_id,
    }

    ctx = SpanContext(span_id=span_id, trace_id=trace_id, name=name)
    prev = get_current_span()
    set_current_span(ctx)

    logger.debug(
        "Span started: %s (kind=invoke_agent, trace_id=%s, span_id=%s)",
        name,
        trace_id,
        span_id,
    )

    status = "OK"
    try:
        result = await func(*args, **kwargs)
        return result
    except BaseException as exc:
        status = "ERROR"
        attributes["error.type"] = type(exc).__name__
        attributes["error.message"] = str(exc)
        logger.error("Exception in span '%s': %s: %s", name, type(exc).__name__, exc)
        raise
    finally:
        end_time = _now_us()
        set_current_span(prev)

        span_dict = _build_span_dict(
            span_id=span_id,
            trace_id=trace_id,
            name=name,
            kind="invoke_agent",
            start_time=start_time,
            parent_span_id=parent_span_id,
            end_time=end_time,
            status=status,
            attributes=attributes,
        )

        duration_ms = (end_time - start_time) / 1000
        logger.debug("Span ended: %s (%.1fms, status=%s)", name, duration_ms, status)

        try:
            if parent_span_id is None:
                logger.debug("Root span bypass: writing %s directly", span_id)
                _write_root_span(span_dict)
            else:
                _enqueue_span(span_dict)
        except BaseException:
            if _get_fail_open():
                logger.debug("Failed to write span (suppressed)", exc_info=True)
            else:
                raise


@contextmanager
def span_context(
    name: str,
    *,
    kind: str = "execute_tool",
) -> Generator[SpanContext, None, None]:
    """Context manager creating a child span (or root span if no active trace).

    Args:
        name: Span name.
        kind: One of "chat", "execute_tool", "invoke_agent".

    Yields:
        SpanContext with set_attribute() support.
    """
    if not _is_initialized():
        _log_not_initialized()
        yield SpanContext(span_id="", trace_id="", name=name)
        return

    parent = get_current_span()
    span_id = _generate_span_id()
    trace_id = parent.trace_id if parent is not None else _generate_trace_id()
    parent_span_id = parent.span_id if parent is not None else None
    start_time = _now_us()

    attributes: dict[str, Any] = {}
    ctx = SpanContext(span_id=span_id, trace_id=trace_id, name=name)
    prev = get_current_span()
    set_current_span(ctx)

    logger.debug(
        "Span started: %s (kind=%s, trace_id=%s, span_id=%s)",
        name,
        kind,
        trace_id,
        span_id,
    )

    status = "OK"
    try:
        yield ctx
    except BaseException as exc:
        status = "ERROR"
        attributes["error.type"] = type(exc).__name__
        attributes["error.message"] = str(exc)
        logger.error("Exception in span '%s': %s: %s", name, type(exc).__name__, exc)
        raise
    finally:
        end_time = _now_us()
        set_current_span(prev)

        span_dict = _build_span_dict(
            span_id=span_id,
            trace_id=trace_id,
            name=name,
            kind=kind,
            start_time=start_time,
            parent_span_id=parent_span_id,
            end_time=end_time,
            status=status,
            attributes=attributes,
        )

        duration_ms = (end_time - start_time) / 1000
        logger.debug("Span ended: %s (%.1fms, status=%s)", name, duration_ms, status)

        try:
            if parent_span_id is None:
                logger.debug("Root span bypass: writing %s directly", span_id)
                _write_root_span(span_dict)
            else:
                _enqueue_span(span_dict)
        except BaseException:
            if _get_fail_open():
                logger.debug("Failed to write span (suppressed)", exc_info=True)
            else:
                raise
