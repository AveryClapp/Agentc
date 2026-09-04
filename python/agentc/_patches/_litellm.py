"""LiteLLM function adapter for optimizer planning and trace capture.

LiteLLM exposes module-level ``completion`` and ``acompletion`` functions.
Agent frameworks commonly call those functions instead of provider SDK
resources, so provider-only patching misses the application-side seam.  This
adapter treats LiteLLM's OpenAI-shaped request and ``ModelResponse`` as one
logical call and suppresses nested provider adapters during dispatch.

The adapter is optional: importing Agentc never requires LiteLLM.  Streaming
calls currently pass through to the provider adapters because neither frozen
MLSys workload uses LiteLLM streaming.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
import time
from types import ModuleType
from typing import Any

import wrapt

from agentc._context import SpanContext, get_current_span
from agentc._interception_context import interception_is_nested, interception_owner
from agentc._span import (
    _build_span_dict,
    _enqueue_span,
    _generate_span_id,
    _generate_trace_id,
    _is_initialized,
    _now_us,
    _write_root_span,
)

logger = logging.getLogger("agentc")

_patched = False
_patches: list[tuple[ModuleType, str, Any, Any]] = []
_sync_original: Any | None = None
_async_original: Any | None = None
_sync_wrapper: Any | None = None
_async_wrapper: Any | None = None

# tau2 imports the function by value; SWE-agent reads ``litellm.completion``
# dynamically and therefore needs no alias repair.  The list is deliberately
# explicit rather than scanning arbitrary application modules.
_KNOWN_ALIASES = (("tau2.utils.llm_utils", "completion", "sync"),)

_POSITIONAL_FIELDS = (
    "model",
    "messages",
    "timeout",
    "temperature",
    "top_p",
    "n",
    "stream",
    "stream_options",
    "stop",
    "max_completion_tokens",
    "max_tokens",
)
_MUTABLE_FIELDS = (
    "model",
    "messages",
    "temperature",
    "top_p",
    "max_completion_tokens",
    "max_tokens",
)
_MISSING = object()


def _get_fail_open() -> bool:
    from agentc._lifecycle import get_config

    config = get_config()
    return config.fail_open if config is not None else True


def _request_kwargs(args: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Project positional LiteLLM arguments into a read-only request map."""
    request = dict(kwargs)
    for index, value in enumerate(args[: len(_POSITIONAL_FIELDS)]):
        request.setdefault(_POSITIONAL_FIELDS[index], value)
    return request


def _values_equal(left: Any, right: Any) -> bool:
    if left is right:
        return True
    try:
        result = left == right
        return result if isinstance(result, bool) else False
    except BaseException:
        return False


