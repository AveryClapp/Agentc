"""Plan dispatcher.

Given a :class:`Plan` from the optimizer, decide what to execute:

* ``PassThrough`` — run the user's original callable.
* ``Cached`` — return the cached value without a network call.
* ``Rewritten`` / ``Composed`` — dispatch the mutated call; if that
  fails (e.g. the downgraded model is unavailable), fall back to the
  original call exactly once and warn. A Composed plan carries the
  fully-composed call in the same ``call`` slot as Rewritten.
* ``Parallel`` — record the independence certificate and run the original
  call once; ``parallel_map`` owns concurrent dispatch.

The dispatcher is provider-agnostic: it hands the caller the
responsibility of turning a ``call_dict`` (the Rust-side ``Call`` JSON)
into a coroutine that executes it. That keeps this module thin and
reusable across vendors.

Only ordinary execution failures fall back. Cancellation, process-exit signals
and fatal transport stops propagate without dispatching another provider call.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

from agentc._optimizer import Plan

log = logging.getLogger(__name__)

CallDispatcher = Callable[[dict[str, Any]], Awaitable[Any]]


def _mark_dispatch_fallback(plan: Plan, reason: str) -> None:
    plan.dispatch_fallback = True
    plan.dispatch_fallback_reason = reason
    plan.executed_model_id = None


async def dispatch(
    plan: Plan,
    *,
    run_original: Callable[[], Awaitable[Any]],
    run_mutated: CallDispatcher,
    decode_cached: Optional[Callable[[Any], Any]] = None,
) -> Any:
    """Execute ``plan``.

    - ``run_original()`` is awaited on ``PassThrough``, on ``Rewritten``
      retry, and whenever fallback is needed.
    - ``run_mutated(call_dict)`` is awaited for a ``Rewritten`` /
      ``Composed`` call.
    - ``decode_cached(value)`` shapes the cached payload for the caller.
      Default is identity.
    """
    decode = decode_cached or (lambda v: v)
    from agentc._output_validation import charge_rejected_candidate, reject_failed_candidate, validate_candidate_async

    async def run_reference() -> Any:
        if not plan.dispatch_fallback:
            return await run_original()
        started = time.perf_counter()
        try:
            return await run_original()
        finally:
            plan.reference_fallback_latency_ms = (time.perf_counter() - started) * 1000.0

    async def release_candidate(result: Any) -> Any:
        if await validate_candidate_async(plan, result):
            return result
        charge_rejected_candidate(plan, result)
        _mark_dispatch_fallback(plan, "output_validation_rejected")
        return await run_reference()

    if plan.kind == "pass_through":
        try:
            return await run_reference()
        except BaseException:
            # A reserved candidate cannot run without a reference response.
            # Close its lease before preserving the provider exception.
            from agentc._patches._optimizer_glue import cancel_exploration

            cancel_exploration(plan)
            raise

    if plan.kind == "cached":
        try:
            decoded = decode(plan.value)
        except Exception:
            reject_failed_candidate(plan, cost_complete=True)
            _mark_dispatch_fallback(plan, "cache_decode_failed")
            log.warning("cached plan decode failed; falling back to original", exc_info=True)
            return await run_reference()
        if decoded is None:
            reject_failed_candidate(plan, cost_complete=True)
            # A cache hit that decodes to None must not be served as a None
            # response — fall back to the real call (mirrors dispatch_sync; bd-8ln).
            _mark_dispatch_fallback(plan, "cache_decode_empty")
            log.warning("cached plan decoded to None; falling back to original")
            return await run_reference()
        return await release_candidate(decoded)

    if plan.kind in ("rewritten", "composed"):
        # A Composed plan carries the fully-composed Call in ``plan.call``,
        # exactly like Rewritten — dispatch it the same way. (dispatch_sync
        # groups them identically.)
        if plan.call is None:
            _mark_dispatch_fallback(plan, "missing_mutated_call")
            log.debug("%s plan missing call; falling back", plan.kind)
            return await run_reference()
        try:
            result = await run_mutated(plan.call)
            plan.executed_model_id = str(plan.call.get("model") or "") or None
        except Exception as exc:
            from agentc._provider_receipts import graph_template_position, graph_discovery_position

            if graph_template_position() is not None or graph_discovery_position() is not None:
                plan.inference_cost_complete = False
                raise
            reject_failed_candidate(plan, cost_complete=False)
            _mark_dispatch_fallback(plan, "mutated_dispatch_failed")
            log.warning(
                "%s plan %r failed (%s); retrying original call once",
                plan.kind,
                plan.rule,
                exc,
            )
            return await run_reference()
        return await release_candidate(result)

    if plan.kind == "parallel":
        # ParallelBranch certifies work already scheduled by parallel_map.
        # Its peer descriptors are not independently executable requests.
        _mark_dispatch_fallback(plan, "parallel_observe_only")
        return await run_reference()

    _mark_dispatch_fallback(plan, "unknown_plan_kind")
    log.debug("unknown plan kind %r; falling back", plan.kind)
    return await run_reference()
