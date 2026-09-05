from copy import deepcopy

import pytest

from bench.openrouter_auto_analysis import analyze, validate_auto
from bench.openrouter_auto import request_for
from bench.openrouter_frontier import CONTEXTS, SOURCE_MODEL
from bench.openrouter_frontier_analysis import calibrate
from bench.openrouter_pilot import PilotError, digest


def sample():
    endpoint = {"provider_name": "Anthropic", "name": "Anthropic | endpoint-model", "tag": "anthropic",
                "pricing": {"prompt": ".000003", "completion": ".000015"}}
    item = {"context": "natural", "task_id": "a", "phase": "holdout"}
    manifest = {"endpoints": {SOURCE_MODEL: endpoint}, "allowed_models": [SOURCE_MODEL], "provider_only": ["anthropic"],
                "contract": "reinforced", "schedule": [item]}
    tasks = {"natural": {"a": {"task_id": "a", "prompt": "Who?", "expected": "Ada", "meta": {"paragraphs": []}}}}
    stage = "auto-default-v1-" + digest(manifest)[:20]
    payload = request_for(manifest, tasks["natural"]["a"])
    row = {**item, "id": stage + "-00000", "stage": stage, "model": SOURCE_MODEL, "provider": "Anthropic",
        "generation_id": "g1", "paper_evidence": False, "optimizer": "none", "arm": "full", "requested_model": "openrouter/auto",
        "provider_tag_requested_for_model": "anthropic", "service_tier_reported": None,
        "router_metadata": {"requested": "openrouter/auto", "strategy": "auto", "attempt": 1, "is_byok": False,
            "endpoints": {"available": [{"model": "endpoint-model", "provider": "Anthropic", "selected": True}]}},
        "cost_usd": ".000045", "usage": {"cost": .000045, "prompt_tokens": 10, "completion_tokens": 1},
        "nominal_uncached_cost_usd": ".000045", "expected": "Ada", "answer": "Ada", "em": 1., "f1": 1.,
        "request_sha256": digest(payload), "fingerprint": digest({"payload": payload,
            "metadata": {**item, "manifest_sha256": digest(manifest), "purpose": "bounded_default_auto_service"}, "stage": stage})}
    return manifest, [row], tasks


@pytest.mark.parametrize("field,value", [("task_id", "other"), ("id", "other"), ("generation_id", ""),
    ("optimizer", "native"), ("arm", "compress"), ("request_sha256", "bad"), ("fingerprint", "bad"),
    ("answer", "wrong"), ("expected", "wrong"), ("f1", 0.), ("nominal_uncached_cost_usd", ".1"),
    ("service_tier_reported", "priority"), ("provider_tag_requested_for_model", "other")])
def test_auto_artifact_validation_checks_requests_attribution_and_scores(field, value):
    manifest, rows, tasks = sample()
    assert validate_auto(manifest, rows, tasks) == rows
    changed = deepcopy(rows)
    changed[0][field] = value
    with pytest.raises(PilotError):
        validate_auto(manifest, changed, tasks)


def test_auto_comparison_keeps_paired_losses_and_charges_static_training():
    manifest, _, _ = sample()
    frontier = {"endpoints": {SOURCE_MODEL: {}, "cheap": {}}, "calibration_tasks": 1,
                "policy_replay": {"risk_margin": .02}}
    manifest.update(frontier_manifest_sha256=digest(frontier), limitations=[])
    rows, auto_rows = [], []
    tasks = {c: {} for c in CONTEXTS}
    for context in CONTEXTS:
        for phase in ("warmup", "calibration", "holdout"):
            task_id = phase
            tasks[context][task_id] = {"prompt": "Who?", "meta": {"paragraphs": []}}
            for model in frontier["endpoints"]:
                for arm in (("full",) if phase == "warmup" else ("full", "compress")):
                    price = "1" if model == SOURCE_MODEL else ".1"
                    rows.append({"context": context, "phase": phase, "task_id": task_id, "model": model, "arm": arm,
                        "f1": 1., "em": 1., "cost_usd": price, "nominal_uncached_cost_usd": price,
                        "usage": {"prompt_tokens": 10}, "native_plan": {"kind": "pass_through"},
                        "request_sha256": f"{context}/{phase}/{model}/{arm}"})
            auto_rows.append({"context": context, "phase": phase, "task_id": task_id, "model": "cheap",
                "f1": 0., "em": 0., "cost_usd": ".1", "nominal_uncached_cost_usd": ".1",
                "usage": {"prompt_tokens": 10}, "request_sha256": f"auto/{context}/{phase}"})
    report = analyze(manifest, auto_rows, frontier, rows, calibrate(frontier, rows), tasks)
    assert len(report["comparisons"]) == 6
    for c in report["comparisons"]:
        assert c["auto_total_calls"] == 3
        assert float(c["auto_total_nominal_uncached_cost_usd"]) == pytest.approx(.3)
        assert c["strict_em_losses"] == 1
        assert c["f1_delta"] == -1
        assert c["treatment_rewrites"] == 0
        if c["reference"] == "source_only":
            assert c["reference_setup_calls"] == 2
        elif c["reference"] == "calibrated_fixed_model":
            assert c["reference_setup_calls"] == 3
        else:
            assert c["reference_setup_calls"] == 5
