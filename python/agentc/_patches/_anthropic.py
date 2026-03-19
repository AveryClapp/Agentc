"""Anthropic SDK patch via wrapt.

Intercepts Messages.create / stream (sync and async) to capture spans
with gen_ai.* attributes, TTFT for streaming, and input/output messages.

Supports anthropic >= 0.30.0 (adapter_v030).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import wrapt

from agentc._context import SpanContext, get_current_span, set_current_span
from agentc._span import (
    _build_span_dict,
    _generate_span_id,
    _generate_trace_id,
    _is_initialized,
    _now_us,
    _write_root_span,
)

logger = logging.getLogger("agentc")

_patched = False


def _get_fail_open() -> bool:
    from agentc._lifecycle import get_config

    config = get_config()
    return config.fail_open if config is not None else True


def _extract_request_attrs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Extract gen_ai.* request attributes from create() kwargs."""
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "anthropic",
    }
    if "model" in kwargs:
        attrs["gen_ai.request.model"] = kwargs["model"]
    if "temperature" in kwargs:
        attrs["gen_ai.request.temperature"] = kwargs["temperature"]
    if "top_p" in kwargs:
        attrs["gen_ai.request.top_p"] = kwargs["top_p"]
    if "max_tokens" in kwargs:
        attrs["gen_ai.request.max_tokens"] = kwargs["max_tokens"]
    return attrs


def _extract_input_messages(kwargs: dict[str, Any]) -> str | None:
    """Extract input messages as JSON string for content storage."""
    messages = kwargs.get("messages")
    if messages is None:
        return None
    try:
        # Messages are typically list[dict], but may contain Pydantic models
        serializable = []
        for msg in messages:
            if isinstance(msg, dict):
                serializable.append(msg)
            elif hasattr(msg, "model_dump"):
                serializable.append(msg.model_dump())
            else:
                serializable.append({"role": str(getattr(msg, "role", "unknown")), "content": str(msg)})
        return json.dumps(serializable, default=str)
    except Exception:
        return None


def _extract_response_attrs(response: Any) -> dict[str, Any]:
    """Extract gen_ai.* response attributes from a Message response."""
    attrs: dict[str, Any] = {}
    if hasattr(response, "model"):
        attrs["gen_ai.response.model"] = response.model
    if hasattr(response, "id"):
        attrs["gen_ai.response.id"] = response.id
    if hasattr(response, "stop_reason") and response.stop_reason:
        attrs["gen_ai.response.finish_reasons"] = response.stop_reason

    # Usage
    usage = getattr(response, "usage", None)
    if usage is not None:
        if hasattr(usage, "input_tokens"):
            attrs["gen_ai.usage.input_tokens"] = usage.input_tokens
        if hasattr(usage, "output_tokens"):
            attrs["gen_ai.usage.output_tokens"] = usage.output_tokens
        if hasattr(usage, "cache_creation_input_tokens") and usage.cache_creation_input_tokens:
            attrs["gen_ai.usage.cache_creation.input_tokens"] = usage.cache_creation_input_tokens
        if hasattr(usage, "cache_read_input_tokens") and usage.cache_read_input_tokens:
            attrs["gen_ai.usage.cache_read.input_tokens"] = usage.cache_read_input_tokens

    return attrs


def _extract_output_messages(response: Any) -> str | None:
    """Extract output messages as JSON string from a Message response."""
    try:
        content = getattr(response, "content", None)
        if content is None:
            return None
        blocks = []
        for block in content:
            if hasattr(block, "model_dump"):
                blocks.append(block.model_dump())
            elif isinstance(block, dict):
                blocks.append(block)
            else:
                blocks.append({"type": "text", "text": str(block)})
        return json.dumps([{"role": "assistant", "content": blocks}], default=str)
    except Exception:
        return None


