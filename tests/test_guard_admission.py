"""Guard admission cannot turn failed or absent measurements into a threshold."""

from copy import deepcopy
from math import isclose

import pytest

from bench.guard_admission import (
    choose_thresholds,
    detection_probability,
    minimum_sampling_rate,
    sampling_feasibility,
)
from bench.guard_evidence import GuardEvidenceError
from bench.guard_frontier import THRESHOLDS
from bench.policy_calibration import CalibrationError, _validate_guard_admission


def frontier():
    return {
        "record_type": "guard_engineering_frontier",
        "source_stage": "E0",
        "calibration_admission": False,
        "unmeasured_requirements": ["disk_durability", "provider_quality"],
        "family_points": [
            {
                "family": family,
                "rate": 0.02,
                "threshold": threshold,
                "sequences": 10080,
                "harmful_sequences": 5040,
                "benign_sequences": 5040,
                "controller_detection_in_budget_rate": 0.0014,
                "benign_disable_rate": 0.0,
                "modeled_savings_retained_after_shadow": 0.977,
                "modeled_net_after_shadow_usd": "10",
                "damage_p95": 100,
                "damage_cumulative": 10000,
            }
            for family in ("route_then_rewrite", "rewrite_then_route")
            for threshold in THRESHOLDS
        ],
    }


def test_even_ideal_single_sample_detection_cannot_meet_loss_and_cost_bounds():
    result = sampling_feasibility(
        loss_per_call=1, reference_cost_usd=".01", candidate_cost_usd=".001"
    )
    assert isclose(
        result["minimum_rate_for_90pct_detection"], 1 - 0.1 ** (1 / 5), abs_tol=1e-12
    )
    assert result["maximum_rate_retaining_80pct_gross_savings"] == 0.18
    assert isclose(result["detection_probability_at_2pct"], 1 - 0.98**5, abs_tol=1e-12)
    assert result["uniform_sampling_can_meet_both_bounds"] is False


def test_two_sample_exposure_controller_has_a_stricter_detection_requirement():
    result = sampling_feasibility(
        loss_per_call=1,
        reference_cost_usd=".01",
        candidate_cost_usd=".001",
        required_samples=2,
    )
    assert result["minimum_rate_for_90pct_detection"] > 0.58
    assert result["detection_probability_at_2pct"] < 0.004
    assert result["uniform_sampling_can_meet_both_bounds"] is False


def test_lower_loss_or_cheaper_verified_feedback_can_change_feasibility():
    low_loss = sampling_feasibility(
        loss_per_call=0.01, reference_cost_usd=".01", candidate_cost_usd=".001"
    )
    cheap_feedback = sampling_feasibility(
        loss_per_call=1,
        reference_cost_usd=".01",
        candidate_cost_usd=".001",
        verification_cost_usd=".001",
    )
    assert low_loss["uniform_sampling_can_meet_both_bounds"] is True
    assert cheap_feedback["uniform_sampling_can_meet_both_bounds"] is True
    assert cheap_feedback["cost_source"].endswith("not provider billing proof")


def test_probabilities_cover_boundary_and_large_horizons_without_overflow():
    assert detection_probability(0, 1) == 0
    assert detection_probability(5, 0) == 0
    assert detection_probability(5, 1, 2) == 1
    assert isclose(detection_probability(5, 0.5, 2), 26 / 32, abs_tol=1e-12)
    assert 0.6 < detection_probability(10000, 0.0001) < 0.7
    assert minimum_sampling_rate(1, 2) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"loss_per_call": 0},
        {"loss_per_call": True},
        {"loss_per_call": float("nan")},
        {"candidate_cost_usd": ".01"},
        {"reference_cost_usd": "0"},
        {"verification_cost_usd": "-1"},
        {"required_samples": False},
    ],
)
def test_invalid_feasibility_inputs_cannot_supply_permission(changes):
    kwargs = dict(loss_per_call=1, reference_cost_usd=".01", candidate_cost_usd=".001")
    kwargs.update(changes)
    with pytest.raises(GuardEvidenceError):
        sampling_feasibility(**kwargs)


def test_failed_grid_disables_every_family_and_selects_no_threshold():
    decision = choose_thresholds(frontier())
    assert decision["calibration_admission"] is False
    for family in decision["families"].values():
        assert family["selected_threshold"] is None
        assert family["disabled_for_benefit_experiment"] is True
        assert all(
            "harmful_detection_below_90pct" in p["rejection_reasons"]
            for p in family["evaluated_thresholds"]
        )


def test_qualified_engineering_point_uses_net_savings_then_damage_then_threshold():
    data = frontier()
    for p in data["family_points"]:
        p.update(controller_detection_in_budget_rate=0.95, damage_p95=4)
        p["modeled_net_after_shadow_usd"] = (
            "11" if p["threshold"] in {0.1, 0.15} else "10"
        )
    decision = choose_thresholds(data)
    assert all(p["selected_threshold"] == 0.1 for p in decision["families"].values())
    assert all(p["provider_admission"] is False for p in decision["families"].values())
    with pytest.raises(CalibrationError):
        _validate_guard_admission(decision, {})


