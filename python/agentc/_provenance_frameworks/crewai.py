"""CrewAI provenance adapter.

CrewAI orchestrates work as a sequence of ``Task`` objects whose
``execute`` method produces a string (or structured payload) that later
tasks consume. The optimizer wants to reason about each task's output
as a tagged ``LlmOutput`` — that's what lets the ``ParallelBranch``
rule tell two independent tasks apart from two tasks that share a
dependency.

Strategy: wrap ``crewai.task.Task.execute`` (and the async variant if
present) so the returned payload is tagged with the current agentc span
id before it flows back to the crew.
"""

from __future__ import annotations

import logging
from typing import Any

from agentc._provenance_frameworks._common import current_span_id, tag_payload

log = logging.getLogger("agentc.provenance_frameworks.crewai")

_patched: bool = False
_originals: dict[str, Any] = {}


def _wrap_sync(original: Any) -> Any:
    import functools

    @functools.wraps(original)
    def _wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        return tag_payload(result, current_span_id())

    return _wrapped


def _wrap_async(original: Any) -> Any:
    import functools

    @functools.wraps(original)
    async def _wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, *args, **kwargs)
        return tag_payload(result, current_span_id())

    return _wrapped


def install() -> bool:
    """Patch ``crewai.task.Task`` execute methods.

    Returns ``True`` on a successful (or already-installed) patch;
    ``False`` if crewai is not importable. Never raises.
    """
    global _patched

    if _patched:
        return True

    try:
        from crewai.task import Task  # type: ignore[import-not-found]
    except BaseException:
        log.debug("crewai not installed — skipping adapter install", exc_info=True)
        return False

    try:
        # Sync path — present on all crewai versions we target.
        sync_method = getattr(Task, "execute_sync", None) or getattr(Task, "execute", None)
        if sync_method is not None and callable(sync_method):
            name = "execute_sync" if hasattr(Task, "execute_sync") else "execute"
            _originals[name] = sync_method
            setattr(Task, name, _wrap_sync(sync_method))

        # Async path — optional across versions.
        async_method = getattr(Task, "execute_async", None)
        if async_method is not None and callable(async_method):
            _originals["execute_async"] = async_method
            setattr(Task, "execute_async", _wrap_async(async_method))
    except BaseException:
        log.debug("crewai: failed to patch Task", exc_info=True)
        return False

    _patched = True
    log.debug("crewai adapter installed (%s)", ",".join(_originals.keys()))
    return True


def uninstall() -> None:
    """Restore original crewai methods. Idempotent."""
    global _patched

    if not _patched:
        return

    try:
        from crewai.task import Task  # type: ignore[import-not-found]

        for name, original in _originals.items():
            try:
                setattr(Task, name, original)
            except BaseException:
                log.debug("crewai: failed to restore %s", name, exc_info=True)
    except BaseException:
        log.debug("crewai uninstall failed", exc_info=True)

    _originals.clear()
    _patched = False
