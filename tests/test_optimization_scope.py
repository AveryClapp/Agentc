"""Behavioral tests for context-local optimizer eligibility."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from agentc import optimization_scope, optimization_scope_report
from agentc._intercept import intercept
from agentc._optimization_scope import _reset_optimization_scope_report
from agentc._optimizer import Plan


@pytest.fixture(autouse=True)
def _fresh_report() -> None:
    _reset_optimization_scope_report()


async def _intercept_once(calls: dict[str, int]) -> str:
    async def original() -> str:
        calls["original"] += 1
        return "original"

    async def mutated(_call: dict[str, Any]) -> str:
        calls["mutated"] += 1
        return "mutated"

    return await intercept(
        build_call=lambda: (
            calls.__setitem__("built", calls["built"] + 1)
            or {"call_site_id": "site", "model": "model", "messages": []}
        ),
        run_original=original,
        run_mutated=mutated,
        extract_outcome=lambda _result, _elapsed: {},
    )


@pytest.mark.asyncio
async def test_excluded_scope_observes_call_without_planning_or_mutation() -> None:
    calls = {"built": 0, "original": 0, "mutated": 0, "planned": 0}

    def _plan(_call: dict[str, Any]) -> Plan:
        calls["planned"] += 1
        return Plan(kind="pass_through")

    with patch("agentc._intercept.plan_call", side_effect=_plan):
        with optimization_scope("tau2.user_simulator", optimize=False):
            result = await _intercept_once(calls)

    assert result == "original"
    assert calls == {"built": 0, "original": 1, "mutated": 0, "planned": 0}
    assert optimization_scope_report() == {
        "schema_version": 1,
        "total_calls": 1,
        "eligible_calls": 0,
        "excluded_calls": 1,
        "scopes": [
            {
                "name": "tau2.user_simulator",
                "scope_enabled": False,
                "total_calls": 1,
                "eligible_calls": 0,
                "excluded_calls": 1,
                "decision_reasons": {"scope_excluded": 1},
            }
        ],
    }


@pytest.mark.asyncio
async def test_nested_scope_restores_outer_eligibility() -> None:
    calls = {"built": 0, "original": 0, "mutated": 0, "planned": 0}

    def _plan(_call: dict[str, Any]) -> Plan:
        calls["planned"] += 1
        return Plan(kind="pass_through")

    with patch("agentc._intercept.plan_call", side_effect=_plan):
        with optimization_scope("tau2.evaluated_assistant", optimize=True):
            await _intercept_once(calls)
            with optimization_scope("tau2.user_simulator", optimize=False):
                await _intercept_once(calls)
            await _intercept_once(calls)

    assert calls == {"built": 2, "original": 3, "mutated": 0, "planned": 2}
    report = optimization_scope_report()
    assert report["total_calls"] == 3
    assert report["eligible_calls"] == 2
    assert report["excluded_calls"] == 1
    assert [row["name"] for row in report["scopes"]] == [
        "tau2.evaluated_assistant",
        "tau2.user_simulator",
    ]


@pytest.mark.asyncio
async def test_scopes_are_isolated_across_async_tasks() -> None:
    calls = {"built": 0, "original": 0, "mutated": 0, "planned": 0}

    async def run(name: str, optimize: bool) -> None:
        with optimization_scope(name, optimize=optimize):
            await asyncio.sleep(0)
            await _intercept_once(calls)

    def _plan(_call: dict[str, Any]) -> Plan:
        calls["planned"] += 1
        return Plan(kind="pass_through")

    with patch("agentc._intercept.plan_call", side_effect=_plan):
        await asyncio.gather(
            run("actor.assistant", True),
            run("actor.environment", False),
        )

    assert calls["planned"] == 1
    report = optimization_scope_report()
    assert report["eligible_calls"] == 1
    assert report["excluded_calls"] == 1


@pytest.mark.asyncio
async def test_request_opt_out_takes_precedence_in_enabled_scope() -> None:
    calls = {"original": 0, "planned": 0}

    async def original() -> str:
        calls["original"] += 1
        return "original"

    def _plan(_call: dict[str, Any]) -> Plan:
        calls["planned"] += 1
        return Plan(kind="pass_through")

    with patch("agentc._intercept.plan_call", side_effect=_plan):
        with optimization_scope("actor.assistant", optimize=True):
            result = await intercept(
                build_call=lambda: {"call_site_id": "site"},
                run_original=original,
                run_mutated=lambda _call: original(),
                extract_outcome=lambda _result, _elapsed: {},
                extra_headers={"Agentc-Optimize": " FALSE "},
            )

    assert result == "original"
    assert calls == {"original": 1, "planned": 0}
    row = optimization_scope_report()["scopes"][0]
    assert row["decision_reasons"] == {"request_opt_out": 1}


@pytest.mark.parametrize(
    "name",
    ["", "has spaces", "task/123", "x" * 129],
)
def test_scope_name_must_be_stable_low_cardinality_identifier(name: str) -> None:
    with pytest.raises(ValueError, match="stable 1-128 character identifier"):
        with optimization_scope(name, optimize=True):
            pass


def test_scope_rejects_truthy_string_configuration() -> None:
    with pytest.raises(TypeError, match="must be a bool"):
        with optimization_scope("actor.assistant", optimize="false"):  # type: ignore[arg-type]
            pass
