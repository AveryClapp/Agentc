"""Typed Python shim over the native optimizer FFI.

``optimize_plan`` / ``optimize_observe`` in the Rust extension take
JSON strings — this module hides that over a small dataclass surface
so the interceptor and executor don't reinvent JSON shepherding.

Every call that leaves this module through the FFI is fail-open: a
native panic returns the passthrough JSON, a deserialization hiccup
here is downgraded to ``PassThrough`` with a debug log. The user is
never handed a broken plan.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from agentc import _native

log = logging.getLogger(__name__)

PlanKind = Literal["pass_through", "cached", "rewritten", "parallel", "composed"]


@dataclass
class Plan:
    """Result of :func:`plan_call`.

    The ``kind`` field mirrors the Rust enum tag. ``value`` is populated
    for ``cached``; ``call`` for ``rewritten``; ``calls`` for ``parallel``.
    """

    kind: PlanKind
    rule: Optional[str] = None
    value: Any = None
    call: Optional[dict[str, Any]] = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    # For composed plans: list of rule names that contributed.
    rules: list[str] = field(default_factory=list)
    projected_savings_usd: float = 0.0
    raw_json: str = "{\"kind\":\"pass_through\"}"
    # Thread-through fields for TraceOptimizer.record() in observe_outcome.
    trace_id: Optional[str] = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_pass_through(self) -> bool:
        return self.kind == "pass_through"


PASS_THROUGH = Plan(kind="pass_through")


def plan_call(call: dict[str, Any]) -> Plan:
    """Invoke the native optimizer on a serialized :class:`Call` dict."""
    try:
        call_json = json.dumps(call)
    except (TypeError, ValueError):
        log.debug("plan_call: call not JSON-serializable; passing through", exc_info=True)
        return PASS_THROUGH

    try:
        plan_json = _native.optimize_plan(call_json)
    except BaseException:
        log.debug("plan_call: native optimize_plan raised; passing through", exc_info=True)
        return PASS_THROUGH

    try:
        data = json.loads(plan_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        log.debug("plan_call: bad JSON from native; passing through: %r", plan_json)
        return PASS_THROUGH

    plan = _plan_from_dict(data, plan_json)
    plan.trace_id = call.get("trace_id")
    plan.messages = list(call.get("messages") or [])
    return plan


def observe_outcome(plan: Plan, outcome: dict[str, Any]) -> None:
    """Feed an outcome back into the cost model.

    ``plan`` is the object returned by :func:`plan_call`; we thread the
    exact ``raw_json`` back to the FFI so the Rust side can correlate
    with its audit ring buffer.
    """
    try:
        outcome_json = json.dumps(outcome)
    except (TypeError, ValueError):
        log.debug("observe_outcome: outcome not serializable; dropping")
        return
    try:
        _native.optimize_observe(plan.raw_json, outcome_json)
    except BaseException:
        log.debug("observe_outcome: native call raised; dropping", exc_info=True)


def _plan_from_dict(data: dict[str, Any], raw_json: str) -> Plan:
    kind = data.get("kind", "pass_through")
    if kind == "pass_through":
        return Plan(kind="pass_through", raw_json=raw_json)
    if kind == "cached":
        return Plan(kind="cached", value=data.get("value"), raw_json=raw_json)
    if kind == "rewritten":
        return Plan(
            kind="rewritten",
            rule=data.get("rule"),
            call=data.get("call"),
            projected_savings_usd=float(data.get("projected_savings_usd", 0.0)),
            raw_json=raw_json,
        )
    if kind == "parallel":
        return Plan(
            kind="parallel",
            rule=data.get("rule"),
            calls=list(data.get("calls", [])),
            projected_savings_usd=float(data.get("projected_savings_usd", 0.0)),
            raw_json=raw_json,
        )
    if kind == "composed":
        rule_apps = data.get("rules", [])
        return Plan(
            kind="composed",
            rules=[r.get("rule", "") for r in rule_apps],
            rule=rule_apps[0].get("rule") if rule_apps else None,
            call=data.get("call"),
            projected_savings_usd=float(data.get("net_savings_usd", 0.0)),
            raw_json=raw_json,
        )
    log.debug("plan_call: unknown kind %r from native", kind)
    return PASS_THROUGH
