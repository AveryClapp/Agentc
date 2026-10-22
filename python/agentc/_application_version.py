"""Application-declared template revisions for content-free plan evidence."""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

VERSION = "application-version-v1"
CONTEXT_KEY = "application_version"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_current: ContextVar[str | None] = ContextVar(
    "agentc_application_version", default=None
)


@contextmanager
def application_version(version: str) -> Iterator[None]:
    """Bind calls to a stable template or application revision.

    Change this identifier whenever prompt semantics change. It must describe
    a release, never a prompt, task, user or credential. Only its versioned
    digest enters native identity. Nested contexts restore the previous
    revision; asyncio tasks and context-propagating executors inherit it.
    Process workers must declare their revision themselves.
    """
    if not isinstance(version, str) or _IDENTIFIER.fullmatch(version) is None:
        raise ValueError(
            "application version must be a stable 1-128 character identifier "
            "containing only letters, digits, '.', '_', ':', or '-'"
        )
    digest = hashlib.sha256((VERSION + "\0" + version).encode("ascii")).hexdigest()
    token = _current.set(digest)
    try:
        yield
    finally:
        _current.reset(token)


def declaration() -> dict[str, str] | None:
    digest = _current.get()
    return None if digest is None else {"version": VERSION, "sha256": digest}
