"""Cohere chat / chat_history canonicalization.

Cohere uses `preamble` (system instruction) + `chat_history` (past turns)
+ `message` (the new user turn). Roles are uppercased (`USER`, `CHATBOT`,
`SYSTEM`, `TOOL`). We remap them onto the four canonical role names.
"""

from __future__ import annotations

from typing import Any

from ._core import (
    build_envelope,
    normalize_content,
    normalize_role,
)

PROVIDER = "cohere"

_COHERE_ROLE_MAP = {
    "user": "user",
    "chatbot": "assistant",
    "system": "system",
    "tool": "tool",
}


def canonicalize(raw: Any) -> dict[str, Any]:
    preamble, history, message, tools, response_format = _extract(raw)
    norm_messages: list[dict[str, Any]] = []
    if preamble:
        norm_messages.append({"role": "system", "content": normalize_content(preamble)})
    for turn in history:
        norm_messages.append(_canonicalize_turn(turn))
    if message is not None and message != "":
        norm_messages.append({"role": "user", "content": normalize_content(message)})
    return build_envelope(PROVIDER, norm_messages, tools, response_format)


def _extract(raw: Any) -> tuple[Any, list[Any], Any, Any, Any]:
    if isinstance(raw, dict):
        return (
            raw.get("preamble"),
            list(raw.get("chat_history") or []),
            raw.get("message"),
            raw.get("tools"),
            raw.get("response_format"),
        )
    if isinstance(raw, list):
        return None, raw, None, None, None
    if isinstance(raw, str):
        return None, [], raw, None, None
    return None, [], None, None, None


def _canonicalize_turn(turn: Any) -> dict[str, Any]:
    if not isinstance(turn, dict):
        return {"role": "user", "content": str(turn).strip()}
    role_raw = turn.get("role")
    if isinstance(role_raw, str):
        mapped = _COHERE_ROLE_MAP.get(role_raw.strip().lower())
        role = mapped or normalize_role(role_raw)
    else:
        role = "user"
    content = normalize_content(turn.get("message") if "message" in turn else turn.get("content"))
    return {"role": role, "content": content}
