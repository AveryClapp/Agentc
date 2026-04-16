"""Unit tests for ``agentc._executor.dispatch``.

Spec exit criteria (bd-0bs):
- Plan round-trip: each Plan variant dispatches the expected number of
  provider calls.
- Executor retries original call exactly once on rewritten-plan failure.
"""

from __future__ import annotations

import asyncio

import pytest

from agentc._executor import dispatch
from agentc._optimizer import Plan


@pytest.mark.asyncio
async def test_pass_through_runs_original_once():
    calls = {"original": 0, "mutated": 0}

    async def original() -> str:
        calls["original"] += 1
        return "ok"

    async def mutated(_c):
        calls["mutated"] += 1
        return "never"

    out = await dispatch(
        Plan(kind="pass_through"),
        run_original=original,
        run_mutated=mutated,
    )
    assert out == "ok"
    assert calls == {"original": 1, "mutated": 0}


@pytest.mark.asyncio
async def test_cached_returns_value_without_provider_call():
    calls = {"original": 0, "mutated": 0}

    async def original() -> str:
        calls["original"] += 1
        return "live"

    async def mutated(_c):
        calls["mutated"] += 1
        return "never"

    plan = Plan(kind="cached", value={"output": "hit"})
    out = await dispatch(
        plan,
        run_original=original,
        run_mutated=mutated,
        decode_cached=lambda v: v["output"],
    )
    assert out == "hit"
    assert calls == {"original": 0, "mutated": 0}


@pytest.mark.asyncio
async def test_rewritten_invokes_mutated_call_exactly_once():
    calls = {"original": 0, "mutated": 0}

    async def original() -> str:
        calls["original"] += 1
        return "orig"

    async def mutated(c):
        calls["mutated"] += 1
        return f"mutated:{c['model']}"

    plan = Plan(
        kind="rewritten",
        rule="ModelDowngrade",
        call={"model": "gpt-4o-mini", "messages": []},
    )
    out = await dispatch(plan, run_original=original, run_mutated=mutated)
    assert out == "mutated:gpt-4o-mini"
    assert calls == {"original": 0, "mutated": 1}


@pytest.mark.asyncio
async def test_rewritten_falls_back_exactly_once_on_failure():
    calls = {"original": 0, "mutated": 0}

    async def original() -> str:
        calls["original"] += 1
        return "orig"

    async def mutated(_c):
        calls["mutated"] += 1
        raise RuntimeError("downgraded model unavailable")

    plan = Plan(
        kind="rewritten",
        rule="ModelDowngrade",
        call={"model": "unavailable-mini", "messages": []},
    )
    out = await dispatch(plan, run_original=original, run_mutated=mutated)
    assert out == "orig"
    # Spec: "Executor retries original call exactly once on
    # downgraded-model failure."
    assert calls == {"original": 1, "mutated": 1}


@pytest.mark.asyncio
async def test_parallel_dispatches_all_calls_concurrently():
    completed = []

    async def original() -> str:
        return "orig"

    async def mutated(c):
        await asyncio.sleep(0.05)
        completed.append(c["id"])
        return c["id"]

    plan = Plan(
        kind="parallel",
        rule="ParallelBranch",
        calls=[{"id": "a"}, {"id": "b"}, {"id": "c"}],
    )

    import time
    t0 = time.monotonic()
    out = await dispatch(plan, run_original=original, run_mutated=mutated)
    elapsed = time.monotonic() - t0
    assert sorted(out) == ["a", "b", "c"]
    # Concurrent → ~50ms, not 150ms.
    assert elapsed < 0.12, f"parallel dispatch took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_parallel_falls_back_on_any_failure():
    calls = {"original": 0, "mutated": 0}

    async def original() -> str:
        calls["original"] += 1
        return "fallback"

    async def mutated(c):
        calls["mutated"] += 1
        if c["id"] == "b":
            raise RuntimeError("boom")
        return c["id"]

    plan = Plan(
        kind="parallel",
        rule="ParallelBranch",
        calls=[{"id": "a"}, {"id": "b"}],
    )
    out = await dispatch(plan, run_original=original, run_mutated=mutated)
    assert out == "fallback"
    assert calls["original"] == 1


@pytest.mark.asyncio
async def test_rewritten_without_call_falls_back():
    calls = {"original": 0, "mutated": 0}

    async def original():
        calls["original"] += 1
        return "orig"

    async def mutated(_c):
        calls["mutated"] += 1
        return "never"

    plan = Plan(kind="rewritten", rule="X", call=None)
    out = await dispatch(plan, run_original=original, run_mutated=mutated)
    assert out == "orig"
    assert calls == {"original": 1, "mutated": 0}
