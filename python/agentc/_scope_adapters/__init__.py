"""Optional framework adapters that assign optimization eligibility scopes."""

from __future__ import annotations

import logging

from agentc._scope_adapters import tau2

log = logging.getLogger("agentc.scope_adapters")


def install_all() -> dict[str, bool]:
    """Install every available actor-scope adapter."""
    return {"tau2": tau2.install()}


def uninstall_all() -> None:
    """Best-effort symmetric removal of actor-scope adapters."""
    try:
        tau2.uninstall()
    except BaseException:
        log.debug("scope adapter uninstall failed (suppressed)", exc_info=True)


__all__ = ["install_all", "uninstall_all", "tau2"]
