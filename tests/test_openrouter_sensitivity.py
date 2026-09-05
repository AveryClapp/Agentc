from copy import deepcopy

from bench.openrouter_frontier import policy_specs
from bench.openrouter_sensitivity import THRESHOLDS, comparable, grid


def test_grid_changes_only_named_tolerance_and_never_mutates_primary():
    manifest = {"policy_replay": {"specs": policy_specs()}}
    original = deepcopy(manifest)
    policies = grid(manifest)
    assert manifest == original
    assert len(policies) == 30
    assert len({p["name"] for p in policies}) == 30
    for threshold in THRESHOLDS:
        subset = [p for p in policies if p["lexical_threshold"] == threshold]
        assert len(subset) == 6
        for candidate, baseline in zip(subset, original["policy_replay"]["specs"]):
            expected = dict(baseline["settings"], AGENTC_SHADOW_DIVERGENCE_BUDGET=threshold)
            assert candidate["settings"] == expected
            assert candidate["primary_policy_name"] == baseline["name"]


def test_behavior_comparison_keeps_revelations_quality_independent_feedback_and_cost():
    trajectory = {"decisions": [{"task_id": "a", "native_plan": {"opaque_token": "one"},
        "revealed": [{"row_id": "r", "scope": "exploration"}], "divergence_feedback": .5,
        "observed_billed_cost_noncausal_usd": ".01"}]}
    changed = deepcopy(trajectory)
    changed["decisions"][0]["native_plan"] = {"opaque_token": "two"}
    assert comparable(trajectory) == comparable(changed)
    changed["decisions"][0]["observed_billed_cost_noncausal_usd"] = ".02"
    assert comparable(trajectory) != comparable(changed)