def _emit_span(
    *,
    attrs: dict[str, Any],
    name: str,
    start_time: int,
    end_time: int,
    parent: SpanContext | None,
    input_messages: str | None = None,
    output_messages: str | None = None,
    status: str = "OK",
) -> None:
    """Build and write a chat span."""
    span_id = _generate_span_id()
    trace_id = parent.trace_id if parent is not None else _generate_trace_id()
    parent_span_id = parent.span_id if parent is not None else None

    # Add agent name from current trace context
    if parent is not None:
        attrs["gen_ai.agent.name"] = parent.name

    span_dict = _build_span_dict(
        span_id=span_id,
        trace_id=trace_id,
        name=name,
        kind="chat",
        start_time=start_time,
        parent_span_id=parent_span_id,
        end_time=end_time,
        status=status,
        attributes=attrs,
    )

    if input_messages is not None:
        span_dict["input_messages"] = input_messages
    if output_messages is not None:
        span_dict["output_messages"] = output_messages

    logger.debug(
        "Anthropic span captured: %s (model=%s, in=%s, out=%s)",
        span_id,
        attrs.get("gen_ai.request.model", "?"),
        attrs.get("gen_ai.usage.input_tokens", "?"),
        attrs.get("gen_ai.usage.output_tokens", "?"),
    )

    if parent_span_id is None:
        logger.debug("Root span bypass: writing %s directly", span_id)
    _write_root_span(span_dict)  # TODO(VelvetHammer, bd-2k4): enqueue non-root


# --- Sync wrappers ---


