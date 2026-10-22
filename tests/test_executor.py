"""Unit tests for ``agentc._executor.dispatch``.

Spec exit criteria (bd-0bs):
- Plan round-trip: each Plan variant dispatches the expected number of
  provider calls.
- Executor retries original call exactly once on rewritten-plan failure.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from agentc._executor import dispatch
from agentc._optimizer import Plan


class FatalStop(BaseException):
    pass


@pytest.mark.parametrize("kind", ["rewritten", "composed", "cached"])
@pytest.mark.parametrize("error_type", [FatalStop, asyncio.CancelledError])
@pytest.mark.asyncio
async def test_abort_signals_never_dispatch_a_fallback(kind, error_type):
    calls = {"original": 0, "mutated": 0}
    plan = Plan(
        kind=kind,
        call={"model": "fixture"},
        calls=[{"model": "fixture"}],
        value="cached",
    )

    async def original():
        calls["original"] += 1
        return "unwanted fallback"

    async def mutated(call):
        calls["mutated"] += 1
        raise error_type()

    def decode(value):
        raise error_type()

    with pytest.raises(error_type):
        await dispatch(
            plan, run_original=original, run_mutated=mutated, decode_cached=decode
        )
    assert calls["original"] == 0
    assert calls["mutated"] == (0 if kind == "cached" else 1)
    assert plan.dispatch_fallback is False


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
async def test_failed_reference_closes_counterfactual_lease():
    async def original() -> str:
        raise RuntimeError("reference provider failure")

    async def mutated(_c):
        raise AssertionError("candidate must not run without a reference result")

    plan = Plan(
        kind="pass_through",
        exploration_lease_token="opaque-exploration-token",
        counterfactual=Plan(
            kind="rewritten",
            call={"model": "mini", "messages": []},
        ),
    )
    with patch(
        "agentc._patches._optimizer_glue.cancel_exploration"
    ) as cancel:
        with pytest.raises(RuntimeError, match="reference provider failure"):
            await dispatch(plan, run_original=original, run_mutated=mutated)

    cancel.assert_called_once_with(plan)


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
    assert plan.dispatch_fallback is True
    assert plan.dispatch_fallback_reason == "mutated_dispatch_failed"


@pytest.mark.asyncio
async def test_composed_invokes_mutated_call_exactly_once():
    # Regression (MNT-020): before the fix a Composed plan fell through to
    # "unknown plan kind" and ran the ORIGINAL call, silently disabling V2
    # composition on the async path. It must dispatch the composed call.
    calls = {"original": 0, "mutated": 0}

    async def original() -> str:
        calls["original"] += 1
        return "orig"

    async def mutated(c):
        calls["mutated"] += 1
        return f"composed:{c['model']}"

    plan = Plan(
        kind="composed",
        rules=["StateDrop", "OutputBudget"],
        rule="StateDrop",
        call={"model": "gpt-4o", "messages": []},
    )
    out = await dispatch(plan, run_original=original, run_mutated=mutated)
    assert out == "composed:gpt-4o"
    assert calls == {"original": 0, "mutated": 1}


@pytest.mark.asyncio
async def test_composed_falls_back_exactly_once_on_failure():
    calls = {"original": 0, "mutated": 0}

    async def original() -> str:
        calls["original"] += 1
        return "orig"

    async def mutated(_c):
        calls["mutated"] += 1
        raise RuntimeError("composed call failed")

    plan = Plan(
        kind="composed",
        rules=["StateDrop", "OutputBudget"],
        rule="StateDrop",
        call={"model": "gpt-4o", "messages": []},
    )
    out = await dispatch(plan, run_original=original, run_mutated=mutated)
    assert out == "orig"
    assert calls == {"original": 1, "mutated": 1}
    assert plan.dispatch_fallback is True


@pytest.mark.asyncio
@pytest.mark.parametrize("branches", [[], [{"id": "base"}, {"id": "peer"}]])
@pytest.mark.parametrize("validation_contract", [None, "fixture-contract"])
async def test_parallel_certificate_returns_one_original_response(
    branches, validation_contract
):
    reference = object()
    calls = {"original": 0, "mutated": 0}

    async def original():
        calls["original"] += 1
        return reference

    async def mutated(call):
        calls["mutated"] += 1
        raise AssertionError("an independence certificate must not dispatch a peer")

    plan = Plan(
        kind="parallel",
        rule="ParallelBranch",
        calls=branches,
        validation_contract_sha256=validation_contract,
    )
    out = await dispatch(plan, run_original=original, run_mutated=mutated)
    assert out is reference
    assert calls == {"original": 1, "mutated": 0}
    assert plan.dispatch_fallback_reason == "parallel_observe_only"
    assert plan.reference_fallback_latency_ms >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", [RuntimeError, FatalStop, asyncio.CancelledError]
)
async def test_parallel_certificate_preserves_original_failure(error_type):
    calls = {"original": 0, "mutated": 0}
    failure = error_type("reference failed")

    async def original():
        calls["original"] += 1
        raise failure

    async def mutated(call):
        calls["mutated"] += 1
        raise AssertionError("a failed reference must not dispatch peers")

    plan = Plan(kind="parallel", calls=[{"id": "base"}, {"id": "peer"}])
    with pytest.raises(error_type) as exc:
        await dispatch(plan, run_original=original, run_mutated=mutated)
    assert exc.value is failure
    assert calls == {"original": 1, "mutated": 0}
    assert plan.dispatch_fallback_reason == "parallel_observe_only"


@pytest.mark.parametrize("fails", [False, True])
def test_sync_parallel_certificate_preserves_the_original_call(fails):
    from agentc._patches._optimizer_glue import dispatch_sync

    reference = object()
    failure = RuntimeError("reference failed")
    calls = {"original": 0, "mutated": 0}

    def original():
        calls["original"] += 1
        if fails:
            raise failure
        return reference

    def mutated(call):
        calls["mutated"] += 1
        raise AssertionError("an independence certificate must not dispatch a peer")

    plan = Plan(kind="parallel", calls=[{"id": "base"}, {"id": "peer"}])
    if fails:
        with pytest.raises(RuntimeError) as exc:
            dispatch_sync(plan, run_original=original, run_mutated=mutated)
        assert exc.value is failure
    else:
        assert (
            dispatch_sync(plan, run_original=original, run_mutated=mutated) is reference
        )
    assert calls == {"original": 1, "mutated": 0}
    assert plan.dispatch_fallback_reason == "parallel_observe_only"


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
