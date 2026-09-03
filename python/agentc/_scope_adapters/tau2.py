"""Actor isolation for tau2's evaluated assistant and user simulator.

The frozen tau2 text workload imports the shared ``generate`` helper into two
distinct modules.  Wrapping those aliases gives Agentc a stable actor seam
without editing tau2, inspecting prompts, or relying on the chosen model IDs.
Both actors still cross the provider interception seam and remain observable;
only the evaluated assistant is eligible for rewrites.
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any

import wrapt

from agentc._optimization_scope import optimization_scope

log = logging.getLogger("agentc.scope_adapters.tau2")

_TARGETS = (
    ("tau2.agent.llm_agent", "generate", "tau2.evaluated_assistant", True),
    ("tau2.user.user_simulator", "generate", "tau2.user_simulator", False),
)

_installed = False
_patches: list[tuple[ModuleType, str, Any, Any]] = []


def _wrapper(scope_name: str, optimize: bool) -> Any:
    def _scoped(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        with optimization_scope(scope_name, optimize=optimize):
            return wrapped(*args, **kwargs)

    return _scoped


def install() -> bool:
    """Install the tau2 text-mode actor scopes atomically when available."""
    global _installed
    if _installed:
        return True

    pending: list[tuple[ModuleType, str, Any, Any]] = []
    try:
        for module_name, attribute, scope_name, optimize in _TARGETS:
            module = importlib.import_module(module_name)
            original = getattr(module, attribute)
            wrapped = wrapt.FunctionWrapper(original, _wrapper(scope_name, optimize))
            pending.append((module, attribute, original, wrapped))
    except (ImportError, AttributeError):
        log.debug("tau2 text actor seam unavailable; skipping adapter", exc_info=True)
        return False

    try:
        for module, attribute, original, wrapped in pending:
            setattr(module, attribute, wrapped)
            _patches.append((module, attribute, original, wrapped))
    except BaseException:
        uninstall()
        log.debug("tau2 actor-scope install failed; restored originals", exc_info=True)
        return False

    _installed = True
    return True


def uninstall() -> None:
    """Restore the exact tau2 function aliases replaced by :func:`install`."""
    global _installed
    while _patches:
        module, attribute, original, wrapped = _patches.pop()
        if getattr(module, attribute, None) is wrapped:
            setattr(module, attribute, original)
    _installed = False


__all__ = ["install", "uninstall"]
