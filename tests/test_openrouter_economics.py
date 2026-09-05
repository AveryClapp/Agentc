from copy import deepcopy

import pytest

from bench.openrouter_economics import amortization, cache_latency, paired_repeats, quantile
from bench.openrouter_frontier import SOURCE_MODEL
from bench.openrouter_pilot import PilotError
from test_openrouter_mechanisms import matrix


def rows_with_costs():
    manifest, rows, _ = matrix()
    for row in rows:
        row.update(cached_input_tokens=None, usage={"prompt_tokens": 100},
            latency_ms=10., cost_usd="1")
    return manifest, rows


def test_cache_missingness_is_not_a_zero_hit_and_difference_is_signed():
    _, rows = rows_with_costs()
    group = [deepcopy(rows[0]) for _ in range(3)]
    group[0].update(cached_input_tokens=80, usage={"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}})
    group[1].update(cached_input_tokens=0, usage={"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}})
    group[2]["cost_usd"] = "2"
    report = cache_latency(group)[0]
    assert report["cache_accounting_known_calls"] == 2
    assert report["cache_accounting_missing_calls"] == 1
    assert report["cache_hit_calls"] == 1
    assert report["cached_fraction_among_known_input_tokens"] == .4
    assert report["nominal_minus_billed_usd"] == "-1"


@pytest.mark.parametrize("cached", [True, -1, 101, .5])
def test_cache_invalid_counters_rejected(cached):
    _, rows = rows_with_costs()
    rows[0].update(cached_input_tokens=cached, usage={"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": cached}})
    with pytest.raises(PilotError, match="cache"):
        cache_latency(rows)


def test_exact_payload_repeat_variability_is_separate_from_rewrite_effect():
    manifest, rows = rows_with_costs()
    for row in rows:
        if row["arm"] == "compress":
            row["request_sha256"] = row["request_sha256"].replace("compress", "full")
            row.update(f1=.5, answer="other", latency_ms=15.)
    reports = paired_repeats(manifest, rows)
    assert len(reports) == 4
    assert all(r["identical_payload"] and r["f1_losses"] == 2 for r in reports)
    assert all(r["mean_request_latency_delta_ms"] == 5 for r in reports)
    assert all(r["request_latency_delta_paired_bootstrap_95_ms"] == [5, 5] for r in reports)


def test_quantile_definition_and_invalid_samples():
    assert quantile([9, 1, 2, 8], .5) == 2
    assert quantile([9, 1, 2, 8], .95) == 9
    for values in ([], [float("nan")], [True]):
        with pytest.raises(PilotError):
            quantile(values, .5)


def test_amortization_charges_all_calibration_and_same_baseline_prefix():
    _, heldout = rows_with_costs()
    rows = deepcopy(heldout)
    for row in heldout:
        cal = deepcopy(row)
        cal["phase"] = "calibration"
        rows.append(cal)
    for row in rows:
        if row["model"] == "cheap":
            row["nominal_uncached_cost_usd"] = ".5"
    lock = {"controls": [{"name": "fixed", "context": "natural", "selected": {"model": "cheap", "arm": "full"},
        "candidates": [{"model": SOURCE_MODEL, "arm": "full"}, {"model": "cheap", "arm": "full"}]}]}
    result = amortization(rows, lock)[0]
    assert result["baseline_setup_calls"] == 2
    assert result["policy_setup_calls"] == 4
    assert result["incremental_setup_nominal_usd"] == "1.0"
    assert result["projected_break_even_post_calibration_tasks"] == 2
    assert result["setup_inclusive_nominal_cost_reduction"] == 0
    for row in rows:
        row["nominal_uncached_cost_usd"] = "1"
    assert amortization(rows, lock)[0]["projected_break_even_post_calibration_tasks"] is None
    rows.pop(0)
    with pytest.raises(PilotError, match="paired"):
        amortization(rows, lock)
