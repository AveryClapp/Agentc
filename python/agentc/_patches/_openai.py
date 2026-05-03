"""OpenAI SDK patch via wrapt.

Intercepts Completions.create (sync and async) to capture spans
with gen_ai.* attributes. Handles streaming with usage injection.

Supports openai >= 1.0.0.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import wrapt

from agentc._context import SpanContext, get_current_span
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
# Context flag to prevent double-instrumentation with httpx fallback
_SDK_INSTRUMENTED_FLAG = "_agentc_openai_sdk_instrumented"


def _get_fail_open() -> bool:
    from agentc._lifecycle import get_config

    config = get_config()
    return config.fail_open if config is not None else True


def _extract_request_attrs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Extract gen_ai.* request attributes from create() kwargs."""
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
    }
    if "model" in kwargs:
        attrs["gen_ai.request.model"] = kwargs["model"]
    if "temperature" in kwargs:
        attrs["gen_ai.request.temperature"] = kwargs["temperature"]
    if "top_p" in kwargs:
        attrs["gen_ai.request.top_p"] = kwargs["top_p"]
    if "max_tokens" in kwargs:
        attrs["gen_ai.request.max_tokens"] = kwargs["max_tokens"]
    if "max_completion_tokens" in kwargs:
        attrs["gen_ai.request.max_tokens"] = kwargs["max_completion_tokens"]
    return attrs


def _extract_input_messages(kwargs: dict[str, Any]) -> str | None:
    """Extract input messages as JSON string."""
    messages = kwargs.get("messages")
    if messages is None:
        return None
    try:
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
    """Extract gen_ai.* response attributes from a ChatCompletion response."""
    attrs: dict[str, Any] = {}
    if hasattr(response, "model"):
        attrs["gen_ai.response.model"] = response.model
    if hasattr(response, "id"):
        attrs["gen_ai.response.id"] = response.id

    # Finish reason from first choice
    choices = getattr(response, "choices", None)
    if choices and len(choices) > 0:
        fr = getattr(choices[0], "finish_reason", None)
        if fr:
            attrs["gen_ai.response.finish_reasons"] = fr

    # Usage
    usage = getattr(response, "usage", None)
    if usage is not None:
        if hasattr(usage, "prompt_tokens"):
            attrs["gen_ai.usage.input_tokens"] = usage.prompt_tokens
        if hasattr(usage, "completion_tokens"):
            attrs["gen_ai.usage.output_tokens"] = usage.completion_tokens

    return attrs


def _extract_output_messages(response: Any) -> str | None:
    """Extract output messages from a ChatCompletion response."""
    try:
        choices = getattr(response, "choices", None)
        if not choices:
            return None
        messages = []
        for choice in choices:
            msg = getattr(choice, "message", None)
            if msg is not None:
                if hasattr(msg, "model_dump"):
                    messages.append(msg.model_dump())
                else:
                    messages.append({
                        "role": getattr(msg, "role", "assistant"),
                        "content": getattr(msg, "content", ""),
                    })
        return json.dumps(messages, default=str) if messages else None
    except Exception:
        return None


def _inject_stream_options(kwargs: dict[str, Any]) -> bool:
    """Inject stream_options.include_usage if streaming. Returns True if injected."""
    if not kwargs.get("stream", False):
        return False

    stream_opts = kwargs.get("stream_options")
    if stream_opts is None:
        kwargs["stream_options"] = {"include_usage": True}
        return True
    elif isinstance(stream_opts, dict) and "include_usage" not in stream_opts:
        stream_opts["include_usage"] = True
        return True
    return False


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
        "OpenAI span captured: %s (model=%s, in=%s, out=%s)",
        span_id,
        attrs.get("gen_ai.request.model", "?"),
        attrs.get("gen_ai.usage.input_tokens", "?"),
        attrs.get("gen_ai.usage.output_tokens", "?"),
    )

    _write_root_span(span_dict)  # bd-4hy: route non-root spans through writer queue


# --- Optimizer plumbing ---


def _plan_openai_call(
    kwargs: dict[str, Any],
    parent: SpanContext | None,
) -> tuple[Any, str | None]:
    """Build a Call dict and ask the optimizer for a Plan.

    Returns ``(plan, call_site_id)`` on success, ``(None, None)`` if
    optimization should be skipped (opt-out, planning error, missing
    optimizer module). Callers fall through to direct dispatch on None.
    """
    try:
        from agentc._intercept import is_opted_out
        from agentc._optimizer import plan_call
        from agentc._patches._optimizer_glue import (
            build_call_dict_openai,
            derive_call_site_id,
        )
    except BaseException:
        logger.debug("optimizer modules unavailable; skipping", exc_info=True)
        return None, None

    if is_opted_out(kwargs.get("extra_headers")):
        return None, None

    try:
        call_site_id = derive_call_site_id()
        trace_id_hex = parent.trace_id if parent is not None else _generate_trace_id()
        span_id_hex = _generate_span_id()
        call = build_call_dict_openai(
            kwargs,
            call_site_id=call_site_id,
            trace_id_hex=trace_id_hex,
            span_id_hex=span_id_hex,
        )
        plan = plan_call(call)
    except BaseException:
        logger.debug("optimizer planning failed; falling through", exc_info=True)
        return None, None

    return plan, call_site_id


