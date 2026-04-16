"""Raw-string and fallback canonicalization.

Wraps any non-vendor input as a single user message. Used when a caller
supplies a plain prompt string (`agentc.memoize.cached_call("hello")`)
or when the provider is unknown.
"""

from __future__ import annotations

from typing import Any

from ._core import (
    build_envelope,
    normalize_content,
    normalize_role,
)

PROVIDER = "raw"


def canonicalize(raw: Any) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if isinstance(raw, str):
        messages.append({"role": "user", "content": raw.strip()})
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                role = normalize_role(item.get("role"))
                messages.append({"role": role, "content": normalize_content(item.get("content"))})
            else:
                messages.append({"role": "user", "content": str(item).strip()})
    elif isinstance(raw, dict):
        role = normalize_role(raw.get("role", "user"))
        messages.append({"role": role, "content": normalize_content(raw.get("content", raw))})
    else:
        messages.append({"role": "user", "content": str(raw).strip()})
    return build_envelope(PROVIDER, messages)