def _wrap_create(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    """Wrapper for Messages.create (sync, non-streaming)."""
    if not _is_initialized():
        return wrapped(*args, **kwargs)

    parent = get_current_span()
    start_time = _now_us()
    req_attrs = _extract_request_attrs(kwargs)
    input_msgs = _extract_input_messages(kwargs)

    try:
        response = wrapped(*args, **kwargs)
    except BaseException as exc:
        end_time = _now_us()
        req_attrs["error.type"] = type(exc).__name__
        req_attrs["error.message"] = str(exc)
        try:
            _emit_span(
                attrs=req_attrs,
                name="anthropic.messages.create",
                start_time=start_time,
                end_time=end_time,
                parent=parent,
                input_messages=input_msgs,
                status="ERROR",
            )
        except BaseException:
            if _get_fail_open():
                logger.debug("Failed to emit error span (suppressed)", exc_info=True)
        raise

    end_time = _now_us()
    resp_attrs = _extract_response_attrs(response)
    req_attrs.update(resp_attrs)
    output_msgs = _extract_output_messages(response)

    try:
        _emit_span(
            attrs=req_attrs,
            name="anthropic.messages.create",
            start_time=start_time,
            end_time=end_time,
            parent=parent,
            input_messages=input_msgs,
            output_messages=output_msgs,
        )
    except BaseException:
        if _get_fail_open():
            logger.debug("Failed to emit span (suppressed)", exc_info=True)
        else:
            raise

    return response


def _wrap_stream(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    """Wrapper for Messages.stream (sync streaming)."""
    if not _is_initialized():
        return wrapped(*args, **kwargs)

    parent = get_current_span()
    start_time = _now_us()
    req_attrs = _extract_request_attrs(kwargs)
    input_msgs = _extract_input_messages(kwargs)

    stream_mgr = wrapped(*args, **kwargs)
    return _WrappedStreamManager(
        stream_mgr=stream_mgr,
        start_time=start_time,
        req_attrs=req_attrs,
        input_msgs=input_msgs,
        parent=parent,
    )


class _WrappedStreamManager:
    """Wraps the Anthropic MessageStream context manager to capture TTFT and usage."""

    def __init__(
        self,
        *,
        stream_mgr: Any,
        start_time: int,
        req_attrs: dict[str, Any],
        input_msgs: str | None,
        parent: SpanContext | None,
    ) -> None:
        self._stream_mgr = stream_mgr
        self._start_time = start_time
        self._req_attrs = req_attrs
        self._input_msgs = input_msgs
        self._parent = parent
        self._ttft_recorded = False
        self._passthrough = False

    def __enter__(self) -> Any:
        self._stream = self._stream_mgr.__enter__()
        return _WrappedStream(self)

    def __exit__(self, *exc_info: Any) -> Any:
        result = self._stream_mgr.__exit__(*exc_info)

        end_time = _now_us()
        # Get final message from stream
        final_message = getattr(self._stream, "get_final_message", lambda: None)()
        if final_message is not None:
            resp_attrs = _extract_response_attrs(final_message)
            self._req_attrs.update(resp_attrs)
            output_msgs = _extract_output_messages(final_message)
        else:
            output_msgs = None

        status = "ERROR" if exc_info[0] is not None else "OK"
        if exc_info[0] is not None:
            self._req_attrs["error.type"] = exc_info[0].__name__
            self._req_attrs["error.message"] = str(exc_info[1])

        try:
            _emit_span(
                attrs=self._req_attrs,
                name="anthropic.messages.stream",
                start_time=self._start_time,
                end_time=end_time,
                parent=self._parent,
                input_messages=self._input_msgs,
                output_messages=output_msgs,
                status=status,
            )
        except BaseException:
            if _get_fail_open():
                logger.debug("Failed to emit stream span (suppressed)", exc_info=True)

        return result

    def _record_ttft(self) -> None:
        if not self._ttft_recorded:
            self._ttft_recorded = True
            ttft_us = _now_us() - self._start_time
            ttft_ms = ttft_us / 1000
            self._req_attrs["agentc.ttft_ms"] = round(ttft_ms, 2)
            logger.debug("Anthropic TTFT: %.1fms", ttft_ms)


class _WrappedStream:
    """Wraps the stream iterator to capture TTFT on first content event."""

    def __init__(self, mgr: _WrappedStreamManager) -> None:
        self._mgr = mgr

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        try:
            event = next(self._mgr._stream)
        except StopIteration:
            raise
        except BaseException:
            raise

        # Record TTFT on first content_block_delta or text event
        event_type = getattr(event, "type", None)
        if event_type in ("content_block_delta", "text"):
            self._mgr._record_ttft()

        return event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mgr._stream, name)


# --- Async wrappers ---


async def _wrap_create_async(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    """Wrapper for AsyncMessages.create (async, non-streaming)."""
    if not _is_initialized():
        return await wrapped(*args, **kwargs)

    parent = get_current_span()
    start_time = _now_us()
    req_attrs = _extract_request_attrs(kwargs)
    input_msgs = _extract_input_messages(kwargs)

    try:
        response = await wrapped(*args, **kwargs)
    except BaseException as exc:
        end_time = _now_us()
        req_attrs["error.type"] = type(exc).__name__
        req_attrs["error.message"] = str(exc)
        try:
            _emit_span(
                attrs=req_attrs,
                name="anthropic.messages.create",
                start_time=start_time,
                end_time=end_time,
                parent=parent,
                input_messages=input_msgs,
                status="ERROR",
            )
        except BaseException:
            if _get_fail_open():
                logger.debug("Failed to emit error span (suppressed)", exc_info=True)
        raise

    end_time = _now_us()
    resp_attrs = _extract_response_attrs(response)
    req_attrs.update(resp_attrs)
    output_msgs = _extract_output_messages(response)

    try:
        _emit_span(
            attrs=req_attrs,
            name="anthropic.messages.create",
            start_time=start_time,
            end_time=end_time,
            parent=parent,
            input_messages=input_msgs,
            output_messages=output_msgs,
        )
    except BaseException:
        if _get_fail_open():
            logger.debug("Failed to emit span (suppressed)", exc_info=True)
        else:
            raise

    return response


async def _wrap_stream_async(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    """Wrapper for AsyncMessages.stream (async streaming)."""
    if not _is_initialized():
        return await wrapped(*args, **kwargs)

    parent = get_current_span()
    start_time = _now_us()
    req_attrs = _extract_request_attrs(kwargs)
    input_msgs = _extract_input_messages(kwargs)

    stream_mgr = wrapped(*args, **kwargs)
    return _AsyncWrappedStreamManager(
        stream_mgr=stream_mgr,
        start_time=start_time,
        req_attrs=req_attrs,
        input_msgs=input_msgs,
        parent=parent,
    )


class _AsyncWrappedStreamManager:
    """Wraps the async Anthropic MessageStream context manager."""

    def __init__(
        self,
        *,
        stream_mgr: Any,
        start_time: int,
        req_attrs: dict[str, Any],
        input_msgs: str | None,
        parent: SpanContext | None,
    ) -> None:
        self._stream_mgr = stream_mgr
        self._start_time = start_time
        self._req_attrs = req_attrs
        self._input_msgs = input_msgs
        self._parent = parent
        self._ttft_recorded = False

    async def __aenter__(self) -> Any:
        self._stream = await self._stream_mgr.__aenter__()
        return _AsyncWrappedStream(self)

    async def __aexit__(self, *exc_info: Any) -> Any:
        result = await self._stream_mgr.__aexit__(*exc_info)

        end_time = _now_us()
        final_message = getattr(self._stream, "get_final_message", lambda: None)()
        if final_message is not None:
            resp_attrs = _extract_response_attrs(final_message)
            self._req_attrs.update(resp_attrs)
            output_msgs = _extract_output_messages(final_message)
        else:
            output_msgs = None

        status = "ERROR" if exc_info[0] is not None else "OK"
        if exc_info[0] is not None:
            self._req_attrs["error.type"] = exc_info[0].__name__
            self._req_attrs["error.message"] = str(exc_info[1])

        try:
            _emit_span(
                attrs=self._req_attrs,
                name="anthropic.messages.stream",
                start_time=self._start_time,
                end_time=end_time,
                parent=self._parent,
                input_messages=self._input_msgs,
                output_messages=output_msgs,
                status=status,
            )
        except BaseException:
            if _get_fail_open():
                logger.debug("Failed to emit async stream span (suppressed)", exc_info=True)

        return result

    def _record_ttft(self) -> None:
        if not self._ttft_recorded:
            self._ttft_recorded = True
            ttft_us = _now_us() - self._start_time
            ttft_ms = ttft_us / 1000
            self._req_attrs["agentc.ttft_ms"] = round(ttft_ms, 2)
            logger.debug("Anthropic TTFT: %.1fms", ttft_ms)


class _AsyncWrappedStream:
    """Wraps the async stream iterator to capture TTFT."""

    def __init__(self, mgr: _AsyncWrappedStreamManager) -> None:
        self._mgr = mgr

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        try:
            event = await self._mgr._stream.__anext__()
        except StopAsyncIteration:
            raise

        event_type = getattr(event, "type", None)
        if event_type in ("content_block_delta", "text"):
            self._mgr._record_ttft()

        return event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mgr._stream, name)


# --- Patch/unpatch ---

_original_methods: dict[str, Any] = {}


def patch() -> None:
    """Apply wrapt patches to the Anthropic SDK."""
    global _patched

    if _patched:
        logger.debug("Anthropic SDK already patched — skipping")
        return

    try:
        import anthropic
    except ImportError:
        logger.debug("Anthropic SDK not installed — skipping patch")
        return

    version = getattr(anthropic, "__version__", "0.0.0")
    logger.info("Patching anthropic SDK (version %s, adapter: v030)", version)

    try:
        # Sync Messages.create
        wrapt.wrap_function_wrapper(
            "anthropic.resources.messages",
            "Messages.create",
            _wrap_create,
        )

        # Async AsyncMessages.create
        wrapt.wrap_function_wrapper(
            "anthropic.resources.messages",
            "AsyncMessages.create",
            _wrap_create_async,
        )

        # Sync Messages.stream
        wrapt.wrap_function_wrapper(
            "anthropic.resources.messages",
            "Messages.stream",
            _wrap_stream,
        )

        # Async AsyncMessages.stream
        wrapt.wrap_function_wrapper(
            "anthropic.resources.messages",
            "AsyncMessages.stream",
            _wrap_stream_async,
        )

        _patched = True
        logger.debug("Anthropic SDK patched (4 methods)")

    except AttributeError as exc:
        logger.warning("Patch target not found: %s. Falling back to httpx transport.", exc)
    except Exception:
        logger.warning("Failed to patch Anthropic SDK", exc_info=True)


def unpatch() -> None:
    """Remove Anthropic SDK patches by restoring original methods."""
    global _patched

    if not _patched:
        return

    try:
        import anthropic.resources.messages as msgs

        # wrapt replaces the attribute on the class — unwrap by checking __wrapped__
        for cls_name, method_name in [
            ("Messages", "create"),
            ("Messages", "stream"),
            ("AsyncMessages", "create"),
            ("AsyncMessages", "stream"),
        ]:
            cls = getattr(msgs, cls_name, None)
            if cls is None:
                continue
            method = getattr(cls, method_name, None)
            if method is not None and hasattr(method, "__wrapped__"):
                setattr(cls, method_name, method.__wrapped__)

        _patched = False
        logger.info("Anthropic SDK unpatched")

    except Exception:
        logger.warning("Failed to unpatch Anthropic SDK", exc_info=True)
