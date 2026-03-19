"""Context tracking for span parent-child relationships.

Uses contextvars for thread-safe and asyncio-safe context propagation.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass
class SpanContext:
    """Lightweight context carried through ContextVar for span linkage."""

    span_id: str
    trace_id: str
    name: str


_current_span: ContextVar[Optional[SpanContext]] = ContextVar("_current_span", default=None)


def get_current_span() -> SpanContext | None:
    """Get the current span context, or None if no active span."""
    return _current_span.get()


def set_current_span(ctx: SpanContext | None) -> None:
    """Set the current span context."""
    _current_span.set(ctx)
