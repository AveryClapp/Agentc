"""Convert provider responses into :class:`NormalizedResponse`.

Accepts a LiteLLM ``ModelResponse`` (OpenAI chat shape, which LiteLLM also
uses for Anthropic) or an equivalent plain dict. Volatile fields such as tool
call IDs are dropped so that two responses can be compared on what the agent
would actually do.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from agentc.change_safety.records import (
    NormalizedResponse,
    ToolAction,
    canonical_json,
)


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def canonical_arguments(arguments: Any) -> str:
    """Canonical JSON for tool-call arguments.

    Arguments that are not valid JSON are kept as a JSON string so that two
    identical malformed payloads still compare equal.
    """
    if isinstance(arguments, str):
        try:
            return canonical_json(json.loads(arguments))
        except (ValueError, TypeError):
            return canonical_json(arguments)
    try:
        return canonical_json(arguments)
    except (ValueError, TypeError):
        return canonical_json(str(arguments))


def _usage(response: Any) -> dict[str, Any]:
    usage = _get(response, "usage")
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "cache_creation_input_tokens": None,
        }
    details = _get(usage, "prompt_tokens_details")
    cached = _int_or_none(_get(details, "cached_tokens")) if details is not None else None
    if cached is None:
        cached = _int_or_none(_get(usage, "cache_read_input_tokens"))
    return {
        "input_tokens": _int_or_none(_get(usage, "prompt_tokens")),
        "output_tokens": _int_or_none(_get(usage, "completion_tokens")),
        "cached_input_tokens": cached,
        "cache_creation_input_tokens": _int_or_none(
            _get(usage, "cache_creation_input_tokens")
        ),
    }


def _cost(response: Any) -> float | None:
    hidden = _get(response, "_hidden_params")
    cost = _get(hidden, "response_cost") if hidden is not None else None
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        return float(cost)
    return None


def normalize_response(
    response: Any,
    *,
    latency_s: float | None = None,
    applied_edits: tuple[Mapping[str, Any], ...] | None = None,
) -> NormalizedResponse:
    """Normalize one chat-completion response.

    Raises ``ValueError`` when the response has no assistant choice; callers
    that hit a provider error should use :func:`error_response` instead.
    """
    choices = _get(response, "choices") or []
    if not choices:
        raise ValueError("response has no choices")
    choice = choices[0]
    message = _get(choice, "message")
    if message is None:
        raise ValueError("response choice has no message")
    raw_calls = _get(message, "tool_calls") or []
    actions = tuple(
        ToolAction(
            name=str(_get(_get(call, "function"), "name", "")),
            arguments_json=canonical_arguments(_get(_get(call, "function"), "arguments", "")),
        )
        for call in raw_calls
    )
    content = _get(message, "content")
    return NormalizedResponse(
        kind="tool_calls" if actions else "text",
        text=content if isinstance(content, str) else "",
        tool_calls=actions,
        finish_reason=_get(choice, "finish_reason"),
        cost_usd=_cost(response),
        latency_s=latency_s,
        applied_edits=applied_edits,
        **_usage(response),
    )


def error_response(error: BaseException, *, latency_s: float | None = None) -> NormalizedResponse:
    return NormalizedResponse(
        kind="error", error=f"{type(error).__name__}: {error}"[:500], latency_s=latency_s
    )


def action_signature(response: NormalizedResponse) -> str:
    """Stable short identifier of the action a response takes.

    Text replies share one signature, because the text itself is compared by
    S1 to S3; tool calls are identified by name and canonical arguments.
    """
    if response.kind == "error":
        return "error"
    if response.kind == "text":
        return "text"
    return canonical_json([[a.name, a.arguments_json] for a in response.tool_calls])


__all__ = [
    "action_signature",
    "canonical_arguments",
    "error_response",
    "normalize_response",
]
