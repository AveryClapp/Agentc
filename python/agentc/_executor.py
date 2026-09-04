"""Plan dispatcher.

Given a :class:`Plan` from the optimizer, decide what to execute:

* ``PassThrough`` — run the user's original callable.
* ``Cached`` — return the cached value without a network call.
* ``Rewritten`` / ``Composed`` — dispatch the mutated call; if that
  fails (e.g. the downgraded model is unavailable), fall back to the
  original call exactly once and warn. A Composed plan carries the
  fully-composed call in the same ``call`` slot as Rewritten.
* ``Parallel`` — ``asyncio.gather`` over the rewritten calls and stitch
  the results back together.

The dispatcher is provider-agnostic: it hands the caller the
responsibility of turning a ``call_dict`` (the Rust-side ``Call`` JSON)
into a coroutine that executes it. That keeps this module thin and
reusable across vendors.
"""

from __future__ import annotations

import asyncio
import logging
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
    - ``run_mutated(call_dict)`` is awaited for each ``Rewritten`` /
      ``Parallel`` call.
    - ``decode_cached(value)`` shapes the cached payload for the caller.
      Default is identity.
    """
    decode = decode_cached or (lambda v: v)

    if plan.kind == "pass_through":
        return await run_original()

    if plan.kind == "cached":
        try:
            decoded = decode(plan.value)
        except BaseException:
            _mark_dispatch_fallback(plan, "cache_decode_failed")
            log.warning("cached plan decode failed; falling back to original", exc_info=True)
            return await run_original()
        if decoded is None:
            # A cache hit that decodes to None must not be served as a None
            # response — fall back to the real call (mirrors dispatch_sync; bd-8ln).
            _mark_dispatch_fallback(plan, "cache_decode_empty")
            log.warning("cached plan decoded to None; falling back to original")
            return await run_original()
        return decoded

    if plan.kind in ("rewritten", "composed"):
        # A Composed plan carries the fully-composed Call in ``plan.call``,
        # exactly like Rewritten — dispatch it the same way. (dispatch_sync
        # groups them identically.)
        if plan.call is None:
            _mark_dispatch_fallback(plan, "missing_mutated_call")
            log.debug("%s plan missing call; falling back", plan.kind)
            return await run_original()
        try:
            result = await run_mutated(plan.call)
            plan.executed_model_id = str(plan.call.get("model") or "") or None
            return result
        except BaseException as exc:
            _mark_dispatch_fallback(plan, "mutated_dispatch_failed")
            log.warning(
                "%s plan %r failed (%s); retrying original call once",
                plan.kind,
                plan.rule,
                exc,
            )
            return await run_original()

    if plan.kind == "parallel":
        if not plan.calls:
            _mark_dispatch_fallback(plan, "parallel_calls_missing")
            log.debug("parallel plan with no calls; falling back")
            return await run_original()
        try:
            return await asyncio.gather(*(run_mutated(c) for c in plan.calls))
        except BaseException as exc:
            _mark_dispatch_fallback(plan, "parallel_dispatch_failed")
            log.warning(
                "parallel plan %r failed (%s); retrying original call once",
                plan.rule,
                exc,
            )
            return await run_original()

    _mark_dispatch_fallback(plan, "unknown_plan_kind")
    log.debug("unknown plan kind %r; falling back", plan.kind)
    return await run_original()
