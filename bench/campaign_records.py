"""Shared joins and measurements for owned workload acquisition.

Copy actual native handles and actual receipts; fixed dispatch identities have
an explicit workload namespace. Missing provider and planner metrics stay null.
"""

from __future__ import annotations

import json
from bench.campaign_accounting import digest, money
from bench.campaign_chat import PARAMETERS
from bench.campaign_transport import CampaignStopped


def calibration_metadata_json(value):
    """Freeze optional trial provenance before any provider dispatch."""
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"calibration_trial_id", "calibration_authorization_sha256"}
        or not isinstance(value["calibration_trial_id"], str)
        or not value["calibration_trial_id"]
        or not isinstance(value["calibration_authorization_sha256"], str)
        or len(value["calibration_authorization_sha256"]) != 64
        or any(
            c not in "0123456789abcdef"
            for c in value["calibration_authorization_sha256"]
        )
    ):
        raise CampaignStopped("invalid calibration receipt provenance")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cell_calls(
    snapshot, *, session_id: str, cell_id: str, plans: list[dict]
) -> list[dict]:
    """Join actual reservation, HTTP receipt and billed result, never infer costs."""
    claims = [
        event
        for event in snapshot.events
        if event["event"] == "reserve"
        and event.get("session_id") == session_id
        and event.get("metadata", {}).get("cell_id") == cell_id
    ]
    plan_by_turn = {event["turn_id"]: event for event in plans}
    if len(plan_by_turn) != len(plans):
        raise CampaignStopped("multiple planning outcomes for one owned turn")
    calls = []
    attributed_turns = set()
    for index, claim in enumerate(claims):
        attempt = claim["attempt_id"]
        responses = [
            event
            for event in snapshot.events
            if event["event"] == "response" and event.get("attempt_id") == attempt
        ]
        results = [
            event
            for event in snapshot.events
            if event["event"] == "result" and event.get("attempt_id") == attempt
        ]
        if len(responses) != 1 or len(results) != 1:
            raise CampaignStopped("owned call lacks one complete owned billing receipt")
        receipt, result = responses[0], results[0]
        metadata = claim["metadata"]
        plan = plan_by_turn.get(metadata["turn_id"])
        attribute_planning = (
            plan is not None
            and metadata["kind"] in {"primary", "fallback"}
            and metadata["turn_id"] not in attributed_turns
        )
        if attribute_planning:
            attributed_turns.add(metadata["turn_id"])
        audit = plan.get("selection_audit") if plan is not None else None
        count = (
            audit.get("candidate_count")
            if isinstance(audit, dict) and attribute_planning
            else None
        )
        calls.append(
            {
                "call_index": index,
                "call_id": claim["id"],
                "attempt_id": attempt,
                "metadata": metadata,
                "request": claim["request"],
                "request_digest": digest(claim["request"]),
                "http_receipt": receipt,
                "response_digest": digest(receipt["response"]),
                "billed_result": result,
                "planning": plan,
                "planning_cost_attributed_to_this_call": attribute_planning,
                "candidate_count": count,
                "candidate_count_available": type(count) is int,
            }
        )
    if not calls:
        raise CampaignStopped("official task has no owned provider calls")
    return calls


def _detail(usage: dict, group: str, field: str, bound: int) -> int | None:
    details = usage.get(group)
    if details is None:
        return None
    if not isinstance(details, dict):
        raise CampaignStopped("provider token detail envelope is invalid")
    value = details.get(field)
    if value is not None and (type(value) is not int or not 0 <= value <= bound):
        raise CampaignStopped("provider token detail count is invalid")
    return value


def _native_identity(call: dict) -> tuple[str | None, list[str], dict | None]:
    metadata = call["metadata"]
    kind = metadata["kind"]
    raw = (
        metadata.get("native_plan_json") if kind in {"primary", "exploration"} else None
    )
    plan = json.loads(raw) if raw is not None else None
    planning = call.get("planning")
    audit = planning.get("selection_audit") if planning is not None else None
    if kind in {"shadow", "fallback"}:
        reference = audit.get("reference") if isinstance(audit, dict) else None
        identity = reference.get("id") if isinstance(reference, dict) else None
        return identity, [], audit
    if not isinstance(plan, dict):
        return None, [], audit
    context = plan.get("agentc_observation_context", {})
    identity = context.get("key", {}).get("execution_plan_id")
    if plan["kind"] == "composed":
        rewrites = [rule["rule"] for rule in plan["rules"]]
    elif plan["kind"] == "rewritten":
        rewrites = [plan["rule"]]
    else:
        rewrites = []
    rewrites = [rule for rule in rewrites if rule != "ModelDowngrade"]
    return identity, rewrites, audit


def _fixed_identity(call: dict, schema: str) -> str:
    request = call["request"]
    return digest(
        {
            "schema_version": schema,
            "provider_protocol": "litellm.completion.v1",
            "actor": call["metadata"]["actor"],
            "requested_model": call["metadata"]["requested_model"],
            "selected_model": request["model"],
            "parameters": {
                name: request[name] for name in PARAMETERS if name in request
            },
            "ordered_rewrites": [],
            **{
                name: call["metadata"][name]
                for name in (
                    "text_action_comparator_version",
                    "text_action_settings_version",
                )
                if name in call["metadata"]
            },
            "provider_response_cache": False,
        }
    )


