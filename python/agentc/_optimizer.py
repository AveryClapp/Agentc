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
    raw_json: str = '{"kind":"pass_through"}'
    # Thread-through fields for TraceOptimizer.record() and cache auto-seeding.
    trace_id: Optional[str] = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Raw parameters dict from the call, used to compute the canonical
    # parameters_hash for cache auto-seeding in _observe_openai_outcome.
    parameters: dict[str, Any] = field(default_factory=dict)
    # Versioned routed-dispatch metadata. These fields are populated from the
    # Rust-selected call and become observation/span dimensions after dispatch.
    provider_protocol: Optional[str] = None
    provider_namespace: Optional[str] = None
    target_model_id: Optional[str] = None
    target_model_version: Optional[str] = None
    catalog_version: Optional[str] = None
    price_table_version: Optional[str] = None
    executed_model_id: Optional[str] = None
    dispatch_fallback: bool = False
    dispatch_fallback_reason: Optional[str] = None
    # Request-path admission can abstain before semantic planning when the
    # local runtime is saturated. This is distinct from provider dispatch
    # fallback and leaves the original call immutable.
    runtime_fallback_reason: Optional[str] = None
    runtime_fallback_limit: Optional[int] = None
    # Opaque Rust-issued handle binding a measured execution to its exact
    # plan profile, runtime version, and sequence. Provider adapters must not
    # inspect or reconstruct it.
    observation_token: Optional[str] = None
    # Initial calibration never exposes an unadmitted candidate. Rust may
    # attach one durably leased candidate to a pass-through Plan; adapters run
    # it off-path and return the reference response unchanged.
    exploration_lease_token: Optional[str] = None
    counterfactual: Optional["Plan"] = None

    @property
    def is_pass_through(self) -> bool:
        return self.kind == "pass_through"


PASS_THROUGH = Plan(kind="pass_through")


