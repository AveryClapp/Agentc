"""LangGraph provenance adapter.

LangGraph agents are built as ``StateGraph`` instances: each node is a
callable that receives the current state dict and returns a *fragment*
dict merged into state by the runtime. The optimizer's ``ParallelBranch``
rule reasons about disjointness between node outputs — for that it needs
each emitted value to carry a ``LlmOutput(span_id=<node's span>)`` tag.

Strategy: wrap ``StateGraph.add_node`` so every callable registered with
a graph is transparently replaced by a tagging wrapper. The wrapper
invokes the original node, then tags each value of the returned dict
with the currently-open agentc span. If no span is open (the SDK hasn't
instrumented this call stack) the wrapper is a pass-through — tagging
silently no-ops and the rule engine falls back to ``Literal``.
"""

from __future__ import annotations

import logging
from typing import Any

from agentc._provenance_frameworks._common import wrap_with_tagging

log = logging.getLogger("agentc.provenance_frameworks.langgraph")

_patched: bool = False
_original_add_node: Any = None


def install() -> bool:
    """Patch ``langgraph.graph.StateGraph.add_node``.

    Returns ``True`` if langgraph was importable and the patch was
    applied (or already in place); ``False`` if langgraph is not
    installed. Never raises.
    """
    global _patched, _original_add_node

    if _patched:
        return True

    try:
        from langgraph.graph import StateGraph  # type: ignore[import-not-found]
    except BaseException:
        log.debug("langgraph not installed — skipping adapter install", exc_info=True)
        return False

    original = StateGraph.add_node
    _original_add_node = original

    def _patched_add_node(self: Any, *args: Any, **kwargs: Any) -> Any:
        # langgraph's add_node has two supported call shapes:
        #   add_node(name, action)  — positional
        #   add_node(action)        — name inferred from callable
        # and accepts action via the ``action=`` kwarg in newer versions.
        # We locate the callable argument, wrap it, and forward.
        new_args = list(args)
        new_kwargs = dict(kwargs)
        try:
            if "action" in new_kwargs and callable(new_kwargs["action"]):
                new_kwargs["action"] = wrap_with_tagging(new_kwargs["action"])
            elif len(new_args) >= 2 and callable(new_args[1]):
                new_args[1] = wrap_with_tagging(new_args[1])
            elif len(new_args) == 1 and callable(new_args[0]):
                new_args[0] = wrap_with_tagging(new_args[0])
        except BaseException:
            log.debug("langgraph add_node: failed to wrap action", exc_info=True)
        return original(self, *new_args, **new_kwargs)

    try:
        StateGraph.add_node = _patched_add_node  # type: ignore[method-assign]
    except BaseException:
        log.debug("langgraph: failed to patch StateGraph.add_node", exc_info=True)
        return False

    _patched = True
    log.debug("langgraph adapter installed")
    return True


def uninstall() -> None:
    """Restore the original ``StateGraph.add_node``. Idempotent."""
    global _patched, _original_add_node

    if not _patched:
        return

    try:
        from langgraph.graph import StateGraph  # type: ignore[import-not-found]

        if _original_add_node is not None:
            StateGraph.add_node = _original_add_node  # type: ignore[method-assign]
    except BaseException:
        log.debug("langgraph uninstall failed", exc_info=True)

    _patched = False
    _original_add_node = None
