"""Versioned SDK response caching, separate from generic function memoization."""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
from typing import Any

VERSION = "optimizer-sdk-response-cache-v1"
CONTEXT_KEY = "sdk_response_cache"
_PROTOCOLS = {
    "openai.chat.completions.v1",
    "anthropic.messages.v1",
    "litellm.completion.v1",
}
log = logging.getLogger(__name__)


def request_declaration(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Hash original message and response shapes without storing their contents."""

    def digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    try:
        messages = {key: kwargs[key] for key in ("messages", "system") if key in kwargs}
        shapes: dict[str, Any] = {
            key: kwargs[key]
            for key in ("tools", "response_format", "output_format")
            if key in kwargs
        }
        output_config = kwargs.get("output_config")
        if isinstance(output_config, dict) and "format" in output_config:
            shapes["output_config_format"] = output_config["format"]
        return {
            "version": VERSION,
            "supported": True,
            "message_sha256": digest(messages),
            "shape_sha256": digest(shapes),
        }
    except (TypeError, ValueError):
        return {"version": VERSION, "supported": False}


def seed_response(plan: Any, response: Any) -> None:
    """Seed only original SDK calls using the native lookup's exact key builder."""
    if getattr(plan, "kind", None) != "pass_through":
        return
    call_json = getattr(plan, "source_call_json", None)
    if not call_json:
        return
    try:
        from agentc import _native
        from agentc._patches._optimizer_glue import estimate_cost_usd
        from agentc._writer import CacheInsertMsg, enqueue_cache_insert

        encoded = _native.optimize_sdk_response_cache_key(call_json)
        if encoded is None:
            return
        key = json.loads(encoded)
        if key.get("version") != VERSION:
            return
        protocol = key["provider_protocol"]
        if not _response_supported(response, protocol):
            return
        usage = response.usage
        anthropic = protocol == "anthropic.messages.v1"
        input_tokens = int(
            getattr(usage, "input_tokens" if anthropic else "prompt_tokens", 0) or 0
        )
        output_tokens = int(
            getattr(usage, "output_tokens" if anthropic else "completion_tokens", 0)
            or 0
        )
        actual_model = str(getattr(response, "model", None) or key["model"])
        envelope = {
            "version": VERSION,
            "provider_protocol": protocol,
            "provider_namespace": key["provider_namespace"],
            "response": response,
        }
        enqueue_cache_insert(
            CacheInsertMsg(
                prompt_hash=bytes.fromhex(key["prompt_hash"]),
                model=key["model"],
                parameters_hash=bytes.fromhex(key["parameters_hash"]),
                call_site_id=key["call_site_id"],
                output_bytes=pickle.dumps(envelope, protocol=pickle.HIGHEST_PROTOCOL),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                recorded_cost_usd=estimate_cost_usd(
                    actual_model, input_tokens, output_tokens
                ),
                ttl_seconds=3600,
                embedding=None,
            )
        )
    except BaseException:
        log.debug("SDK response cache seed failed", exc_info=True)


def _response_supported(response: Any, protocol: str) -> bool:
    if protocol not in _PROTOCOLS or getattr(response, "usage", None) is None:
        return False
    name = "content" if protocol == "anthropic.messages.v1" else "choices"
    return isinstance(getattr(response, name, None), list)


def decode_response(value: Any, *, provider_protocol: str | None = None) -> Any:
    """Reject legacy/function payloads and mismatched SDK protocol envelopes."""
    if (
        not isinstance(value, dict)
        or value.get("sdk_response_cache_version") != VERSION
    ):
        return None
    protocol = value.get("provider_protocol")
    namespace = value.get("provider_namespace")
    content_id = value.get("output_content_id")
    if protocol not in _PROTOCOLS or (
        provider_protocol is not None and protocol != provider_protocol
    ):
        return None
    if (
        not isinstance(content_id, str)
        or not content_id
        or not isinstance(namespace, str)
    ):
        return None
    from agentc import _native

    raw = _native.output_content_load(content_id)
    if raw is None:
        return None
    envelope = pickle.loads(raw)
    if not isinstance(envelope, dict) or envelope.get("version") != VERSION:
        return None
    if (
        envelope.get("provider_protocol") != protocol
        or envelope.get("provider_namespace") != namespace
    ):
        return None
    response = envelope.get("response")
    return response if _response_supported(response, protocol) else None
