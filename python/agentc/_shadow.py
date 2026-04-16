"""Background shadow-mode execution.

When the optimizer emits a rewritten plan and the [`ShadowSampler`]
fires, the SDK executor is expected to:

1. Dispatch the rewritten plan, return its result to the caller (primary).
2. In parallel, dispatch the *unrewritten* plan as a background task.
3. When the background task completes, compare its result to the primary
   using the Rust-side divergence meters, and feed the result through
   ``optimize_observe`` so the accuracy-budget machinery can auto-disable
   drifting rules.
4. If the background task has not completed within twice the primary
   latency, drop it. We never block the user-visible call on the shadow.

This module supplies the ``shadow_dispatch`` helper that encapsulates
steps 2–4. Step 1 is caller-controlled (the SDK executor already owns
dispatch). The caller passes a coroutine that produces the unrewritten
result and a callback to invoke once divergence has been measured.

The 2× primary-latency cap is measured once at primary completion —
not refreshed as the shadow runs — so a stall in the shadow can never
hold resources indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

__all__ = ["shadow_dispatch", "ShadowResult"]


@dataclass
class ShadowResult:
    """Returned to the caller-supplied completion callback once the
    shadow completes (or is dropped).

    ``dropped`` is True iff the shadow did not finish within the 2×
    primary-latency cap. ``result`` and ``error`` describe the shadow
    run's outcome when it did finish; both are ``None`` when dropped.
    """

    dropped: bool
    result: Any = None
    error: Optional[BaseException] = None


async def shadow_dispatch(
    shadow_coro: Awaitable[Any],
    primary_latency_s: float,
    on_complete: Callable[[ShadowResult], None],
    *,
    max_multiplier: float = 2.0,
) -> None:
    """Run ``shadow_coro`` with a hard timeout of ``max_multiplier *
    primary_latency_s`` seconds and deliver a :class:`ShadowResult` to
    ``on_complete`` exactly once.

    This coroutine returns as soon as the shadow completes (or is
    dropped). Callers typically fire-and-forget it via
    :func:`asyncio.create_task` so the primary response returns first.
    ``on_complete`` is invoked *before* this coroutine returns; it must
    not raise (we catch + log any exception it does raise).
    """
    cap_s = max(0.0, primary_latency_s * max_multiplier)
    # Zero/negative cap → drop immediately. Close the coroutine so it
    # doesn't trigger an "un-awaited coroutine" warning at GC time.
    if cap_s <= 0.0:
        if hasattr(shadow_coro, "close"):
            shadow_coro.close()
        _deliver(on_complete, ShadowResult(dropped=True))
        return
    try:
        result = await asyncio.wait_for(shadow_coro, timeout=cap_s)
        _deliver(on_complete, ShadowResult(dropped=False, result=result))
    except asyncio.TimeoutError:
        _deliver(on_complete, ShadowResult(dropped=True))
    except BaseException as exc:  # noqa: BLE001 — fail-open by design
        # Shadow errors never surface to the user; log at debug because
        # transient errors in the unrewritten plan are expected during
        # provider flakes and shouldn't generate log noise.
        log.debug("shadow dispatch failed", exc_info=True)
        _deliver(on_complete, ShadowResult(dropped=False, error=exc))


def _deliver(on_complete: Callable[[ShadowResult], None], result: ShadowResult) -> None:
    try:
        on_complete(result)
    except BaseException:  # noqa: BLE001
        log.exception("shadow completion callback raised; suppressed")
