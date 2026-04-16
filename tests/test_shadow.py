"""Unit tests for ``agentc._shadow`` — background shadow dispatch.

The spec (§ Architecture > Shadow mode) promises:
- The shadow runs in parallel with the primary response.
- If the shadow doesn't complete within 2× the primary latency, it is
  dropped without surfacing the error to the user.
- Primary-call latency must be unchanged by shadow mode. We approximate
  that with an upper bound: ``shadow_dispatch`` must return promptly
  when given a long shadow and a short primary latency.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agentc._shadow import ShadowResult, shadow_dispatch


@pytest.mark.asyncio
async def test_shadow_completes_within_cap_delivers_result():
    async def shadow():
        await asyncio.sleep(0.01)
        return "shadow-value"

    completed: list[ShadowResult] = []
    await shadow_dispatch(shadow(), primary_latency_s=1.0, on_complete=completed.append)
    assert len(completed) == 1
    r = completed[0]
    assert not r.dropped
    assert r.result == "shadow-value"
    assert r.error is None


@pytest.mark.asyncio
async def test_shadow_exceeding_2x_primary_is_dropped():
    async def slow_shadow():
        await asyncio.sleep(0.5)
        return "never-arrives"

    completed: list[ShadowResult] = []
    # Primary took 10 ms → shadow cap is 20 ms. Shadow needs 500 ms.
    started = time.monotonic()
    await shadow_dispatch(
        slow_shadow(), primary_latency_s=0.01, on_complete=completed.append
    )
    elapsed = time.monotonic() - started
    assert len(completed) == 1
    assert completed[0].dropped
    # The dispatch should have returned near the cap, not near the
    # shadow's own latency.
    assert elapsed < 0.15, f"dispatch took {elapsed:.3f}s; should honour 2× cap"


@pytest.mark.asyncio
async def test_shadow_error_is_captured_not_raised():
    async def failing_shadow():
        raise RuntimeError("shadow boom")

    completed: list[ShadowResult] = []
    # shadow_dispatch must swallow the shadow's error, never raise.
    await shadow_dispatch(
        failing_shadow(), primary_latency_s=1.0, on_complete=completed.append
    )
    assert len(completed) == 1
    assert not completed[0].dropped
    assert isinstance(completed[0].error, RuntimeError)


@pytest.mark.asyncio
async def test_zero_primary_latency_drops_shadow_immediately():
    async def shadow():
        await asyncio.sleep(0.01)
        return "x"

    completed: list[ShadowResult] = []
    await shadow_dispatch(shadow(), primary_latency_s=0.0, on_complete=completed.append)
    assert len(completed) == 1
    assert completed[0].dropped


@pytest.mark.asyncio
async def test_primary_is_not_blocked_by_shadow():
    # End-to-end style: simulate what the SDK executor is expected to
    # do — return the primary immediately, fire-and-forget the shadow.
    async def slow_shadow():
        await asyncio.sleep(0.2)
        return "slow"

    completed: list[ShadowResult] = []

    started = time.monotonic()
    task = asyncio.create_task(
        shadow_dispatch(
            slow_shadow(), primary_latency_s=0.05, on_complete=completed.append
        )
    )
    # Primary returns "immediately" (we don't await the task yet).
    primary_elapsed = time.monotonic() - started
    assert primary_elapsed < 0.02, f"primary blocked for {primary_elapsed:.3f}s"

    # Now drain the shadow; it should drop at ~100 ms (2× 50 ms).
    await task
    total_elapsed = time.monotonic() - started
    assert len(completed) == 1
    assert completed[0].dropped
    assert total_elapsed < 0.25


@pytest.mark.asyncio
async def test_completion_callback_exception_is_suppressed():
    async def shadow():
        return 42

    def bad_callback(_r: ShadowResult) -> None:
        raise RuntimeError("caller bug")

    # Must not raise — the callback error is suppressed + logged.
    await shadow_dispatch(shadow(), primary_latency_s=1.0, on_complete=bad_callback)
