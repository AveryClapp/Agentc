"""Lightweight object-provenance tagging for the optimizer.

The optimizer's ``ParallelBranch`` and ``StateDrop`` rules need to know
where each message in a ``Call`` came from: literal user code, a tool
call's output, an earlier LLM response, the trace's root input, or a
named field of agent state. We express that as :class:`DepSource`.

This module supplies the minimum machinery for a framework-free Python
agent (or, via the framework adapters in O7, a langgraph / crewai /
autogen agent) to attach a ``DepSource`` tag to a Python object and for
the SDK interceptor to retrieve the tag when it assembles the ``Call``
payload. Untagged objects default to :class:`Literal`; an agent that
never uses the tagger still produces correct (if under-informed)
``Call`` payloads — just with the ``ParallelBranch`` / ``StateDrop``
rules effectively no-oping.

Internals: object id → tag map, held in a :class:`WeakValueDictionary`
so that tagged objects are not kept alive purely by virtue of being
tagged. For primitives (``str``, ``int``) that can't be weakly
referenced, we fall back to a bounded dict keyed on ``id(obj)``; this
is safe because the SDK interceptor consumes the tag at call
assembly time, after which the tag is no longer load-bearing.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Literal as TypingLiteral, Optional, Union
from weakref import WeakValueDictionary

__all__ = [
    "DepSource",
    "Literal",
    "UserInput",
    "ToolOutput",
    "LlmOutput",
    "State",
    "tag",
    "tag_of",
    "clear",
    "as_json",
    "PROVENANCE_UNSET",
]


@dataclass(frozen=True)
class Literal:
    """Hardcoded in user code — template strings, system prompts, etc."""

    kind: TypingLiteral["literal"] = "literal"


@dataclass(frozen=True)
class UserInput:
    """Value originated from the trace's root input (user-facing prompt)."""

    span_id: str
    kind: TypingLiteral["user_input"] = "user_input"


@dataclass(frozen=True)
class ToolOutput:
    """Value came from a prior tool call's output."""

    span_id: str
    kind: TypingLiteral["tool_output"] = "tool_output"


@dataclass(frozen=True)
class LlmOutput:
    """Value came from a prior LLM call's output."""

    span_id: str
    kind: TypingLiteral["llm_output"] = "llm_output"


@dataclass(frozen=True)
class State:
    """Value came from agent state under the given named key."""

    key: str
    kind: TypingLiteral["state"] = "state"


DepSource = Union[Literal, UserInput, ToolOutput, LlmOutput, State]

# Sentinel returned by ``tag_of`` when no provenance has been recorded.
# Callers should treat this as equivalent to ``Literal()`` for serialization
# — but keep it distinct so instrumentation can distinguish "never tagged"
# from "explicitly tagged as literal".
PROVENANCE_UNSET: Literal = Literal()

# Bounded id-keyed fallback: caps memory when tagging primitives.
_MAX_PRIMITIVE_TAGS = 4096

_lock = threading.Lock()
_weak_tags: "WeakValueDictionary[int, _TagHolder]" = WeakValueDictionary()
_primitive_tags: dict[int, DepSource] = {}
_primitive_order: list[int] = []


class _TagHolder:
    """Weakly-referenceable container for a ``DepSource``. Used because
    ``WeakValueDictionary`` requires values that themselves support weak
    references — dataclass instances of built-in shape do not."""

    __slots__ = ("source",)

    def __init__(self, source: DepSource) -> None:
        self.source: DepSource = source


def tag(obj: Any, source: DepSource) -> Any:
    """Attach a provenance tag to ``obj`` and return ``obj`` unchanged.

    Idempotent by design: re-tagging overwrites. Returns the object so
    callers can write ``messages.append(tag(content, UserInput(...)))``
    without breaking their expression chain.
    """
    key = id(obj)
    holder = _TagHolder(source)
    with _lock:
        try:
            _weak_tags[key] = holder
        except TypeError:
            # str/int/bytes/frozenset don't support weak references — fall
            # back to the bounded id-keyed dict. We intentionally keep this
            # path small and opinionated: the primitive fallback is a FIFO
            # not an LRU because "touch" on every tag lookup would defeat
            # the bound.
            _primitive_tags[key] = source
            _primitive_order.append(key)
            while len(_primitive_order) > _MAX_PRIMITIVE_TAGS:
                evict = _primitive_order.pop(0)
                _primitive_tags.pop(evict, None)
    return obj


def tag_of(obj: Any) -> DepSource:
    """Return the provenance tag for ``obj``, or ``PROVENANCE_UNSET``.

    Never raises — unknown or untracked objects resolve to
    ``PROVENANCE_UNSET``, which JSON-serializes as a ``Literal``. This
    is the raw-SDK fallback: if no framework adapter tagged anything,
    every retrieved tag comes back as literal.
    """
    key = id(obj)
    with _lock:
        holder = _weak_tags.get(key)
        if holder is not None:
            return holder.source
        tagged = _primitive_tags.get(key)
        if tagged is not None:
            return tagged
    return PROVENANCE_UNSET


def clear() -> None:
    """Reset the tag map. Tests call this between cases; production
    callers do not — tagged objects expire naturally via weak refs."""
    with _lock:
        _weak_tags.clear()
        _primitive_tags.clear()
        _primitive_order.clear()


def as_json(source: Optional[DepSource]) -> dict[str, Any]:
    """Serialize to the shape the Rust ``DepSource`` enum expects
    (see ``crates/agentc-optimizer/src/dag.rs`` — tagged JSON, lowercase
    snake_case kinds). ``None`` → ``{"kind": "literal"}``."""
    if source is None:
        return {"kind": "literal"}
    if isinstance(source, Literal):
        return {"kind": "literal"}
    if isinstance(source, UserInput):
        return {"kind": "user_input", "span_id": source.span_id}
    if isinstance(source, ToolOutput):
        return {"kind": "tool_output", "span_id": source.span_id}
    if isinstance(source, LlmOutput):
        return {"kind": "llm_output", "span_id": source.span_id}
    if isinstance(source, State):
        return {"kind": "state", "key": source.key}
    # Unknown subtypes — treat as literal so the rule engine no-ops
    # rather than crashing.
    return {"kind": "literal"}
