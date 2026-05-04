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

from agentc._provenance_frameworks import autogen, crewai, langgraph

log = logging.getLogger("agentc.provenance_frameworks")

__all__ = ["install_all", "uninstall_all", "langgraph", "crewai", "autogen"]


def install_all() -> dict[str, bool]:
    """Attempt to install every framework adapter.

    Returns a ``framework → bool`` map reporting whether each adapter's
    install attempt saw the framework present on import. Missing
    frameworks are quietly skipped — the raw ``Literal`` fallback in
    :mod:`agentc._provenance` keeps the SDK correct without them.
    """
    return {
        "langgraph": langgraph.install(),
        "crewai": crewai.install(),
        "autogen": autogen.install(),
    }


def uninstall_all() -> None:
    """Best-effort symmetric un-patch. Called from agentc shutdown so a
    re-init in the same process re-attaches cleanly. Each adapter's
    ``uninstall`` is idempotent — calling it on a never-installed
    adapter is a no-op."""
    for mod in (langgraph, crewai, autogen):
        try:
            mod.uninstall()
        except BaseException:
            log.debug("framework adapter uninstall failed (suppressed)", exc_info=True)
