"""OpenAI chat.completions canonicalization.

Accepts either the full request kwargs (dict with `messages`, `tools`,
`response_format`) or a bare `messages` list. Tool-call envelopes in
assistant messages collapse to tool_use parts with hashed inputs so
benign reformattings don't cause misses.
"""

from __future__ import annotations

from typing import Any

from ._core import (
    build_envelope,
    normalize_content,
    normalize_role,
    sha256_hex,
)

PROVIDER = "openai"


def canonicalize(raw: Any) -> dict[str, Any]:
    messages, tools, response_format = _extract(raw)
    norm_messages = [_canonicalize_message(m) for m in messages]
    return build_envelope(PROVIDER, norm_messages, tools, response_format)


def _extract(raw: Any) -> tuple[list[Any], Any, Any]:
    if isinstance(raw, dict):
        messages = raw.get("messages") or []
        tools = raw.get("tools")
        response_format = raw.get("response_format")
        return list(messages), tools, response_format
    if isinstance(raw, list):
        return raw, None, None
    return [], None, None


def _canonicalize_message(msg: Any) -> dict[str, Any]:
    if not isinstance(msg, dict):
        return {"role": "user", "content": str(msg).strip()}
    role = normalize_role(msg.get("role"))
    content = normalize_content(msg.get("content"))

    tool_calls = msg.get("tool_calls")
    if tool_calls:
        call_parts = [_canonicalize_tool_call(c) for c in tool_calls]
        if isinstance(content, str):
            if content:
                content = [{"type": "text", "text": content}, *call_parts]
            else:
                content = call_parts
        elif isinstance(content, list):
            content = [*content, *call_parts]

    if role == "tool":
        tool_call_id = str(msg.get("tool_call_id", ""))
        inner = content if isinstance(content, str) else content
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": inner,
        }
    return {"role": role, "content": content}


def _canonicalize_tool_call(call: Any) -> dict[str, Any]:
    if not isinstance(call, dict):
        return {"type": "tool_use", "name": "", "input_sha256": sha256_hex("{}")}
    fn = call.get("function") or {}
    name = str(fn.get("name", "") or call.get("name", ""))
    args = fn.get("arguments") if isinstance(fn, dict) else call.get("arguments")
    if isinstance(args, (bytes, bytearray)):
        args_bytes = bytes(args)
    else:
        args_bytes = (args or "").encode("utf-8") if isinstance(args, str) else str(args or "").encode("utf-8")
    return {
        "type": "tool_use",
        "name": name,
        "input_sha256": sha256_hex(args_bytes),
    }
