"""Shared helpers for the framework adapters.

Each framework adapter wraps a small entry point on the framework's
public surface and tags the returned payload with
``LlmOutput(span_id=<current>)``. The tagging logic is identical across
adapters — the only difference is *which* method/class gets wrapped and
*what shape* the payload takes. That shared logic lives here.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

from agentc._context import get_current_span
from agentc._provenance import LlmOutput, tag

log = logging.getLogger("agentc.provenance_frameworks")


def current_span_id() -> Optional[str]:
    """Return the active agentc span id, or ``None`` if no span is open.

    Safe to call under any condition — we never raise when the SDK is
    not initialized or no context has been pushed.
    """
    try:
        ctx = get_current_span()
    except BaseException:
        return None
    return ctx.span_id if ctx is not None else None


def tag_payload(obj: Any, span_id: Optional[str]) -> Any:
    """Tag ``obj`` with :class:`LlmOutput(span_id)` when a span is open.

    Fail-open: any exception from the tag layer is swallowed; the
    returned object is the original, unmodified value so that a broken
    provenance path can never crash a user's agent.
    """
    if span_id is None:
        return obj
    try:
        return tag(obj, LlmOutput(span_id=span_id))
    except BaseException:
        log.debug("tag_payload: provenance tag raised; returning untagged", exc_info=True)
        return obj


def tag_dict_values(payload: Any, span_id: Optional[str]) -> Any:
    """Tag every value of a ``dict`` payload, leaving the dict identity.

    langgraph nodes return a state-fragment dict; each value corresponds
    to a distinct state field so we want one tag per value, not one tag
    on the enclosing dict.
    """
    if span_id is None or not isinstance(payload, dict):
        return tag_payload(payload, span_id)
    for v in payload.values():
        tag_payload(v, span_id)
    # The dict itself also gets a tag so downstream ``tag_of(dict)`` hits
    # — no-op if the dict is not weakly referenceable.
    return tag_payload(payload, span_id)


def tag_items(items: Iterable[Any], span_id: Optional[str]) -> None:
    """Tag every element of an iterable. Useful for adapters that return
    a list of messages rather than a single payload."""
    if span_id is None:
        return
    for item in items:
        tag_payload(item, span_id)


def wrap_with_tagging(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a callable so its return value is tagged post-call.

    Used by the langgraph adapter to compose a decorator stack without
    needing a new wrapt wrapper per node function.
    """

    import functools

    @functools.wraps(func)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        result = func(*args, **kwargs)
        span_id = current_span_id()
        return tag_dict_values(result, span_id)

    return _wrapped
