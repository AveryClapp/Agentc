"""Engineering frontier accounting preserves quality, feedback and censoring."""

import json
from copy import deepcopy

import pytest

from bench.guard_evidence import GuardEvidenceError
from bench.guard_frontier import (
    RATES,
    THRESHOLDS,
    analyze,
    fixture_quality,
    replay_key,
    sampling_draws,
    score_sequence,
    summarize,
)


def fixture(*, change=1, reference_errors=None, horizon=100):
    scenario = {
        "scenario_id": "explicit-unit-oracle",
        "family": "best_static_joint",
        "expected_answers": [7],
        "reference_outputs": ["ANSWER:7"],
        "candidate_before_outputs": ["ANSWER:7"],
        "candidate_after_outputs": ["ANSWER:8"],
        "divergence_before": 0.0,
        "divergence_after": 1.0,
        "change_at_call": change,
        "reference_error_every": reference_errors,
        "execution_plan_id": "a" * 64,
        "runtime_version": {"provider_protocol": "unit"},
    }
    contract = {
        "horizon": horizon,
        "seed": 7,
        "divergence_mode": "lexical",
        "repetitions": 1,
        "rates": list(RATES),
        "thresholds": list(THRESHOLDS),
        "cost_model": {"reference_usd": "0.01", "candidate_usd": "0.001"},
    }
    return scenario, contract


def unit_row(scenario, contract, *, rate=1.0, threshold=0.05):
    # Unit native-shaped records; actual native replay is separately verified.
    sampled, failed, feedback = [], [], []
    exposure, disabled, n = 0.0, None, 0
    for call, value in enumerate(
        sampling_draws(contract["seed"], contract["horizon"]), 1
    ):
        n = call
        if value >= rate:
            continue
        sampled.append(call)
        if (
            scenario["reference_error_every"]
            and call % scenario["reference_error_every"] == 0
        ):
            failed.append(call)
            continue
        divergence = (
            scenario["divergence_before"]
            if call < scenario["change_at_call"]
            else scenario["divergence_after"]
        )
        exposure += max(0, divergence - threshold)
        feedback.append([call, divergence, exposure])
        if exposure >= 1:
            disabled = call
            break
    return {
        "record_type": "native_guard_replay_sequence",
        "scenario_id": scenario["scenario_id"],
        "family": scenario["family"],
        "repetition": 0,
        "rate": rate,
        "threshold": threshold,
        "rng_seed": contract["seed"],
        "key": replay_key(scenario, contract, rate, threshold),
        "executed_candidate_calls": n,
        "remaining_reference_calls": contract["horizon"] - n,
        "sampled_calls": sampled,
        "failed_reference_calls": failed,
        "feedback": feedback,
        "disabled_at_call": disabled,
        "disabled_at_us": None
        if disabled is None
        else 1_000_000 + disabled * 1_000_000,
        "final_blocks_user_visible": disabled is not None,
        "warm_paired_observations": 20,
        "warm_divergence_upper_p95": 0.0,
        "durable_disable_measured": False,
    }


def test_quality_labels_come_from_answer_bytes_not_a_supplied_safe_flag():
    assert fixture_quality([7, 8], ["correct prose ANSWER:7", "ANSWER:9"]) == 0.5
    assert fixture_quality([7], ["ANSWER:7 ANSWER:8"]) == 0
    assert fixture_quality([7], ["missing answer"]) == 0


def test_disable_limits_executed_damage_and_preserves_all_cost_components():
    scenario, contract = fixture()
    result = score_sequence(unit_row(scenario, contract), scenario, contract, 0)
    assert result["damage"] == 2
    assert result["controller_caught_in_budget"] is True
    assert result["modeled_primary_usd"] == "0.982"
    assert result["modeled_shadow_usd"] == "0.02"
    assert result["modeled_learning_usd"] == "0.220"
    assert result["modeled_all_cost_usd"] == "1.222"
    assert result["modeled_cost_to_disable_usd"] == "0.022"
    summary = summarize([result])
    assert summary["durable_damage_contract_verified"] is False
    assert summary["physical_provider_calls"] == 0
    assert summary["modeled_savings_retained_after_shadow"] < 0


