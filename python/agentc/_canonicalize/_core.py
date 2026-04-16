"""Shared primitives for canonicalization adapters.

Key design choices:
    * JSON with sort_keys=True and (',', ':') separators — byte-identical output
      for the same logical structure.
    * Floats are rounded to 6 decimals and serialized without scientific
      notation, so Python and Rust produce identical byte strings.
    * Tool schemas hash into a fixed-length hex string — prompts remain stable
      even when the tool's internal schema is reformatted.
    * Multi-modal content parts collapse to {"type": ..., "sha256": ...} so
      byte-identical images hash the same regardless of vendor envelope.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_VALID_ROLES = frozenset({"system", "user", "assistant", "tool"})

_PARAM_RETAIN = frozenset({
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "stop",
    "seed",
    "response_format",
    "tool_choice",
    "frequency_penalty",
    "presence_penalty",
    "logit_bias",
})

_FLOAT_ROUND_PLACES = 6


def deterministic_dumps(obj: Any) -> bytes:
    """Serialize `obj` to UTF-8 JSON bytes with stable ordering."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def round_float(value: float) -> float:
    """Round to 6 decimals; `-0.0` collapses to `0.0` for byte parity."""
    rounded = round(float(value), _FLOAT_ROUND_PLACES)
    if rounded == 0.0:
        return 0.0
    return rounded


def normalize_role(role: Any) -> str:
    """Lowercase and validate a role; unknown roles collapse to 'user'."""
    if not isinstance(role, str):
        return "user"
    lowered = role.strip().lower()
    if lowered in _VALID_ROLES:
        return lowered
    if lowered in {"human"}:
        return "user"
    if lowered in {"ai", "model"}:
        return "assistant"
    return "user"


