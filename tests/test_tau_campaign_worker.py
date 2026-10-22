"""Exact acquisition projection, missing measurements and shadow identities."""

from copy import deepcopy

import pytest

from bench.campaign_accounting import digest
from bench.campaign_transport import CampaignStopped
from bench.joint_campaign import CampaignError, _aggregate_arm, _zero_opportunity_status
from bench.tau_campaign_worker import normalize_tau_cell

REF, CANDIDATE = "a" * 64, "b" * 64


def test_frozen_policy_count_is_unknown_because_it_was_not_enumerated():
    result, calls, request = acquisition()
    calls[0]["candidate_count"] = None
    calls[0]["planning"]["selection_audit"]["search_status"] = (
        "frozen_policy_not_enumerated"
    )
    result["calls_sha256"] = digest(calls)
    record = normalize_tau_cell(result, calls, request)[1]
    assert record["candidate_count"] is None
    assert (
        record["unavailable_metric_reasons"]["candidate_count"]
        == "frozen_policy_not_enumerated"
    )


def test_model_routing_is_not_reported_as_a_semantic_rewrite():
    import json
    from bench.campaign_records import _native_identity

    call = {
        "metadata": {
            "kind": "primary",
            "native_plan_json": json.dumps(
                {
                    "kind": "composed",
                    "rules": [{"rule": "ModelDowngrade"}, {"rule": "OutputBudget"}],
                    "agentc_observation_context": {
                        "key": {"execution_plan_id": CANDIDATE}
                    },
                }
            ),
        },
        "planning": None,
    }
    assert _native_identity(call) == (CANDIDATE, ["OutputBudget"], None)


def acquisition(*, kind="primary", details=True):
    request = {
        "schema_version": 1,
        "campaign_id": "test",
        "stage": "E0",
        "paper_evidence": False,
        "workload_id": "tau",
        "family": "retail",
        "split": "control",
        "task_id": "0",
        "arm": "joint_guarded",
        "repetition": 0,
        "run_seed": 42,
        "schedule_ordinal": 0,
        "network_policy": "loopback_only",
    }
    cell = {
        "task_id": "0",
        "arm": "joint_guarded",
        "repetition": 0,
        "seed": 42,
        "ordinal": 0,
    }
    body = {
        "model": "openai/strong",
        "messages": [{"role": "user", "content": "original"}],
        "max_tokens": 128,
    }
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost": "0.00123",
    }
    if details:
        usage.update(
            prompt_tokens_details={"cached_tokens": 30},
            completion_tokens_details={"reasoning_tokens": 10},
        )
    native = {
        "kind": "rewritten",
        "rule": "OutputBudget",
        "agentc_observation_context": {"key": {"execution_plan_id": CANDIDATE}},
    }
    metadata = {
        "actor": "agent",
        "kind": kind,
        "task_id": "0",
        "arm": "joint_guarded",
        "turn_id": "agent-1",
        "requested_model": "openai/strong",
        "cell_id": digest(cell),
    }
    if kind in {"primary", "exploration"}:
        import json

        metadata["native_plan_json"] = json.dumps(native)
    response = {"id": "provider-actual-id", "usage": usage}
    bill = {
        "id": "call",
        "attempt_id": "attempt",
        "metadata": metadata,
        "reserve_sha256": "c" * 64,
        "usage": usage,
        "model": "openai/strong",
        "cost_usd": "0.00123",
        "latency_ms": 12.345,
        "router_metadata": {"attempt": 1},
        "generation_id": "provider-actual-id",
        "session_id": "owned-session",
    }
    call = {
        "call_id": "call",
        "attempt_id": "attempt",
        "call_index": 0,
        "metadata": metadata,
        "request": body,
        "request_digest": digest(body),
        "response_digest": digest(response),
        "http_receipt": {
            "id": "call",
            "attempt_id": "attempt",
            "reserve_sha256": "c" * 64,
            "response": response,
        },
        "billed_result": bill,
        "planning": {
            "call_site_id": "actual.site",
            "planning_overhead_us": 34.567,
            "planning_timing_scope": "optimizer.plan_call",
            "selection_audit": {
                "schema_version": 1,
                "candidate_count": 4,
                "enumeration_complete": True,
                "reference": {"id": REF},
                "abstention_reason": None,
            },
        },
        "planning_cost_attributed_to_this_call": kind == "primary",
        "candidate_count": 4 if kind == "primary" else None,
    }
    calls = [call]
    result = {
        "record_type": "tau_task_result",
        **cell,
        "cell_id": digest(cell),
        "calls_sha256": digest(calls),
        "provider_call_count": 1,
        "official_task": True,
        "upstream_source_modified": False,
        "synthetic_billing": True,
        "provider_accounting": False,
        "all_call_cost_usd": "0.00123",
        "task_status": "completed",
        "official_score": 0.0,
        "end_to_end_latency_ms": 56.789,
        "latency_scope": "task_cell_with_lifecycle_and_accounting",
        "termination_reason": "user_stop",
        "reward_info": {"reward": 0.0},
        "safety_failure": None,
        "safety_metric_available": False,
        "enrollment_sha256": "d" * 64,
        "official_task_sha256": "e" * 64,
    }
    return result, calls, request


