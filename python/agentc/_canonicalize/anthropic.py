"""Anthropic messages API canonicalization.

Anthropic carries a top-level `system` string alongside the `messages`
list; we lift it into the envelope as a leading system message so the
final envelope is consistent with OpenAI/Cohere. `tools` use
`input_schema` rather than `parameters`; the shared tool-normalizer
handles that distinction.
"""

from __future__ import annotations

from typing import Any

from ._core import (
    build_envelope,
    normalize_content,
    normalize_role,
)

PROVIDER = "anthropic"


def canonicalize(raw: Any) -> dict[str, Any]:
    messages, system, tools, response_format = _extract(raw)
    norm_messages: list[dict[str, Any]] = []
    if system:
        sys_content = normalize_content(system)
        norm_messages.append({"role": "system", "content": sys_content})
    for m in messages:
        norm_messages.append(_canonicalize_message(m))
    return build_envelope(PROVIDER, norm_messages, tools, response_format)


def _extract(raw: Any) -> tuple[list[Any], Any, Any, Any]:
    if isinstance(raw, dict):
        messages = raw.get("messages") or []
        system = raw.get("system")
        tools = raw.get("tools")
        # Anthropic does not use response_format today, but allow the generic key.
        response_format = raw.get("response_format") or raw.get("output_schema")
        return list(messages), system, tools, response_format
    if isinstance(raw, list):
        return raw, None, None, None
    return [], None, None, None


def _canonicalize_message(msg: Any) -> dict[str, Any]:
    if not isinstance(msg, dict):
        return {"role": "user", "content": str(msg).strip()}
    role = normalize_role(msg.get("role"))
    content = normalize_content(msg.get("content"))
    return {"role": role, "content": content}
