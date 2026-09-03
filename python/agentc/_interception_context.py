"""Context-local ownership for nested LLM interception seams."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_owner: ContextVar[str | None] = ContextVar("_agentc_interception_owner", default=None)


def interception_is_nested() -> bool:
    """Return whether an outer adapter already owns this logical LLM call."""
    return _owner.get() is not None


@contextmanager
def interception_owner(name: str) -> Iterator[None]:
    """Suppress inner adapters while ``name`` dispatches one logical call."""
    token = _owner.set(name)
    try:
        yield
    finally:
        _owner.reset(token)
