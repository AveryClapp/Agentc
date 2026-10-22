"""Derive guard operating-point choices and sampling feasibility from evidence.

Engineering replay decisions never authorize provider calibration. This module
keeps the detection requirement, verification cost and observed frontier together.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from bench.campaign_accounting import digest
from bench.guard_evidence import GuardEvidenceError, require
from bench.guard_frontier import RATES, THRESHOLDS

VERSION = "guard-operating-point-v1"
RECALL = 0.90
MAX_FALSE_DISABLE = 0.05
RETAINED_SAVINGS = 0.80
DAMAGE_BUDGET = 5.0


def finite_fraction(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1


def detection_probability(calls, rate, required_samples=1):
    """Binomial probability of enough informative samples before damage exceeds 5."""
    require(type(calls) is int and calls >= 0, "invalid available call count")
    require(
        type(required_samples) is int and required_samples > 0,
        "invalid detection sample count",
    )
    require(finite_fraction(rate), "invalid sampling rate")
    if calls < required_samples or rate == 0:
        return 0.0
    if rate == 1:
        return 1.0
    # Accumulate the smaller tail directly to avoid cancellation at small rates.
    normalizer = math.lgamma(calls + 1)
    probability = math.fsum(
        math.exp(
            normalizer
            - math.lgamma(k + 1)
            - math.lgamma(calls - k + 1)
            + k * math.log(rate)
            + (calls - k) * math.log1p(-rate)
        )
        for k in range(required_samples, calls + 1)
    )
    return min(1.0, max(0.0, probability))


def minimum_sampling_rate(calls, required_samples=1):
    require(
        type(calls) is int
        and calls >= 0
        and type(required_samples) is int
        and required_samples > 0,
        "invalid detection horizon",
    )
    if calls < required_samples:
        return None
    low, high = 0.0, 1.0
    for _ in range(80):
        middle = (low + high) / 2
        if detection_probability(calls, middle, required_samples) >= RECALL:
            high = middle
        else:
            low = middle
    return high


def sampling_feasibility(
    *,
    loss_per_call,
    reference_cost_usd,
    candidate_cost_usd,
    verification_cost_usd=None,
    required_samples=1,
):
    """Necessary bound for uniform post-execution sampling, with perfect feedback.

    It grants immediate correct classification and no feedback errors, latency,
    drift, restart overhead or learning costs. Failure under these optimistic
    assumptions rules out merely lowering the divergence threshold.
    """
    require(
        type(loss_per_call) in {int, float}
        and math.isfinite(loss_per_call)
        and 0 < loss_per_call <= 1,
        "invalid normalized per-call loss",
    )
    prices = [
        Decimal(str(v))
        for v in (
            reference_cost_usd,
            candidate_cost_usd,
            reference_cost_usd
            if verification_cost_usd is None
            else verification_cost_usd,
        )
    ]
    require(
        all(p.is_finite() and p >= 0 for p in prices) and prices[0] > 0,
        "invalid verification cost model",
    )
    reference, candidate, verification = prices
    require(reference > candidate, "candidate has no gross savings")
    calls = int(
        (Decimal(str(DAMAGE_BUDGET)) / Decimal(str(loss_per_call))).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    require(calls <= 10_000, "loss bound requires a larger numerical solver")
    minimum = minimum_sampling_rate(calls, required_samples)
    maximum = (
        1.0
        if verification == 0
        else float(
            min(
                Decimal(1),
                (Decimal(1) - Decimal(str(RETAINED_SAVINGS)))
                * (reference - candidate)
                / verification,
            )
        )
    )
    return {
        "record_type": "uniform_sampling_feasibility",
        "version": VERSION,
        "loss_per_call": loss_per_call,
        "calls_before_exceeding_damage_budget": calls,
        "required_informative_samples": required_samples,
        "minimum_rate_for_90pct_detection": minimum,
        "maximum_rate_retaining_80pct_gross_savings": maximum,
        "detection_probability_at_2pct": detection_probability(
            calls, 0.02, required_samples
        ),
        "best_detection_probability_within_savings_budget": detection_probability(
            calls, maximum, required_samples
        ),
        "uniform_sampling_can_meet_both_bounds": minimum is not None
        and minimum <= maximum,
        "assumptions": [
            "constant declared loss and prices",
            "independent Bernoulli sampling",
            "perfect immediate informative feedback",
            "no reference failures, delay or learning overhead",
        ],
        "cost_source": "caller-declared prices; not provider billing proof",
    }


def per_call_validation_feasibility(
    *,
    harmful_signal_probability,
    benign_signal_probability,
    verification_cost_usd,
    reference_cost_usd,
    candidate_cost_usd,
    loss_per_call=1.0,
    shadow_rate=0.02,
    benign_calls=100,
    required_signals=2,
):
    """Necessary accuracy/cost bounds for an additional check on every call.

    The existing Bernoulli reference channel remains present. This optimistic
    model grants it perfect feedback and treats validator errors as independent
    across calls. Declared classifier rates are never calibration permission.
    """
    require(
        all(
            finite_fraction(value)
            for value in (
                harmful_signal_probability,
                benign_signal_probability,
                shadow_rate,
            )
        ),
        "invalid validation signal probability",
    )
    require(
        type(benign_calls) is int and 0 < benign_calls <= 10_000,
        "invalid benign validation horizon",
    )
    require(
        type(loss_per_call) in {int, float}
        and math.isfinite(loss_per_call)
        and 0 < loss_per_call <= 1,
        "invalid normalized per-call loss",
    )
    require(
        type(required_signals) is int and required_signals > 0,
        "invalid detection signal count",
    )
    try:
        reference, candidate, verification = (
            Decimal(str(value))
            for value in (
                reference_cost_usd,
                candidate_cost_usd,
                verification_cost_usd,
            )
        )
    except (InvalidOperation, ValueError, TypeError) as error:
        raise GuardEvidenceError("invalid validation cost model") from error
    require(
        all(
            value.is_finite() and value >= 0
            for value in (reference, candidate, verification)
        )
        and reference > candidate,
        "invalid validation cost model",
    )
    calls = int(
        (Decimal(str(DAMAGE_BUDGET)) / Decimal(str(loss_per_call))).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    require(calls <= 10_000, "loss bound requires a larger numerical solver")
    combined_signal = shadow_rate + (1 - shadow_rate) * harmful_signal_probability
    detection = detection_probability(calls, combined_signal, required_signals)
    false_disable = detection_probability(
        benign_calls, benign_signal_probability, required_signals
    )
    minimum_combined = minimum_sampling_rate(calls, required_signals)
    minimum_validator = (
        None
        if minimum_combined is None
        else 0.0
        if shadow_rate == 1
        else max(0.0, (minimum_combined - shadow_rate) / (1 - shadow_rate))
    )
    low, high = 0.0, 1.0
    for _ in range(80):
        middle = (low + high) / 2
        if (
            detection_probability(benign_calls, middle, required_signals)
            <= MAX_FALSE_DISABLE
        ):
            low = middle
        else:
            high = middle
    gross = reference - candidate
    shadow_cost = Decimal(str(shadow_rate)) * reference
    maximum_verification = (
        Decimal(1) - Decimal(str(RETAINED_SAVINGS))
    ) * gross - shadow_cost
    retention = Decimal(1) - (shadow_cost + verification) / gross
    reasons = []
    if detection < RECALL:
        reasons.append("harmful_detection_below_90pct")
    if false_disable > MAX_FALSE_DISABLE:
        reasons.append("benign_disables_above_5pct")
    if retention < Decimal(str(RETAINED_SAVINGS)):
        reasons.append("savings_retention_below_80pct")
    return {
        "schema_version": 1,
        "record_type": "per_call_validation_feasibility",
        "version": "per-call-validation-feasibility-v1",
        "source_stage": "E0",
        "shadow_rate": shadow_rate,
        "required_informative_signals": required_signals,
        "calls_before_exceeding_damage_budget": calls,
        "benign_calls": benign_calls,
        "harmful_signal_probability": harmful_signal_probability,
        "benign_signal_probability": benign_signal_probability,
        "optimistic_detection_in_budget_probability": detection,
        "optimistic_benign_disable_probability": false_disable,
        "minimum_validator_harmful_signal_probability": minimum_validator,
        "maximum_validator_benign_signal_probability": low,
        "maximum_per_call_verification_cost_usd": format(maximum_verification, "f"),
        "modeled_savings_retained": format(retention, "f"),
        "necessary_model_constraints_pass": not reasons,
        "rejection_reasons": reasons,
        "calibration_admission": False,
        "paper_evidence": False,
        "unverified_requirements": [
            "actual informative signal strength and controller signal count",
            "measured classifier accuracy on independent held-out scenarios",
            "correlated errors, reference failures and feedback delays",
            "full native planner, durable disable, restart and model drift",
            "provider billing, fallback, learning and resource costs",
        ],
    }


def choose_thresholds(frontier, *, rate=0.02):
    """Apply the frozen frontier criteria per family; never choose a failed point."""
    require(
        frontier.get("record_type") == "guard_engineering_frontier"
        and frontier.get("source_stage") == "E0"
        and frontier.get("calibration_admission") is False,
        "unsupported frontier evidence contract",
    )
    require(finite_fraction(rate) and rate in RATES, "unregistered shadow rate")
    points = [p for p in frontier["family_points"] if p["rate"] == rate]
    families = sorted({p["family"] for p in points})
    require(bool(families), "missing guard family measurements")
    decisions = {}
    for family in families:
        values = [p for p in points if p["family"] == family]
        require(
            len(values) == len(THRESHOLDS)
            and {p["threshold"] for p in values} == set(THRESHOLDS),
            "incomplete or duplicated threshold grid",
        )
        candidates, evaluated = [], []
        for point in values:
            require(
                type(point["sequences"]) is int
                and point["sequences"] > 0
                and type(point["harmful_sequences"]) is int
                and point["harmful_sequences"] > 0
                and type(point["benign_sequences"]) is int
                and point["benign_sequences"] > 0,
                "missing harmful or benign denominator",
            )
            require(
                finite_fraction(point["controller_detection_in_budget_rate"])
                and finite_fraction(point["benign_disable_rate"])
                and (
                    point["modeled_savings_retained_after_shadow"] is None
                    or type(point["modeled_savings_retained_after_shadow"])
                    in {int, float}
                    and math.isfinite(point["modeled_savings_retained_after_shadow"])
                ),
                "invalid measured guard rates",
            )
            reasons = []
            if point["controller_detection_in_budget_rate"] < RECALL:
                reasons.append("harmful_detection_below_90pct")
            if point["benign_disable_rate"] > MAX_FALSE_DISABLE:
                reasons.append("benign_disables_above_5pct")
            if (
                point["modeled_savings_retained_after_shadow"] is None
                or point["modeled_savings_retained_after_shadow"] < RETAINED_SAVINGS
            ):
                reasons.append("savings_retention_below_80pct")
            eligible = not reasons
            if eligible:
                candidates.append(point)
            evaluated.append(
                {
                    "threshold": point["threshold"],
                    "engineering_point_qualifies": eligible,
                    "rejection_reasons": reasons,
                }
            )
        selected = (
            min(
                candidates,
                key=lambda p: (
                    -Decimal(p["modeled_net_after_shadow_usd"]),
                    p["damage_cumulative"],
                    p["threshold"],
                ),
            )
            if candidates
            else None
        )
        decisions[family] = {
            "selected_threshold": None if selected is None else selected["threshold"],
            "disabled_for_benefit_experiment": selected is None,
            "evaluated_thresholds": evaluated,
            "provider_admission": False,
            "unverified_requirements": frontier["unmeasured_requirements"],
        }
    return {
        "schema_version": 1,
        "record_type": "guard_threshold_decision",
        "version": VERSION,
        "source_stage": "E0",
        "source_frontier_sha256": digest(frontier),
        "shadow_rate": rate,
        "families": decisions,
        "calibration_admission": False,
        "paper_evidence": False,
    }
