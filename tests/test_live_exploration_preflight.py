"""Regression coverage for the production-adapter exploration preflight."""

import os
import sqlite3
from contextlib import closing
from unittest.mock import patch

import agentc

from bench.live_exploration_preflight import (
    CALIBRATION_PAIRS,
    MAX_CALIBRATION_USER_CALLS,
    MODEL,
    REFERENCE_CAP,
    FakeProvider,
    invoke,
    run,
)


def test_reference_visible_calibration_survives_restart_and_admits_candidate(
    tmp_path,
) -> None:
    artifact = run(tmp_path)
    results = artifact["results"]

    assert artifact["paper_evidence"] is False
    assert artifact["network_calls"] == 0
    assert 3 + CALIBRATION_PAIRS <= results["calibration_user_calls"]
    assert results["calibration_user_calls"] <= MAX_CALIBRATION_USER_CALLS
    assert (
        results["reference_visible_calibration_calls"]
        == results["calibration_user_calls"]
    )
    assert results["background_candidate_calls"] == CALIBRATION_PAIRS
    assert results["persisted_completed_attempts"] == CALIBRATION_PAIRS
    assert results["persisted_incomplete_attempts"] == 0
    assert results["candidate_paired_observations"] == CALIBRATION_PAIRS
    assert results["candidate_divergence_upper_p95"] == 0.0
    assert results["post_restart_candidate_admitted"] is True
    assert (
        results["provider_calls_total"]
        == results["calibration_user_calls"] + CALIBRATION_PAIRS + 1
    )


def test_zero_total_overhead_budget_never_dispatches_counterfactual(tmp_path) -> None:
    storage = tmp_path.resolve()
    provider = FakeProvider()
    request = {
        "model": MODEL,
        "max_tokens": REFERENCE_CAP,
        "temperature": 0,
        "messages": [{"role": "user", "content": "same shaped prompt"}],
    }
    environment = {
        "AGENTC_ENABLED_RULES": "OutputBudget",
        "AGENTC_OPTIMIZE_EXPLORATION": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
    }

    with patch.dict(os.environ, environment, clear=False), patch(
        "agentc._lifecycle._apply_patches"
    ), patch("agentc._patches._openai._write_root_span"):
        agentc.init(storage_path=str(storage))
        try:
            responses = [invoke(provider, request) for _ in range(5)]
        finally:
            agentc.shutdown()

    assert all(response.origin == "reference" for response in responses)
    assert all(call["origin"] == "reference" for call in provider.calls)
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        attempts = connection.execute(
            "SELECT COUNT(*) FROM execution_plan_exploration"
        ).fetchone()[0]
    assert attempts == 0
