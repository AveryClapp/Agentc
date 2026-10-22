"""Content-free declarations of provider request controls for exact profiles.

Only the declaration reaches Rust. Prompt bodies, credentials, caller metadata
and arbitrary extension payloads are never serialized into the declaration.
Unknown controls cause abstention rather than sharing a known request cohort.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

VERSION = "provider-request-config-v1"
CONTEXT_KEY = "provider_configuration"

# These are generation, output, execution or price/queue controls. Preserve
# presence and exact values; an explicit null need not equal a provider default.
_CONTROLS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "seed",
        "stop",
        "stop_sequences",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "min_tokens",
        "reasoning_effort",
        "reasoning",
        "thinking",
        "verbosity",
        "output_config",
        "tool_choice",
        "parallel_tool_calls",
        "function_call",
        "functions",
        "frequency_penalty",
        "presence_penalty",
        "repetition_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "n",
        "best_of",
        "service_tier",
        "betas",
        "prompt_cache_retention",
        "stream_options",
        "response_mime_type",
        "drop_params",
        "caching",
        "custom_llm_provider",
        "api_version",
    }
)
_STRUCTURAL = frozenset(
    {
        "model",
        "messages",
        "system",
        "tools",
        "response_format",
        "output_format",
        "stream",
    }
)
# Transport and annotations do not describe generation settings. Never inspect
# their values: they can contain credentials, user identifiers or live clients.
_TRANSPORT = frozenset(
    {
        "api_key",
        "api_base",
        "base_url",
        "organization",
        "project",
        "api_type",
        "client",
        "http_client",
        "timeout",
        "request_timeout",
        "max_retries",
        "num_retries",
        "metadata",
        "user",
        "safety_identifier",
        "store",
        "litellm_call_id",
        "litellm_logging_obj",
        "tags",
        "logger_fn",
        "success_callback",
        "failure_callback",
        "acompletion",
    }
)
_NESTED_CONTROLS = {
    "reasoning": {"effort", "max_tokens", "enabled", "exclude"},
    "thinking": {"type", "budget_tokens", "display"},
    "output_config": {"effort", "format"},
    "stream_options": {"include_usage", "include_obfuscation"},
}
_CONTENT_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "access_token",
        "headers",
        "messages",
        "system",
        "prompt",
        "content",
        "input",
        "arguments",
        "metadata",
        "user",
    }
)


class _Unsupported(ValueError):
    pass


def _omitted(value: Any) -> bool:
    value_type = type(value)
    return value_type.__module__.split(".", 1)[0] in {
        "openai",
        "anthropic",
    } and value_type.__name__ in {"NotGiven", "Omit"}


def _plain(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        raise _Unsupported
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item, depth + 1) for item in value]
    if isinstance(value, dict):
        if any(
            not isinstance(key, str) or key.lower() in _CONTENT_KEYS for key in value
        ):
            raise _Unsupported
        return {key: _plain(item, depth + 1) for key, item in value.items()}
    if type(value).__module__.split(".", 1)[0] in {"openai", "anthropic", "pydantic"}:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return _plain(model_dump(mode="json"), depth + 1)
    raise _Unsupported


def _control(name: str, value: Any) -> Any:
    if name == "functions":
        # Legacy function definitions require their own action/schema contract;
        # hashing arbitrary definitions here would admit an unsupported tool seam.
        raise _Unsupported
    if name in _NESTED_CONTROLS and value is not None:
        if not isinstance(value, dict):
            model_dump = getattr(value, "model_dump", None)
            if type(value).__module__.split(".", 1)[0] not in {
                "openai",
                "anthropic",
                "pydantic",
            } or not callable(model_dump):
                raise _Unsupported
            value = model_dump(mode="json")
        if not isinstance(value, dict) or set(value) - _NESTED_CONTROLS[name]:
            raise _Unsupported
        if name == "output_config":
            # Output format already has its own schema identity in the adapter.
            value = {key: item for key, item in value.items() if key != "format"}
    return _plain(value)


def declaration(kwargs: dict[str, Any], *, provider_protocol: str) -> dict[str, Any]:
    """Return only a version, a supported flag and a canonical SHA-256 digest."""
    try:
        settings = {}
        for name, value in kwargs.items():
            if name in _TRANSPORT or name in _STRUCTURAL or _omitted(value):
                continue
            if name in _CONTROLS:
                settings[name] = _control(name, value)
            elif (
                name == "allowed_openai_params"
                and provider_protocol == "litellm.completion.v1"
                and type(value) is list
                and value == ["store"]
            ):
                # Permit only the verified forwarding control used by prepared
                # batches, preserving its presence in the exact request cohort.
                settings[name] = value
            elif name == "fallbacks" and (
                value is None or isinstance(value, list) and not value
            ):
                # SWE-agent passes its explicit no-fallback default to LiteLLM.
                # Nonempty provider fallback chains need their own accounting seam.
                settings[name] = value
            elif name == "extra_body":
                if value is None:
                    settings[name] = None
                    continue
                if not isinstance(value, dict) or set(value) - _CONTROLS:
                    raise _Unsupported
                output_config = value.get("output_config")
                if output_config is not None and (
                    not isinstance(output_config, dict) or "format" in output_config
                ):
                    raise _Unsupported  # No extension-body output-schema contract.
                settings[name] = {
                    key: _control(key, item) for key, item in value.items()
                }
            elif name == "extra_headers":
                if value is None:
                    continue
                if not isinstance(value, dict):
                    raise _Unsupported
                features = {}
                for header, item in value.items():
                    key = header.lower()
                    if key in {
                        "authorization",
                        "x-api-key",
                        "api-key",
                        "openai-organization",
                        "openai-project",
                        "agentc-optimize",
                        "x-request-id",
                    }:
                        continue
                    if key not in {
                        "anthropic-beta",
                        "anthropic-version",
                        "openai-beta",
                    }:
                        raise _Unsupported
                    if key in features:
                        raise _Unsupported
                    features[key] = _plain(item)
                if features:
                    settings[name] = features
            elif name == "extra_query" and value in (None, {}):
                continue
            else:
                # Includes opaque prediction/audio/search/context payloads and
                # future controls absent from this version's explicit projection.
                raise _Unsupported
        from agentc._provider_tariff import active_scope

        tariff = active_scope()
        if tariff is not None:
            settings["agentc_tariff_scope"] = {
                "quantity": "published_token_tariff_usd",
                "snapshot_sha256": tariff.snapshot_sha256,
            }
        payload = json.dumps(
            {
                "version": VERSION,
                "provider_protocol": provider_protocol,
                "settings": settings,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > 1_000_000:
            raise _Unsupported
        return {
            "version": VERSION,
            "supported": True,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    except (TypeError, ValueError, UnicodeError, AttributeError):
        return {"version": VERSION, "supported": False, "sha256": None}
