"""Framework-specific provenance adapters.

Each sub-module installs lightweight monkey-patches on a supported agent
framework so that every inter-node / inter-task / inter-agent payload
leaves the framework with a :class:`agentc._provenance.LlmOutput` tag
attached. The optimizer's ``ParallelBranch`` and ``StateDrop`` rules
consult that tag when reasoning about DAG disjointness; without a tag
the deps are treated as :class:`Literal` and the rules conservatively
refuse to fire.

Design:

- **No hard dependencies.** Each adapter imports its framework lazily
  inside ``install()``; a missing framework is a debug log, never an
  error.
- **Idempotent.** ``install()`` checks a module-level flag before
  patching.
- **Fail-open wrappers.** The wrappers capture any exception from the
  provenance layer (tag allocation, weak-ref limits, etc.) and return
  the wrapped function's unmodified result — we never let provenance
  tagging crash a user agent.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import wrapt

from agentc._provenance_frameworks import autogen, crewai, langgraph

log = logging.getLogger("agentc.provenance_frameworks")

__all__ = ["install_all", "uninstall_all", "langgraph", "crewai", "autogen"]


_ADAPTERS: dict[str, tuple[Any, tuple[str, ...]]] = {
    "langgraph": (langgraph, ("langgraph",)),
    "crewai": (crewai, ("crewai",)),
    "autogen": (autogen, ("autogen", "pyautogen")),
}

# Adapters for frameworks imported after init() install from post-import hooks.
# wrapt cannot unregister a hook, so each is registered once per process and
# consults ``_hooks_active`` to respect shutdown.
_hooks_active = False
_hooks_registered: set[str] = set()


def _deferred_install(adapter: Any) -> Any:
    def _hook(_module: Any) -> None:
        if not _hooks_active:
            return
        try:
            adapter.install()
        except BaseException:
            log.debug("deferred framework adapter install failed (suppressed)", exc_info=True)

    return _hook


def install_all() -> dict[str, bool]:
    """Install adapters for frameworks the process has already imported.

    Importing a framework only to patch it is expensive (crewai alone pulls
    in chromadb and sqlite3), so a framework that is not yet imported gets a
    post-import hook instead and is patched when the application imports it.

    Returns a ``framework → bool`` map reporting whether each adapter is
    installed now. Missing frameworks are quietly skipped — the raw
    ``Literal`` fallback in :mod:`agentc._provenance` keeps the SDK correct
    without them.
    """
    global _hooks_active
    _hooks_active = True
    status: dict[str, bool] = {}
    for name, (adapter, modules) in _ADAPTERS.items():
        if any(m in sys.modules for m in modules):
            status[name] = adapter.install()
            continue
        status[name] = False
        for module_name in modules:
            if module_name not in _hooks_registered:
                wrapt.register_post_import_hook(_deferred_install(adapter), module_name)
                _hooks_registered.add(module_name)
    return status


def uninstall_all() -> None:
    """Best-effort symmetric un-patch. Called from agentc shutdown so a
    re-init in the same process re-attaches cleanly. Each adapter's
    ``uninstall`` is idempotent — calling it on a never-installed
    adapter is a no-op."""
    global _hooks_active
    _hooks_active = False
    for mod in (langgraph, crewai, autogen):
        try:
            mod.uninstall()
        except BaseException:
            log.debug("framework adapter uninstall failed (suppressed)", exc_info=True)