def test_unobserved_harm_remains_in_denominator_and_delay_is_censored():
    scenario, contract = fixture()
    result = score_sequence(
        unit_row(scenario, contract, rate=0.0), scenario, contract, 0
    )
    summary = summarize([result])
    assert summary["controller_detection_in_budget_rate"] == 0
    assert summary["missed_harm_rate"] == 1
    assert summary["undisabled_harmful_sequences"] == 1
    assert summary["calls_to_disable_p95"] is None
    assert summary["damage_p95"] == 100


def test_semantic_change_delay_starts_at_changed_output_and_keeps_prior_costs():
    scenario, contract = fixture(change=26)
    result = score_sequence(unit_row(scenario, contract), scenario, contract, 0)
    assert result["damage"] == 2
    assert result["calls_to_disable"] == 2
    assert result["virtual_time_to_disable_ms"] == 2000
    assert result["modeled_cost_to_disable_usd"] == "0.297"


def test_reference_errors_consume_modeled_cost_and_supply_no_divergence():
    scenario, contract = fixture(reference_errors=1)
    result = score_sequence(unit_row(scenario, contract), scenario, contract, 0)
    assert result["failed_reference_calls"] == 100
    assert result["damage"] == 100
    assert result["modeled_shadow_usd"] == "1.00"
    assert result["disabled"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "sampling",
        "feedback",
        "cost-free-error",
        "seed",
        "early-stop",
        "timestamp",
        "durability",
        "warm",
        "grid",
    ],
)
def test_tampered_native_replay_cannot_supply_measurement(mutation):
    scenario, contract = fixture()
    row = unit_row(scenario, contract)
    if mutation == "sampling":
        row["sampled_calls"].pop()
    elif mutation == "feedback":
        row["feedback"][0][2] = 0
    elif mutation == "cost-free-error":
        row["failed_reference_calls"] = [1]
    elif mutation == "seed":
        row["rng_seed"] += 1
    elif mutation == "early-stop":
        row["executed_candidate_calls"] = 3
        row["remaining_reference_calls"] = 97
    elif mutation == "timestamp":
        row["disabled_at_us"] += 1
    elif mutation == "durability":
        row["durable_disable_measured"] = True
    elif mutation == "warm":
        row["warm_paired_observations"] = 19
    else:
        row["rate"] = 0.03
    with pytest.raises(GuardEvidenceError):
        score_sequence(row, scenario, contract, 0)


def test_complete_frontier_is_counterbalanced_and_keeps_fixture_scope(tmp_path):
    scenario, contract = fixture(horizon=2)
    rows = [
        unit_row(scenario, contract, rate=rate, threshold=threshold)
        for threshold in THRESHOLDS
        for rate in RATES
    ]
    path = tmp_path / "native.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    report = analyze(path, {"contract": contract, "scenarios": [scenario]})
    assert report["sequence_count"] == 36
    assert report["calibration_admission"] is False
    assert report["provider_billing"] is False
    assert len(report["family_points"]) == 36
    assert len(report["operating_points"]) == 36


@pytest.mark.parametrize("mutation", ["omit", "extra", "reorder", "duplicate", "label"])
def test_partial_or_relabelled_matrix_is_rejected(tmp_path, mutation):
    scenario, contract = fixture(horizon=2)
    rows = [
        unit_row(scenario, contract, rate=rate, threshold=threshold)
        for threshold in THRESHOLDS
        for rate in RATES
    ]
    if mutation == "omit":
        rows.pop()
    elif mutation == "extra":
        rows.append(deepcopy(rows[-1]))
    elif mutation == "reorder":
        rows[0], rows[1] = rows[1], rows[0]
    elif mutation == "duplicate":
        rows[1] = deepcopy(rows[0])
    else:
        rows[0]["scenario_id"] = "another-oracle"
    path = tmp_path / "native.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(GuardEvidenceError):
        analyze(path, {"contract": contract, "scenarios": [scenario]})