def normalize_content(content: Any) -> str | list[dict[str, Any]]:
    """Collapse vendor content arrays.

    * A plain string returns its stripped form.
    * An array of text-only parts returns their concatenation.
    * An array with any non-text part returns a structured list where each
      element is either {"type": "text", "text": str} or
      {"type": <kind>, "sha256": str}.
    * None returns an empty string.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return str(content).strip()

    parts = [_normalize_part(p) for p in content]
    if all(p.get("type") == "text" for p in parts):
        return "".join(p["text"] for p in parts).strip()
    return parts


def _normalize_part(part: Any) -> dict[str, Any]:
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return {"type": "text", "text": str(part)}

    kind = part.get("type") or _infer_part_type(part)
    if kind == "text":
        text = part.get("text") or part.get("content") or ""
        return {"type": "text", "text": str(text)}

    if kind in {"image", "image_url"}:
        return {"type": "image", "sha256": _hash_multimodal_payload(part)}
    if kind in {"audio", "input_audio"}:
        return {"type": "audio", "sha256": _hash_multimodal_payload(part)}
    if kind in {"document", "file"}:
        return {"type": "document", "sha256": _hash_multimodal_payload(part)}
    if kind == "tool_use":
        return {
            "type": "tool_use",
            "name": str(part.get("name", "")),
            "input_sha256": sha256_hex(deterministic_dumps(part.get("input", {}))),
        }
    if kind == "tool_result":
        inner = part.get("content")
        if isinstance(inner, list):
            inner_norm = normalize_content(inner)
        else:
            inner_norm = str(inner or "").strip()
        return {
            "type": "tool_result",
            "tool_use_id": str(part.get("tool_use_id", "")),
            "content": inner_norm,
        }
    return {"type": str(kind), "sha256": _hash_multimodal_payload(part)}


def _infer_part_type(part: dict[str, Any]) -> str:
    if "text" in part:
        return "text"
    if "image_url" in part or "image" in part or "source" in part:
        return "image"
    if "audio" in part or "input_audio" in part:
        return "audio"
    return "unknown"


def _hash_multimodal_payload(part: dict[str, Any]) -> str:
    """Reduce an image/audio/document to a SHA-256 of its payload.

    We prefer explicit byte content when present; otherwise we hash the
    vendor's URL or data-URI verbatim. The goal is stability, not semantic
    equivalence across re-encoded versions.
    """
    for key in ("data", "bytes", "b64", "base64"):
        v = part.get(key)
        if isinstance(v, (bytes, bytearray)):
            return sha256_hex(bytes(v))
        if isinstance(v, str) and v:
            return sha256_hex(v)
    source = part.get("source")
    if isinstance(source, dict):
        for key in ("data", "bytes", "b64", "base64", "url"):
            v = source.get(key)
            if isinstance(v, (bytes, bytearray)):
                return sha256_hex(bytes(v))
            if isinstance(v, str) and v:
                return sha256_hex(v)
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        url = image_url.get("url")
        if isinstance(url, str):
            return sha256_hex(url)
    if isinstance(image_url, str):
        return sha256_hex(image_url)
    return sha256_hex(deterministic_dumps(part))


def normalize_tools(tools: Any) -> list[dict[str, str]]:
    """Return a list of {"name": str, "schema_hash": hex} sorted by name."""
    if not tools:
        return []
    out: list[dict[str, str]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name, schema = _extract_tool_name_and_schema(tool)
        if not name:
            continue
        out.append({
            "name": name,
            "schema_hash": sha256_hex(deterministic_dumps(schema)),
        })
    out.sort(key=lambda t: t["name"])
    return out


def _extract_tool_name_and_schema(tool: dict[str, Any]) -> tuple[str, Any]:
    """Support OpenAI ({"type": "function", "function": {...}}) and Anthropic
    ({"name": ..., "input_schema": ...}) / Cohere ({"name": ..., "parameter_definitions": ...})
    shapes, plus bare {"name": ..., "schema": ...}."""
    if "function" in tool and isinstance(tool["function"], dict):
        fn = tool["function"]
        return str(fn.get("name", "")), fn.get("parameters") or fn.get("schema") or {}
    name = str(tool.get("name", ""))
    schema = (
        tool.get("input_schema")
        or tool.get("parameters")
        or tool.get("parameter_definitions")
        or tool.get("schema")
        or {}
    )
    return name, schema


def response_schema_hash(response_format: Any) -> str | None:
    """Hash a response_format/output schema dict; return None if absent."""
    if not response_format:
        return None
    if isinstance(response_format, dict):
        schema = (
            response_format.get("json_schema")
            or response_format.get("schema")
            or response_format
        )
        return sha256_hex(deterministic_dumps(schema))
    return sha256_hex(str(response_format))


def build_envelope(
    provider: str,
    messages: list[dict[str, Any]],
    tools: Any = None,
    response_format: Any = None,
) -> dict[str, Any]:
    """Assemble the canonical envelope for a prompt."""
    return {
        "provider": provider,
        "messages": messages,
        "tools": normalize_tools(tools),
        "response_schema_hash": response_schema_hash(response_format),
    }


def canonicalize_parameters(raw: dict[str, Any] | None) -> bytes:
    """Filter, normalize, and serialize sampling parameters.

    Retained keys: temperature, top_p, top_k, max_tokens, stop, seed,
    response_format, tool_choice, frequency_penalty, presence_penalty,
    logit_bias. Floats round to 6 decimals. Lists of strings sort for
    order-insensitive fields like `stop`. `response_format` is hashed
    the same way as in the prompt envelope so two calls differing only
    in schema formatting still hit the same cache row.
    """
    if not raw:
        return deterministic_dumps({})

    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _PARAM_RETAIN:
            continue
        out[key] = _normalize_param_value(key, value)
    return deterministic_dumps(out)


def _normalize_param_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in {"temperature", "top_p", "frequency_penalty", "presence_penalty"}:
        if isinstance(value, (int, float)):
            return round_float(value)
        return value
    if key == "stop":
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            strs = [str(s) for s in value]
            return sorted(strs)
        return value
    if key == "response_format":
        return {"schema_hash": response_schema_hash(value)}
    if key == "tool_choice":
        return _normalize_tool_choice(value)
    if key == "logit_bias":
        if isinstance(value, dict):
            return {str(k): _normalize_numeric(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        return value
    return value


def _normalize_tool_choice(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "function" in value and isinstance(value["function"], dict):
            name = value["function"].get("name")
            return {"type": "function", "name": str(name) if name else ""}
        if "name" in value:
            return {"type": "tool", "name": str(value["name"])}
    return value


def _normalize_numeric(value: Any) -> Any:
    if isinstance(value, float):
        return round_float(value)
    return value
