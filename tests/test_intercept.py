"""Unit tests for ``agentc._intercept.intercept``.

Covers the per-call opt-out path, outcome observation, and the happy
pass-through.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agentc._intercept import intercept, is_opted_out
from agentc._optimizer import Plan


def test_is_opted_out_case_insensitive():
    assert is_opted_out({"agentc-optimize": "false"})
    assert is_opted_out({"Agentc-Optimize": "FALSE"})
    assert not is_opted_out({"agentc-optimize": "true"})
    assert not is_opted_out(None)
    assert not is_opted_out({})


@pytest.mark.asyncio
async def test_opt_out_skips_optimizer_entirely():
    calls = {"original": 0, "mutated": 0, "plan": 0}

    async def original():
        calls["original"] += 1
        return "orig"

    async def mutated(_c):
        calls["mutated"] += 1
        return "never"

    def _plan(_call):
        calls["plan"] += 1
        return Plan(kind="pass_through")

    with patch("agentc._intercept.plan_call", side_effect=_plan):
        out = await intercept(
            build_call=lambda: {"call_site_id": "x", "model": "gpt-4o", "messages": []},
            run_original=original,
            run_mutated=mutated,
            extract_outcome=lambda _r, _s: {},
            extra_headers={"agentc-optimize": "false"},
        )
    assert out == "orig"
    # Critical: plan_call must NOT be called under opt-out.
    assert calls == {"original": 1, "mutated": 0, "plan": 0}


@pytest.mark.asyncio
async def test_pass_through_invokes_optimizer_and_observes():
    observed = []

    async def original():
        return "ok"

    async def mutated(_c):
        raise AssertionError("should not dispatch mutated under pass-through")

    def _plan(_call):
        return Plan(kind="pass_through")

    def _observe(_plan, outcome):
        observed.append(outcome)

    with patch("agentc._intercept.plan_call", side_effect=_plan), patch(
        "agentc._intercept.observe_outcome", side_effect=_observe
    ):
        out = await intercept(
            build_call=lambda: {"call_site_id": "x", "model": "gpt-4o", "messages": []},
            run_original=original,
            run_mutated=mutated,
            extract_outcome=lambda r, _s: {"input_tokens": 10, "output_tokens": 5, "result": r},
        )
    assert out == "ok"
    assert observed and observed[0]["result"] == "ok"


@pytest.mark.asyncio
async def test_rewritten_plan_observed_after_dispatch():
    observed = []

    async def original():
        raise AssertionError("should not run original on successful rewrite")

    async def mutated(c):
        return f"mut:{c['model']}"

    def _plan(_call):
        return Plan(kind="rewritten", rule="ModelDowngrade", call={"model": "mini", "messages": []})

    def _observe(_plan, outcome):
        observed.append(outcome)

    with patch("agentc._intercept.plan_call", side_effect=_plan), patch(
        "agentc._intercept.observe_outcome", side_effect=_observe
    ):
        out = await intercept(
            build_call=lambda: {"call_site_id": "x", "model": "gpt-4o", "messages": []},
            run_original=original,
            run_mutated=mutated,
            extract_outcome=lambda r, _s: {"result": r},
        )
    assert out == "mut:mini"
    assert observed[0]["result"] == "mut:mini"


@pytest.mark.asyncio
async def test_build_call_error_falls_back_to_original():
    async def original():
        return "orig"

    async def mutated(_c):
        return "never"

    def _build():
        raise RuntimeError("bad state")

    out = await intercept(
        build_call=_build,
        run_original=original,
        run_mutated=mutated,
        extract_outcome=lambda _r, _s: {},
    )
    assert out == "orig"


@pytest.mark.asyncio
async def test_extract_outcome_error_is_suppressed():
    async def original():
        return "ok"

    async def mutated(_c):
        return "never"

    def _plan(_call):
        return Plan(kind="pass_through")

    def _bad_extract(_r, _s):
        raise RuntimeError("outcome bug")

    # Must not raise even though extract_outcome blows up.
    with patch("agentc._intercept.plan_call", side_effect=_plan):
        out = await intercept(
            build_call=lambda: {"call_site_id": "x", "model": "gpt-4o", "messages": []},
            run_original=original,
            run_mutated=mutated,
            extract_outcome=_bad_extract,
        )
    assert out == "ok"