def normalize_owned_calls(
    calls: list[dict],
    request: dict,
    *,
    cell_id: str,
    optimized_arms: frozenset[str],
    fixed_schema: str,
    call_site_prefix: str,
) -> list[dict]:
    records = []
    identities = set()
    for call in calls:
        metadata, bill, receipt = (
            call["metadata"],
            call["billed_result"],
            call["http_receipt"],
        )
        identity = (call["call_id"], call["attempt_id"])
        if (
            identity in identities
            or metadata.get("cell_id") != cell_id
            or metadata.get("task_id") != request["task_id"]
            or metadata.get("arm") != request["arm"]
            or bill.get("metadata") != metadata
            or bill.get("id") != call["call_id"]
            or receipt.get("id") != call["call_id"]
            or any(
                value.get("attempt_id") != call["attempt_id"]
                for value in (bill, receipt)
            )
            or bill.get("reserve_sha256") != receipt.get("reserve_sha256")
            or call["request_digest"] != digest(call["request"])
            or call["response_digest"] != digest(receipt["response"])
            or bill["model"] != call["request"]["model"]
            or bill["usage"] != receipt["response"]["usage"]
            or money(bill["cost_usd"]) != money(receipt["response"]["usage"]["cost"])
        ):
            raise CampaignStopped("physical call lacks consistent owned attribution")
        identities.add(identity)
        usage = bill["usage"]
        total = usage["prompt_tokens"] + usage["completion_tokens"]
        if "total_tokens" in usage and (
            type(usage["total_tokens"]) is not int or usage["total_tokens"] != total
        ):
            raise CampaignStopped("provider total contradicts inclusive token counts")
        cached = _detail(
            usage, "prompt_tokens_details", "cached_tokens", usage["prompt_tokens"]
        )
        reasoning = _detail(
            usage,
            "completion_tokens_details",
            "reasoning_tokens",
            usage["completion_tokens"],
        )
        plan_id, rewrites, audit = _native_identity(call)
        kind = metadata["kind"]
        planning = call.get("planning")
        attributed = call["planning_cost_attributed_to_this_call"]
        count = call["candidate_count"]
        availability = {
            "cached_input_tokens": cached is not None,
            "reasoning_tokens": reasoning is not None,
            "tool_tokens": False,
            "candidate_count": count is not None,
        }
        reasons = {
            field: "provider_did_not_report"
            for field in ("cached_input_tokens", "reasoning_tokens", "tool_tokens")
            if not availability[field]
        }
        if count is None:
            reasons["candidate_count"] = (
                "counterfactual_inherits_primary"
                if kind in {"exploration", "shadow"}
                else audit["abstention_reason"]
                if isinstance(audit, dict) and audit.get("abstention_reason")
                else "frozen_policy_not_enumerated"
                if isinstance(audit, dict)
                and audit.get("search_status") == "frozen_policy_not_enumerated"
                else "not_planned"
            )
        eligible = (metadata["actor"] == "agent" and request["arm"] in optimized_arms
                    and kind != "validation")
        records.append(
            {
                "schema_version": 1,
                "record_type": "call",
                "call_index": call["call_index"],
                "requested_model": metadata["requested_model"],
                "selected_model": bill["model"],
                "call_site_id": planning["call_site_id"]
                if planning is not None
                else call_site_prefix + metadata["actor"],
                "execution_plan_id": plan_id
                if plan_id is not None
                else _fixed_identity(call, fixed_schema),
                "execution_plan_id_source": "native_execution_plan_v1"
                if plan_id is not None
                else "fixed_dispatch_policy_v1",
                "ordered_rewrites": rewrites,
                "input_tokens": usage["prompt_tokens"],
                "output_tokens": usage["completion_tokens"],
                "cached_input_tokens": cached,
                "reasoning_tokens": reasoning,
                "tool_tokens": None,
                "total_tokens": total,
                "token_count_semantics": "provider_prompt_completion_inclusive_v1",
                "retry_count": bill["router_metadata"]["attempt"] - 1,
                "candidate_count": count,
                "metric_availability": availability,
                "unavailable_metric_reasons": reasons,
                "enumeration_complete": audit.get("enumeration_complete")
                if isinstance(audit, dict)
                else None,
                "cost_usd": float(money(bill["cost_usd"])),
                "cost_usd_exact": bill["cost_usd"],
                "request_latency_ms": bill["latency_ms"],
                "planning_overhead_us": planning["planning_overhead_us"]
                if attributed
                else 0.0,
                "planning_timing_scope": planning["planning_timing_scope"]
                if attributed
                else "no_planning_attributed_to_this_invocation",
                "eligible": eligible,
                "is_exploration": kind == "exploration",
                "is_shadow": kind == "shadow",
                "is_validation": kind == "validation",
                "failed": False,
                "dispatch_fallback": kind == "fallback",
                "abstention_reason": audit.get("abstention_reason")
                if attributed and isinstance(audit, dict)
                else None,
                "request_digest": call["request_digest"],
                "response_digest": call["response_digest"],
                "actor": metadata["actor"],
                "dispatch_kind": kind,
                "network_calls": 1,
                "provider_usage": usage,
                "provider_generation_id": bill["generation_id"],
                "cell_id": cell_id,
                "attempt_id": call["attempt_id"],
                "transport_session_id": bill["session_id"],
                "selection_audit": audit,
            }
        )
        if (
            "calibration_trial_id" in metadata
            or "calibration_authorization_sha256" in metadata
        ):
            value = {
                k: metadata.get(k)
                for k in ("calibration_trial_id", "calibration_authorization_sha256")
            }
            records[-1].update(json.loads(calibration_metadata_json(value)))
        if "judge_purpose" in metadata or "judge_evidence_sha256" in metadata:
            from bench.campaign_chat import validate_metadata

            validate_metadata(metadata)
            records[-1].update(
                {k: metadata[k] for k in ("judge_purpose", "judge_evidence_sha256")}
            )
    return records