def plan_call(call: dict[str, Any]) -> Plan:
    """Invoke the native optimizer on a serialized :class:`Call` dict."""
    try:
        call_json = json.dumps(call)
    except (TypeError, ValueError):
        log.debug(
            "plan_call: call not JSON-serializable; passing through", exc_info=True
        )
        return PASS_THROUGH

    try:
        plan_json = _native.optimize_plan(call_json)
    except BaseException:
        log.debug(
            "plan_call: native optimize_plan raised; passing through", exc_info=True
        )
        return PASS_THROUGH

    try:
        data = json.loads(plan_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        log.debug("plan_call: bad JSON from native; passing through: %r", plan_json)
        return PASS_THROUGH

    plan = _plan_from_dict(data, plan_json)
    plan.trace_id = call.get("trace_id")
    plan.messages = list(call.get("messages") or [])
    plan.parameters = dict(call.get("parameters") or {})
    _hydrate_dispatch_metadata(plan, call)
    if plan.counterfactual is not None:
        _hydrate_dispatch_metadata(plan.counterfactual, call)
    return plan


def model_catalog() -> dict[str, Any]:
    """Return the exact versioned model catalog owned by the native runtime."""
    try:
        value = json.loads(_native.optimize_model_catalog())
        return value if isinstance(value, dict) else {"targets": []}
    except BaseException:
        log.debug("model_catalog: native catalog unavailable", exc_info=True)
        return {"targets": []}


def observe_outcome(plan: Plan, outcome: dict[str, Any]) -> Optional[str]:
    """Feed an outcome back into the cost model.

    ``plan`` is the object returned by :func:`plan_call`; we thread the
    exact ``raw_json`` back to the FFI so the Rust side can correlate
    with its audit ring buffer.
    """
    # A Plan is normally single-use, but clearing first makes reuse fail safe:
    # no later shadow comparison can inherit a prior execution's token.
    plan.observation_token = None
    try:
        outcome_json = json.dumps(outcome)
    except (TypeError, ValueError):
        log.debug("observe_outcome: outcome not serializable; dropping")
        return None
    try:
        token = _native.optimize_observe(plan.raw_json, outcome_json)
    except BaseException:
        log.debug("observe_outcome: native call raised; dropping", exc_info=True)
        return None
    if isinstance(token, str) and token:
        plan.observation_token = token
        return token
    return None


def record_divergence(observation_token: str, divergence: float) -> None:
    """Attach one shadow comparison to its exact Rust-issued observation.

    The token is opaque and binds the plan profile, runtime version, and
    execution sequence. Non-finite or out-of-range values are discarded by
    Rust without mutating guard state. Fail-open: a native hiccup must never
    surface to the user, whose primary call already returned.
    """
    if not observation_token:
        return
    try:
        _native.optimize_record_divergence(observation_token, float(divergence))
    except BaseException:
        log.debug("record_divergence: native call raised; dropping", exc_info=True)


def complete_exploration(
    plan: Plan,
    outcome: dict[str, Any],
    divergence: float,
) -> bool:
    """Commit one leased counterfactual outcome to its exact plan profile."""
    token = plan.exploration_lease_token
    if not token:
        return False
    try:
        outcome_json = json.dumps(outcome)
    except (TypeError, ValueError):
        log.debug("complete_exploration: outcome not serializable; failing lease")
        fail_exploration(plan)
        return False
    try:
        recorded = bool(
            _native.optimize_complete_exploration(
                token,
                outcome_json,
                float(divergence),
            )
        )
    except BaseException:
        log.debug("complete_exploration: native call raised; dropping", exc_info=True)
        return False
    if recorded:
        plan.exploration_lease_token = None
    return recorded


def fail_exploration(plan: Plan) -> bool:
    """Mark a leased counterfactual failed without surfacing an exception."""
    token = plan.exploration_lease_token
    if not token:
        return False
    try:
        recorded = bool(_native.optimize_fail_exploration(token))
    except BaseException:
        log.debug("fail_exploration: native call raised; dropping", exc_info=True)
        return False
    if recorded:
        plan.exploration_lease_token = None
    return recorded


def _plan_from_dict(data: dict[str, Any], raw_json: str) -> Plan:
    kind = data.get("kind", "pass_through")
    if kind == "pass_through":
        plan = Plan(kind="pass_through", raw_json=raw_json)
        _hydrate_runtime_fallback(plan, data)
        _hydrate_exploration(plan, data)
        return plan
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


def _hydrate_runtime_fallback(plan: Plan, data: dict[str, Any]) -> None:
    diagnostics = data.get("agentc_runtime_fallback")
    if not isinstance(diagnostics, dict) or diagnostics.get("schema_version") != 1:
        return
    reason = diagnostics.get("fallback_reason")
    limit = diagnostics.get("max_inflight_plans")
    if isinstance(reason, str) and reason:
        plan.runtime_fallback_reason = reason
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        plan.runtime_fallback_limit = limit


def _hydrate_exploration(plan: Plan, data: dict[str, Any]) -> None:
    context = data.get("agentc_exploration_context")
    if not isinstance(context, dict) or context.get("schema_version") != 1:
        return
    lease_token = context.get("lease_token")
    candidate_data = context.get("candidate_plan")
    if (
        not isinstance(lease_token, str)
        or not lease_token
        or not isinstance(candidate_data, dict)
    ):
        return
    try:
        candidate_json = json.dumps(candidate_data, separators=(",", ":"))
        candidate = _plan_from_dict(candidate_data, candidate_json)
    except (TypeError, ValueError, RecursionError):
        log.debug("plan_call: malformed counterfactual envelope", exc_info=True)
        return
    if candidate.kind not in ("rewritten", "composed") or candidate.call is None:
        log.debug("plan_call: non-executable counterfactual ignored")
        return
    plan.exploration_lease_token = lease_token
    plan.counterfactual = candidate


def _hydrate_dispatch_metadata(plan: Plan, original_call: dict[str, Any]) -> None:
    parameters = original_call.get("parameters")
    extra = parameters.get("extra") if isinstance(parameters, dict) else None
    route_context = (
        extra.get("agentc_route_context") if isinstance(extra, dict) else None
    )
    if isinstance(route_context, dict):
        plan.provider_protocol = _optional_string(
            route_context.get("provider_protocol")
        )
        plan.provider_namespace = _optional_string(
            route_context.get("provider_namespace")
        )

    routed_call = plan.call if isinstance(plan.call, dict) else None
    routed_params = routed_call.get("parameters") if routed_call is not None else None
    routed_extra = (
        routed_params.get("extra") if isinstance(routed_params, dict) else None
    )
    metadata = (
        routed_extra.get("agentc_routed_target")
        if isinstance(routed_extra, dict)
        else None
    )
    if not isinstance(metadata, dict):
        return
    plan.provider_protocol = _optional_string(metadata.get("provider_protocol"))
    plan.provider_namespace = _optional_string(metadata.get("provider_namespace"))
    plan.target_model_id = _optional_string(metadata.get("target_model_id"))
    plan.target_model_version = _optional_string(metadata.get("target_model_version"))
    plan.catalog_version = _optional_string(metadata.get("catalog_version"))
    plan.price_table_version = _optional_string(metadata.get("price_table_version"))


def _optional_string(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None
