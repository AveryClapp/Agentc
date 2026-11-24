"""Optional framework adapters that assign optimization eligibility scopes."""

from __future__ import annotations

import logging
import sys
from typing import Any

import wrapt

from agentc._scope_adapters import tau2

log = logging.getLogger("agentc.scope_adapters")

#: Modules whose import means the application uses the framework.
_TRIGGERS = {"tau2": (tau2, ("tau2.agent.llm_agent", "tau2.user.user_simulator"))}

# Importing a framework only to patch it changes the application: tau2's
# import alone reconfigures LiteLLM globally. A framework not yet imported gets
# post-import hooks instead. wrapt cannot unregister a hook, so each is
# registered once per process and consults ``_hooks_active``.
_hooks_active = False
_hooks_registered: set[str] = set()


def _deferred_install(adapter: Any) -> Any:
    def _hook(_module: Any) -> None:
        if not _hooks_active:
            return
        try:
            adapter.install()
        except BaseException:
            log.debug("deferred scope adapter install failed (suppressed)", exc_info=True)

    return _hook


def install_all() -> dict[str, bool]:
    """Install adapters for frameworks the process has already imported.

    Returns whether each adapter is installed now; the others install when
    the application imports their framework.
    """
    global _hooks_active
    _hooks_active = True
    status: dict[str, bool] = {}
    for name, (adapter, modules) in _TRIGGERS.items():
        if any(module in sys.modules for module in modules):
            status[name] = adapter.install()
            continue
        status[name] = False
        for module in modules:
            if module not in _hooks_registered:
                wrapt.register_post_import_hook(_deferred_install(adapter), module)
                _hooks_registered.add(module)
    return status


def uninstall_all() -> None:
    """Best-effort symmetric removal of actor-scope adapters."""
    global _hooks_active
    _hooks_active = False
    try:
        tau2.uninstall()
    except BaseException:
        log.debug("scope adapter uninstall failed (suppressed)", exc_info=True)


__all__ = ["install_all", "uninstall_all", "tau2"]
