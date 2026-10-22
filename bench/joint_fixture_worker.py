"""Deterministic no-network worker for exercising ``bench.joint_campaign``.

This worker is an engineering fixture, never paper evidence.  It exists so a
clean checkout can test scheduling, arm isolation, raw-record validation, and
selection-valid analysis without API keys or heavyweight upstream workloads.
It intentionally includes task-dependent model/rewrite interactions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import time
from pathlib import Path
from typing import Any, Mapping, Sequence, cast


SCHEMA_VERSION = 1
JsonObject = dict[str, Any]


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_request(path: Path) -> JsonObject:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return cast(JsonObject, value)


def _complexity(task_id: str) -> float:
    digest = hashlib.sha256(f"fixture-complexity\0{task_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def _policy(arm: str, complexity: float) -> tuple[str, list[str], str | None, int]:
    """Return target class, rewrites, abstention reason, and candidate count."""
    if arm in {"unmodified_fixed_strong", "trace_only_fixed_strong"}:
        return ("strong", [], None, 1)
    if arm == "fixed_cheap":
        return ("cheap", [], None, 1)
    if arm == "routing_only":
        return ("cheap" if complexity < 0.62 else "strong", [], None, 2)
    if arm == "rewrite_only_fixed_strong":
        return ("strong", ["ContextCompress"], None, 2)
    if arm == "best_static_joint":
        return ("cheap", ["ContextCompress"], None, 1)
    if arm == "route_then_rewrite":
        return (
            "cheap" if complexity < 0.62 else "strong",
            ["ContextCompress"],
            None,
            2,
        )
    if arm == "rewrite_then_route":
        return (
            "cheap" if complexity < 0.76 else "strong",
            ["ContextCompress"],
            None,
            2,
        )
    if arm == "current_greedy":
        return ("cheap", ["ContextCompress", "OutputBudget"], None, 4)
    if arm == "joint_guarded":
        if complexity >= 0.82:
            return ("strong", [], "insufficient_exact_plan_evidence", 4)
        if complexity >= 0.58:
            return ("strong", ["ContextCompress"], None, 4)
        return ("cheap", ["ContextCompress", "OutputBudget"], None, 4)
    raise ValueError(f"unsupported arm: {arm}")


def _quality(
    *,
    complexity: float,
    target_class: str,
    rewrites: Sequence[str],
    interaction_strength: float,
) -> float:
    score = 1.0
    if target_class == "cheap" and complexity > 0.68:
        score -= 0.18 + 0.22 * complexity
    if rewrites and complexity > 0.78:
        score -= 0.12
    if target_class == "cheap" and rewrites and complexity > 0.55:
        score -= interaction_strength
    return max(0.0, min(1.0, score))


def run(request: Mapping[str, Any]) -> list[JsonObject]:
    if request.get("paper_evidence") is not False or request.get("stage") != "E0":
        raise ValueError("fixture worker is restricted to non-paper Stage E0")
    if request.get("network_policy") != "forbidden":
        raise ValueError("fixture worker requires forbidden network policy")
    task_id = cast(str, request["task_id"])
    arm = cast(str, request["arm"])
    run_seed = cast(int, request["run_seed"])
    model_pair = cast(Mapping[str, str], request["model_pair"])
    config = cast(Mapping[str, Any], request.get("workload_configuration", {}))
    interaction_strength = float(config.get("interaction_strength", 0.18))
    input_scale = float(config.get("input_token_scale", 1.0))
    base_cost = float(config.get("strong_cost_per_million", 10.0))
    cheap_cost = float(config.get("cheap_cost_per_million", 1.0))
    if not all(
        math.isfinite(value) and value >= 0.0
        for value in (interaction_strength, input_scale, base_cost, cheap_cost)
    ):
        raise ValueError("fixture numeric settings must be finite and non-negative")

    complexity = _complexity(task_id)
    target_class, rewrites, abstention, candidate_count = _policy(arm, complexity)
    selected_model = model_pair[target_class]
    requested_model = model_pair["strong"]
    base_input_tokens = round((850 + 1150 * complexity) * input_scale)
    input_tokens = round(base_input_tokens * (0.62 if rewrites else 1.0))
    output_tokens = round(70 + 90 * complexity)
    if "OutputBudget" in rewrites:
        output_tokens = min(output_tokens, 96)
    per_million = base_cost if target_class == "strong" else cheap_cost
    cost_usd = (input_tokens + 2.5 * output_tokens) * per_million / 1_000_000
    latency_ms = 80.0 + 0.08 * input_tokens + 0.45 * output_tokens
    if target_class == "cheap":
        latency_ms *= 0.62
    if arm == "trace_only_fixed_strong":
        latency_ms += 0.4
    rng = random.Random(run_seed)
    latency_ms += rng.uniform(0.0, 1.0)
    quality = _quality(
        complexity=complexity,
        target_class=target_class,
        rewrites=rewrites,
        interaction_strength=interaction_strength,
    )
    plan_spec = {
        "target": selected_model,
        "rewrites": list(rewrites),
        "arm": arm,
        "fixture_version": 1,
    }
    request_projection = {
        "workload_id": request["workload_id"],
        "task_id": task_id,
        "model": requested_model,
        "input_tokens": base_input_tokens,
    }
    response_projection = {
        "score": quality,
        "selected_model": selected_model,
        "output_tokens": output_tokens,
    }
    call: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "call",
        "call_index": 0,
        "requested_model": requested_model,
        "selected_model": selected_model,
        "returned_model": selected_model,
        "call_site_id": f"fixture.{request['family']}.solve",
        "call_site_version": _canonical_digest(
            {"family": request["family"], "schema": 1}
        ),
        "execution_plan_id": _canonical_digest(plan_spec),
        "ordered_rewrites": list(rewrites),
        "input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_tokens": 0,
        "tool_tokens": 0,
        "cost_usd": cost_usd,
        "request_latency_ms": latency_ms,
        "planning_overhead_us": 0.0 if arm == "unmodified_fixed_strong" else 45.0,
        "eligible": arm != "unmodified_fixed_strong",
        "is_exploration": False,
        "is_shadow": False,
        "retry_count": 0,
        "candidate_count": candidate_count,
        "failed": False,
        "dispatch_fallback": False,
        "abstention_reason": abstention,
        "request_digest": _canonical_digest(request_projection),
        "response_digest": _canonical_digest(response_projection),
        "network_calls": 0,
        "fixture_only": True,
    }
    records = [call]
    if arm == "joint_guarded" and complexity < 0.20:
        exploration_cost = cost_usd * 0.55
        records.append(
            {
                **call,
                "call_index": 1,
                "selected_model": model_pair["cheap"],
                "returned_model": model_pair["cheap"],
                "execution_plan_id": _canonical_digest(
                    {**plan_spec, "exploration": True}
                ),
                "cost_usd": exploration_cost,
                "request_latency_ms": latency_ms * 0.6,
                "planning_overhead_us": 0.0,
                "is_exploration": True,
                "candidate_count": 4,
                "response_digest": _canonical_digest(
                    {**response_projection, "exploration": True}
                ),
            }
        )
    task: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "task",
        "task_status": "completed",
        "official_score": quality,
        "resolved": quality == 1.0,
        "end_to_end_latency_ms": latency_ms + 12.0,
        "safety_failure": False,
        "network_calls": 0,
        "fixture_only": True,
        "conformance": {
            "worker_kind": "deterministic_campaign_fixture",
            "upstream_source_modified": False,
            "official_task": False,
            "official_score": False,
            "provider_accounting": False,
        },
    }
    return [task, *records]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    request = _read_request(args.request)
    started = time.monotonic()
    records = run(request)
    task = next(record for record in records if record["record_type"] == "task")
    task["worker_wall_time_ms"] = (time.monotonic() - started) * 1000.0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, allow_nan=False, sort_keys=True))
            handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