def reseal(result, calls):
    result["calls_sha256"] = digest(calls)


def test_actual_counts_cost_models_and_breakdowns_are_preserved_without_double_counting():
    records = normalize_tau_cell(*acquisition())
    task, call = records
    assert task["official_score"] == 0 and task["safety_failure"] is None
    assert call["execution_plan_id"] == CANDIDATE
    assert call["ordered_rewrites"] == ["OutputBudget"]
    assert call["cost_usd_exact"] == "0.00123" and call["candidate_count"] == 4
    assert call["planning_overhead_us"] == 34.567
    summary = _aggregate_arm([task], [call])
    assert summary["total_tokens"] == 150
    assert summary["cached_input_tokens"] == 30 and summary["reasoning_tokens"] == 10
    assert summary["tool_tokens"] is None and summary["safety_unknown_tasks"] == 1


@pytest.mark.parametrize("kind", ["shadow", "fallback"])
def test_reference_replay_uses_real_reference_identity_not_modified_parent(kind):
    task, call = normalize_tau_cell(*acquisition(kind=kind))
    assert call["execution_plan_id"] == REF
    assert call["ordered_rewrites"] == []
    assert call["candidate_count"] is None and call["planning_overhead_us"] == 0


def test_missing_provider_details_are_unknown_even_for_a_cache_miss():
    records = normalize_tau_cell(*acquisition(details=False))
    call = records[1]
    assert call["cached_input_tokens"] is None and call["reasoning_tokens"] is None
    assert not call["metric_availability"]["cached_input_tokens"]
    assert _aggregate_arm(records[:1], records[1:])["total_tokens"] == 150


@pytest.mark.parametrize(
    "mutation",
    [
        "seed",
        "score_kind",
        "cost",
        "duplicate",
        "foreign_cell",
        "receipt_usage",
        "receipt_bound",
        "bad_total",
        "bad_reasoning",
    ],
)
def test_inconsistent_acquisition_is_rejected_without_republishing_a_score(mutation):
    result, calls, request = acquisition()
    if mutation == "seed":
        result["seed"] = 43
    elif mutation == "score_kind":
        result["record_type"] = "unscored_failure"
    elif mutation == "cost":
        result["all_call_cost_usd"] = "0"
    elif mutation == "duplicate":
        calls.append(deepcopy(calls[0]))
        result["provider_call_count"] = 2
        result["all_call_cost_usd"] = "0.00246"
    elif mutation == "foreign_cell":
        calls[0]["metadata"]["cell_id"] = "f" * 64
    elif mutation == "receipt_usage":
        calls[0]["http_receipt"] = deepcopy(calls[0]["http_receipt"])
        calls[0]["http_receipt"]["response"]["usage"]["prompt_tokens"] = 80
        calls[0]["response_digest"] = digest(calls[0]["http_receipt"]["response"])
    elif mutation == "receipt_bound":
        calls[0]["http_receipt"]["reserve_sha256"] = "wrong"
    elif mutation == "bad_total":
        calls[0]["billed_result"]["usage"]["total_tokens"] = 151
        calls[0]["response_digest"] = digest(calls[0]["http_receipt"]["response"])
    else:
        calls[0]["billed_result"]["usage"]["completion_tokens_details"][
            "reasoning_tokens"
        ] = 51
        calls[0]["response_digest"] = digest(calls[0]["http_receipt"]["response"])
    reseal(result, calls)
    with pytest.raises(CampaignStopped):
        normalize_tau_cell(result, calls, request)


def test_unknown_safety_cannot_publish_a_paper_stage_and_synthetic_billing_cannot_be_promoted():
    result, calls, request = acquisition()
    request.update(stage="C", paper_evidence=True, network_policy="provider_allowed")
    with pytest.raises(CampaignStopped):
        normalize_tau_cell(result, calls, request)
    result.update(synthetic_billing=False, provider_accounting=True)
    with pytest.raises(CampaignError, match="measured task.safety_failure"):
        normalize_tau_cell(result, calls, request)


def test_cold_and_missing_selector_evidence_are_not_zero_opportunity():
    call = normalize_tau_cell(*acquisition())[1]
    assert _zero_opportunity_status([call]) is False
    call.update(candidate_count=1, enumeration_complete=False)
    assert _zero_opportunity_status([call]) is None
    call.update(enumeration_complete=True)
    assert _zero_opportunity_status([call]) is True
    call.update(candidate_count=None)
    assert _zero_opportunity_status([call]) is None
    assert _zero_opportunity_status([]) is None


def test_normalized_task_preserves_its_verified_physical_billing_join():
    result, calls, request = acquisition()
    records = normalize_tau_cell(result, calls, request)
    task = records[0]
    assert task["calls_sha256"] == digest(calls)
    assert task["provider_call_count"] == len(calls)
    assert task["all_call_cost_usd"] == result["all_call_cost_usd"]
