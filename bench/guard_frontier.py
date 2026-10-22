"""Score native controller traces against predeclared engineering outcomes.

Fixture quality is scored from answer bytes. Costs are modeled, never billed.
Controller decisions are distinct from durable, provider-backed admissions.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from decimal import Decimal

from bench.campaign_accounting import digest
from bench.guard_evidence import GuardEvidenceError, require, strict_json

RATES = (0.0, 0.01, 0.02, 0.05, 0.10, 1.0)
THRESHOLDS = (0.05, 0.10, 0.15, 0.20, 0.30, 0.50)
MASK = (1 << 64) - 1


def sampling_draws(seed, count):
    require(type(seed) is int and 0 <= seed <= MASK, "invalid replay seed")
    for _ in range(count):
        seed = (seed + 0x9E3779B97F4A7C15) & MASK
        value = ((seed ^ (seed >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK
        value ^= value >> 31
        yield (value >> 11) / (1 << 53)


def fixture_quality(expected, outputs):
    require(
        len(expected) == len(outputs) and bool(expected), "incomplete fixture answers"
    )
    scores = []
    for answer, output in zip(expected, outputs):
        require(
            type(answer) is int and isinstance(output, str), "invalid fixture answer"
        )
        found = re.findall(r"\bANSWER:([+-]?\d+)\b", output)
        scores.append(len(found) == 1 and int(found[0]) == answer)
    return sum(scores) / len(scores)


def replay_key(scenario, contract, rate, threshold):
    return {
        "call_site_version": digest(
            {
                "schema_version": 1,
                "call_site_id": "guard-replay:" + scenario["scenario_id"],
                "prompt_shape_version": "guard-arithmetic-fixture-v1",
                "provider_protocol": scenario["runtime_version"]["provider_protocol"],
                "tool_schema_version": None,
                "application_config_version": f"engineered-replay:{scenario['family']}:{contract['divergence_mode']}:{rate:g}:{threshold:g}",
            }
        ),
        "execution_plan_id": scenario["execution_plan_id"],
    }


def score_sequence(record, scenario, contract, ordinal):
    require(
        record["record_type"] == "native_guard_replay_sequence",
        "wrong native replay record",
    )
    require(
        record["scenario_id"] == scenario["scenario_id"]
        and record["family"] == scenario["family"],
        "foreign replay scenario",
    )
    horizon = contract["horizon"]
    n = record["executed_candidate_calls"]
    require(
        type(n) is int
        and 1 <= n <= horizon
        and record["remaining_reference_calls"] == horizon - n,
        "incomplete execution horizon",
    )
    require(
        record["rate"] in RATES and record["threshold"] in THRESHOLDS,
        "unregistered operating point",
    )
    require(
        record["key"]
        == replay_key(scenario, contract, record["rate"], record["threshold"]),
        "replay has a different complete-plan identity",
    )
    expected_seed = (
        contract["seed"] + ordinal * 1_000_003 + record["repetition"]
    ) & MASK
    require(
        record["rng_seed"] == expected_seed, "replay resampled its predeclared seed"
    )
    sampled = [
        i
        for i, value in enumerate(sampling_draws(expected_seed, n), 1)
        if value < record["rate"]
    ]
    failed = [
        i
        for i in sampled
        if scenario["reference_error_every"]
        and i % scenario["reference_error_every"] == 0
    ]
    require(
        record["sampled_calls"] == sampled
        and record["failed_reference_calls"] == failed,
        "native sampling differs from predeclared Bernoulli draws",
    )
    events = [i for i in sampled if i not in set(failed)]
    require(len(record["feedback"]) == len(events), "incomplete native guard feedback")
    exposure, disabled = 0.0, None
    for call, observed in zip(events, record["feedback"]):
        divergence = (
            scenario["divergence_before"]
            if call < scenario["change_at_call"]
            else scenario["divergence_after"]
        )
        exposure += max(0, divergence - record["threshold"])
        require(
            observed[0] == call
            and observed[1] == divergence
            and math.isclose(observed[2], exposure, rel_tol=0, abs_tol=1e-12),
            "native exposure differs from actual sampled divergence",
        )
        if exposure >= 1.0 and disabled is None:
            disabled = call
    require(
        record["disabled_at_call"] == disabled
        and record["final_blocks_user_visible"] is (disabled is not None),
        "native disable differs from replayed exposure",
    )
    require(
        (disabled is None and n == horizon) or disabled == n,
        "candidate executed after a disable or stopped early",
    )
    require(
        record["disabled_at_us"]
        == (None if disabled is None else 1_000_000 + disabled * 1_000_000),
        "wrong disable timestamp",
    )
    require(
        record["warm_paired_observations"] == 20
        and record["warm_divergence_upper_p95"] == 0.0,
        "replay did not start from an admitted benign window",
    )
    require(
        record["durable_disable_measured"] is False,
        "in-memory replay cannot certify durable decisions",
    )
    reference_quality = fixture_quality(
        scenario["expected_answers"], scenario["reference_outputs"]
    )
    before = fixture_quality(
        scenario["expected_answers"], scenario["candidate_before_outputs"]
    )
    after = fixture_quality(
        scenario["expected_answers"], scenario["candidate_after_outputs"]
    )
    first = min(n, scenario["change_at_call"] - 1)
    damage = first * max(0, reference_quality - before) + (n - first) * max(
        0, reference_quality - after
    )
    harmful = before < reference_quality or after < reference_quality
    prices = contract["cost_model"]
    baseline = Decimal(prices["reference_usd"]) * horizon
    primary = Decimal(prices["candidate_usd"]) * n + Decimal(
        prices["reference_usd"]
    ) * (horizon - n)
    shadow = Decimal(prices["reference_usd"]) * len(sampled)
    learning = (
        Decimal(prices["candidate_usd"]) + Decimal(prices["reference_usd"])
    ) * 20
    gross = baseline - primary
    delay = (
        None if disabled is None else max(0, disabled - scenario["change_at_call"] + 1)
    )
    return {
        "harmful": harmful,
        "disabled": disabled is not None,
        "controller_caught_in_budget": harmful
        and disabled is not None
        and damage <= 5.0,
        "damage": damage,
        "calls_to_disable": delay,
        "virtual_time_to_disable_ms": None if delay is None else delay * 1000,
        "modeled_cost_to_disable_usd": None
        if disabled is None
        else str(Decimal(prices["candidate_usd"]) * n + shadow),
        "modeled_baseline_usd": str(baseline),
        "modeled_primary_usd": str(primary),
        "modeled_shadow_usd": str(shadow),
        "modeled_learning_usd": str(learning),
        "modeled_all_cost_usd": str(primary + shadow + learning),
        "modeled_gross_savings_usd": str(gross),
        "modeled_net_after_shadow_usd": str(gross - shadow),
        "modeled_net_after_learning_usd": str(gross - shadow - learning),
        "candidate_calls": n,
        "sampled_calls": len(sampled),
        "failed_reference_calls": len(failed),
    }


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def summarize(rows):
    harmful = [r for r in rows if r["harmful"]]
    benign = [r for r in rows if not r["harmful"]]
    money_fields = [
        k
        for k in rows[0]
        if k.startswith("modeled_") and k != "modeled_cost_to_disable_usd"
    ]
    totals = {k: sum((Decimal(r[k]) for r in rows), Decimal(0)) for k in money_fields}
    damage = [r["damage"] for r in harmful]
    delays = [
        r["calls_to_disable"] for r in harmful if r["calls_to_disable"] is not None
    ]
    times = [
        r["virtual_time_to_disable_ms"]
        for r in harmful
        if r["virtual_time_to_disable_ms"] is not None
    ]
    costs = [
        Decimal(r["modeled_cost_to_disable_usd"])
        for r in harmful
        if r["modeled_cost_to_disable_usd"] is not None
    ]
    gross = totals["modeled_gross_savings_usd"]
    return {
        "sequences": len(rows),
        "harmful_sequences": len(harmful),
        "benign_sequences": len(benign),
        "controller_caught_in_budget": sum(
            r["controller_caught_in_budget"] for r in harmful
        ),
        "controller_detection_in_budget_rate": None
        if not harmful
        else sum(r["controller_caught_in_budget"] for r in harmful) / len(harmful),
        "missed_harm_rate": None
        if not harmful
        else sum(not r["controller_caught_in_budget"] for r in harmful) / len(harmful),
        "benign_disable_rate": None
        if not benign
        else sum(r["disabled"] for r in benign) / len(benign),
        "damage_p50": percentile(damage, 0.50),
        "damage_p95": percentile(damage, 0.95),
        "damage_p99": percentile(damage, 0.99),
        "damage_cumulative": math.fsum(damage),
        "calls_to_disable_p50": percentile(delays, 0.50),
        "calls_to_disable_p95": percentile(delays, 0.95),
        "calls_to_disable_p99": percentile(delays, 0.99),
        "virtual_time_to_disable_ms_p50": percentile(times, 0.50),
        "virtual_time_to_disable_ms_p95": percentile(times, 0.95),
        "virtual_time_to_disable_ms_p99": percentile(times, 0.99),
        "modeled_cost_to_disable_usd_p50": str(percentile(costs, 0.50))
        if costs
        else None,
        "modeled_cost_to_disable_usd_p95": str(percentile(costs, 0.95))
        if costs
        else None,
        "modeled_cost_to_disable_usd_p99": str(percentile(costs, 0.99))
        if costs
        else None,
        "undisabled_harmful_sequences": sum(not r["disabled"] for r in harmful),
        "delay_percentile_scope": "detected_sequences_only; undisabled counts reported separately",
        "modeled_savings_retained_after_shadow": None
        if gross <= 0
        else float(totals["modeled_net_after_shadow_usd"] / gross),
        **{k: str(v) for k, v in totals.items()},
        "physical_provider_calls": 0,
        "durable_damage_contract_verified": False,
    }


def analyze(native_path, fixture):
    contract = fixture["contract"]
    require(
        contract["rates"] == list(RATES) and contract["thresholds"] == list(THRESHOLDS),
        "replay changed the frozen frontier grid",
    )
    scenarios = fixture["scenarios"]
    require(
        len({s["scenario_id"] for s in scenarios}) == len(scenarios),
        "duplicate replay scenario",
    )
    expected = (
        (threshold, rate, ordinal, repetition)
        for threshold in THRESHOLDS
        for rate in RATES
        for ordinal in range(len(scenarios))
        for repetition in range(contract["repetitions"])
    )
    groups, by_scenario, by_family = (
        defaultdict(list),
        defaultdict(list),
        defaultdict(list),
    )
    count = 0
    with native_path.open() as stream:
        for line in stream:
            try:
                threshold, rate, ordinal, repetition = next(expected)
            except StopIteration as error:
                raise GuardEvidenceError(
                    "native replay has unexpected extra sequences"
                ) from error
            record = strict_json(line)
            require(
                (record["threshold"], record["rate"], record["repetition"])
                == (threshold, rate, repetition),
                "native replay omitted, reordered or duplicated a sequence",
            )
            measured = score_sequence(record, scenarios[ordinal], contract, ordinal)
            groups[(threshold, rate)].append(measured)
            by_family[(threshold, rate, scenarios[ordinal]["family"])].append(measured)
            by_scenario[(threshold, rate, scenarios[ordinal]["scenario_id"])].append(
                measured
            )
            count += 1
    require(next(expected, None) is None, "native replay is incomplete")
    return {
        "schema_version": 1,
        "record_type": "guard_engineering_frontier",
        "source_stage": "E0",
        "synthetic_quality_fixtures": True,
        "provider_billing": False,
        "paper_evidence": False,
        "calibration_admission": False,
        "durable_damage_contract_verified": False,
        "sequence_count": count,
        "fixture_sha256": digest(fixture),
        "operating_points": [
            {"threshold": threshold, "rate": rate, **summarize(rows)}
            for (threshold, rate), rows in groups.items()
        ],
        "scenario_points": [
            {
                "threshold": threshold,
                "rate": rate,
                "scenario_id": scenario,
                **summarize(rows),
            }
            for (threshold, rate, scenario), rows in by_scenario.items()
        ],
        "family_points": [
            {"threshold": threshold, "rate": rate, "family": family, **summarize(rows)}
            for (threshold, rate, family), rows in by_family.items()
        ],
        "unmeasured_requirements": [
            "provider_backed_task_safety",
            "disk_durability_timing",
            "real_provider_cost_and_latency",
            "full_planner_admission_and_drift",
        ],
    }
