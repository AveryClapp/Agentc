"""Validate bounded evidence refresh through the native runtime and SDK adapter.

All outcomes are synthetic and all provider dispatches use a local callback.
Timestamp aging touches only native-generated samples in the test's temporary
store, after its writers close; it changes no counts, divergences or lease rows.
These checks establish recovery wiring, not workload quality or savings.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

import agentc
from agentc import _native
from agentc._patches._optimizer_glue import (
    build_call_dict_openai,
    drain_exploration,
    estimate_cost_usd,
)
from bench.live_exploration_preflight import FakeProvider, MODEL, REFERENCE_CAP, invoke

SITE = "native.evidence.refresh"


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    agentc.shutdown()
    _native.optimize_reset()
    for key, value in {
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_COMPOSE": "1",
        "AGENTC_ENABLED_RULES": "OutputBudget",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "50",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_EVAL_PLANNER_MODE": "current_greedy",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION", raising=False)
    monkeypatch.delenv("AGENTC_SHADOW_DIVERGENCE_BUDGET", raising=False)
    yield
    drain_exploration(5000)
    agentc.shutdown()
    _native.optimize_reset()


def seed_profile(storage: Path, divergences: list[float]) -> tuple[dict, str]:
    request = {
        "model": MODEL,
        "max_tokens": REFERENCE_CAP,
        "temperature": 0,
        "messages": [{"role": "user", "content": "same shaped prompt"}],
    }
    call = build_call_dict_openai(
        request, call_site_id=SITE, trace_id_hex="00" * 16, span_id_hex="01" * 8
    )
    _native.optimize_configure(str(storage))

    def observe(plan_json: str, output_tokens: int) -> str:
        token = _native.optimize_observe(
            plan_json,
            json.dumps(
                {
                    "call_site_id": SITE,
                    "model": MODEL,
                    "input_tokens": 100,
                    "output_tokens": output_tokens,
                    "latency_ms": 100.0,
                    "cost_usd": estimate_cost_usd(MODEL, 100, output_tokens),
                    "cost_source": "provider_reported",
                    "output_is_short": True,
                }
            ),
        )
        assert token
        return token

    for _ in range(3):
        plan = _native.optimize_plan(json.dumps(call))
        assert json.loads(plan)["kind"] == "pass_through"
        observe(plan, 100)
    candidate_json = _native.optimize_plan(json.dumps(call))
    candidate = json.loads(candidate_json)
    assert candidate["kind"] == "rewritten" and candidate["rule"] == "OutputBudget"
    candidate_id = candidate["agentc_observation_context"]["key"]["execution_plan_id"]
    for divergence in divergences:
        _native.optimize_record_divergence(observe(candidate_json, 40), divergence)
    _native.optimize_reset()
    return request, candidate_id


@pytest.mark.parametrize("recovery", ["passing", "breaching", "failed"])
def test_cooldown_readmission_uses_fresh_pairs_through_sdk_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str
) -> None:
    storage = (tmp_path / ".agentc").resolve()
    request, candidate_id = seed_profile(storage, [0.0] * 18 + [1.0, 1.0])
    # Advance only this closed fixture's guard clock. No counts, divergences,
    # profile observations, or exploration spend are changed.
    now_us = time.time_ns() // 1000
    age_us = 2 * 24 * 60 * 60 * 1_000_000
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM execution_plan_disabled WHERE execution_plan_id = ?",
                (candidate_id,),
            ).fetchone()[0]
            == 1
        )
        connection.execute(
            "UPDATE execution_plan_disabled SET disabled_at = disabled_at - ?, "
            "reenable_at = reenable_at - ? WHERE execution_plan_id = ?",
            (age_us, age_us, candidate_id),
        )
        connection.execute(
            "UPDATE execution_plan_guard_observation SET observed_at = observed_at - ? "
            "WHERE execution_plan_id = ?",
            (age_us, candidate_id),
        )
        connection.commit()
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "joint_guarded")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
    call = build_call_dict_openai(
        request, call_site_id=SITE, trace_id_hex="00" * 16, span_id_hex="01" * 8
    )
    _native.optimize_configure(str(storage))
    attempts = 20 if recovery == "passing" else 2
    for pair in range(attempts):
        planned = json.loads(_native.optimize_plan(json.dumps(call)))
        assert planned["kind"] == "pass_through"
        exploration = planned["agentc_exploration_context"]
        if recovery == "failed":
            assert _native.optimize_fail_exploration(exploration["lease_token"])
        else:
            assert _native.optimize_complete_exploration(
                exploration["lease_token"],
                json.dumps(
                    {
                        "call_site_id": SITE,
                        "input_tokens": 100,
                        "output_tokens": 40,
                        "latency_ms": 100,
                        "cost_usd": estimate_cost_usd(MODEL, 100, 40),
                        "cost_source": "provider_reported",
                    }
                ),
                0.0 if recovery == "passing" else 1.0,
            )
        _native.optimize_reset()
        with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
            assert connection.execute(
                "SELECT paired_observations FROM execution_plan_profile WHERE execution_plan_id = ?",
                (candidate_id,),
            ).fetchone()[0] == (0 if recovery == "failed" else pair + 1)
            boundary, floor = connection.execute(
                "SELECT started_at, observation_floor FROM execution_plan_readmission "
                "WHERE execution_plan_id = ?",
                (candidate_id,),
            ).fetchone()
            assert 0 < boundary < now_us
            assert floor == 20
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM execution_plan_exploration"
                ).fetchone()[0]
                == pair + 1
            )
        _native.optimize_configure(str(storage))
    selected = json.loads(_native.optimize_plan(json.dumps(call)))
    assert selected["kind"] == (
        "rewritten" if recovery == "passing" else "pass_through"
    )
    if recovery == "breaching":
        assert "agentc_exploration_context" not in selected
    _native.optimize_reset()
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM execution_plan_disabled WHERE execution_plan_id = ?",
            (candidate_id,),
        ).fetchone()[0] == (0 if recovery == "passing" else 1)


@pytest.mark.parametrize(
    "state,probes", [("current", 0), ("stale", 1), ("rejected", 19), ("disabled", 0)]
)
def test_refresh_preserves_counts_and_admission_through_sdk_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, probes: int
) -> None:
    storage = (tmp_path / ".agentc").resolve()
    divergences = [0.0] * 20
    if state == "rejected":
        divergences[-1] = 0.1
    elif state == "disabled":
        divergences[-2:] = [1.0, 1.0]
    request, candidate_id = seed_profile(storage, divergences)
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM execution_plan_exploration"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT paired_observations FROM execution_plan_profile WHERE execution_plan_id = ?",
                (candidate_id,),
            ).fetchone()[0]
            == 20
        )
        if state == "stale":
            # Controlled aging of this fixture's existing samples. Admission
            # hydrates summaries from these retained rows on the next init.
            age_us = (24 * 60 * 60 + 1) * 1_000_000
            for table in (
                "execution_plan_observation",
                "execution_plan_divergence_observation",
            ):
                connection.execute(
                    f"UPDATE {table} SET observed_at = observed_at - ? WHERE execution_plan_id = ?",
                    (age_us, candidate_id),
                )
            connection.commit()
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "joint_guarded")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
    provider = FakeProvider()
    with (
        patch("agentc._lifecycle._apply_patches"),
        patch("agentc._patches._openai._write_root_span"),
        patch("agentc._patches._optimizer_glue.derive_call_site_id", return_value=SITE),
    ):
        agentc.init(storage_path=str(storage))
        try:
            for _ in range(probes):
                result = invoke(provider, request)
                assert result.origin == "reference", (
                    "rejected evidence must remain reference visible"
                )
                drain_exploration(5000)
            if probes == 0:
                result = invoke(provider, request)
                drain_exploration(5000)
                assert result.origin == (
                    "reference" if state == "disabled" else "candidate"
                )
        finally:
            agentc.shutdown()

        # The recovered decision and durable attempt count must survive a
        # full SDK/native lifecycle reset. Accepted evidence stops probing.
        if probes:
            agentc.init(storage_path=str(storage))
            try:
                recovered = invoke(provider, request)
                drain_exploration(5000)
                assert recovered.origin == "candidate"
            finally:
                agentc.shutdown()
    assert len(provider.calls) == (2 * probes + 1 if probes else 1)
    background = [
        call for call in provider.calls if call["thread"] == "agentc-counterfactual"
    ]
    assert len(background) == probes
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        attempts = connection.execute(
            "SELECT execution_plan_id, status, COUNT(*) FROM execution_plan_exploration "
            "GROUP BY execution_plan_id, status"
        ).fetchall()
        profile = connection.execute(
            "SELECT paired_observations, n_paired_observations, divergence_upper_p95 "
            "FROM execution_plan_profile WHERE execution_plan_id = ?",
            (candidate_id,),
        ).fetchone()
        retained = connection.execute(
            "SELECT COUNT(*), MAX(divergence) FROM execution_plan_divergence_observation "
            "WHERE execution_plan_id = ?",
            (candidate_id,),
        ).fetchone()
    assert attempts == ([(candidate_id, "completed", probes)] if probes else [])
    assert profile[:2] == (20 + probes, 20 + probes)
    assert retained == (20 + probes, max(divergences)), (
        "refresh cannot erase adverse samples"
    )
    assert profile[2] == (1.0 if state == "disabled" else 0.0)
