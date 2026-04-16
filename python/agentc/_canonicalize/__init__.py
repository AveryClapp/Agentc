"""Prompt and parameter canonicalization for memoization cache keys.

Vendor-specific prompt formats (OpenAI `messages`, Anthropic `messages`,
Cohere `chat_history`, raw strings) normalize into a single JSON structure.
The SHA-256 of the resulting bytes is the `prompt_hash` used in cache keys.

Parameters normalize separately — only the keys that affect the model's
output are retained; transport-level flags (stream, user, metadata,
extra_headers, agentc_*) are dropped.

Public API:
    canonicalize_prompt(raw, provider) -> bytes
    canonicalize_parameters(raw) -> bytes
    prompt_hash(raw, provider) -> bytes  (SHA-256 digest)
    parameters_hash(raw) -> bytes        (SHA-256 digest)
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import anthropic as _anthropic
from . import cohere as _cohere
from . import openai as _openai
from . import raw as _raw
from ._core import canonicalize_parameters, deterministic_dumps

__all__ = [
    "canonicalize_prompt",
    "canonicalize_parameters",
    "prompt_hash",
    "parameters_hash",
]


def canonicalize_prompt(raw: Any, provider: str) -> bytes:
    """Return the canonical UTF-8 JSON bytes for a prompt.

    `provider` selects the adapter; unknown providers fall back to the
    raw-string adapter, which coerces arbitrary input to a one-message
    user prompt.
    """
    prov = (provider or "").strip().lower()
    if prov == "openai":
        envelope = _openai.canonicalize(raw)
    elif prov == "anthropic":
        envelope = _anthropic.canonicalize(raw)
    elif prov == "cohere":
        envelope = _cohere.canonicalize(raw)
    else:
        envelope = _raw.canonicalize(raw)
    return deterministic_dumps(envelope)


def prompt_hash(raw: Any, provider: str) -> bytes:
    return hashlib.sha256(canonicalize_prompt(raw, provider)).digest()


def parameters_hash(raw: dict[str, Any] | None) -> bytes:
    return hashlib.sha256(canonicalize_parameters(raw or {})).digest()
