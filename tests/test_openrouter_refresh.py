from copy import deepcopy

import pytest

from bench.openrouter_pilot import PilotError
from bench.openrouter_refresh import PATCH_FILES, compare, validate_patch


def test_only_exact_reviewed_patch_surface_is_permitted():
    original = {p: "old" for p in PATCH_FILES | {"Cargo.lock"}}
    patched = {p: "new" if p in PATCH_FILES else "old" for p in original}
    validate_patch(original, patched)
    for invalid in (original, {**patched, "Cargo.lock": "changed"}, {**patched, "extra": "x"}):
        with pytest.raises(PilotError):
            validate_patch(original, invalid)


def trajectory():
    return {"policy": "p", "context": "natural", "settings": {"threshold": ".05"}, "revealed_calls": 1,
        "decisions": [{"task_id": "t", "phase": "calibration", "primary_row_id": "r",
            "nominal_uncached_cost_estimate_usd": "1", "native_plan": {"opaque_time": 1}}]}


def test_comparison_ignores_opaque_tokens_but_detects_paid_feedback_changes():
    original = trajectory()
    patched = deepcopy(original)
    patched["decisions"][0]["native_plan"] = {"opaque_time": 2}
    assert compare([original], [patched])[0]["different_behavior_or_cost_decisions"] == 0
    patched["decisions"][0]["nominal_uncached_cost_estimate_usd"] = "1.25"
    patched["revealed_calls"] = 2
    report = compare([original], [patched])[0]
    assert report["different_behavior_or_cost_decisions"] == 1
    assert report["different_primary_outcome_decisions"] == 0
    assert report["patched_minus_original_nominal_cost_usd"] == "0.25"


@pytest.mark.parametrize("mutation", ["setting", "chronology", "duplicate", "missing"])
def test_comparison_rejects_nonmatched_design(mutation):
    original = trajectory()
    patched = deepcopy(original)
    target = [patched]
    if mutation == "setting":
        patched["settings"]["threshold"] = "1"
    elif mutation == "chronology":
        patched["decisions"][0]["task_id"] = "other"
    elif mutation == "duplicate":
        target.append(deepcopy(patched))
    else:
        target.clear()
    with pytest.raises(PilotError):
        compare([original], target)
