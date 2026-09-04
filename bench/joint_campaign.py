"""Reproducible paired campaign runner for joint model-and-rewrite policies.

The runner deliberately knows nothing about a workload's implementation.  A
workload command receives one content-free JSON request per task/arm/repetition
and writes normalized task and call records.  This keeps tau2, SWE-agent, and
OSWorld in their own dependency environments while centralizing the parts that
must be identical across them: task membership, seeds, arm order, validation,
provenance, and statistical analysis.

Usage::

    python -m bench.joint_campaign campaign.json --output /tmp/agentc-campaign

Worker command contract::

    <configured command> --request REQUEST.json --output RESULT.jsonl

The worker must emit exactly one ``task`` record and zero or more ``call``
records.  A completed task must contain at least one call.  The campaign runner
injects schedule identity fields, rejects mismatches, and appends records to a
single canonical ledger only after the entire worker result validates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import re
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, cast


SCHEMA_VERSION = 1
PRIMARY_ARMS: tuple[str, ...] = (
    "unmodified_fixed_strong",
    "trace_only_fixed_strong",
    "fixed_cheap",
    "routing_only",
    "rewrite_only_fixed_strong",
    "best_static_joint",
    "route_then_rewrite",
    "rewrite_then_route",
    "current_greedy",
    "joint_guarded",
)
INTERACTION_CONTROLS: tuple[str, ...] = (
    "routing_only",
    "rewrite_only_fixed_strong",
    "best_static_joint",
    "route_then_rewrite",
    "rewrite_then_route",
    "current_greedy",
)
REFERENCE_ARM = "trace_only_fixed_strong"
HELD_OUT_STAGES = frozenset({"P", "T"})
KNOWN_STAGES = frozenset({"E0", "E1", "C", "P", "T"})
SECRET_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "TOGETHER_API_KEY",
        "HF_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_ENV_TOKEN = re.compile(r"\{env:([A-Z][A-Z0-9_]*)\}")


JsonObject = dict[str, Any]


class CampaignError(RuntimeError):
    """Raised when a campaign would be incomplete or non-reproducible."""


@dataclass(frozen=True)
class ScheduledRun:
    workload_id: str
    task_id: str
    arm: str
    repetition: int
    run_seed: int
    ordinal: int

    @property
    def key(self) -> tuple[str, str, str, int]:
        return (self.workload_id, self.task_id, self.arm, self.repetition)


@dataclass(frozen=True)
class BootstrapInterval:
    estimate: float
    low: float
    high: float

    def as_dict(self) -> JsonObject:
        return {"estimate": self.estimate, "low": self.low, "high": self.high}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_value(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def ordered_id_digest(task_ids: Sequence[str]) -> str:
    payload = "".join(f"{task_id}\n" for task_id in task_ids).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def derive_run_seed(
    workload_id: str,
    task_id: str,
    arm: str,
    repetition: int,
) -> int:
    payload = (
        f"agentc-run-v1\0{workload_id}\0{task_id}\0{arm}\0{repetition}"
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % (
        2**31 - 1
    )


def ordered_arms(
    workload_id: str,
    task_id: str,
    repetition: int,
    arms: Sequence[str],
) -> list[str]:
    def key(arm: str) -> tuple[bytes, str]:
        payload = (
            f"agentc-arm-order-v1\0{workload_id}\0{task_id}\0{arm}\0{repetition}"
        ).encode("utf-8")
        return (hashlib.sha256(payload).digest(), arm)

    return sorted(arms, key=key)


def _require_object(value: Any, label: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CampaignError(f"{label} must be a JSON object")
    return cast(JsonObject, value)


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignError(f"{label} must be a non-empty string")
    return value


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CampaignError(f"{label} must be an integer >= {minimum}")
    return value


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampaignError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        suffix = f" >= {minimum}" if minimum is not None else ""
        raise CampaignError(f"{label} must be finite{suffix}")
    return result


def _load_json(path: Path) -> JsonObject:
    try:
        return _require_object(json.loads(path.read_text()), str(path))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignError(f"cannot read JSON {path}: {error}") from error


def load_campaign(path: Path) -> JsonObject:
    """Load and validate the frozen campaign description."""
    spec = _load_json(path)
    if spec.get("schema_version") != SCHEMA_VERSION:
        raise CampaignError(f"campaign schema_version must be {SCHEMA_VERSION}")
    _require_string(spec.get("campaign_id"), "campaign_id")
    stage = _require_string(spec.get("stage"), "stage")
    if stage not in KNOWN_STAGES:
        raise CampaignError(f"stage must be one of {sorted(KNOWN_STAGES)}")
    paper_evidence = spec.get("paper_evidence")
    if not isinstance(paper_evidence, bool):
        raise CampaignError("paper_evidence must be boolean")
    if stage in {"E0", "E1"} and paper_evidence:
        raise CampaignError(f"Stage {stage} cannot be labeled paper evidence")

    protocol = _require_object(spec.get("protocol"), "protocol")
    protocol_path = Path(_require_string(protocol.get("path"), "protocol.path"))
    expected_protocol_digest = _require_string(
        protocol.get("sha256"), "protocol.sha256"
    )
    if not _HEX_64.fullmatch(expected_protocol_digest):
        raise CampaignError("protocol.sha256 must be a lowercase SHA-256 digest")
    resolved_protocol = (
        protocol_path if protocol_path.is_absolute() else path.parent / protocol_path
    ).resolve()
    if not resolved_protocol.is_file():
        raise CampaignError(f"protocol file does not exist: {protocol_path}")
    actual_protocol_digest = digest_file(resolved_protocol)
    if actual_protocol_digest != expected_protocol_digest:
        raise CampaignError(
            "protocol digest mismatch: "
            f"expected {expected_protocol_digest}, got {actual_protocol_digest}"
        )
    protocol["_resolved_path"] = str(resolved_protocol)

    configured_arms = spec.get("arms", list(PRIMARY_ARMS))
    if not isinstance(configured_arms, list) or not all(
        isinstance(arm, str) for arm in configured_arms
    ):
        raise CampaignError("arms must be a list of strings")
    arms = cast(list[str], configured_arms)
    if arms != list(PRIMARY_ARMS):
        raise CampaignError(
            "primary campaign arms must exactly match the frozen order: "
            + ", ".join(PRIMARY_ARMS)
        )
    spec["arms"] = arms

    bootstrap_resamples = _require_int(
        spec.get("bootstrap_resamples", 10_000),
        "bootstrap_resamples",
        minimum=1,
    )
    spec["bootstrap_resamples"] = bootstrap_resamples

    expected_spend_usd = _finite_number(
        spec.get("expected_spend_usd"),
        "expected_spend_usd",
        minimum=0.0,
    )
    if stage in {"C", "P", "T"} and expected_spend_usd <= 0.0:
        raise CampaignError(
            f"Stage {stage} requires a positive expected_spend_usd"
        )
    spec["expected_spend_usd"] = expected_spend_usd

    if stage in HELD_OUT_STAGES:
        lock = _require_object(spec.get("calibration_lock"), "calibration_lock")
        lock_digest = _require_string(lock.get("sha256"), "calibration_lock.sha256")
        if not _HEX_64.fullmatch(lock_digest):
            raise CampaignError("calibration_lock.sha256 must be a lowercase SHA-256")
        if not isinstance(lock.get("selections"), dict):
            raise CampaignError(
                "held-out stages require calibration_lock.selections; "
                "the runner never tunes from held-out outcomes"
            )

    workloads_value = spec.get("workloads")
    if not isinstance(workloads_value, list) or len(workloads_value) < 2:
        raise CampaignError("campaign must contain at least two workload families")
    workloads = workloads_value
    seen_ids: set[str] = set()
    seen_families: set[str] = set()
    unengineered_families: set[str] = set()
    for index, raw_workload in enumerate(workloads):
        workload = _require_object(raw_workload, f"workloads[{index}]")
        workload_id = _require_string(
            workload.get("workload_id"), f"workloads[{index}].workload_id"
        )
        family = _require_string(
            workload.get("family"), f"workloads[{index}].family"
        )
        if workload_id in seen_ids:
            raise CampaignError(f"duplicate workload_id: {workload_id}")
        seen_ids.add(workload_id)
        seen_families.add(family)
        unengineered = workload.get("unengineered_upstream")
        if not isinstance(unengineered, bool):
            raise CampaignError(
                f"{workload_id}.unengineered_upstream must be boolean"
            )
        if unengineered:
            unengineered_families.add(family)
        provenance = _require_object(
            workload.get("provenance"), f"{workload_id}.provenance"
        )
        upstream_commit = _require_string(
            provenance.get("upstream_commit"),
            f"{workload_id}.provenance.upstream_commit",
        )
        if not re.fullmatch(r"[0-9a-f]{40}", upstream_commit):
            raise CampaignError(
                f"{workload_id}.provenance.upstream_commit must be a lowercase Git commit"
            )
        _validate_digest(
            provenance.get("task_universe_sha256"),
            f"{workload_id}.provenance.task_universe_sha256",
        )
        command = workload.get("worker_command")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise CampaignError(
                f"{workload_id}.worker_command must be a non-empty string list"
            )
        task_ids_value = workload.get("task_ids")
        if not isinstance(task_ids_value, list) or not task_ids_value or not all(
            isinstance(task_id, str) and task_id for task_id in task_ids_value
        ):
            raise CampaignError(f"{workload_id}.task_ids must be non-empty strings")
        task_ids = cast(list[str], task_ids_value)
        if len(set(task_ids)) != len(task_ids):
            raise CampaignError(f"{workload_id}.task_ids contains duplicates")
        expected_ids_digest = _require_string(
            workload.get("task_ids_sha256"), f"{workload_id}.task_ids_sha256"
        )
        actual_ids_digest = ordered_id_digest(task_ids)
        if actual_ids_digest != expected_ids_digest:
            raise CampaignError(
                f"{workload_id} task_ids digest mismatch: expected "
                f"{expected_ids_digest}, got {actual_ids_digest}"
            )
        _require_int(workload.get("repetitions"), f"{workload_id}.repetitions", minimum=2)
        model_pair = _require_object(
            workload.get("model_pair"), f"{workload_id}.model_pair"
        )
        _require_string(model_pair.get("strong"), f"{workload_id}.model_pair.strong")
        _require_string(model_pair.get("cheap"), f"{workload_id}.model_pair.cheap")
        margin = _finite_number(
            workload.get("quality_margin"), f"{workload_id}.quality_margin"
        )
        if not -1.0 <= margin <= 0.0:
            raise CampaignError(f"{workload_id}.quality_margin must be in [-1, 0]")
        network_policy = workload.get("network_policy", "forbidden")
        if network_policy not in {"forbidden", "provider_allowed"}:
            raise CampaignError(
                f"{workload_id}.network_policy must be forbidden or provider_allowed"
            )
        if stage in {"C", "P", "T"} and network_policy != "provider_allowed":
            raise CampaignError(
                f"{workload_id}: Stage {stage} requires provider_allowed network policy"
            )
    if len(seen_families) < 2:
        raise CampaignError("campaign must contain at least two distinct workload families")
    if stage in {"C", "P", "T"} and len(unengineered_families) < 2:
        raise CampaignError(
            f"Stage {stage} requires at least two distinct unengineered workload families"
        )
    return spec


def build_schedule(spec: Mapping[str, Any]) -> list[ScheduledRun]:
    """Build the task-paired, deterministically counterbalanced schedule."""
    arms = cast(Sequence[str], spec["arms"])
    workloads = cast(Sequence[Mapping[str, Any]], spec["workloads"])
    schedule: list[ScheduledRun] = []
    ordinal = 0
    for workload in workloads:
        workload_id = cast(str, workload["workload_id"])
        repetitions = cast(int, workload["repetitions"])
        task_ids = cast(Sequence[str], workload["task_ids"])
        for repetition in range(repetitions):
            for task_id in task_ids:
                for arm in ordered_arms(workload_id, task_id, repetition, arms):
                    schedule.append(
                        ScheduledRun(
                            workload_id=workload_id,
                            task_id=task_id,
                            arm=arm,
                            repetition=repetition,
                            run_seed=derive_run_seed(
                                workload_id, task_id, arm, repetition
                            ),
                            ordinal=ordinal,
                        )
                    )
                    ordinal += 1
    return schedule


def _expand_command(
    command: Sequence[str], *, repo_root: Path, spec_dir: Path
) -> list[str]:
    return [
        cast(
            str,
            _expand_runtime_value(part, repo_root=repo_root, spec_dir=spec_dir),
        )
        for part in command
    ]


def _expand_runtime_value(value: Any, *, repo_root: Path, spec_dir: Path) -> Any:
    """Expand portable runtime tokens without changing the frozen spec."""
    if isinstance(value, str):
        expanded = value.replace("{python}", sys.executable)
        expanded = expanded.replace("{repo}", str(repo_root)).replace(
            "{spec}", str(spec_dir)
        )
        for match in list(_ENV_TOKEN.finditer(expanded)):
            name = match.group(1)
            replacement = os.environ.get(name)
            if not replacement:
                raise CampaignError(f"required runtime path variable {name} is unset")
            expanded = expanded.replace(match.group(0), replacement)
        return expanded
    if isinstance(value, list):
        return [
            _expand_runtime_value(item, repo_root=repo_root, spec_dir=spec_dir)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _expand_runtime_value(item, repo_root=repo_root, spec_dir=spec_dir)
            for key, item in value.items()
        }
    return value


def _portable_command(command: Sequence[str], repo_root: Path) -> list[str]:
    portable: list[str] = []
    home = str(Path.home())
    for part in command:
        value = part.replace(str(repo_root), "{repo}")
        if value == sys.executable:
            value = "{python}"
        value = value.replace(home, "{home}")
        portable.append(value)
    return portable


def _workload_by_id(spec: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    workloads = cast(Sequence[Mapping[str, Any]], spec["workloads"])
    return {cast(str, workload["workload_id"]): workload for workload in workloads}


def _cell_storage_path(output_dir: Path, run: ScheduledRun) -> Path:
    return output_dir / "state" / run.workload_id / run.arm / f"rep-{run.repetition}"


def _artifact_stem(run: ScheduledRun) -> str:
    """Return a path-safe name without trusting upstream task identifiers."""
    identity = {
        "workload_id": run.workload_id,
        "task_id": run.task_id,
        "arm": run.arm,
        "repetition": run.repetition,
        "ordinal": run.ordinal,
    }
    return f"{run.ordinal:08d}-{digest_value(identity)[:20]}"


def _request_payload(
    spec: Mapping[str, Any],
    workload: Mapping[str, Any],
    run: ScheduledRun,
    output_dir: Path,
    *,
    repo_root: Path,
    spec_dir: Path,
) -> JsonObject:
    payload: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": spec["campaign_id"],
        "stage": spec["stage"],
        "paper_evidence": spec["paper_evidence"],
        "workload_id": run.workload_id,
        "family": workload["family"],
        "split": workload.get("split", "unspecified"),
        "task_id": run.task_id,
        "arm": run.arm,
        "repetition": run.repetition,
        "run_seed": run.run_seed,
        "schedule_ordinal": run.ordinal,
        "model_pair": workload["model_pair"],
        "workload_provenance": workload["provenance"],
        "network_policy": workload.get("network_policy", "forbidden"),
        "storage_path": str(_cell_storage_path(output_dir, run)),
        "arm_configuration": cast(Mapping[str, Any], spec.get("arm_configurations", {})).get(
            run.arm, {}
        ),
        "workload_configuration": _expand_runtime_value(
            workload.get("worker_configuration", {}),
            repo_root=repo_root,
            spec_dir=spec_dir,
        ),
    }
    calibration_lock = spec.get("calibration_lock")
    if calibration_lock is not None:
        payload["calibration_lock"] = calibration_lock
    return payload


def _read_jsonl(path: Path) -> list[JsonObject]:
    records: list[JsonObject] = []
    try:
        with path.open() as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if not raw_line.strip():
                    continue
                try:
                    value = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    raise CampaignError(
                        f"{path}:{line_number}: invalid JSON: {error}"
                    ) from error
                records.append(
                    _require_object(value, f"{path}:{line_number}")
                )
    except OSError as error:
        raise CampaignError(f"cannot read worker output {path}: {error}") from error
    return records


def _validate_digest(value: Any, label: str) -> str:
    result = _require_string(value, label)
    if not _HEX_64.fullmatch(result):
        raise CampaignError(f"{label} must be a lowercase SHA-256 digest")
    return result


def validate_worker_records(
    records: Sequence[JsonObject],
    request: Mapping[str, Any],
) -> list[JsonObject]:
    """Validate one atomic worker result and add schedule-owned fields."""
    if not records:
        raise CampaignError("worker emitted no records")
    task_records = [record for record in records if record.get("record_type") == "task"]
    call_records = [record for record in records if record.get("record_type") == "call"]
    unknown = [
        record.get("record_type")
        for record in records
        if record.get("record_type") not in {"task", "call"}
    ]
    if unknown:
        raise CampaignError(f"worker emitted unknown record types: {unknown}")
    if len(task_records) != 1:
        raise CampaignError("worker must emit exactly one task record")

    expected = {
        "workload_id": request["workload_id"],
        "task_id": request["task_id"],
        "arm": request["arm"],
        "repetition": request["repetition"],
        "run_seed": request["run_seed"],
    }
    normalized: list[JsonObject] = []
    for index, original in enumerate(records):
        record = dict(original)
        if record.get("schema_version") != SCHEMA_VERSION:
            raise CampaignError(f"worker record {index} has wrong schema_version")
        for field, expected_value in expected.items():
            supplied = record.get(field, expected_value)
            if supplied != expected_value:
                raise CampaignError(
                    f"worker record {index} mismatches {field}: "
                    f"expected {expected_value!r}, got {supplied!r}"
                )
            record[field] = expected_value
        record["campaign_id"] = request["campaign_id"]
        record["stage"] = request["stage"]
        record["paper_evidence"] = request["paper_evidence"]
        record["family"] = request["family"]
        record["split"] = request["split"]
        record["schedule_ordinal"] = request["schedule_ordinal"]
        normalized.append(record)

    task = next(record for record in normalized if record["record_type"] == "task")
    status = _require_string(task.get("task_status"), "task.task_status")
    if status not in {"completed", "failed", "incomplete"}:
        raise CampaignError("task.task_status must be completed, failed, or incomplete")
    score = _finite_number(task.get("official_score"), "task.official_score")
    if not 0.0 <= score <= 1.0:
        raise CampaignError("task.official_score must be in [0, 1]")
    _finite_number(
        task.get("end_to_end_latency_ms"),
        "task.end_to_end_latency_ms",
        minimum=0.0,
    )
    if not isinstance(task.get("safety_failure"), bool):
        raise CampaignError("task.safety_failure must be boolean")
    conformance = _require_object(task.get("conformance"), "task.conformance")
    for field in (
        "upstream_source_modified",
        "official_task",
        "official_score",
        "provider_accounting",
    ):
        if not isinstance(conformance.get(field), bool):
            raise CampaignError(f"task.conformance.{field} must be boolean")
    _require_string(conformance.get("worker_kind"), "task.conformance.worker_kind")
    if request["stage"] in {"C", "P", "T"}:
        if conformance["upstream_source_modified"]:
            raise CampaignError("paper stage worker reports modified upstream source")
        for field in ("official_task", "official_score", "provider_accounting"):
            if not conformance[field]:
                raise CampaignError(f"paper stage requires task.conformance.{field}=true")
    if status == "completed" and not call_records:
        raise CampaignError("completed task must emit at least one call record")

    call_indices: set[int] = set()
    for call in (record for record in normalized if record["record_type"] == "call"):
        call_index = _require_int(call.get("call_index"), "call.call_index")
        if call_index in call_indices:
            raise CampaignError(f"duplicate call_index {call_index}")
        call_indices.add(call_index)
        for field in ("requested_model", "selected_model", "call_site_id"):
            _require_string(call.get(field), f"call.{field}")
        plan_id = _require_string(call.get("execution_plan_id"), "call.execution_plan_id")
        if not _HEX_64.fullmatch(plan_id):
            raise CampaignError("call.execution_plan_id must be a lowercase SHA-256")
        rewrites = call.get("ordered_rewrites")
        if not isinstance(rewrites, list) or not all(
            isinstance(rewrite, str) and rewrite for rewrite in rewrites
        ):
            raise CampaignError("call.ordered_rewrites must be a string list")
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "tool_tokens",
            "retry_count",
            "candidate_count",
        ):
            _require_int(call.get(field), f"call.{field}")
        for field in ("cost_usd", "request_latency_ms", "planning_overhead_us"):
            _finite_number(call.get(field), f"call.{field}", minimum=0.0)
        for field in (
            "eligible",
            "is_exploration",
            "is_shadow",
            "failed",
            "dispatch_fallback",
        ):
            if not isinstance(call.get(field), bool):
                raise CampaignError(f"call.{field} must be boolean")
        abstention = call.get("abstention_reason")
        if abstention is not None and not isinstance(abstention, str):
            raise CampaignError("call.abstention_reason must be null or string")
        _validate_digest(call.get("request_digest"), "call.request_digest")
        _validate_digest(call.get("response_digest"), "call.response_digest")

    if request["network_policy"] == "forbidden":
        network_calls = sum(
            _require_int(record.get("network_calls", 0), "record.network_calls")
            for record in normalized
        )
        if network_calls:
            raise CampaignError(
                f"network-forbidden worker reported {network_calls} network calls"
            )
    serialized = _canonical_bytes(normalized).decode("utf-8")
    if str(Path.home()) in serialized:
        raise CampaignError("worker output contains a forbidden home-directory path")
    return normalized


def _append_records(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(_canonical_bytes(record).decode("utf-8"))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _validated_completed_keys(
    ledger_path: Path,
    *,
    spec: Mapping[str, Any],
    output_dir: Path,
    schedule: Sequence[ScheduledRun],
    repo_root: Path,
    spec_dir: Path,
) -> set[tuple[str, str, str, int]]:
    if not ledger_path.is_file():
        return set()
    scheduled_by_key = {run.key: run for run in schedule}
    grouped: dict[tuple[str, str, str, int], list[JsonObject]] = defaultdict(list)
    for record in _read_jsonl(ledger_path):
        try:
            key = (
                cast(str, record["workload_id"]),
                cast(str, record["task_id"]),
                cast(str, record["arm"]),
                cast(int, record["repetition"]),
            )
        except KeyError as error:
            raise CampaignError(
                f"partial ledger record lacks schedule field {error.args[0]}"
            ) from error
        grouped[key].append(record)
    extra = set(grouped) - set(scheduled_by_key)
    if extra:
        raise CampaignError(f"partial ledger contains {len(extra)} unscheduled run(s)")
    workload_map = _workload_by_id(spec)
    for key, records in grouped.items():
        run = scheduled_by_key[key]
        request = _request_payload(
            spec,
            workload_map[run.workload_id],
            run,
            output_dir,
            repo_root=repo_root,
            spec_dir=spec_dir,
        )
        normalized = validate_worker_records(records, request)
        if _canonical_bytes(normalized) != _canonical_bytes(records):
            raise CampaignError(
                f"partial ledger run {key} lacks campaign-owned normalized fields"
            )
    return set(grouped)


def _frozen_spec(spec: Mapping[str, Any]) -> JsonObject:
    copied = cast(JsonObject, json.loads(json.dumps(spec)))
    protocol = _require_object(copied["protocol"], "protocol")
    protocol.pop("_resolved_path", None)
    return copied


def _worker_environment(network_policy: str, repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    env["AGENTC_BENCH_NETWORK_POLICY"] = network_policy
    import_roots = [str(repo_root / "python"), str(repo_root)]
    if inherited := env.get("PYTHONPATH"):
        import_roots.append(inherited)
    env["PYTHONPATH"] = os.pathsep.join(import_roots)
    if network_policy == "forbidden":
        for name in SECRET_NAMES:
            env.pop(name, None)
    return env


def _safe_working_directory(
    workload: Mapping[str, Any], *, repo_root: Path, spec_dir: Path
) -> Path:
    configured = cast(str, workload.get("worker_cwd", "{repo}"))
    expanded = cast(
        str,
        _expand_runtime_value(
            configured, repo_root=repo_root, spec_dir=spec_dir
        ),
    )
    path = Path(expanded).resolve()
    if not path.is_dir():
        raise CampaignError(f"worker_cwd does not exist: {configured}")
    return path


def _run_git(repo_root: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _digest_tree(root: Path) -> JsonObject:
    entries: list[JsonObject] = []
    if root.is_dir():
        for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": digest_file(path),
                }
            )
    return {
        "sha256": digest_value(entries),
        "file_count": len(entries),
        "size_bytes": sum(cast(int, entry["size_bytes"]) for entry in entries),
    }


def _spend_summary(
    spec: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> JsonObject:
    expected = float(spec["expected_spend_usd"])
    stop_threshold = expected * 1.25
    recorded = sum(
        float(record["cost_usd"])
        for record in records
        if record.get("record_type") == "call"
    )
    network_policies = {
        cast(str, workload.get("network_policy", "forbidden"))
        for workload in cast(Sequence[Mapping[str, Any]], spec["workloads"])
    }
    tasks = [record for record in records if record.get("record_type") == "task"]
    provider_accounted = bool(tasks) and all(
        bool(cast(Mapping[str, Any], task["conformance"])["provider_accounting"])
        for task in tasks
    )
    if network_policies == {"forbidden"}:
        actual: float | None = 0.0
        basis = "network_forbidden_no_billed_calls"
    elif provider_accounted:
        actual = recorded
        basis = "worker_provider_accounting"
    else:
        actual = None
        basis = "unavailable_nonconforming_worker"
    threshold_exceeded = actual is not None and actual > stop_threshold
    return {
        "expected_usd": expected,
        "stop_threshold_usd": stop_threshold,
        "recorded_cost_usd": recorded,
        "actual_spend_usd": actual,
        "actual_spend_basis": basis,
        "threshold_exceeded": threshold_exceeded,
        "stop_reason": "schedule_complete",
    }


def run_campaign(
    spec_path: Path,
    output_dir: Path,
    *,
    resume: bool = False,
) -> JsonObject:
    """Execute, validate, analyze, and seal a campaign directory."""
    spec_path = spec_path.resolve()
    spec = load_campaign(spec_path)
    repo_root = Path(__file__).resolve().parent.parent
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not resume:
        raise CampaignError(
            f"output directory is not empty: {output_dir}; use --resume or a fresh path"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    frozen_spec = _frozen_spec(spec)
    frozen_spec_path = output_dir / "campaign.json"
    frozen_spec_payload = _canonical_bytes(frozen_spec) + b"\n"
    if resume:
        if not frozen_spec_path.is_file():
            raise CampaignError("resume requires the original output campaign.json")
        if frozen_spec_path.read_bytes() != frozen_spec_payload:
            raise CampaignError("resume campaign does not match the frozen output campaign.json")
    else:
        frozen_spec_path.write_bytes(frozen_spec_payload)
    requests_dir = output_dir / "requests"
    worker_dir = output_dir / "worker-results"
    requests_dir.mkdir(exist_ok=True)
    worker_dir.mkdir(exist_ok=True)
    ledger_path = output_dir / "raw-records.jsonl"
    schedule = build_schedule(spec)
    completed = (
        _validated_completed_keys(
            ledger_path,
            spec=spec,
            output_dir=output_dir,
            schedule=schedule,
            repo_root=repo_root,
            spec_dir=spec_path.parent,
        )
        if resume
        else set()
    )
    workload_map = _workload_by_id(spec)
    started = time.monotonic()
    portable_commands: dict[str, list[str]] = {}

    for run in schedule:
        if run.key in completed:
            continue
        workload = workload_map[run.workload_id]
        request = _request_payload(
            spec,
            workload,
            run,
            output_dir,
            repo_root=repo_root,
            spec_dir=spec_path.parent,
        )
        stem = _artifact_stem(run)
        request_path = requests_dir / f"{stem}.json"
        result_path = worker_dir / f"{stem}.jsonl"
        request_path.write_bytes(_canonical_bytes(request) + b"\n")

        configured_command = cast(Sequence[str], workload["worker_command"])
        command = _expand_command(
            configured_command, repo_root=repo_root, spec_dir=spec_path.parent
        )
        command.extend(["--request", str(request_path), "--output", str(result_path)])
        portable_commands[run.workload_id] = _portable_command(
            cast(Sequence[str], workload["worker_command"]), repo_root
        )
        cwd = _safe_working_directory(
            workload, repo_root=repo_root, spec_dir=spec_path.parent
        )
        timeout_s = _require_int(
            workload.get("timeout_seconds", 3600),
            f"{run.workload_id}.timeout_seconds",
            minimum=1,
        )
        try:
            completed_process = subprocess.run(
                command,
                cwd=cwd,
                env=_worker_environment(
                    cast(str, workload.get("network_policy", "forbidden")),
                    repo_root,
                ),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as error:
            raise CampaignError(
                f"worker timed out for {run.key} after {timeout_s}s"
            ) from error
        (worker_dir / f"{stem}.stdout").write_text(completed_process.stdout)
        (worker_dir / f"{stem}.stderr").write_text(completed_process.stderr)
        if completed_process.returncode != 0:
            raise CampaignError(
                f"worker failed for {run.key} with exit {completed_process.returncode}; "
                f"see {stem}.stderr"
            )
        normalized = validate_worker_records(_read_jsonl(result_path), request)
        _append_records(ledger_path, normalized)
        request_path.unlink()
        result_path.unlink()
        (worker_dir / f"{stem}.stdout").unlink()
        (worker_dir / f"{stem}.stderr").unlink()

    records = _read_jsonl(ledger_path)
    validate_complete_ledger(records, schedule)
    spend = _spend_summary(spec, records)
    if spend["threshold_exceeded"]:
        raise CampaignError(
            "actual provider spend exceeded 125% of the frozen estimate; "
            "the completed schedule cannot be sealed"
        )
    analysis = analyze_campaign(spec, records)
    analysis_path = output_dir / "analysis.json"
    analysis_path.write_bytes(_canonical_bytes(analysis) + b"\n")
    report_path = output_dir / "report.md"
    report_path.write_text(render_report(analysis))
    protocol_copy = dict(cast(Mapping[str, Any], frozen_spec["protocol"]))

    manifest: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "joint_campaign_manifest",
        "campaign_id": spec["campaign_id"],
        "stage": spec["stage"],
        "paper_evidence": spec["paper_evidence"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": time.monotonic() - started,
        "agentc_git_commit": _run_git(repo_root, "rev-parse", "HEAD"),
        "agentc_git_dirty": bool(_run_git(repo_root, "status", "--short")),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "protocol": protocol_copy,
        "workloads": [
            {
                "workload_id": workload["workload_id"],
                "family": workload["family"],
                "unengineered_upstream": workload["unengineered_upstream"],
                "split": workload.get("split", "unspecified"),
                "task_count": len(cast(Sequence[str], workload["task_ids"])),
                "task_ids_sha256": workload["task_ids_sha256"],
                "repetitions": workload["repetitions"],
                "model_pair": workload["model_pair"],
                "provenance": workload["provenance"],
            }
            for workload in cast(Sequence[Mapping[str, Any]], spec["workloads"])
        ],
        "worker_commands": portable_commands,
        "schedule": {
            "runs": len(schedule),
            "digest": digest_value(
                [
                    {
                        "workload_id": run.workload_id,
                        "task_id": run.task_id,
                        "arm": run.arm,
                        "repetition": run.repetition,
                        "run_seed": run.run_seed,
                        "ordinal": run.ordinal,
                    }
                    for run in schedule
                ]
            ),
            "arms": list(PRIMARY_ARMS),
        },
        "spend": spend,
        "artifacts": {
            "campaign.json": digest_file(frozen_spec_path),
            "raw-records.jsonl": digest_file(ledger_path),
            "analysis.json": digest_file(analysis_path),
            "report.md": digest_file(report_path),
            "state": _digest_tree(output_dir / "state"),
        },
        "completeness": {
            "status": "complete",
            "task_records": sum(record["record_type"] == "task" for record in records),
            "call_records": sum(record["record_type"] == "call" for record in records),
            "scheduled_runs": len(schedule),
            "distinct_unengineered_families": len(
                {
                    cast(str, workload["family"])
                    for workload in cast(
                        Sequence[Mapping[str, Any]], spec["workloads"]
                    )
                    if bool(workload["unengineered_upstream"])
                }
            ),
        },
        "interpretation_limits": spec.get("interpretation_limits", []),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(_canonical_bytes(manifest) + b"\n")
    return manifest


def validate_complete_ledger(
    records: Sequence[Mapping[str, Any]], schedule: Sequence[ScheduledRun]
) -> None:
    expected = {run.key for run in schedule}
    task_records = [record for record in records if record.get("record_type") == "task"]
    actual = [
        (
            cast(str, record["workload_id"]),
            cast(str, record["task_id"]),
            cast(str, record["arm"]),
            cast(int, record["repetition"]),
        )
        for record in task_records
    ]
    duplicates = len(actual) - len(set(actual))
    missing = expected - set(actual)
    extra = set(actual) - expected
    if duplicates or missing or extra:
        raise CampaignError(
            "incomplete ledger: "
            f"duplicates={duplicates}, missing={len(missing)}, extra={len(extra)}"
        )


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _mean(values: Iterable[float]) -> float:
    materialized = list(values)
    return statistics.fmean(materialized) if materialized else 0.0


def _task_key(record: Mapping[str, Any]) -> tuple[str, int]:
    return (cast(str, record["task_id"]), cast(int, record["repetition"]))


def _bootstrap_hierarchical(
    by_arm: Mapping[str, Mapping[tuple[str, int], Mapping[str, Any]]],
    arms: Sequence[str],
    statistic: Callable[[dict[str, list[Mapping[str, Any]]]], float],
    *,
    resamples: int,
    seed: int,
) -> BootstrapInterval:
    common_tasks = sorted(
        set.intersection(
            *(set(record[0] for record in by_arm[arm]) for arm in arms)
        )
    )
    if not common_tasks:
        raise CampaignError(f"no paired tasks for bootstrap arms {arms}")

    def observed_sample() -> dict[str, list[Mapping[str, Any]]]:
        return {
            arm: [record for record in by_arm[arm].values()]
            for arm in arms
        }

    estimate = statistic(observed_sample())
    rng = random.Random(seed)
    draws: list[float] = []
    repetitions_by_arm_task: dict[tuple[str, str], list[int]] = {}
    for arm in arms:
        for task_id in common_tasks:
            repetitions_by_arm_task[(arm, task_id)] = sorted(
                repetition
                for candidate_task, repetition in by_arm[arm]
                if candidate_task == task_id
            )
    for _ in range(resamples):
        sampled: dict[str, list[Mapping[str, Any]]] = {arm: [] for arm in arms}
        for _task_slot in range(len(common_tasks)):
            task_id = common_tasks[rng.randrange(len(common_tasks))]
            common_repetitions = set(
                repetitions_by_arm_task[(arms[0], task_id)]
            )
            for arm in arms[1:]:
                common_repetitions &= set(repetitions_by_arm_task[(arm, task_id)])
            repetitions = sorted(common_repetitions)
            if not repetitions:
                raise CampaignError(f"task {task_id} has no paired repetitions")
            for _rep_slot in range(len(repetitions)):
                repetition = repetitions[rng.randrange(len(repetitions))]
                for arm in arms:
                    sampled[arm].append(by_arm[arm][(task_id, repetition)])
        value = statistic(sampled)
        if math.isfinite(value):
            draws.append(value)
    if not draws:
        return BootstrapInterval(estimate=estimate, low=estimate, high=estimate)
    low = cast(float, _percentile(draws, 0.025))
    high = cast(float, _percentile(draws, 0.975))
    return BootstrapInterval(estimate=estimate, low=low, high=high)


def _mcnemar(
    reference: Mapping[tuple[str, int], Mapping[str, Any]],
    candidate: Mapping[tuple[str, int], Mapping[str, Any]],
) -> JsonObject:
    both = sorted(set(reference) & set(candidate))
    ref_pass_candidate_fail = 0
    ref_fail_candidate_pass = 0
    for key in both:
        reference_pass = float(reference[key]["official_score"]) == 1.0
        candidate_pass = float(candidate[key]["official_score"]) == 1.0
        if reference_pass and not candidate_pass:
            ref_pass_candidate_fail += 1
        elif not reference_pass and candidate_pass:
            ref_fail_candidate_pass += 1
    discordant = ref_pass_candidate_fail + ref_fail_candidate_pass
    if discordant == 0:
        p_value = 1.0
    else:
        smaller = min(ref_pass_candidate_fail, ref_fail_candidate_pass)
        tail = sum(
            math.comb(discordant, index) * 0.5**discordant
            for index in range(smaller + 1)
        )
        p_value = min(1.0, 2.0 * tail)
    return {
        "reference_pass_candidate_fail": ref_pass_candidate_fail,
        "reference_fail_candidate_pass": ref_fail_candidate_pass,
        "p_value_two_sided_exact": p_value,
    }


def _task_damage(
    reference: Mapping[tuple[str, int], Mapping[str, Any]],
    candidate: Mapping[tuple[str, int], Mapping[str, Any]],
) -> JsonObject:
    paired = sorted(set(reference) & set(candidate))
    losses = [
        max(
            0.0,
            float(reference[key]["official_score"])
            - float(candidate[key]["official_score"]),
        )
        for key in paired
    ]
    return {
        "definition": "sum(max(0, q_reference - q_candidate))",
        "total": sum(losses),
        "mean_per_assigned_task": _mean(losses),
        "damaged_task_repetitions": sum(loss > 0.0 for loss in losses),
    }


def _aggregate_arm(
    task_records: Sequence[Mapping[str, Any]],
    call_records: Sequence[Mapping[str, Any]],
) -> JsonObject:
    completed = [record for record in task_records if record["task_status"] == "completed"]
    scores = [float(record["official_score"]) for record in task_records]
    latencies = [float(record["request_latency_ms"]) for record in call_records]
    total_input = sum(int(record["input_tokens"]) for record in call_records)
    total_cached = sum(int(record["cached_input_tokens"]) for record in call_records)
    total_output = sum(int(record["output_tokens"]) for record in call_records)
    total_reasoning = sum(int(record["reasoning_tokens"]) for record in call_records)
    total_tool = sum(int(record["tool_tokens"]) for record in call_records)
    total_cost = sum(float(record["cost_usd"]) for record in call_records)
    eligible = [record for record in call_records if bool(record["eligible"])]
    abstentions = [
        record for record in eligible if record.get("abstention_reason") is not None
    ]
    exploration = [record for record in call_records if bool(record["is_exploration"])]
    return {
        "assigned_tasks": len(task_records),
        "completed_tasks": len(completed),
        "failed_or_incomplete_tasks": len(task_records) - len(completed),
        "quality_mean": _mean(scores),
        "full_pass_rate": _mean(float(score == 1.0) for score in scores),
        "total_cost_usd": total_cost,
        "mean_cost_per_task_usd": total_cost / len(task_records) if task_records else 0.0,
        "input_tokens": total_input,
        "cached_input_tokens": total_cached,
        "output_tokens": total_output,
        "reasoning_tokens": total_reasoning,
        "tool_tokens": total_tool,
        "total_tokens": total_input + total_output + total_reasoning + total_tool,
        "request_latency_p50_ms": _percentile(latencies, 0.50),
        "request_latency_p95_ms": _percentile(latencies, 0.95),
        "request_latency_p99_ms": _percentile(latencies, 0.99),
        "mean_end_to_end_latency_ms": _mean(
            float(record["end_to_end_latency_ms"]) for record in task_records
        ),
        "eligible_calls": len(eligible),
        "abstention_calls": len(abstentions),
        "abstention_rate": len(abstentions) / len(eligible) if eligible else 0.0,
        "exploration_calls": len(exploration),
        "exploration_cost_usd": sum(float(record["cost_usd"]) for record in exploration),
        "shadow_calls": sum(bool(record["is_shadow"]) for record in call_records),
        "retry_count": sum(int(record["retry_count"]) for record in call_records),
        "failed_calls": sum(bool(record["failed"]) for record in call_records),
        "dispatch_fallbacks": sum(
            bool(record["dispatch_fallback"]) for record in call_records
        ),
        "safety_failures": sum(bool(record["safety_failure"]) for record in task_records),
    }


def _paired_statistic(
    field: str, reference_arm: str, candidate_arm: str
) -> Callable[[dict[str, list[Mapping[str, Any]]]], float]:
    def statistic(sample: dict[str, list[Mapping[str, Any]]]) -> float:
        reference = sample[reference_arm]
        candidate = sample[candidate_arm]
        return _mean(float(record[field]) for record in candidate) - _mean(
            float(record[field]) for record in reference
        )

    return statistic


def _paired_advantage_statistic(
    field: str, control_arm: str, candidate_arm: str
) -> Callable[[dict[str, list[Mapping[str, Any]]]], float]:
    def statistic(sample: dict[str, list[Mapping[str, Any]]]) -> float:
        control = sample[control_arm]
        candidate = sample[candidate_arm]
        return _mean(float(record[field]) for record in control) - _mean(
            float(record[field]) for record in candidate
        )

    return statistic


def _geometric_ratio_interval(
    by_arm: Mapping[str, Mapping[tuple[str, int], Mapping[str, Any]]],
    *,
    field: str,
    reference_arm: str,
    candidate_arm: str,
    resamples: int,
    seed: int,
) -> JsonObject:
    paired_keys = sorted(set(by_arm[reference_arm]) & set(by_arm[candidate_arm]))
    if not paired_keys or any(
        float(by_arm[reference_arm][key][field]) <= 0.0
        or float(by_arm[candidate_arm][key][field]) <= 0.0
        for key in paired_keys
    ):
        return {
            "estimate": None,
            "low": None,
            "high": None,
            "reason": "nonpositive paired value",
        }

    def log_ratio(sample: dict[str, list[Mapping[str, Any]]]) -> float:
        reference = sample[reference_arm]
        candidate = sample[candidate_arm]
        return _mean(
            math.log(float(candidate_record[field]) / float(reference_record[field]))
            for reference_record, candidate_record in zip(reference, candidate)
        )

    interval = _bootstrap_hierarchical(
        by_arm,
        (reference_arm, candidate_arm),
        log_ratio,
        resamples=resamples,
        seed=seed,
    )
    return {
        "estimate": math.exp(interval.estimate),
        "low": math.exp(interval.low),
        "high": math.exp(interval.high),
        "reason": None,
    }


def _interaction_cost_statistic(
    sample: dict[str, list[Mapping[str, Any]]]
) -> float:
    reference = _mean(
        float(record["_task_cost_usd"]) for record in sample[REFERENCE_ARM]
    )
    routing = _mean(
        float(record["_task_cost_usd"]) for record in sample["routing_only"]
    )
    rewrite = _mean(
        float(record["_task_cost_usd"])
        for record in sample["rewrite_only_fixed_strong"]
    )
    joint = _mean(
        float(record["_task_cost_usd"]) for record in sample["joint_guarded"]
    )
    return (reference - joint) - (reference - routing) - (reference - rewrite)


def _cost_per_task(
    task_records: Sequence[Mapping[str, Any]],
    calls: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str, int], float]:
    totals: dict[tuple[str, str, int], float] = {
        (
            cast(str, record["arm"]),
            cast(str, record["task_id"]),
            cast(int, record["repetition"]),
        ): 0.0
        for record in task_records
    }
    for record in calls:
        key = (
            cast(str, record["arm"]),
            cast(str, record["task_id"]),
            cast(int, record["repetition"]),
        )
        totals[key] = totals.get(key, 0.0) + float(record["cost_usd"])
    return totals


def _selection_valid_statistic(
    arms: Sequence[str], quality_margin: float
) -> Callable[[dict[str, list[Mapping[str, Any]]]], float]:
    def statistic(sample: dict[str, list[Mapping[str, Any]]]) -> float:
        reference_quality = _mean(
            float(record["official_score"]) for record in sample[REFERENCE_ARM]
        )
        admissible: list[str] = []
        for arm in INTERACTION_CONTROLS:
            quality = _mean(float(record["official_score"]) for record in sample[arm])
            if quality - reference_quality > quality_margin:
                admissible.append(arm)
        if not admissible:
            admissible = [REFERENCE_ARM]
        best_control_cost = min(
            _mean(float(record["_task_cost_usd"]) for record in sample[arm])
            for arm in admissible
        )
        joint_cost = _mean(
            float(record["_task_cost_usd"]) for record in sample["joint_guarded"]
        )
        return best_control_cost - joint_cost

    return statistic


def analyze_campaign(
    spec: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> JsonObject:
    """Produce ITT summaries, paired intervals, interactions, and negatives."""
    task_records = [record for record in records if record["record_type"] == "task"]
    call_records = [record for record in records if record["record_type"] == "call"]
    costs = _cost_per_task(task_records, call_records)
    enriched_tasks: list[JsonObject] = []
    for record in task_records:
        enriched = dict(record)
        enriched["_task_cost_usd"] = costs[
            (
                cast(str, record["arm"]),
                cast(str, record["task_id"]),
                cast(int, record["repetition"]),
            )
        ]
        enriched_tasks.append(enriched)

    resamples = cast(int, spec["bootstrap_resamples"])
    result_workloads: JsonObject = {}
    for workload in cast(Sequence[Mapping[str, Any]], spec["workloads"]):
        workload_id = cast(str, workload["workload_id"])
        workload_tasks = [
            record for record in enriched_tasks if record["workload_id"] == workload_id
        ]
        workload_calls = [
            record for record in call_records if record["workload_id"] == workload_id
        ]
        by_arm: dict[str, dict[tuple[str, int], Mapping[str, Any]]] = {
            arm: {
                _task_key(record): record
                for record in workload_tasks
                if record["arm"] == arm
            }
            for arm in PRIMARY_ARMS
        }
        arm_summaries: JsonObject = {}
        comparisons: JsonObject = {}
        for arm in PRIMARY_ARMS:
            arm_task_records = [record for record in workload_tasks if record["arm"] == arm]
            arm_call_records = [record for record in workload_calls if record["arm"] == arm]
            arm_summaries[arm] = _aggregate_arm(arm_task_records, arm_call_records)
        for arm in PRIMARY_ARMS:
            quality_interval = _bootstrap_hierarchical(
                by_arm,
                (REFERENCE_ARM, arm),
                _paired_statistic("official_score", REFERENCE_ARM, arm),
                resamples=resamples,
                seed=int.from_bytes(
                    hashlib.sha256(
                        f"quality\0{workload_id}\0{arm}".encode("utf-8")
                    ).digest()[:8],
                    "big",
                ),
            )
            cost_interval = _bootstrap_hierarchical(
                by_arm,
                (REFERENCE_ARM, arm),
                _paired_statistic("_task_cost_usd", REFERENCE_ARM, arm),
                resamples=resamples,
                seed=int.from_bytes(
                    hashlib.sha256(
                        f"cost\0{workload_id}\0{arm}".encode("utf-8")
                    ).digest()[:8],
                    "big",
                ),
            )
            comparisons[arm] = {
                "quality_delta_vs_reference": quality_interval.as_dict(),
                "cost_delta_usd_vs_reference": cost_interval.as_dict(),
                "cost_total_delta_usd_vs_reference": (
                    float(arm_summaries[arm]["total_cost_usd"])
                    - float(arm_summaries[REFERENCE_ARM]["total_cost_usd"])
                ),
                "cost_geometric_mean_ratio_vs_reference": _geometric_ratio_interval(
                    by_arm,
                    field="_task_cost_usd",
                    reference_arm=REFERENCE_ARM,
                    candidate_arm=arm,
                    resamples=resamples,
                    seed=int.from_bytes(
                        hashlib.sha256(
                            f"cost-ratio\0{workload_id}\0{arm}".encode("utf-8")
                        ).digest()[:8],
                        "big",
                    ),
                ),
                "latency_geometric_mean_ratio_vs_reference": _geometric_ratio_interval(
                    by_arm,
                    field="end_to_end_latency_ms",
                    reference_arm=REFERENCE_ARM,
                    candidate_arm=arm,
                    resamples=resamples,
                    seed=int.from_bytes(
                        hashlib.sha256(
                            f"latency-ratio\0{workload_id}\0{arm}".encode("utf-8")
                        ).digest()[:8],
                        "big",
                    ),
                ),
                "task_damage_vs_reference": _task_damage(
                    by_arm[REFERENCE_ARM], by_arm[arm]
                ),
                "mcnemar_full_pass": _mcnemar(by_arm[REFERENCE_ARM], by_arm[arm]),
                "quality_noninferior_point": quality_interval.estimate
                > float(workload["quality_margin"]),
                "quality_noninferior_interval": quality_interval.low
                > float(workload["quality_margin"]),
            }

        selection_valid_arms = tuple(
            dict.fromkeys(
                (REFERENCE_ARM, *INTERACTION_CONTROLS, "joint_guarded")
            )
        )
        selection_valid = _bootstrap_hierarchical(
            by_arm,
            selection_valid_arms,
            _selection_valid_statistic(
                selection_valid_arms, float(workload["quality_margin"])
            ),
            resamples=resamples,
            seed=int.from_bytes(
                hashlib.sha256(
                    f"selection-valid\0{workload_id}".encode("utf-8")
                ).digest()[:8],
                "big",
            ),
        )
        reference_cost = float(arm_summaries[REFERENCE_ARM]["mean_cost_per_task_usd"])
        joint_cost = float(arm_summaries["joint_guarded"]["mean_cost_per_task_usd"])
        joint_summary = cast(Mapping[str, Any], arm_summaries["joint_guarded"])
        named_control_intervals: JsonObject = {}
        for control in INTERACTION_CONTROLS:
            cost_advantage = _bootstrap_hierarchical(
                by_arm,
                (control, "joint_guarded"),
                _paired_advantage_statistic(
                    "_task_cost_usd", control, "joint_guarded"
                ),
                resamples=resamples,
                seed=int.from_bytes(
                    hashlib.sha256(
                        f"joint-control-cost\0{workload_id}\0{control}".encode(
                            "utf-8"
                        )
                    ).digest()[:8],
                    "big",
                ),
            )
            quality_delta = _bootstrap_hierarchical(
                by_arm,
                (control, "joint_guarded"),
                _paired_statistic("official_score", control, "joint_guarded"),
                resamples=resamples,
                seed=int.from_bytes(
                    hashlib.sha256(
                        f"joint-control-quality\0{workload_id}\0{control}".encode(
                            "utf-8"
                        )
                    ).digest()[:8],
                    "big",
                ),
            )
            named_control_intervals[control] = {
                "joint_cost_advantage_usd": cost_advantage.as_dict(),
                "joint_quality_delta": quality_delta.as_dict(),
            }
        cost_synergy = _bootstrap_hierarchical(
            by_arm,
            (
                REFERENCE_ARM,
                "routing_only",
                "rewrite_only_fixed_strong",
                "joint_guarded",
            ),
            _interaction_cost_statistic,
            resamples=resamples,
            seed=int.from_bytes(
                hashlib.sha256(
                    f"interaction-cost\0{workload_id}".encode("utf-8")
                ).digest()[:8],
                "big",
            ),
        )
        result_workloads[workload_id] = {
            "family": workload["family"],
            "split": workload.get("split", "unspecified"),
            "quality_margin": workload["quality_margin"],
            "arms": arm_summaries,
            "paired_comparisons": comparisons,
            "selection_valid_joint_cost_advantage_usd": selection_valid.as_dict(),
            "interaction": {
                "cost_synergy_usd_per_task": cost_synergy.as_dict(),
                "joint_vs_named_controls_cost_usd": {
                    arm: float(arm_summaries[arm]["mean_cost_per_task_usd"])
                    - joint_cost
                    for arm in INTERACTION_CONTROLS
                },
                "joint_vs_named_controls_intervals": named_control_intervals,
                "joint_beats_every_named_control_point": all(
                    joint_cost
                    < float(arm_summaries[arm]["mean_cost_per_task_usd"])
                    for arm in INTERACTION_CONTROLS
                ),
            },
            "negative_regimes": {
                "zero_opportunity": all(
                    int(record["candidate_count"]) <= 1
                    for record in workload_calls
                    if record["arm"] == "joint_guarded" and bool(record["eligible"])
                ),
                "abstention_dominant": float(joint_summary["abstention_rate"]) >= 0.5,
                "no_joint_efficiency_gain": joint_cost >= reference_cost,
                "joint_quality_point_beyond_margin": float(
                    comparisons["joint_guarded"]["quality_delta_vs_reference"]["estimate"]
                )
                <= float(workload["quality_margin"]),
                "interaction_not_positive": selection_valid.estimate <= 0.0,
                "safety_failure_observed": int(joint_summary["safety_failures"]) > 0,
            },
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "joint_campaign_analysis",
        "campaign_id": spec["campaign_id"],
        "stage": spec["stage"],
        "paper_evidence": spec["paper_evidence"],
        "analysis_contract": {
            "intention_to_treat": True,
            "pairing": "workload task, then repetition",
            "bootstrap": "deterministic hierarchical percentile",
            "bootstrap_resamples": resamples,
            "selection_repeated_within_resample": True,
            "held_out_tuning": "forbidden; P/T require a calibration lock",
        },
        "workloads": result_workloads,
    }


def render_report(analysis: Mapping[str, Any]) -> str:
    lines = [
        f"# Joint campaign: {analysis['campaign_id']}",
        "",
        f"Stage: `{analysis['stage']}`  ",
        f"Paper evidence: `{str(analysis['paper_evidence']).lower()}`",
        "",
    ]
    workloads = cast(Mapping[str, Mapping[str, Any]], analysis["workloads"])
    for workload_id, workload in workloads.items():
        lines.extend(
            [
                f"## {workload_id}",
                "",
                "| Arm | Quality | Cost/task | Tokens | p50 ms | p95 ms | Abstain | Explore $ |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        arms = cast(Mapping[str, Mapping[str, Any]], workload["arms"])
        for arm in PRIMARY_ARMS:
            summary = arms[arm]
            p50 = summary["request_latency_p50_ms"]
            p95 = summary["request_latency_p95_ms"]
            lines.append(
                f"| `{arm}` | {float(summary['quality_mean']):.4f} | "
                f"${float(summary['mean_cost_per_task_usd']):.6f} | "
                f"{int(summary['total_tokens'])} | "
                f"{float(p50) if p50 is not None else 0.0:.2f} | "
                f"{float(p95) if p95 is not None else 0.0:.2f} | "
                f"{100.0 * float(summary['abstention_rate']):.1f}% | "
                f"${float(summary['exploration_cost_usd']):.6f} |"
            )
        interval = cast(
            Mapping[str, float], workload["selection_valid_joint_cost_advantage_usd"]
        )
        negatives = cast(Mapping[str, bool], workload["negative_regimes"])
        active_negatives = [name for name, active in negatives.items() if active]
        lines.extend(
            [
                "",
                "Selection-valid joint cost advantage over the best admissible "
                f"named control: ${interval['estimate']:.6f} "
                f"[${interval['low']:.6f}, ${interval['high']:.6f}].",
                "",
                (
                    "Negative-regime flags: " + ", ".join(active_negatives)
                    if active_negatives
                    else "Negative-regime flags: none."
                ),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path, help="frozen campaign JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume a partially completed directory after validating its ledger",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        manifest = run_campaign(args.campaign, args.output, resume=args.resume)
    except CampaignError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
