"""Pinned published token tariffs, independent of actual provider bills."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from agentc._billing import _record

VERSION = "provider-token-tariff-v1"
_current: ContextVar[TariffAccounting | None] = ContextVar(
    "agentc_tariff_scope", default=None
)


@dataclass
class TariffAccounting:
    """Reports original invocations in this scope; reports authorize no release."""

    snapshot_sha256: str
    _batches: set[str] = field(default_factory=set, repr=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def note_batch(self, batch_id: str) -> None:
        with self._lock:
            self._batches.add(batch_id)

    def reports(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            batches = sorted(self._batches)
        return tuple(provider_tariff_report(batch) for batch in batches)


@contextmanager
def tariff_accounting() -> Iterator[TariffAccounting]:
    """Predeclare the runtime's dated public tariff for original SDK calls.

    Initialize Agentc first. This scope does not resolve billed USD, authorize
    an optimized plan from missing quality evidence.
    """
    result = _record("tariff_policy")
    if result.get("status") != "recorded":
        raise RuntimeError("tariff snapshot is unavailable")
    scope = TariffAccounting(result["snapshot_sha256"])
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)


def active_scope() -> TariffAccounting | None:
    return _current.get()


def provider_tariff_report(batch_id: str) -> dict[str, Any]:
    result = _record("tariff_report", batch_id=batch_id)
    if result.get("status") != "recorded":
        raise RuntimeError("original tariff evidence is unavailable")
    report: dict[str, Any] = result["tariff"]
    return report


def provider_tariff_plan_report(batch_id: str) -> dict[str, Any]:
    """Read original whole-plan tariff observations and their retained window.

    This projects retained SDK evidence without dispatching inference. It does
    not settle USD or supply the paired quality required for plan admission.
    """
    result = _record("tariff_plan_profile", batch_id=batch_id)
    if result.get("status") != "recorded":
        raise RuntimeError("original tariff plan evidence is unavailable")
    return result


def provider_original_execution(batch_id: str) -> dict[str, Any] | None:
    """Read one original execution identity shared by its economic views."""
    result = _record("original_provider_execution", batch_id=batch_id)
    if result.get("status") != "recorded":
        raise RuntimeError("original provider execution is unavailable")
    execution: dict[str, Any] | None = result["execution"]
    return execution


def provider_original_quality_report(batch_id: str) -> dict[str, Any]:
    """Read retained original pairs, lifetime evidence, exposure and cooldown."""
    result = _record("original_provider_quality", batch_id=batch_id)
    if result.get("status") != "recorded":
        raise RuntimeError("original provider quality is unavailable")
    return result


def compare_original_provider_executions(
    reference_execution_id: str, candidate_execution_id: str
) -> dict[str, Any]:
    """Pair a serial reference with its parallel schedule's original results.

    Both runs must retain the same ordered inputs and runtime. This operation
    preserves the first pair through billing and restart; it dispatches no calls.
    """
    result = _record(
        "compare_original_provider_executions",
        reference_execution_id=reference_execution_id,
        candidate_execution_id=candidate_execution_id,
    )
    if result.get("status") != "recorded":
        raise RuntimeError("original provider comparison is unavailable")
    pair: dict[str, Any] = result["pair"]
    return pair


def _cache_control(value: Any) -> bool:
    return value is None or (
        isinstance(value, dict)
        and not set(value) - {"type", "ttl"}
        and value.get("type") == "ephemeral"
        and value.get("ttl", "5m") in {"5m", "1h"}
    )


def _blocks(value: Any, protocol: str) -> bool:
    if value is None or isinstance(value, str):
        return True
    if not isinstance(value, list):
        return False
    for block in value:
        if not isinstance(block, dict) or not _cache_control(
            block.get("cache_control")
        ):
            return False
        kind = block.get("type")
        if kind == "text":
            if set(block) - {"type", "text", "cache_control"} or not isinstance(
                block.get("text"), str
            ):
                return False
        elif protocol == "anthropic.messages.v1" and kind == "tool_use":
            if set(block) - {"type", "id", "name", "input", "cache_control"}:
                return False
        elif protocol == "anthropic.messages.v1" and kind == "tool_result":
            if set(block) - {
                "type",
                "tool_use_id",
                "is_error",
                "content",
                "cache_control",
            } or not _blocks(block.get("content"), "openai.chat.completions.v1"):
                return False
        else:
            return False
    return True


def request_scope_supported(kwargs: dict[str, Any], protocol: str) -> bool:
    """Keep only a boolean; neither tool schemas nor prompt content is retained."""
    from agentc._provider_config import _omitted, declaration
    from agentc._response_cache import request_declaration

    try:
        values = {key: value for key, value in kwargs.items() if not _omitted(value)}
        headers = values.get("extra_headers")
        if headers is not None:
            if not isinstance(headers, dict) or any(
                key.lower()
                not in {"x-stainless-raw-response", "x-stainless-override-cast-to"}
                for key in headers
            ):
                return False
            values.pop("extra_headers")
        allowed = {
            "model",
            "messages",
            "system",
            "tools",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "top_k",
            "stop",
            "stop_sequences",
            "seed",
            "frequency_penalty",
            "presence_penalty",
            "tool_choice",
            "parallel_tool_calls",
            "response_format",
            "service_tier",
            "stream",
            "n",
            "timeout",
            "metadata",
            "user",
            "store",
            "safety_identifier",
            "extra_body",
            "extra_query",
        }
        if set(values) - allowed or values.get("stream") or values.get("n", 1) != 1:
            return False
        if values.get("extra_body") not in (None, {}) or values.get(
            "extra_query"
        ) not in (None, {}):
            return False
        if protocol not in {"openai.chat.completions.v1", "anthropic.messages.v1"}:
            return False
        if (
            not declaration(values, provider_protocol=protocol)["supported"]
            or not request_declaration(values)["supported"]
        ):
            return False
        if not _blocks(values.get("system"), protocol):
            return False
        messages = values.get("messages")
        if not isinstance(messages, list) or not messages:
            return False
        for message in messages:
            if not isinstance(message, dict) or set(message) - {
                "role",
                "content",
                "name",
                "tool_call_id",
                "tool_calls",
            }:
                return False
            if not _blocks(message.get("content"), protocol):
                return False
            calls = message.get("tool_calls")
            if calls is not None and (
                not isinstance(calls, list)
                or any(
                    not isinstance(call, dict)
                    or call.get("type") != "function"
                    or set(call) - {"id", "type", "function"}
                    or not isinstance(call.get("function"), dict)
                    or set(call["function"]) - {"name", "arguments"}
                    for call in calls
                )
            ):
                return False
        tools = values.get("tools") or []
        if not isinstance(tools, list):
            return False
        for tool in tools:
            if not isinstance(tool, dict):
                return False
            if protocol == "openai.chat.completions.v1":
                if (
                    tool.get("type") != "function"
                    or set(tool) - {"type", "function"}
                    or not isinstance(tool.get("function"), dict)
                ):
                    return False
            elif set(tool) - {
                "name",
                "description",
                "input_schema",
                "cache_control",
                "strict",
            } or not _cache_control(tool.get("cache_control")):
                return False
        return True
    except Exception:
        return False
