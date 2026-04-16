"""Plan dispatcher.

Given a :class:`Plan` from the optimizer, decide what to execute:

* ``PassThrough`` — run the user's original callable.
* ``Cached`` — return the cached value without a network call.
* ``Rewritten`` — dispatch the mutated call; if that fails (e.g. the
  downgraded model is unavailable), fall back to the original call
  exactly once and warn.
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
            return decode(plan.value)
        except BaseException:
            log.warning("cached plan decode failed; falling back to original", exc_info=True)
            return await run_original()

    if plan.kind == "rewritten":
        if plan.call is None:
            log.debug("rewritten plan missing call; falling back")
            return await run_original()
        try:
            return await run_mutated(plan.call)
        except BaseException as exc:
            log.warning(
                "rewritten plan %r failed (%s); retrying original call once",
                plan.rule,
                exc,
            )
            return await run_original()

    if plan.kind == "parallel":
        if not plan.calls:
            log.debug("parallel plan with no calls; falling back")
            return await run_original()
        try:
            return await asyncio.gather(*(run_mutated(c) for c in plan.calls))
        except BaseException as exc:
            log.warning(
                "parallel plan %r failed (%s); retrying original call once",
                plan.rule,
                exc,
            )
            return await run_original()

    log.debug("unknown plan kind %r; falling back", plan.kind)
    return await run_original()
