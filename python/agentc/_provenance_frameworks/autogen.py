"""AutoGen provenance adapter.

AutoGen multi-agent conversations flow through
``ConversableAgent.generate_reply`` — each agent's turn produces a
message (string or structured dict) that other agents consume as input.
The ``ParallelBranch`` rule needs these messages tagged with their
originating span so it can distinguish independent agent outputs from
a shared upstream.

Strategy: wrap ``generate_reply`` (sync + async) so the returned
message is tagged with the current agentc span id.
"""

from __future__ import annotations

import logging
from typing import Any

from agentc._provenance_frameworks._common import current_span_id, tag_payload

log = logging.getLogger("agentc.provenance_frameworks.autogen")

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


def _resolve_conversable_agent() -> Any | None:
    """Import ``ConversableAgent`` from whichever autogen distribution
    is installed. ``pyautogen`` and ``autogen`` both expose it under
    the top-level package."""
    for mod_name in ("autogen", "pyautogen"):
        try:
            mod = __import__(mod_name)
        except BaseException:
            continue
        agent = getattr(mod, "ConversableAgent", None)
        if agent is not None:
            return agent
    return None


def install() -> bool:
    """Patch ``ConversableAgent.generate_reply`` / ``a_generate_reply``.

    Returns ``True`` if autogen was importable and patches applied;
    ``False`` otherwise. Never raises.
    """
    global _patched

    if _patched:
        return True

    agent_cls = _resolve_conversable_agent()
    if agent_cls is None:
        log.debug("autogen not installed — skipping adapter install")
        return False

    try:
        sync_method = getattr(agent_cls, "generate_reply", None)
        if sync_method is not None and callable(sync_method):
            _originals["generate_reply"] = sync_method
            setattr(agent_cls, "generate_reply", _wrap_sync(sync_method))

        async_method = getattr(agent_cls, "a_generate_reply", None)
        if async_method is not None and callable(async_method):
            _originals["a_generate_reply"] = async_method
            setattr(agent_cls, "a_generate_reply", _wrap_async(async_method))
    except BaseException:
        log.debug("autogen: failed to patch ConversableAgent", exc_info=True)
        return False

    _patched = True
    log.debug("autogen adapter installed (%s)", ",".join(_originals.keys()))
    return True


def uninstall() -> None:
    """Restore original autogen methods. Idempotent."""
    global _patched

    if not _patched:
        return

    agent_cls = _resolve_conversable_agent()
    if agent_cls is not None:
        for name, original in _originals.items():
            try:
                setattr(agent_cls, name, original)
            except BaseException:
                log.debug("autogen: failed to restore %s", name, exc_info=True)

    _originals.clear()
    _patched = False
