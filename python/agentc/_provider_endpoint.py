"""Content-free SDK endpoint identities, independent of model spelling."""

from __future__ import annotations

import hashlib
from typing import Any

VERSION = "provider-endpoint-v1"
CONTEXT_KEY = "provider_endpoint"


def validate_namespace(value: Any, protocol: str) -> str | None:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "sha256",
        "namespace",
        "supported",
    }:
        return None
    if value.get("version") != VERSION or value.get("supported") is not True:
        return None
    namespace = value.get("namespace")
    if not isinstance(namespace, str):
        return None
    endpoint = {
        "openai": "https://api.openai.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "anthropic": "https://api.anthropic.com",
    }.get(namespace)
    if endpoint is None:
        return None
    if (
        value["sha256"]
        != hashlib.sha256(("billing-scope-v1\0" + endpoint).encode()).hexdigest()
    ):
        return None
    if (namespace == "anthropic") != (protocol == "anthropic.messages.v1"):
        return None
    if protocol not in {
        "openai.chat.completions.v1",
        "litellm.completion.v1",
        "anthropic.messages.v1",
    }:
        return None
    return namespace


def declaration(client: Any, protocol: str) -> dict[str, Any]:
    endpoint = str(client.base_url).rstrip("/")
    digest = hashlib.sha256(("billing-scope-v1\0" + endpoint).encode()).hexdigest()
    namespace = {
        "https://api.openai.com/v1": "openai",
        "https://openrouter.ai/api/v1": "openrouter",
        "https://api.anthropic.com": "anthropic",
    }.get(endpoint)
    supported = namespace is not None and (
        (namespace == "anthropic" and protocol == "anthropic.messages.v1")
        or (
            namespace in {"openai", "openrouter"}
            and protocol
            in {
                "openai.chat.completions.v1",
                "litellm.completion.v1",
            }
        )
    )
    # Native-only callers can declare an endpoint without an SDK instance.
    # Actual SDK clients also bind the adapter's standard transport contract.
    if getattr(client, "_client", None) is not None:
        from agentc._provider_receipts import _client_transport_verified

        transport_protocol = (
            "openai.chat.completions.v1"
            if protocol == "litellm.completion.v1"
            else protocol
        )
        supported = supported and _client_transport_verified(client, transport_protocol)
    return {
        "version": VERSION,
        "sha256": digest,
        "namespace": namespace if supported else "unqualified:" + digest,
        "supported": supported,
    }
