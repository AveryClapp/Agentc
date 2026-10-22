"""Content-free original SDK usage needed by future tariff-bound verification.

This projection is evidence, not a price or a bill. Missing categories stay
missing; new SDK fields set a coverage flag without retaining their contents.
"""

from __future__ import annotations

from typing import Any

VERSION = "provider-billing-usage-v1"
OPENAI = "openai.chat.completions.v1"
ANTHROPIC = "anthropic.messages.v1"


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump()
        if isinstance(result, dict):
            return result
    raise ValueError("SDK usage is not an object")


def _count(data: dict[str, Any], key: str, *, required: bool = False) -> int | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if type(value) is not int or not 0 <= value < 2**32:
        raise ValueError("SDK usage count is invalid")
    return value


def _details(
    data: dict[str, Any], key: str, fields: tuple[str, ...]
) -> tuple[dict[str, int | None] | None, bool]:
    value = data.get(key)
    if value is None:
        return None, False
    obj = _object(value)
    return {field: _count(obj, field) for field in fields}, bool(set(obj) - set(fields))


def _label(value: Any, allowed: set[str]) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if type(value) is str and value in allowed:
        return value, False
    return None, True


def projection(response: Any, protocol: str) -> dict[str, Any] | None:
    """Project only usage categories, never arbitrary SDK/provider payloads."""
    try:
        data = _object(getattr(response, "usage", None))
        if protocol == OPENAI:
            prompt = _count(data, "prompt_tokens", required=True)
            output = _count(data, "completion_tokens", required=True)
            assert prompt is not None and output is not None
            total = _count(data, "total_tokens")
            if total is not None and total != prompt + output:
                return None
            input_details, input_unknown = _details(
                data,
                "prompt_tokens_details",
                (
                    "cached_tokens",
                    "cache_write_tokens",
                    "audio_tokens",
                    "image_tokens",
                    "text_tokens",
                ),
            )
            output_details, output_unknown = _details(
                data,
                "completion_tokens_details",
                (
                    "accepted_prediction_tokens",
                    "rejected_prediction_tokens",
                    "audio_tokens",
                    "reasoning_tokens",
                    "text_tokens",
                ),
            )
            for details, count in ((input_details, prompt), (output_details, output)):
                if details is not None and any(
                    value is not None and value > count for value in details.values()
                ):
                    return None
            tier, tier_unknown = _label(
                getattr(response, "service_tier", None),
                {"auto", "default", "flex", "scale", "priority", "fast"},
            )
            known = {
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_tokens_details",
                "completion_tokens_details",
            }
            usage = {
                "provider_protocol": protocol,
                "input_tokens": prompt,
                "output_tokens": output,
                "total_tokens": total,
                "input_details": input_details,
                "output_details": output_details,
                "service_tier": tier,
                "unrecognized_fields": bool(set(data) - known)
                or input_unknown
                or output_unknown
                or tier_unknown,
            }
        elif protocol == ANTHROPIC:
            prompt = _count(data, "input_tokens", required=True)
            output = _count(data, "output_tokens", required=True)
            creation = _count(data, "cache_creation_input_tokens")
            read = _count(data, "cache_read_input_tokens")
            cache, cache_unknown = _details(
                data,
                "cache_creation",
                ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"),
            )
            if cache is not None and creation is not None:
                values = list(cache.values())
                present = sum(value for value in values if value is not None)
                if (
                    present > creation
                    or all(value is not None for value in values)
                    and present != creation
                ):
                    return None
            tools, tools_unknown = _details(
                data, "server_tool_use", ("web_search_requests", "web_fetch_requests")
            )
            tier, tier_unknown = _label(
                data.get("service_tier"), {"standard", "priority", "batch"}
            )
            geo, geo_unknown = _label(data.get("inference_geo"), {"global", "us"})
            known = {
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "cache_creation",
                "server_tool_use",
                "service_tier",
                "inference_geo",
            }
            usage = {
                "provider_protocol": protocol,
                "input_tokens": prompt,
                "output_tokens": output,
                "cache_creation_input_tokens": creation,
                "cache_read_input_tokens": read,
                "cache_creation": cache,
                "server_tool_use": tools,
                "service_tier": tier,
                "inference_geo": geo,
                "unrecognized_fields": bool(set(data) - known)
                or cache_unknown
                or tools_unknown
                or tier_unknown
                or geo_unknown,
            }
        else:
            return None
        return {"version": VERSION, "usage": usage}
    except Exception:
        return None
