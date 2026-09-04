"""Stage-E0 joint-campaign worker for frozen tau2 and SWE-agent call sites.

This worker runs inside each upstream project's own Python environment.  It
loads a frozen public task, invokes the unmodified upstream LiteLLM call site,
uses LiteLLM's local ``mock_response`` transport, and emits the normalized
records required by :mod:`bench.joint_campaign`.

It is intentionally restricted to E0 and cannot produce paper evidence.  The
static and sequential arms are plumbing controls here; their real policies must
come from a Stage-C calibration lock before a paid P/T campaign.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from bench import litellm_admission_preflight as admission


SCHEMA_VERSION = 1
_TAU2_USER_MODEL = "openai/gpt-4.1-mini-2025-04-14"
JsonObject = dict[str, Any]


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        default=str,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _git(root: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _read_request(path: Path) -> JsonObject:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return cast(JsonObject, value)


def _load_json_rows(path: Path) -> list[JsonObject]:
    value = json.loads(path.read_text())
    if not isinstance(value, list):
        raise ValueError(f"task universe is not a JSON list: {path}")
    return [cast(JsonObject, row) for row in value if isinstance(row, dict)]


def _load_task(path: Path, task_id: str, id_field: str) -> JsonObject:
    suffix = path.suffix.lower()
    if suffix == ".json":
        rows = _load_json_rows(path)
    elif suffix in {".jsonl", ".ndjson"}:
        rows = [
            cast(JsonObject, json.loads(line))
            for line in path.read_text().splitlines()
            if line.strip()
        ]
    elif suffix == ".parquet":
        try:
            import pyarrow.parquet as parquet  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("reading the frozen Parquet requires pyarrow") from error
        rows = cast(list[JsonObject], parquet.read_table(path).to_pylist())
    else:
        raise ValueError(f"unsupported task-universe format: {path.suffix}")
    matches = [row for row in rows if str(row.get(id_field, "")) == task_id]
    if len(matches) != 1:
        raise ValueError(
            f"expected one task {task_id!r} by {id_field!r}, found {len(matches)}"
        )
    return matches[0]


def _task_prompt(workload_kind: str, task: Mapping[str, Any]) -> str:
    if workload_kind == "tau2":
        scenario = task.get("user_scenario")
        if not isinstance(scenario, dict):
            raise ValueError("tau2 task lacks user_scenario")
        instructions = scenario.get("instructions")
        if not isinstance(instructions, str) or not instructions:
            raise ValueError("tau2 task lacks user instructions")
        return instructions
    statement = task.get("problem_statement")
    if not isinstance(statement, str) or not statement:
        raise ValueError("SWE-bench task lacks problem_statement")
    return statement


def _task_complexity(task_id: str) -> float:
    digest = hashlib.sha256(f"arm-plumbing\0{task_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def _arm_settings(
    arm: str,
    *,
    strong_model: str,
    cheap_model: str,
    task_id: str,
) -> JsonObject:
    target = strong_model
    requested_model = strong_model
    max_output_tokens = 256
    optimize = False
    compose = True
    planner_mode = "joint_guarded"
    enabled_rules = ""
    implementation = "agentc_trace_proxy"
    candidate_count = 1
    applied_rewrites: list[str] = []
    complexity = _task_complexity(task_id)

    if arm == "unmodified_fixed_strong":
        implementation = "agentc_trace_proxy_not_overhead_control"
    elif arm == "trace_only_fixed_strong":
        implementation = "agentc_trace_only"
    elif arm == "fixed_cheap":
        target = cheap_model
        requested_model = cheap_model
        implementation = "fixed_target"
    elif arm == "routing_only":
        optimize = True
        enabled_rules = "ModelDowngrade"
        implementation = "profiled_router"
        candidate_count = 2
    elif arm == "rewrite_only_fixed_strong":
        optimize = True
        enabled_rules = "OutputBudget"
        implementation = "profiled_rewriter"
        candidate_count = 2
    elif arm == "best_static_joint":
        target = cheap_model
        max_output_tokens = 128
        applied_rewrites = ["OutputBudget"]
        implementation = "e0_static_plan_plumbing_control"
    elif arm == "route_then_rewrite":
        target = cheap_model if complexity < 0.62 else strong_model
        max_output_tokens = 128
        applied_rewrites = ["OutputBudget"]
        implementation = "e0_route_then_rewrite_plumbing_control"
        candidate_count = 2
    elif arm == "rewrite_then_route":
        max_output_tokens = 128
        target = cheap_model if complexity < 0.76 else strong_model
        applied_rewrites = ["OutputBudget"]
        implementation = "e0_rewrite_then_route_plumbing_control"
        candidate_count = 2
    elif arm == "current_greedy":
        optimize = True
        planner_mode = "current_greedy"
        enabled_rules = "ModelDowngrade,OutputBudget"
        implementation = "projected_savings_greedy"
        candidate_count = 4
    elif arm == "joint_guarded":
        optimize = True
        enabled_rules = "ModelDowngrade,OutputBudget"
        implementation = "profiled_joint_guarded"
        candidate_count = 4
    else:
        raise ValueError(f"unsupported arm {arm!r}")
    return {
        "target_model": target,
        "requested_model": requested_model,
        "max_output_tokens": max_output_tokens,
        "optimize": optimize,
        "compose": compose,
        "planner_mode": planner_mode,
        "enabled_rules": enabled_rules,
        "implementation": implementation,
        "candidate_count": candidate_count,
        "applied_rewrites": applied_rewrites,
    }


def _configure_runtime(storage: Path, settings: Mapping[str, Any]) -> None:
    admission._configure_optimizer_environment(storage)
    os.environ.update(
        {
            "AGENTC_OPTIMIZE": "1" if settings["optimize"] else "0",
            "AGENTC_COMPOSE": "1" if settings["compose"] else "0",
            "AGENTC_EVAL_PLANNER_MODE": cast(str, settings["planner_mode"]),
            "AGENTC_ENABLED_RULES": cast(str, settings["enabled_rules"]),
            "AGENTC_OPTIMIZE_EXPLORATION": "0",
            "AGENTC_OPTIMIZE_SHADOW": "0",
        }
    )


def _normalize_call(
    *,
    call_index: int,
    scope: str,
    eligible: bool,
    requested_model: str,
    settings: Mapping[str, Any],
    plan: Mapping[str, Any] | None,
    outcome: Mapping[str, Any] | None,
    request_projection: Mapping[str, Any],
    response_digest: str,
) -> JsonObject:
    runtime_rules = cast(list[str], plan.get("rules", []) if plan else [])
    configured_rules = cast(list[str], settings.get("applied_rewrites", []))
    rules = list(dict.fromkeys([*configured_rules, *runtime_rules]))
    rewritten = plan.get("rewritten") if plan else None
    selected_model = cast(str, settings["target_model"])
    if isinstance(rewritten, dict) and isinstance(rewritten.get("model"), str):
        selected_model = cast(str, rewritten["model"])
    plan_id = plan.get("execution_plan_id") if plan else None
    if not isinstance(plan_id, str) or len(plan_id) != 64:
        plan_id = _digest(
            {
                "model": selected_model,
                "rules": rules,
                "scope": scope,
                "e0_fallback_identity": True,
            }
        )
    input_tokens = int(outcome.get("input_tokens", 0)) if outcome else 0
    output_tokens = int(outcome.get("output_tokens", 0)) if outcome else 0
    latency_ms = float(outcome.get("latency_ms", 0.0)) if outcome else 0.0
    cost_usd = float(outcome.get("cost_usd", 0.0)) if outcome else 0.0
    if not all(math.isfinite(value) and value >= 0 for value in (latency_ms, cost_usd)):
        raise ValueError("upstream outcome contains invalid latency or cost")
    abstention = None
    if eligible and bool(settings["optimize"]) and not rules:
        abstention = "e0_cold_or_inadmissible"
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "call",
        "call_index": call_index,
        "requested_model": requested_model,
        "selected_model": selected_model,
        "returned_model": selected_model,
        "call_site_id": (
            str(plan.get("call_site_id")) if plan else f"upstream.{scope}"
        ),
        "execution_plan_id": plan_id,
        "ordered_rewrites": rules,
        "input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_tokens": 0,
        "tool_tokens": 0,
        "cost_usd": cost_usd,
        "request_latency_ms": latency_ms,
        "planning_overhead_us": 0.0,
        "eligible": eligible,
        "is_exploration": False,
        "is_shadow": False,
        "retry_count": 0,
        "candidate_count": int(settings["candidate_count"]) if eligible else 1,
        "failed": False,
        "dispatch_fallback": False,
        "abstention_reason": abstention,
        "request_digest": _digest(request_projection),
        "response_digest": response_digest,
        "network_calls": 0,
        "arm_implementation": settings["implementation"],
    }


def run(request: Mapping[str, Any], *, upstream_root: Path) -> list[JsonObject]:
    if request.get("stage") != "E0" or request.get("paper_evidence") is not False:
        raise ValueError("LiteLLM joint preflight is restricted to non-paper Stage E0")
    if request.get("network_policy") != "forbidden":
        raise ValueError("LiteLLM joint preflight requires forbidden network policy")
    config = cast(Mapping[str, Any], request.get("workload_configuration", {}))
    workload_kind = cast(str, config.get("workload_kind"))
    if workload_kind not in {"tau2", "sweagent"}:
        raise ValueError("workload_kind must be tau2 or sweagent")
    provenance = cast(Mapping[str, Any], request.get("workload_provenance", {}))
    expected_commit = cast(str, provenance.get("upstream_commit", ""))
    actual_commit = _git(upstream_root, "rev-parse", "HEAD")
    if actual_commit != expected_commit:
        raise ValueError(
            f"upstream commit mismatch: expected {expected_commit}, got {actual_commit}"
        )
    dirty = _git(upstream_root, "status", "--short") or ""
    if dirty:
        raise ValueError("upstream worktree is dirty")

    task_path_value = config.get("task_universe_path")
    if not isinstance(task_path_value, str) or not task_path_value:
        raise ValueError("task_universe_path is required")
    task_path = (upstream_root / task_path_value).resolve()
    if not task_path.is_file():
        raise ValueError(f"task universe is missing: {task_path_value}")
    expected_task_digest = cast(str, provenance.get("task_universe_sha256", ""))
    if _file_digest(task_path) != expected_task_digest:
        raise ValueError("task-universe digest mismatch")
    id_field = "id" if workload_kind == "tau2" else "instance_id"
    task_id = cast(str, request["task_id"])
    task = _load_task(task_path, task_id, id_field)
    prompt = _task_prompt(workload_kind, task)

    model_pair = cast(Mapping[str, str], request["model_pair"])
    settings = _arm_settings(
        cast(str, request["arm"]),
        strong_model=model_pair["strong"],
        cheap_model=model_pair["cheap"],
        task_id=task_id,
    )
    storage = Path(cast(str, request["storage_path"]))
    storage.mkdir(parents=True, exist_ok=True)
    _configure_runtime(storage, settings)
    started = time.monotonic()
    if workload_kind == "tau2":
        result = admission._run_tau2(
            1,
            storage,
            model=cast(str, settings["target_model"]),
            user_model=_TAU2_USER_MODEL,
            prompt_prefix=prompt,
            max_output_tokens=int(settings["max_output_tokens"]),
        )
    else:
        result = admission._run_sweagent(
            storage,
            model=cast(str, settings["target_model"]),
            prompt=prompt,
            max_output_tokens=int(settings["max_output_tokens"]),
        )
    elapsed_ms = (time.monotonic() - started) * 1000.0
    plans = cast(list[Mapping[str, Any]], result.get("plan_events", []))
    outcomes = cast(list[Mapping[str, Any]], result.get("outcome_events", []))
    plan = plans[0] if plans else None
    outcome = outcomes[0] if outcomes else None
    projection = {
        "task_id": task_id,
        "prompt_sha256": _digest(prompt),
        "requested_model": settings["target_model"],
        "max_output_tokens": settings["max_output_tokens"],
    }
    calls = [
        _normalize_call(
            call_index=0,
            scope=(
                "tau2.evaluated_assistant"
                if workload_kind == "tau2"
                else "sweagent.model"
            ),
            eligible=True,
            requested_model=cast(str, settings["requested_model"]),
            settings=settings,
            plan=plan,
            outcome=outcome,
            request_projection=projection,
            response_digest=cast(str, result["response_digest"]),
        )
    ]
    if workload_kind == "tau2":
        calls.append(
            _normalize_call(
                call_index=1,
                scope="tau2.user_simulator",
                eligible=False,
                requested_model=_TAU2_USER_MODEL,
                settings={**settings, "target_model": _TAU2_USER_MODEL},
                plan=None,
                outcome=None,
                request_projection={**projection, "actor": "user_simulator"},
                response_digest=_digest(
                    {"bundle_response_digest": result["response_digest"], "actor": "user"}
                ),
            )
        )
    task_record: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "task",
        "task_status": "completed",
        "official_score": 1.0,
        "resolved": True,
        "end_to_end_latency_ms": elapsed_ms,
        "safety_failure": False,
        "network_calls": 0,
        "conformance": {
            "worker_kind": "frozen_upstream_litellm_mock_call_shape",
            "upstream_source_modified": False,
            "official_task": False,
            "official_score": False,
            "provider_accounting": False,
        },
        "upstream": {
            "git_commit": actual_commit,
            "task_universe_sha256": expected_task_digest,
            "task_input_sha256": _digest(task),
        },
        "arm_implementation": settings["implementation"],
        "mocked_provider": True,
    }
    return [task_record, *calls]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    request = _read_request(args.request)
    records = run(request, upstream_root=Path.cwd().resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, allow_nan=False, sort_keys=True))
            handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
