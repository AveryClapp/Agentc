"""Exercise cold-plan allocation through the real native runtime, without a provider."""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from agentc import _native
from agentc._patches._optimizer_glue import build_call_dict_openai


@pytest.fixture(autouse=True)
def isolated_optimizer(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _native.optimize_reset()
    for key, value in {
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_COMPOSE": "1",
        "AGENTC_ENABLED_RULES": "OutputBudget,ModelDowngrade",
        "AGENTC_OPTIMIZE_EXPLORATION": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "50",
        "AGENTC_OPTIMIZE_SHADOW": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTC_EVAL_PLANNER_MODE", raising=False)
    monkeypatch.delenv("AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION", raising=False)
    yield
    _native.optimize_reset()


def call_and_outcome() -> tuple[dict, dict]:
    call = build_call_dict_openai(
        {
            "model": "gpt-4o-2024-11-20",
            "messages": [{"role": "user", "content": "original user instruction"}],
            "max_tokens": 1024,
            "temperature": 0,
        },
        call_site_id="native.exploration.allocation",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
    )
    outcome = {
        "call_site_id": call["call_site_id"],
        "model": call["model"],
        "input_tokens": 100,
        "output_tokens": 100,
        "latency_ms": 100.0,
        "cost_usd": 0.01,
        "cost_source": "provider_reported",
    }
    return call, outcome


def warm(call: dict, outcome: dict) -> None:
    for _ in range(3):
        plan = _native.optimize_plan(json.dumps(call))
        assert json.loads(plan)["kind"] == "pass_through"
        _native.optimize_observe(plan, json.dumps(outcome))


@pytest.mark.parametrize("allocation", [None, "least_evidence"])
def test_twenty_probes_reach_one_exact_plan_only_with_completion_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allocation: str | None
) -> None:
    if allocation is not None:
        monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION", allocation)
    storage = tmp_path / ".agentc"
    _native.optimize_configure(str(storage))
    call, outcome = call_and_outcome()
    warm(call, outcome)
    candidate_ids: list[str] = []
    reference_id: str | None = None
    for index in range(20):
        if index == 10:
            _native.optimize_reset()
            _native.optimize_configure(str(storage))
        plan_json = _native.optimize_plan(json.dumps(call))
        plan = json.loads(plan_json)
        assert plan["kind"] == "pass_through", (
            "cold outputs must remain reference visible"
        )
        current_reference = plan["agentc_observation_context"]["key"][
            "execution_plan_id"
        ]
        reference_id = reference_id or current_reference
        assert current_reference == reference_id
        _native.optimize_observe(plan_json, json.dumps(outcome))
        exploration = plan["agentc_exploration_context"]
        candidate = exploration["candidate_plan"]
        candidate_id = candidate["agentc_observation_context"]["key"][
            "execution_plan_id"
        ]
        candidate_ids.append(candidate_id)
        # Deterministic fixture feedback tests acquisition/admission wiring;
        # these costs and zero divergence are not workload efficacy evidence.
        candidate_outcome = dict(
            outcome, model=candidate["call"]["model"], cost_usd=0.001
        )
        assert _native.optimize_complete_exploration(
            exploration["lease_token"], json.dumps(candidate_outcome), 0.0
        )
    _native.optimize_reset()
    _native.optimize_configure(str(storage))
    next_plan = json.loads(_native.optimize_plan(json.dumps(call)))
    assert "agentc_exploration_context" not in next_plan, (
        "restart must preserve the cap"
    )
    _native.optimize_flush()
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        attempts = connection.execute(
            "SELECT status, COUNT(*) FROM execution_plan_exploration GROUP BY status"
        ).fetchall()
        pairs = connection.execute(
            "SELECT execution_plan_id, paired_observations FROM execution_plan_profile "
            "WHERE execution_plan_id != ?",
            (reference_id,),
        ).fetchall()
    assert attempts == [("completed", 20)]
    assert sum(count for _, count in pairs) == 20
    if allocation is None:
        assert len(set(candidate_ids)) == 1
        assert pairs == [(candidate_ids[0], 20)]
        assert next_plan["kind"] in {"rewritten", "composed"}
        assert (
            next_plan["agentc_observation_context"]["key"]["execution_plan_id"]
            == candidate_ids[0]
        )
    else:
        assert len(set(candidate_ids)) > 1
        assert max(count for _, count in pairs) < 20
        assert next_plan["kind"] == "pass_through"


def test_routing_lease_round_trip_is_exact_and_one_ulp_tampering_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTC_ENABLED_RULES", "ModelDowngrade")
    storage = tmp_path / ".agentc"
    _native.optimize_configure(str(storage))
    call, outcome = call_and_outcome()
    warm(call, outcome)
    plan = json.loads(_native.optimize_plan(json.dumps(call)))
    exploration = plan["agentc_exploration_context"]
    token = json.loads(exploration["lease_token"])
    lease = token["lease"]
    expected = struct.unpack("f", struct.pack("f", 0.03))[0]
    assert lease["divergence_threshold"] == expected
    assert token["context"]["divergence_threshold"] == expected
    candidate_outcome = dict(
        outcome, model=exploration["candidate_plan"]["call"]["model"]
    )
    lease["divergence_threshold"] = math.nextafter(expected, math.inf)
    assert not _native.optimize_complete_exploration(
        json.dumps(token), json.dumps(candidate_outcome), 0.0
    )
    for _ in range(2):
        assert _native.optimize_complete_exploration(
            exploration["lease_token"], json.dumps(candidate_outcome), 0.0
        )
    _native.optimize_reset()
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        durable = connection.execute(
            "SELECT divergence_threshold FROM execution_plan_exploration "
            "WHERE call_site_version = ? AND exploration_sequence = ?",
            (lease["key"]["call_site_version"], lease["sequence"]),
        ).fetchone()[0]
        assert durable == expected
        assert (
            connection.execute(
                "SELECT SUM(paired_observations) FROM execution_plan_profile"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute(
            "SELECT status, COUNT(*) FROM execution_plan_exploration GROUP BY status"
        ).fetchall() == [("completed", 1)]


def test_invalid_allocation_disables_optional_probes_without_losing_reference_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION", "misspelled-policy")
    storage = tmp_path / ".agentc"
    _native.optimize_configure(str(storage))
    call, outcome = call_and_outcome()
    warm(call, outcome)
    plan = json.loads(_native.optimize_plan(json.dumps(call)))
    assert plan["kind"] == "pass_through"
    assert "agentc_observation_context" in plan
    assert "agentc_exploration_context" not in plan
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM execution_plan_exploration"
            ).fetchone()[0]
            == 0
        )


def test_allocation_is_resolved_once_per_optimizer_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / ".agentc"
    _native.optimize_configure(str(storage))
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION", "invalid")
    call, outcome = call_and_outcome()
    warm(call, outcome)
    plan = json.loads(_native.optimize_plan(json.dumps(call)))
    assert "agentc_exploration_context" in plan
    assert _native.optimize_fail_exploration(
        plan["agentc_exploration_context"]["lease_token"]
    )
    _native.optimize_configure(str(storage))
    after_reconfigure = json.loads(_native.optimize_plan(json.dumps(call)))
    assert after_reconfigure["kind"] == "pass_through"
    assert "agentc_exploration_context" not in after_reconfigure
