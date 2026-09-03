"""Context-local optimizer eligibility and auditable decision accounting.

Agent workloads can contain LLM calls that are part of the evaluated system
and calls that belong to the environment around it (for example, a simulated
user).  Provider interception must observe both classes while rewriting only
the evaluated system.  This module owns that distinction so provider and
framework adapters do not each invent their own filtering rules.

The public interface is intentionally small:

``optimization_scope(name, optimize=...)``
    Marks every intercepted call made in the context.

``optimization_scope_report()``
    Returns deterministic aggregate counts suitable for a run manifest.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

OPT_OUT_HEADER = "agentc-optimize"
OPT_OUT_VALUE = "false"

_SCOPE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class _OptimizationScope:
    name: str
    optimize: bool


@dataclass(frozen=True, slots=True)
class OptimizationDecision:
    """One recorded eligibility decision made at an interception seam."""

    scope: str
    scope_enabled: bool
    eligible: bool
    reason: str

    def span_attributes(self) -> dict[str, Any]:
        """Return low-cardinality attributes for the provider span."""
        return {
            "agentc.optimization.scope": self.scope,
            "agentc.optimization.scope_enabled": self.scope_enabled,
            "agentc.optimization.eligible": self.eligible,
            "agentc.optimization.decision_reason": self.reason,
        }


_DEFAULT_SCOPE = _OptimizationScope(name="unscoped", optimize=True)
_current_scope: ContextVar[_OptimizationScope] = ContextVar(
    "_agentc_optimization_scope",
    default=_DEFAULT_SCOPE,
)

_report_lock = threading.Lock()
_report_counts: dict[tuple[str, bool, str], int] = {}


def _validate_scope_name(name: str) -> str:
    if not isinstance(name, str) or _SCOPE_NAME.fullmatch(name) is None:
        raise ValueError(
            "optimization scope name must be a stable 1-128 character identifier "
            "containing only letters, digits, '.', '_', ':', or '-'"
        )
    return name


@contextmanager
def optimization_scope(name: str, *, optimize: bool) -> Iterator[None]:
    """Mark calls in this context as eligible or ineligible for rewriting.

    Scope names are persisted as low-cardinality evidence.  They must identify
    a stable actor or subsystem, never a task ID, prompt, user, or outcome.
    Context variables make nested scopes safe across both threads and asyncio
    tasks; exiting restores the prior scope exactly.
    """
    if not isinstance(optimize, bool):
        raise TypeError("optimization scope 'optimize' must be a bool")
    scope = _OptimizationScope(name=_validate_scope_name(name), optimize=optimize)
    token = _current_scope.set(scope)
    try:
        yield
    finally:
        _current_scope.reset(token)


def is_opted_out(extra_headers: Mapping[str, Any] | None) -> bool:
    """Return whether a request carries the explicit per-call opt-out."""
    if not extra_headers:
        return False
    for key, value in extra_headers.items():
        if str(key).lower() != OPT_OUT_HEADER:
            continue
        if isinstance(value, str) and value.strip().lower() == OPT_OUT_VALUE:
            return True
    return False


def decide_optimization(
    extra_headers: Mapping[str, Any] | None = None,
) -> OptimizationDecision:
    """Record and return the current call's optimizer eligibility decision."""
    scope = _current_scope.get()
    if is_opted_out(extra_headers):
        eligible = False
        reason = "request_opt_out"
    elif not scope.optimize:
        eligible = False
        reason = "scope_excluded"
    else:
        eligible = True
        reason = "scope_eligible"

    decision = OptimizationDecision(
        scope=scope.name,
        scope_enabled=scope.optimize,
        eligible=eligible,
        reason=reason,
    )
    with _report_lock:
        key = (decision.scope, decision.scope_enabled, decision.reason)
        _report_counts[key] = _report_counts.get(key, 0) + 1
    return decision


def optimization_scope_report() -> dict[str, Any]:
    """Return a deterministic manifest fragment for all decisions this run."""
    with _report_lock:
        counts = dict(_report_counts)

    scopes: dict[tuple[str, bool], dict[str, Any]] = {}
    for (name, enabled, reason), count in counts.items():
        row = scopes.setdefault(
            (name, enabled),
            {
                "name": name,
                "scope_enabled": enabled,
                "total_calls": 0,
                "eligible_calls": 0,
                "excluded_calls": 0,
                "decision_reasons": {},
            },
        )
        row["total_calls"] += count
        if reason == "scope_eligible":
            row["eligible_calls"] += count
        else:
            row["excluded_calls"] += count
        row["decision_reasons"][reason] = count

    ordered_scopes = []
    for key in sorted(scopes):
        row = scopes[key]
        row["decision_reasons"] = dict(sorted(row["decision_reasons"].items()))
        ordered_scopes.append(row)

    return {
        "schema_version": 1,
        "total_calls": sum(row["total_calls"] for row in ordered_scopes),
        "eligible_calls": sum(row["eligible_calls"] for row in ordered_scopes),
        "excluded_calls": sum(row["excluded_calls"] for row in ordered_scopes),
        "scopes": ordered_scopes,
    }


def _reset_optimization_scope_report() -> None:
    """Start a fresh run-level report. Called once by ``agentc.init``."""
    with _report_lock:
        _report_counts.clear()


def _write_optimization_scope_report(storage_path: Path) -> Path:
    """Atomically persist this process's report as a manifest fragment."""
    report_dir = storage_path / "optimization-scopes"
    report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = report_dir / f"pid-{os.getpid()}.json"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=report_dir,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                optimization_scope_report(),
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(temporary, target)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise
    return target


__all__ = ["optimization_scope", "optimization_scope_report"]
