"""Typed uncached-price counterfactual follows documented override boundaries."""
from copy import deepcopy
from decimal import Decimal

import pytest

from bench.openrouter_rules_live import nominal_uncached_cost


MANIFEST = {
    "workload": "typed_relational_v1",
    "endpoints": {"source": {"pricing": {
        "prompt": "0.000003", "completion": "0.000015",
        "overrides": [{"min_prompt_tokens": 200000, "prompt": "0.000006", "completion": "0.0000225"}],
    }}},
}


@pytest.mark.parametrize("tokens,expected", [
    (0, "0.00015"), (199999, "0.600147"),
    (200000, "0.600150"), (200001, "1.200231"), (1_000_000, "6.000225"),
])
def test_strict_greater_than_threshold_and_entire_request_rates(tokens, expected):
    usage = {"prompt_tokens": tokens, "completion_tokens": 10}
    assert nominal_uncached_cost(MANIFEST, "source", usage) == Decimal(expected)


def test_last_matching_override_wins_per_key_not_sorted_thresholds():
    manifest = deepcopy(MANIFEST)
    manifest["endpoints"]["source"]["pricing"]["overrides"] += [
        {"min_prompt_tokens": 250000, "completion": "0.000025"},
        {"min_prompt_tokens": 100000, "prompt": "0.000005"},
    ]
    assert nominal_uncached_cost(manifest, "source", {"prompt_tokens": 300000, "completion_tokens": 10}) == Decimal("1.50025")


@pytest.mark.parametrize("workload", [None, "structured_relational_v1"])
def test_legacy_base_rate_counterfactual_unchanged(workload):
    manifest = deepcopy(MANIFEST)
    manifest["workload"] = workload
    assert nominal_uncached_cost(manifest, "source", {"prompt_tokens": 200001, "completion_tokens": 10}) == Decimal("0.600153")


def test_nominal_counterfactual_does_not_use_or_modify_actual_bill():
    usage = {"prompt_tokens": 200001, "completion_tokens": 0, "cost": "0.123"}
    before = deepcopy(usage)
    assert nominal_uncached_cost(MANIFEST, "source", usage) == Decimal("1.200006")
    assert usage == before