def _observe_openai_outcome(
    *,
    plan: Any,
    response: Any,
    call_site_id: str,
    kwargs: dict[str, Any],
    elapsed_s: float,
) -> None:
    """Build an Outcome and feed it back to the cost model. Best-effort."""
    try:
        from agentc._optimizer import observe_outcome
        from agentc._patches._optimizer_glue import build_outcome_openai

        # Prefer the model echoed by the response (matches what was
        # actually billed when the optimizer rewrote the request).
        model = str(getattr(response, "model", None) or kwargs.get("model", "") or "")
        outcome = build_outcome_openai(
            response,
            elapsed_s=elapsed_s,
            model=model,
            call_site_id=call_site_id,
        )
        observe_outcome(plan, outcome)
    except BaseException:
        logger.debug("optimizer observe failed; skipping", exc_info=True)


# --- Sync wrapper ---


def _wrap_create(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    """Wrapper for Completions.create (sync)."""
    if not _is_initialized():
        return wrapped(*args, **kwargs)

    parent = get_current_span()
    start_time = _now_us()
    req_attrs = _extract_request_attrs(kwargs)
    input_msgs = _extract_input_messages(kwargs)

    is_streaming = kwargs.get("stream", False)

    if is_streaming:
        return _handle_streaming_sync(wrapped, args, kwargs, parent, start_time, req_attrs, input_msgs)

    plan, call_site_id = _plan_openai_call(kwargs, parent)

    # Non-streaming
    try:
        if plan is not None:
            from agentc._patches._optimizer_glue import (
                apply_call_mutations_openai,
                dispatch_sync,
            )

            def _run_original() -> Any:
                return wrapped(*args, **kwargs)

            def _run_mutated(mutated_call: dict[str, Any]) -> Any:
                new_kwargs = apply_call_mutations_openai(kwargs, mutated_call)
                return wrapped(*args, **new_kwargs)

            response = dispatch_sync(plan, run_original=_run_original, run_mutated=_run_mutated)
        else:
            response = wrapped(*args, **kwargs)
    except BaseException as exc:
        end_time = _now_us()
        req_attrs["error.type"] = type(exc).__name__
        req_attrs["error.message"] = str(exc)
        try:
            _emit_span(
                attrs=req_attrs,
                name="openai.chat.completions.create",
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

    if plan is not None and call_site_id is not None:
        _observe_openai_outcome(
            plan=plan,
            response=response,
            call_site_id=call_site_id,
            kwargs=kwargs,
            elapsed_s=(end_time - start_time) / 1_000_000.0,
        )

    resp_attrs = _extract_response_attrs(response)
    req_attrs.update(resp_attrs)
    output_msgs = _extract_output_messages(response)

    try:
        _emit_span(
            attrs=req_attrs,
            name="openai.chat.completions.create",
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


def _handle_streaming_sync(
    wrapped: Any,
    args: Any,
    kwargs: Any,
    parent: SpanContext | None,
    start_time: int,
    req_attrs: dict[str, Any],
    input_msgs: str | None,
) -> Any:
    """Handle streaming create() calls."""
    injected = _inject_stream_options(kwargs)

    try:
        stream = wrapped(*args, **kwargs)
    except BaseException as exc:
        # If injection caused the error, retry without
        if injected:
            logger.debug("stream_options injection may have caused error, retrying without")
            kwargs.pop("stream_options", None)
            try:
                stream = wrapped(*args, **kwargs)
            except BaseException:
                raise exc  # raise original
        else:
            end_time = _now_us()
            req_attrs["error.type"] = type(exc).__name__
            req_attrs["error.message"] = str(exc)
            try:
                _emit_span(
                    attrs=req_attrs,
                    name="openai.chat.completions.create",
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

    return _StreamingIterator(
        stream=stream,
        start_time=start_time,
        req_attrs=req_attrs,
        input_msgs=input_msgs,
        parent=parent,
    )


class _StreamingIterator:
    """Wraps OpenAI streaming iterator to capture TTFT and usage."""

    def __init__(
        self,
        *,
        stream: Any,
        start_time: int,
        req_attrs: dict[str, Any],
        input_msgs: str | None,
        parent: SpanContext | None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._req_attrs = req_attrs
        self._input_msgs = input_msgs
        self._parent = parent
        self._ttft_recorded = False
        self._usage: Any = None
        self._finalized = False

    def __iter__(self) -> "_StreamingIterator":
        return self

    def __next__(self) -> Any:
        try:
            chunk = next(self._stream)
        except StopIteration:
            self._finalize()
            raise
        except BaseException:
            self._finalize()
            raise

        # TTFT on first content delta
        if not self._ttft_recorded:
            choices = getattr(chunk, "choices", [])
            if choices and getattr(choices[0], "delta", None):
                delta = choices[0].delta
                if getattr(delta, "content", None) is not None:
                    self._ttft_recorded = True
                    ttft_us = _now_us() - self._start_time
                    self._req_attrs["agentc.ttft_ms"] = round(ttft_us / 1000, 2)
                    logger.debug("OpenAI TTFT: %.1fms", ttft_us / 1000)

        # Capture usage from final chunk
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self._usage = usage

        return chunk

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True

        end_time = _now_us()
        if self._usage is not None:
            if hasattr(self._usage, "prompt_tokens"):
                self._req_attrs["gen_ai.usage.input_tokens"] = self._usage.prompt_tokens
            if hasattr(self._usage, "completion_tokens"):
                self._req_attrs["gen_ai.usage.output_tokens"] = self._usage.completion_tokens

        try:
            _emit_span(
                attrs=self._req_attrs,
                name="openai.chat.completions.create",
                start_time=self._start_time,
                end_time=end_time,
                parent=self._parent,
                input_messages=self._input_msgs,
            )
        except BaseException:
            if _get_fail_open():
                logger.debug("Failed to emit streaming span (suppressed)", exc_info=True)

    def __enter__(self) -> "_StreamingIterator":
        return self

    def __exit__(self, *args: Any) -> None:
        if hasattr(self._stream, "__exit__"):
            self._stream.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


# --- Async wrapper ---


async def _wrap_create_async(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    """Wrapper for AsyncCompletions.create (async)."""
    if not _is_initialized():
        return await wrapped(*args, **kwargs)

    parent = get_current_span()
    start_time = _now_us()
    req_attrs = _extract_request_attrs(kwargs)
    input_msgs = _extract_input_messages(kwargs)

    plan, call_site_id = _plan_openai_call(kwargs, parent)

    # For now, handle non-streaming only (async streaming is similar pattern)
    try:
        if plan is not None:
            from agentc._executor import dispatch
            from agentc._patches._optimizer_glue import apply_call_mutations_openai

            async def _run_original() -> Any:
                return await wrapped(*args, **kwargs)

            async def _run_mutated(mutated_call: dict[str, Any]) -> Any:
                new_kwargs = apply_call_mutations_openai(kwargs, mutated_call)
                return await wrapped(*args, **new_kwargs)

            response = await dispatch(plan, run_original=_run_original, run_mutated=_run_mutated)
        else:
            response = await wrapped(*args, **kwargs)
    except BaseException as exc:
        end_time = _now_us()
        req_attrs["error.type"] = type(exc).__name__
        req_attrs["error.message"] = str(exc)
        try:
            _emit_span(
                attrs=req_attrs,
                name="openai.chat.completions.create",
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

    if plan is not None and call_site_id is not None:
        _observe_openai_outcome(
            plan=plan,
            response=response,
            call_site_id=call_site_id,
            kwargs=kwargs,
            elapsed_s=(end_time - start_time) / 1_000_000.0,
        )

    resp_attrs = _extract_response_attrs(response)
    req_attrs.update(resp_attrs)
    output_msgs = _extract_output_messages(response)

    try:
        _emit_span(
            attrs=req_attrs,
            name="openai.chat.completions.create",
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


# --- Patch/unpatch ---


def patch() -> None:
    """Apply wrapt patches to the OpenAI SDK."""
    global _patched

    if _patched:
        logger.debug("OpenAI SDK already patched — skipping")
        return

    try:
        import openai
    except ImportError:
        logger.debug("OpenAI SDK not installed — skipping patch")
        return

    version = getattr(openai, "__version__", "0.0.0")
    logger.info("Patching openai SDK (version %s, adapter: adapter_openai_v1)", version)

    try:
        wrapt.wrap_function_wrapper(
            "openai.resources.chat.completions",
            "Completions.create",
            _wrap_create,
        )

        wrapt.wrap_function_wrapper(
            "openai.resources.chat.completions",
            "AsyncCompletions.create",
            _wrap_create_async,
        )

        _patched = True
        logger.debug("OpenAI SDK patched (2 methods)")

    except AttributeError as exc:
        logger.warning("Patch target not found: %s.", exc)
    except Exception:
        logger.warning("Failed to patch OpenAI SDK", exc_info=True)


def unpatch() -> None:
    """Remove OpenAI SDK patches."""
    global _patched

    if not _patched:
        return

    try:
        import openai.resources.chat.completions as cc

        for cls_name, method_name in [
            ("Completions", "create"),
            ("AsyncCompletions", "create"),
        ]:
            cls = getattr(cc, cls_name, None)
            if cls is None:
                continue
            method = getattr(cls, method_name, None)
            if method is not None and hasattr(method, "__wrapped__"):
                setattr(cls, method_name, method.__wrapped__)

        _patched = False
        logger.info("OpenAI SDK unpatched")

    except Exception:
        logger.warning("Failed to unpatch OpenAI SDK", exc_info=True)
