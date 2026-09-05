from copy import deepcopy

import pytest

from bench.openrouter_pilot import PilotError, digest
from bench.openrouter_refresh import PATCH_FILES, compare, validate_baseline, validate_patch


def test_only_exact_reviewed_patch_surface_is_permitted():
    original = {p: "old" for p in PATCH_FILES | {"Cargo.lock"}}
    patched = {p: "new" if p in PATCH_FILES else "old" for p in original}
    validate_patch(original, patched)
    for invalid in (original, {**patched, "Cargo.lock": "changed"}, {**patched, "extra": "x"}):
        with pytest.raises(PilotError):
            validate_patch(original, invalid)


@pytest.mark.parametrize("field,value", [("replay_source_sha256", "wrong"), ("replay_source_sha256", None),
    ("paper_evidence", True), ("paper_evidence", None), ("evaluation_kind", "other"),
    ("calibration_only", 1), ("restart_after_calibration", 0)])
def test_baseline_requires_exact_replay_source_and_original_evidence_scope(field, value):
    manifest, rows = {"source": "frozen"}, [{"id": "measured"}]
    frozen = {"analysis_source_files": {"bench/openrouter_replay.py": "reviewed"}}
    baseline = {"manifest_sha256": digest(manifest), "consumed_rows_sha256": digest(rows),
        "calibration_only": True, "restart_after_calibration": False, "paper_evidence": False,
        "evaluation_kind": "offline_selected_feedback_replay", "replay_source_sha256": "reviewed"}
    validate_baseline(baseline, frozen, manifest, rows, True, False)
    if value is None:
        baseline.pop(field)
    else:
        baseline[field] = value
    with pytest.raises(PilotError, match="provenance"):
        validate_baseline(baseline, frozen, manifest, rows, True, False)


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
