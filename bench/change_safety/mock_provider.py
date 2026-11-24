"""Deterministic LiteLLM-compatible provider for Stage E0 (no network, $0).

The mock exercises the whole pipeline: tool calls with schema-valid
arguments, text replies, a user simulator that eventually stops, usage and
cost fields, and provider context-edit echoes. Its behavior is a pure
function of the request content, the model name, and a per-dispatch noise
draw, so:

- a change that alters the request content or model alters behavior;
- resending the identical request (C0) disagrees at the configured noise
  rate, which gives the A/A floor something nonzero to measure.

None of its outputs are evidence about any real model.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

#: Fake prices per million tokens (input, output). Only ratios matter for E0.
MOCK_PRICES_PER_MTOK = (1.0, 4.0)


def _unit(*parts: object) -> float:
    digest = hashlib.sha256("\x00".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")) for part in content if isinstance(part, Mapping)
        )
    return ""


def _fill(schema: Mapping[str, Any], seed: str) -> Any:
    kind = schema.get("type")
    if "enum" in schema and schema["enum"]:
        options = list(schema["enum"])
        return options[int(_unit(seed) * len(options))]
    if kind == "integer":
        return 1 + int(_unit(seed) * 100)
    if kind == "number":
        return round(1 + _unit(seed) * 1000, 2)
    if kind == "boolean":
        return _unit(seed) < 0.5
    if kind == "array":
        return []
    if kind == "object":
        props = schema.get("properties", {})
        required = schema.get("required", list(props))
        return {k: _fill(props[k], f"{seed}/{k}") for k in required if k in props}
    return f"value-{hashlib.sha256(seed.encode()).hexdigest()[:6]}"


@dataclass
class MockProvider:
    """Callable with LiteLLM's ``completion(**kwargs)`` signature."""

    actor: Callable[[], str]
    noise: float = 0.1
    tool_rate: float = 0.6
    user_turns: tuple[int, int] = (3, 6)
    seed: str = "agentc-mock-v1"
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _counter: Any = field(default_factory=itertools.count, repr=False)

    def __call__(self, **kwargs: Any) -> Any:
        from litellm import ModelResponse

        with self._lock:
            draw = next(self._counter)
            self.calls += 1
        model = str(kwargs.get("model", ""))
        messages = list(kwargs.get("messages") or [])
        tools = list(kwargs.get("tools") or [])
        content_key = hashlib.sha256(
            json.dumps([model, messages, tools], sort_keys=True, default=str).encode()
        ).hexdigest()
        # With probability `noise`, this dispatch behaves as if sampled differently.
        variant = "alt" if _unit(self.seed, "noise", draw) < self.noise else "base"
        key = f"{content_key}/{variant}"
        if self.actor() == "user":
            message = self._user_message(messages, key)
        else:
            message = self._agent_message(messages, tools, key)
        text = message.get("content") or ""
        tool_text = json.dumps(message.get("tool_calls") or [])
        prompt_tokens = max(1, len(json.dumps(messages)) // 4)
        completion_tokens = max(1, (len(text) + len(tool_text)) // 4)
        response = ModelResponse(
            id=f"mock-{uuid.uuid4().hex[:12]}",
            model=model,
            choices=[
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    "message": message,
                }
            ],
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )
        price_in, price_out = MOCK_PRICES_PER_MTOK
        response._hidden_params["response_cost"] = (
            prompt_tokens * price_in + completion_tokens * price_out
        ) / 1e6
        edits = kwargs.get("context_management") or (kwargs.get("extra_body") or {}).get(
            "context_management"
        )
        if edits:
            response._hidden_params["mock_context_management"] = edits
        return response

    def _user_message(self, messages: list[Any], key: str) -> dict[str, Any]:
        turns = sum(1 for m in messages if m.get("role") == "assistant")
        low, high = self.user_turns
        limit = low + int(_unit(self.seed, "turns", _content_text(messages[0].get("content")) if messages else "") * (high - low + 1))
        if turns >= limit:
            return {"role": "assistant", "content": "Thanks, that is all. ###STOP###"}
        return {
            "role": "assistant",
            "content": f"I still need help with my account (request {key[:8]}).",
        }

    def _agent_message(self, messages: list[Any], tools: list[Any], key: str) -> dict[str, Any]:
        last = _content_text(messages[-1].get("content")) if messages else ""
        if tools and messages and messages[-1].get("role") != "tool" and _unit(key, "tool") < self.tool_rate:
            tool = tools[int(_unit(key, "which") * len(tools))]["function"]
            arguments = _fill(tool.get("parameters") or {"type": "object"}, key)
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{key[:10]}",
                        "type": "function",
                        "function": {"name": tool["name"], "arguments": json.dumps(arguments)},
                    }
                ],
            }
        words = " ".join(last.split()[:12])
        return {"role": "assistant", "content": f"Understood ({key[:6]}). {words}".strip()}


__all__ = ["MOCK_PRICES_PER_MTOK", "MockProvider"]