def _mutated_dispatch_args(
    args: Any,
    kwargs: dict[str, Any],
    request: dict[str, Any],
    mutated_call: dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Apply supported Call mutations without disturbing unrelated arguments."""
    from agentc._patches._optimizer_glue import (
        LITELLM_COMPLETION_PROTOCOL,
        apply_call_mutations_openai,
    )

    mutated_request = apply_call_mutations_openai(
        request,
        mutated_call,
        provider_protocol=LITELLM_COMPLETION_PROTOCOL,
    )
    new_args = list(args)
    new_kwargs = dict(kwargs)
    for field in _MUTABLE_FIELDS:
        before = request.get(field, _MISSING)
        after = mutated_request.get(field, _MISSING)
        if before is _MISSING and after is _MISSING:
            continue
        if (
            before is not _MISSING
            and after is not _MISSING
            and _values_equal(before, after)
        ):
            continue

        position = _POSITIONAL_FIELDS.index(field)
        if position < len(new_args):
            new_args[position] = None if after is _MISSING else after
            new_kwargs.pop(field, None)
        elif after is _MISSING:
            new_kwargs.pop(field, None)
        else:
            new_kwargs[field] = after
    return tuple(new_args), new_kwargs


def _extract_request_attrs(request: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "litellm",
    }
    if "model" in request:
        attrs["gen_ai.request.model"] = str(request["model"])
    if request.get("temperature") is not None:
        attrs["gen_ai.request.temperature"] = request["temperature"]
    if request.get("top_p") is not None:
        attrs["gen_ai.request.top_p"] = request["top_p"]
    cap = request.get("max_completion_tokens", request.get("max_tokens"))
    if cap is not None:
        attrs["gen_ai.request.max_tokens"] = cap
    return attrs


def _extract_input_messages(request: dict[str, Any]) -> str | None:
    messages = request.get("messages")
    if messages is None:
        return None
    try:
        return json.dumps(messages, default=str)
    except BaseException:
        return None


def _extract_response_attrs(response: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    model = getattr(response, "model", None)
    if model:
        attrs["gen_ai.response.model"] = str(model)
    response_id = getattr(response, "id", None)
    if response_id:
        attrs["gen_ai.response.id"] = str(response_id)
    choices = getattr(response, "choices", None) or []
    if choices:
        finish_reason = getattr(choices[0], "finish_reason", None)
        if finish_reason:
            attrs["gen_ai.response.finish_reasons"] = str(finish_reason)
    usage = getattr(response, "usage", None)
    if usage is not None:
        input_tokens = getattr(usage, "prompt_tokens", None)
        output_tokens = getattr(usage, "completion_tokens", None)
        if input_tokens is not None:
            attrs["gen_ai.usage.input_tokens"] = int(input_tokens)
        if output_tokens is not None:
            attrs["gen_ai.usage.output_tokens"] = int(output_tokens)
    return attrs


def _extract_output_messages(response: Any) -> str | None:
    try:
        choices = getattr(response, "choices", None) or []
        messages = []
        for choice in choices:
            message = getattr(choice, "message", None)
            if message is None:
                continue
            if hasattr(message, "model_dump"):
                messages.append(message.model_dump())
            elif hasattr(message, "to_dict"):
                messages.append(message.to_dict())
            else:
                messages.append(
                    {
                        "role": getattr(message, "role", "assistant"),
                        "content": getattr(message, "content", ""),
                    }
                )
        return json.dumps(messages, default=str) if messages else None
    except BaseException:
        return None


def _emit_span(
    *,
    attrs: dict[str, Any],
    start_time: int,
    end_time: int,
    parent: SpanContext | None,
    input_messages: str | None = None,
    output_messages: str | None = None,
    status: str = "OK",
) -> None:
    span_id = _generate_span_id()
    trace_id = parent.trace_id if parent is not None else _generate_trace_id()
    parent_span_id = parent.span_id if parent is not None else None
    if parent is not None:
        attrs["gen_ai.agent.name"] = parent.name

    span = _build_span_dict(
        span_id=span_id,
        trace_id=trace_id,
        name="litellm.completion",
        kind="chat",
        start_time=start_time,
        parent_span_id=parent_span_id,
        end_time=end_time,
        status=status,
        attributes=attrs,
    )
    if input_messages is not None:
        span["input_messages"] = input_messages
    if output_messages is not None:
        span["output_messages"] = output_messages
    if parent_span_id is None:
        _write_root_span(span)
    else:
        _enqueue_span(span)


def _plan_call(
    request: dict[str, Any],
    parent: SpanContext | None,
    decision: Any,
) -> tuple[Any, str | None]:
    if not decision.eligible:
        return None, None
    try:
        from agentc._optimizer import plan_call
        from agentc._patches._optimizer_glue import (
            LITELLM_COMPLETION_PROTOCOL,
            build_call_dict_openai,
            derive_call_site_id,
        )

        call_site_id = derive_call_site_id()
        call = build_call_dict_openai(
            request,
            call_site_id=call_site_id,
            trace_id_hex=parent.trace_id
            if parent is not None
            else _generate_trace_id(),
            span_id_hex=_generate_span_id(),
            provider_protocol=LITELLM_COMPLETION_PROTOCOL,
        )
        return plan_call(call), call_site_id
    except BaseException:
        logger.debug(
            "LiteLLM optimizer planning failed; passing through", exc_info=True
        )
        return None, None


def _observe(
    plan: Any,
    response: Any,
    call_site_id: str,
    request: dict[str, Any],
    elapsed_s: float,
) -> None:
    # LiteLLM ModelResponse intentionally follows the OpenAI response shape,
    # including usage and serializable choices.  Reuse the mature observation,
    # trace-window, and cache-seeding implementation at that schema seam.
    from agentc._patches._openai import _observe_openai_outcome

    _observe_openai_outcome(
        plan=plan,
        response=response,
        call_site_id=call_site_id,
        kwargs=request,
        elapsed_s=elapsed_s,
    )


def _call_original(wrapped: Any, args: Any, kwargs: dict[str, Any]) -> Any:
    with interception_owner("litellm"):
        return wrapped(*args, **kwargs)


async def _call_original_async(
    wrapped: Any,
    args: Any,
    kwargs: dict[str, Any],
) -> Any:
    with interception_owner("litellm"):
        return await wrapped(*args, **kwargs)


def _wrap_completion(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    if not _is_initialized() or interception_is_nested():
        return wrapped(*args, **kwargs)

    request = _request_kwargs(args, kwargs)
    if request.get("stream"):
        return wrapped(*args, **kwargs)

    from agentc._optimization_scope import decide_optimization

    parent = get_current_span()
    start_time = _now_us()
    started = time.perf_counter()
    attrs = _extract_request_attrs(request)
    input_messages = _extract_input_messages(request)
    decision = decide_optimization(request.get("extra_headers"))
    attrs.update(decision.span_attributes())
    plan, call_site_id = _plan_call(request, parent, decision)

    def run_original() -> Any:
        return _call_original(wrapped, args, kwargs)

    def run_mutated(mutated_call: dict[str, Any]) -> Any:
        new_args, new_kwargs = _mutated_dispatch_args(
            args,
            kwargs,
            request,
            mutated_call,
        )
        return _call_original(wrapped, new_args, new_kwargs)

    try:
        if plan is None:
            response = run_original()
        else:
            from agentc._patches._optimizer_glue import (
                dispatch_sync,
                maybe_shadow_record,
            )

            response = dispatch_sync(
                plan,
                run_original=run_original,
                run_mutated=run_mutated,
            )
            primary_elapsed_s = time.perf_counter() - started
            if call_site_id is not None:
                _observe(plan, response, call_site_id, request, primary_elapsed_s)
            maybe_shadow_record(plan, call_site_id, response, run_original)
    except BaseException as exc:
        attrs["error.type"] = type(exc).__name__
        attrs["error.message"] = str(exc)
        try:
            _emit_span(
                attrs=attrs,
                start_time=start_time,
                end_time=_now_us(),
                parent=parent,
                input_messages=input_messages,
                status="ERROR",
            )
        except BaseException:
            if _get_fail_open():
                logger.debug("LiteLLM error-span emission failed", exc_info=True)
        raise

    end_time = _now_us()
    if plan is not None:
        from agentc._patches._optimizer_glue import (
            dispatch_span_attributes,
            resolve_executed_model_id,
        )

        executed_model = resolve_executed_model_id(plan, response, request.get("model"))
        attrs.update(dispatch_span_attributes(plan, executed_model))
    attrs.update(_extract_response_attrs(response))
    try:
        _emit_span(
            attrs=attrs,
            start_time=start_time,
            end_time=end_time,
            parent=parent,
            input_messages=input_messages,
            output_messages=_extract_output_messages(response),
        )
    except BaseException:
        if _get_fail_open():
            logger.debug("LiteLLM span emission failed", exc_info=True)
        else:
            raise
    return response


async def _wrap_acompletion(
    wrapped: Any,
    instance: Any,
    args: Any,
    kwargs: Any,
) -> Any:
    if not _is_initialized() or interception_is_nested():
        return await wrapped(*args, **kwargs)

    request = _request_kwargs(args, kwargs)
    if request.get("stream"):
        return await wrapped(*args, **kwargs)

    from agentc._optimization_scope import decide_optimization

    parent = get_current_span()
    start_time = _now_us()
    started = time.perf_counter()
    attrs = _extract_request_attrs(request)
    input_messages = _extract_input_messages(request)
    decision = decide_optimization(request.get("extra_headers"))
    attrs.update(decision.span_attributes())
    plan, call_site_id = _plan_call(request, parent, decision)

    async def run_original() -> Any:
        return await _call_original_async(wrapped, args, kwargs)

    async def run_mutated(mutated_call: dict[str, Any]) -> Any:
        new_args, new_kwargs = _mutated_dispatch_args(
            args,
            kwargs,
            request,
            mutated_call,
        )
        return await _call_original_async(wrapped, new_args, new_kwargs)

    try:
        if plan is None:
            response = await run_original()
        else:
            from agentc._executor import dispatch
            from agentc._patches._optimizer_glue import maybe_shadow_record_async

            response = await dispatch(
                plan,
                run_original=run_original,
                run_mutated=run_mutated,
            )
            primary_elapsed_s = time.perf_counter() - started
            if call_site_id is not None:
                _observe(plan, response, call_site_id, request, primary_elapsed_s)
            await maybe_shadow_record_async(
                plan,
                call_site_id,
                response,
                run_original,
            )
    except BaseException as exc:
        attrs["error.type"] = type(exc).__name__
        attrs["error.message"] = str(exc)
        try:
            _emit_span(
                attrs=attrs,
                start_time=start_time,
                end_time=_now_us(),
                parent=parent,
                input_messages=input_messages,
                status="ERROR",
            )
        except BaseException:
            if _get_fail_open():
                logger.debug("LiteLLM async error-span emission failed", exc_info=True)
        raise

    end_time = _now_us()
    if plan is not None:
        from agentc._patches._optimizer_glue import (
            dispatch_span_attributes,
            resolve_executed_model_id,
        )

        executed_model = resolve_executed_model_id(plan, response, request.get("model"))
        attrs.update(dispatch_span_attributes(plan, executed_model))
    attrs.update(_extract_response_attrs(response))
    try:
        _emit_span(
            attrs=attrs,
            start_time=start_time,
            end_time=end_time,
            parent=parent,
            input_messages=input_messages,
            output_messages=_extract_output_messages(response),
        )
    except BaseException:
        if _get_fail_open():
            logger.debug("LiteLLM async span emission failed", exc_info=True)
        else:
            raise
    return response


def _install_target(module: ModuleType, attribute: str, wrapper: Any) -> None:
    original = getattr(module, attribute)
    setattr(module, attribute, wrapper)
    _patches.append((module, attribute, original, wrapper))


def _repair_loaded_aliases() -> None:
    for module_name, attribute, kind in _KNOWN_ALIASES:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        current = getattr(module, attribute, None)
        originals = (_sync_original,) if kind == "sync" else (_async_original,)
        wrapper = _sync_wrapper if kind == "sync" else _async_wrapper
        if wrapper is not None and any(
            original is not None and current is original for original in originals
        ):
            _install_target(module, attribute, wrapper)


def patch() -> None:
    """Patch LiteLLM sync/async functions and known pre-imported aliases."""
    global _patched, _sync_original, _async_original, _sync_wrapper, _async_wrapper
    if _patched:
        return
    try:
        litellm = importlib.import_module("litellm")
        main = importlib.import_module("litellm.main")
        _sync_original = getattr(litellm, "completion")
        _async_original = getattr(litellm, "acompletion")
        _sync_wrapper = wrapt.FunctionWrapper(_sync_original, _wrap_completion)
        _async_wrapper = wrapt.FunctionWrapper(_async_original, _wrap_acompletion)
        _install_target(litellm, "completion", _sync_wrapper)
        _install_target(litellm, "acompletion", _async_wrapper)
        if getattr(main, "completion", None) is _sync_original:
            _install_target(main, "completion", _sync_wrapper)
        if getattr(main, "acompletion", None) is _async_original:
            _install_target(main, "acompletion", _async_wrapper)
        _repair_loaded_aliases()
    except BaseException:
        unpatch()
        logger.debug("LiteLLM unavailable or patching failed; skipping", exc_info=True)
        return
    _patched = True
    logger.debug("LiteLLM patched (sync, async, and loaded aliases)")


def unpatch() -> None:
    """Restore exact pre-Agentc LiteLLM functions and known aliases."""
    global _patched, _sync_original, _async_original, _sync_wrapper, _async_wrapper
    try:
        # Repair aliases imported after patching before restoring module exports.
        for module_name, attribute, kind in _KNOWN_ALIASES:
            try:
                module = sys.modules.get(module_name)
                if module is None:
                    continue
                wrapper = _sync_wrapper if kind == "sync" else _async_wrapper
                original = _sync_original if kind == "sync" else _async_original
                if wrapper is not None and getattr(module, attribute, None) is wrapper:
                    setattr(module, attribute, original)
            except BaseException:
                logger.debug(
                    "Failed to restore LiteLLM alias %s.%s",
                    module_name,
                    attribute,
                    exc_info=True,
                )

        while _patches:
            module, attribute, original, wrapper = _patches.pop()
            try:
                if getattr(module, attribute, None) is wrapper:
                    setattr(module, attribute, original)
            except BaseException:
                logger.debug(
                    "Failed to restore LiteLLM target %s.%s",
                    getattr(module, "__name__", type(module).__name__),
                    attribute,
                    exc_info=True,
                )
    finally:
        _patched = False
        _sync_original = None
        _async_original = None
        _sync_wrapper = None
        _async_wrapper = None


__all__ = ["patch", "unpatch"]