def test_opposite_orders_cannot_borrow_a_qualifying_family_point():
    data = frontier()
    for p in data["family_points"]:
        if p["family"] == "route_then_rewrite":
            p["controller_detection_in_budget_rate"] = 0.95
    decision = choose_thresholds(data)
    assert decision["families"]["route_then_rewrite"]["selected_threshold"] == 0.05
    assert decision["families"]["rewrite_then_route"]["selected_threshold"] is None


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "stage", "benign", "retention", "nonfinite", "empty"],
)
def test_grid_rejection_and_denominators_remain_explicit(mutation):
    data = frontier()
    if mutation == "missing":
        data["family_points"].pop()
    elif mutation == "duplicate":
        data["family_points"].append(deepcopy(data["family_points"][0]))
    elif mutation == "stage":
        data["source_stage"] = "C"
    elif mutation == "benign":
        data["family_points"][0]["benign_sequences"] = 0
    elif mutation == "retention":
        data["family_points"][0]["modeled_savings_retained_after_shadow"] = None
        assert (
            "savings_retention_below_80pct"
            in choose_thresholds(data)["families"]["route_then_rewrite"][
                "evaluated_thresholds"
            ][0]["rejection_reasons"]
        )
        return
    elif mutation == "nonfinite":
        data["family_points"][0]["controller_detection_in_budget_rate"] = float("nan")
    else:
        data["family_points"].clear()
    with pytest.raises(GuardEvidenceError):
        choose_thresholds(data)


def validation_model(**changes):
    from bench.guard_admission import per_call_validation_feasibility

    values = dict(
        harmful_signal_probability=0.6,
        benign_signal_probability=0.001,
        verification_cost_usd=".001",
        reference_cost_usd=".01",
        candidate_cost_usd=".001",
    )
    values.update(changes)
    return per_call_validation_feasibility(**values)


def test_every_call_validation_preserves_shadow_cost_and_remains_engineering_only():
    result = validation_model()
    assert result["shadow_rate"] == 0.02
    assert result["necessary_model_constraints_pass"]
    assert result["optimistic_detection_in_budget_probability"] >= 0.90
    assert result["optimistic_benign_disable_probability"] <= 0.05
    assert result["maximum_per_call_verification_cost_usd"] == "0.0016"
    assert 0.57 < result["minimum_validator_harmful_signal_probability"] < 0.59
    assert (
        result["calibration_admission"] is False and result["paper_evidence"] is False
    )
    with pytest.raises(CalibrationError):
        _validate_guard_admission(result, {})


def test_every_call_false_signals_accumulate_despite_low_per_call_error():
    result = validation_model(benign_signal_probability=0.01)
    assert result["optimistic_benign_disable_probability"] > 0.26
    assert result["necessary_model_constraints_pass"] is False
    assert result["rejection_reasons"] == ["benign_disables_above_5pct"]
    assert 0 < result["maximum_validator_benign_signal_probability"] < 0.004


def test_cheap_validation_must_pay_for_both_its_calls_and_reference_samples():
    result = validation_model(verification_cost_usd=".0017")
    assert result["rejection_reasons"] == ["savings_retention_below_80pct"]
    exhausted = validation_model(candidate_cost_usd=".0095", verification_cost_usd="0")
    assert float(exhausted["maximum_per_call_verification_cost_usd"]) < 0
    assert exhausted["necessary_model_constraints_pass"] is False


def test_zero_useful_validator_signal_recovers_the_existing_sampling_failure():
    result = validation_model(
        harmful_signal_probability=0,
        benign_signal_probability=0,
        verification_cost_usd=0,
    )
    assert isclose(
        result["optimistic_detection_in_budget_probability"],
        detection_probability(5, 0.02, 2),
        abs_tol=1e-12,
    )
    assert result["rejection_reasons"] == ["harmful_detection_below_90pct"]


@pytest.mark.parametrize(
    "changes",
    [
        {"harmful_signal_probability": True},
        {"benign_signal_probability": float("nan")},
        {"shadow_rate": 1.1},
        {"benign_calls": False},
        {"benign_calls": 0},
        {"required_signals": 0},
        {"required_signals": True},
        {"loss_per_call": 0},
        {"verification_cost_usd": "-1"},
        {"candidate_cost_usd": ".02"},
    ],
)
def test_invalid_per_call_validation_models_cannot_supply_admission(changes):
    with pytest.raises(GuardEvidenceError):
        validation_model(**changes)


@pytest.mark.parametrize("cost", [None, True, "not-money", "NaN", "Infinity"])
def test_malformed_validator_cost_is_explicitly_rejected(cost):
    with pytest.raises(GuardEvidenceError):
        validation_model(verification_cost_usd=cost)
