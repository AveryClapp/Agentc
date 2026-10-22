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
from contextlib import ExitStack, contextmanager
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence, cast


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
        "OPENROUTER_API_KEY",
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
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


JsonObject = dict[str, Any]


class CampaignError(RuntimeError):
    """Raised when a campaign would be incomplete or non-reproducible."""


class CampaignWorkerSession(Protocol):
    def __enter__(self) -> CampaignWorkerSession: ...
    def run(self, request: JsonObject) -> Sequence[JsonObject]: ...
    def __exit__(self, *exc: Any) -> Any: ...


@contextmanager
def _owned_worker_context(
    session: CampaignWorkerSession,
) -> Iterator[CampaignWorkerSession]:
    entered = session.__enter__()
    try:
        yield entered
    except BaseException as error:
        if session.__exit__(*sys.exc_info()):
            raise CampaignError(
                "owned worker sessions cannot suppress campaign failures"
            ) from error
        raise
    else:
        session.__exit__(None, None, None)


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
    payload = (f"agentc-run-v1\0{workload_id}\0{task_id}\0{arm}\0{repetition}").encode(
        "utf-8"
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % (2**31 - 1)


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
    diagnostic = spec.get("diagnostic_arms")
    if diagnostic is not None:
        if (
            stage != "E0"
            or spec["paper_evidence"] is not False
            or not isinstance(diagnostic, list)
            or not diagnostic
            or any(arm not in PRIMARY_ARMS for arm in diagnostic)
            or diagnostic != [arm for arm in PRIMARY_ARMS if arm in diagnostic]
        ):
            raise CampaignError(
                "diagnostic_arms requires a nonempty frozen-order subset in non-paper E0"
            )

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
        raise CampaignError(f"Stage {stage} requires a positive expected_spend_usd")
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
        if lock["selections"].get("record_type") == "calibration_trial_authorization":
            raise CampaignError(
                "unsealed calibration trials cannot enter held-out stages"
            )
        if digest_value(lock["selections"]) != lock_digest:
            raise CampaignError("calibration_lock selections digest mismatch")

    workloads_value = spec.get("workloads")
    if not isinstance(workloads_value, list) or len(workloads_value) < 2:
        raise CampaignError("campaign must contain at least two workload families")
    workloads = workloads_value
    seen_ids: set[str] = set()
    seen_families: set[str] = set()
    unengineered_families: set[str] = set()
    summed_workload_spend = 0.0
    for index, raw_workload in enumerate(workloads):
        workload = _require_object(raw_workload, f"workloads[{index}]")
        workload_id = _require_string(
            workload.get("workload_id"), f"workloads[{index}].workload_id"
        )
        if not _PORTABLE_ID.fullmatch(workload_id):
            raise CampaignError(
                f"workloads[{index}].workload_id must be a portable identifier"
            )
        family = _require_string(workload.get("family"), f"workloads[{index}].family")
        if workload_id in seen_ids:
            raise CampaignError(f"duplicate workload_id: {workload_id}")
        seen_ids.add(workload_id)
        seen_families.add(family)
        unengineered = workload.get("unengineered_upstream")
        if not isinstance(unengineered, bool):
            raise CampaignError(f"{workload_id}.unengineered_upstream must be boolean")
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
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise CampaignError(
                f"{workload_id}.worker_command must be a non-empty string list"
            )
        task_ids_value = workload.get("task_ids")
        if (
            not isinstance(task_ids_value, list)
            or not task_ids_value
            or not all(
                isinstance(task_id, str) and task_id for task_id in task_ids_value
            )
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
        _require_int(
            workload.get("repetitions"),
            f"{workload_id}.repetitions",
            minimum=5 if stage in HELD_OUT_STAGES else 2,
        )
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
        if network_policy not in {"forbidden", "provider_allowed", "loopback_only"}:
            raise CampaignError(
                f"{workload_id}.network_policy must be forbidden, loopback_only or provider_allowed"
            )
        if network_policy == "loopback_only" and (
            stage != "E0" or spec["paper_evidence"] is not False
        ):
            raise CampaignError("loopback_only requires non-paper Stage E0")
        if stage in {"C", "P", "T"} and network_policy != "provider_allowed":
            raise CampaignError(
                f"{workload_id}: Stage {stage} requires provider_allowed network policy"
            )
        workload_spend = _finite_number(
            workload.get("expected_spend_usd"),
            f"{workload_id}.expected_spend_usd",
            minimum=0.0,
        )
        if network_policy == "provider_allowed" and workload_spend <= 0.0:
            raise CampaignError(
                f"{workload_id}: provider_allowed requires positive expected_spend_usd"
            )
        workload["expected_spend_usd"] = workload_spend
        summed_workload_spend += workload_spend
    if len(seen_families) < 2:
        raise CampaignError(
            "campaign must contain at least two distinct workload families"
        )
    if stage in {"C", "P", "T"} and len(unengineered_families) < 2:
        raise CampaignError(
            f"Stage {stage} requires at least two distinct unengineered workload families"
        )
    if not math.isclose(
        expected_spend_usd,
        summed_workload_spend,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise CampaignError(
            "expected_spend_usd must equal the sum of workload expected spend"
        )
    return spec


def build_schedule(spec: Mapping[str, Any]) -> list[ScheduledRun]:
    """Build the task-paired, deterministically counterbalanced schedule."""
    arms = cast(Sequence[str], spec.get("diagnostic_arms", spec["arms"]))
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
        "protocol_sha256": spec["protocol"]["sha256"],
        "workload_id": run.workload_id,
        "family": workload["family"],
        "split": workload.get("split", "unspecified"),
        "task_id": run.task_id,
        "arm": run.arm,
        "repetition": run.repetition,
        "run_seed": run.run_seed,
        "schedule_ordinal": run.ordinal,
        "model_pair": workload["model_pair"],
        "quality_margin": workload["quality_margin"],
        "workload_provenance": workload["provenance"],
        "network_policy": workload.get("network_policy", "forbidden"),
        "spend_contract": {
            "expected_cell_usd": workload["expected_spend_usd"],
            "stop_threshold_cell_usd": float(workload["expected_spend_usd"]) * 1.25,
        },
        "storage_path": str(_cell_storage_path(output_dir, run)),
        "arm_configuration": cast(
            Mapping[str, Any], spec.get("arm_configurations", {})
        ).get(run.arm, {}),
        "workload_configuration": _expand_runtime_value(
            workload.get("worker_configuration", {}),
            repo_root=repo_root,
            spec_dir=spec_dir,
        ),
        "worker_timeout_seconds": workload.get("timeout_seconds", 3600),
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
                records.append(_require_object(value, f"{path}:{line_number}"))
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
    score_available = _metric_available(task, "official_score")
    if score_available:
        score = _finite_number(task.get("official_score"), "task.official_score")
        if not 0.0 <= score <= 1.0:
            raise CampaignError("task.official_score must be in [0, 1]")
    elif status != "incomplete" or request["stage"] in {"C", "P", "T"}:
        raise CampaignError(
            "unknown official score requires an incomplete diagnostic cell"
        )
    _finite_number(
        task.get("end_to_end_latency_ms"),
        "task.end_to_end_latency_ms",
        minimum=0.0,
    )
    if _metric_available(task, "safety_failure"):
        if not isinstance(task.get("safety_failure"), bool):
            raise CampaignError("task.safety_failure must be boolean")
    elif request["stage"] in {"C", "P", "T"}:
        raise CampaignError("paper stage requires measured task.safety_failure")
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
    if not score_available and conformance["official_score"]:
        raise CampaignError("unknown official score cannot claim grading conformance")
    if request["stage"] in {"C", "P", "T"}:
        if conformance["upstream_source_modified"]:
            raise CampaignError("paper stage worker reports modified upstream source")
        for field in ("official_task", "official_score", "provider_accounting"):
            if not conformance[field]:
                raise CampaignError(
                    f"paper stage requires task.conformance.{field}=true"
                )
    if (
        request["network_policy"] == "provider_allowed"
        and not conformance["provider_accounting"]
    ):
        raise CampaignError(
            "provider-allowed worker requires task.conformance.provider_accounting=true"
        )
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
        plan_id = _require_string(
            call.get("execution_plan_id"), "call.execution_plan_id"
        )
        if not _HEX_64.fullmatch(plan_id):
            raise CampaignError("call.execution_plan_id must be a lowercase SHA-256")
        rewrites = call.get("ordered_rewrites")
        if not isinstance(rewrites, list) or not all(
            isinstance(rewrite, str) and rewrite for rewrite in rewrites
        ):
            raise CampaignError("call.ordered_rewrites must be a string list")
        for field in (
            "input_tokens",
            "output_tokens",
            "retry_count",
        ):
            _require_int(call.get(field), f"call.{field}")
        for field in (
            "cached_input_tokens",
            "reasoning_tokens",
            "tool_tokens",
            "candidate_count",
        ):
            if _metric_available(call, field):
                _require_int(call.get(field), f"call.{field}")
        if (
            call.get("cached_input_tokens") is not None
            and call["cached_input_tokens"] > call["input_tokens"]
        ):
            raise CampaignError("cached input tokens exceed inclusive input tokens")
        if (
            call.get("reasoning_tokens") is not None
            and call["reasoning_tokens"] > call["output_tokens"]
        ):
            raise CampaignError("reasoning tokens exceed inclusive output tokens")
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


def _metric_available(record: Mapping[str, Any], field: str) -> bool:
    availability = _require_object(
        record.get("metric_availability", {}), "metric_availability"
    )
    available = availability.get(field, True)
    if type(available) is not bool:
        raise CampaignError(f"metric_availability.{field} must be boolean")
    if not available:
        if field not in record or record[field] is not None:
            raise CampaignError(f"unavailable metric {field} must be explicitly null")
        reasons = _require_object(
            record.get("unavailable_metric_reasons"), "unavailable_metric_reasons"
        )
        _require_string(reasons.get(field), f"unavailable_metric_reasons.{field}")
    return available


def _append_records(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        for record in records:
            handle.write(_canonical_bytes(record).decode("utf-8"))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_artifact(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)


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
    if network_policy in {"forbidden", "loopback_only"}:
        for name in SECRET_NAMES:
            env.pop(name, None)
    return env


def _safe_working_directory(
    workload: Mapping[str, Any], *, repo_root: Path, spec_dir: Path
) -> Path:
    configured = cast(str, workload.get("worker_cwd", "{repo}"))
    expanded = cast(
        str,
        _expand_runtime_value(configured, repo_root=repo_root, spec_dir=spec_dir),
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


def _run_git_bytes(repo_root: Path, *args: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
    )
    return completed.stdout if completed.returncode == 0 else None


def _agentc_source_context(
    repo_root: Path,
    output_dir: Path,
    *,
    paper_evidence: bool,
) -> JsonObject:
    commit = _run_git(repo_root, "rev-parse", "HEAD")
    tracked_diff = _run_git_bytes(
        repo_root, "diff", "--binary", "--no-ext-diff", "HEAD", "--"
    )
    untracked_raw = _run_git_bytes(
        repo_root, "ls-files", "--others", "--exclude-standard", "-z"
    )
    if commit is None or tracked_diff is None or untracked_raw is None:
        raise CampaignError("cannot freeze the Agentc Git source context")

    try:
        output_relative = output_dir.relative_to(repo_root)
    except ValueError:
        output_relative = None
    untracked: list[JsonObject] = []
    for raw_path in untracked_raw.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(os.fsdecode(raw_path))
        if output_relative is not None and (
            relative == output_relative or output_relative in relative.parents
        ):
            continue
        source_path = (repo_root / relative).resolve()
        try:
            source_path.relative_to(repo_root)
        except ValueError as error:
            raise CampaignError(
                "Git reported an untracked path outside the repo"
            ) from error
        if source_path.is_file():
            untracked.append(
                {
                    "path": relative.as_posix(),
                    "sha256": digest_file(source_path),
                }
            )
    untracked.sort(key=lambda entry: cast(str, entry["path"]))
    dirty = bool(tracked_diff or untracked)
    if paper_evidence and dirty:
        raise CampaignError(
            "paper-evidence campaigns require a clean Agentc source tree"
        )
    tracked_diff_sha256 = hashlib.sha256(tracked_diff).hexdigest()
    source_state_sha256 = digest_value(
        {
            "commit": commit,
            "tracked_diff_sha256": tracked_diff_sha256,
            "untracked": untracked,
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "joint_campaign_source_context",
        "agentc_git_commit": commit,
        "agentc_git_dirty": dirty,
        "agentc_source_state_sha256": source_state_sha256,
        "tracked_diff_sha256": tracked_diff_sha256,
        "untracked_file_count": len(untracked),
        "runner_sha256": digest_file(Path(__file__).resolve()),
        "runtime": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
    }


def _digest_tree(root: Path) -> JsonObject:
    entries: list[JsonObject] = []
    if root.is_dir():
        for path in sorted(
            candidate for candidate in root.rglob("*") if candidate.is_file()
        ):
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
    spec: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    stop_reason: str = "schedule_complete",
) -> JsonObject:
    recorded = sum(
        float(record["cost_usd"])
        for record in records
        if record.get("record_type") == "call"
    )
    by_workload: JsonObject = {}
    actual_total = 0.0
    exceeded_workloads: list[str] = []
    for workload in cast(Sequence[Mapping[str, Any]], spec["workloads"]):
        workload_id = cast(str, workload["workload_id"])
        policy = cast(str, workload.get("network_policy", "forbidden"))
        expected = float(workload["expected_spend_usd"])
        stop_threshold = expected * 1.25
        workload_calls = [
            record
            for record in records
            if record.get("record_type") == "call"
            and record.get("workload_id") == workload_id
        ]
        workload_recorded = sum(float(record["cost_usd"]) for record in workload_calls)
        if policy == "provider_allowed":
            workload_tasks = [
                record
                for record in records
                if record.get("record_type") == "task"
                and record.get("workload_id") == workload_id
            ]
            if not all(
                bool(
                    cast(Mapping[str, Any], task["conformance"])["provider_accounting"]
                )
                for task in workload_tasks
            ):
                raise CampaignError(
                    f"{workload_id}: provider spend lacks conforming accounting"
                )
            actual = workload_recorded
            basis = "worker_provider_accounting"
        else:
            actual = 0.0
            basis = (
                "loopback_synthetic_billing"
                if policy == "loopback_only"
                else "network_forbidden_no_billed_calls"
            )
        threshold_exceeded = actual > stop_threshold
        if threshold_exceeded:
            exceeded_workloads.append(workload_id)
        actual_total += actual
        by_workload[workload_id] = {
            "expected_usd": expected,
            "stop_threshold_usd": stop_threshold,
            "recorded_cost_usd": workload_recorded,
            "actual_spend_usd": actual,
            "actual_spend_basis": basis,
            "threshold_exceeded": threshold_exceeded,
        }

    bases = {workload["actual_spend_basis"] for workload in by_workload.values()}
    total_basis = (
        next(iter(bases))
        if len(bases) == 1
        else "mixed_network_policy_per_workload_accounting"
    )
    return {
        "expected_usd": float(spec["expected_spend_usd"]),
        "stop_threshold_usd": float(spec["expected_spend_usd"]) * 1.25,
        "recorded_cost_usd": recorded,
        "actual_spend_usd": actual_total,
        "actual_spend_basis": total_basis,
        "threshold_exceeded": bool(exceeded_workloads),
        "threshold_exceeded_workloads": exceeded_workloads,
        "stop_reason": stop_reason,
        "workloads": by_workload,
    }


def _workload_manifest_entries(spec: Mapping[str, Any]) -> list[JsonObject]:
    return [
        {
            "workload_id": workload["workload_id"],
            "family": workload["family"],
            "unengineered_upstream": workload["unengineered_upstream"],
            "split": workload.get("split", "unspecified"),
            "task_count": len(cast(Sequence[str], workload["task_ids"])),
            "task_ids_sha256": workload["task_ids_sha256"],
            "repetitions": workload["repetitions"],
            "model_pair": workload["model_pair"],
            "network_policy": workload.get("network_policy", "forbidden"),
            "expected_spend_usd": workload["expected_spend_usd"],
            "provenance": workload["provenance"],
        }
        for workload in cast(Sequence[Mapping[str, Any]], spec["workloads"])
    ]


def _schedule_manifest(schedule: Sequence[ScheduledRun]) -> JsonObject:
    return {
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
        "arms": [
            arm for arm in PRIMARY_ARMS if any(run.arm == arm for run in schedule)
        ],
    }


def _write_spend_stop_manifest(
    *,
    output_dir: Path,
    spec: Mapping[str, Any],
    frozen_spec: Mapping[str, Any],
    frozen_spec_path: Path,
    source_context: Mapping[str, Any],
    source_context_path: Path,
    schedule: Sequence[ScheduledRun],
    records: Sequence[Mapping[str, Any]],
    portable_commands: Mapping[str, Sequence[str]],
    started: float,
    workload_id: str,
) -> None:
    spend = _spend_summary(
        spec,
        records,
        stop_reason=f"spend_threshold_exceeded:{workload_id}",
    )
    manifest: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "joint_campaign_manifest",
        "campaign_id": spec["campaign_id"],
        "stage": spec["stage"],
        "paper_evidence": spec["paper_evidence"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": time.monotonic() - started,
        "agentc_git_commit": source_context["agentc_git_commit"],
        "agentc_git_dirty": source_context["agentc_git_dirty"],
        "agentc_source_state_sha256": source_context["agentc_source_state_sha256"],
        "runtime": source_context["runtime"],
        "protocol": dict(cast(Mapping[str, Any], frozen_spec["protocol"])),
        "workloads": _workload_manifest_entries(spec),
        "worker_commands": {
            key: list(value) for key, value in portable_commands.items()
        },
        "schedule": _schedule_manifest(schedule),
        "spend": spend,
        "artifacts": {
            "campaign.json": digest_file(frozen_spec_path),
            "run-context.json": digest_file(source_context_path),
            "raw-records.jsonl": digest_file(output_dir / "raw-records.jsonl"),
            "state": _digest_tree(output_dir / "state"),
        },
        "completeness": {
            "status": "stopped",
            "task_records": sum(
                record.get("record_type") == "task" for record in records
            ),
            "call_records": sum(
                record.get("record_type") == "call" for record in records
            ),
            "scheduled_runs": len(schedule),
            "missing_scheduled_runs": len(schedule)
            - sum(record.get("record_type") == "task" for record in records),
            "stopped_workload_id": workload_id,
        },
        "interpretation_limits": spec.get("interpretation_limits", []),
    }
    _write_artifact(output_dir / "manifest.json", _canonical_bytes(manifest) + b"\n")


def run_campaign(
    spec_path: Path,
    output_dir: Path,
    *,
    resume: bool = False,
    worker_sessions: Mapping[str, CampaignWorkerSession] | None = None,
) -> JsonObject:
    """Execute, validate, analyze, and seal a campaign directory."""
    spec_path = spec_path.resolve()
    spec = load_campaign(spec_path)
    if worker_sessions is not None:
        if resume:
            raise CampaignError(
                "owned worker sessions cannot resume a financial campaign"
            )
        if not worker_sessions.keys() <= {
            workload["workload_id"] for workload in spec["workloads"]
        }:
            raise CampaignError("owned session names an unknown workload")
    for workload in spec["workloads"]:
        if workload["worker_command"][0].startswith("owned:") and workload[
            "workload_id"
        ] not in (worker_sessions or {}):
            raise CampaignError(
                "owned workload requires an explicitly supplied worker session"
            )
    repo_root = Path(__file__).resolve().parent.parent
    output_dir = output_dir.resolve()
    source_context = _agentc_source_context(
        repo_root,
        output_dir,
        paper_evidence=bool(spec["paper_evidence"]),
    )
    source_context["worker_execution_modes"] = {
        workload["workload_id"]: "owned_session"
        if workload["workload_id"] in (worker_sessions or {})
        else "subprocess"
        for workload in spec["workloads"]
    }
    if output_dir.exists() and any(output_dir.iterdir()) and not resume:
        raise CampaignError(
            f"output directory is not empty: {output_dir}; use --resume or a fresh path"
        )
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if worker_sessions:
        output_dir.chmod(0o700)
    frozen_spec = _frozen_spec(spec)
    frozen_spec_path = output_dir / "campaign.json"
    frozen_spec_payload = _canonical_bytes(frozen_spec) + b"\n"
    if resume:
        if not frozen_spec_path.is_file():
            raise CampaignError("resume requires the original output campaign.json")
        if frozen_spec_path.read_bytes() != frozen_spec_payload:
            raise CampaignError(
                "resume campaign does not match the frozen output campaign.json"
            )
    else:
        _write_artifact(frozen_spec_path, frozen_spec_payload)
    source_context_path = output_dir / "run-context.json"
    source_context_payload = _canonical_bytes(source_context) + b"\n"
    if resume:
        if not source_context_path.is_file():
            raise CampaignError("resume requires the original output run-context.json")
        if source_context_path.read_bytes() != source_context_payload:
            raise CampaignError(
                "resume source context does not match the implementation that "
                "produced the existing ledger"
            )
    else:
        _write_artifact(source_context_path, source_context_payload)
    manifest_path = output_dir / "manifest.json"
    if resume and manifest_path.is_file():
        previous_manifest = _load_json(manifest_path)
        previous_completeness = _require_object(
            previous_manifest.get("completeness"), "manifest.completeness"
        )
        if previous_completeness.get("status") == "stopped":
            raise CampaignError(
                "campaign was stopped by its frozen spend contract; "
                "resume requires a newly reviewed campaign and output directory"
            )
    requests_dir = output_dir / "requests"
    worker_dir = output_dir / "worker-results"
    requests_dir.mkdir(mode=0o700, exist_ok=True)
    worker_dir.mkdir(mode=0o700, exist_ok=True)
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
    records_so_far = _read_jsonl(ledger_path) if ledger_path.is_file() else []
    workload_map = _workload_by_id(spec)
    started = time.monotonic()
    portable_commands = {
        cast(str, workload["workload_id"]): _portable_command(
            cast(Sequence[str], workload["worker_command"]), repo_root
        )
        for workload in cast(Sequence[Mapping[str, Any]], spec["workloads"])
    }

    initial_spend = _spend_summary(
        spec,
        records_so_far,
        stop_reason="in_progress",
    )
    initially_exceeded = cast(
        Sequence[str], initial_spend["threshold_exceeded_workloads"]
    )
    if initially_exceeded:
        stopped_workload = initially_exceeded[0]
        _write_spend_stop_manifest(
            output_dir=output_dir,
            spec=spec,
            frozen_spec=frozen_spec,
            frozen_spec_path=frozen_spec_path,
            source_context=source_context,
            source_context_path=source_context_path,
            schedule=schedule,
            records=records_so_far,
            portable_commands=portable_commands,
            started=started,
            workload_id=stopped_workload,
        )
        raise CampaignError(
            f"{stopped_workload}: existing ledger exceeds 125% of the frozen "
            "cell estimate; campaign stopped"
        )

    with ExitStack() as session_stack:
        active_sessions = {
            name: session_stack.enter_context(_owned_worker_context(session))
            for name, session in sorted((worker_sessions or {}).items())
        }
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
            spend_before = _spend_summary(
                spec,
                records_so_far,
                stop_reason="in_progress",
            )
            request_spend = _require_object(
                request["spend_contract"], "request.spend_contract"
            )
            request_spend["actual_before_run_usd"] = cast(
                Mapping[str, Any], spend_before["workloads"]
            )[run.workload_id]["actual_spend_usd"]
            stem = _artifact_stem(run)
            request_path = requests_dir / f"{stem}.json"
            result_path = worker_dir / f"{stem}.jsonl"
            _write_artifact(request_path, _canonical_bytes(request) + b"\n")

            if run.workload_id in active_sessions:
                worker_started = time.monotonic()
                normalized = validate_worker_records(
                    active_sessions[run.workload_id].run(request), request
                )
                if (
                    time.monotonic() - worker_started
                    > request["worker_timeout_seconds"]
                ):
                    raise CampaignError(
                        "owned worker exceeded its frozen cell timeout; campaign stopped"
                    )
            else:
                configured_command = cast(Sequence[str], workload["worker_command"])
                command = _expand_command(
                    configured_command, repo_root=repo_root, spec_dir=spec_path.parent
                )
                command.extend(
                    ["--request", str(request_path), "--output", str(result_path)]
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
                _write_artifact(
                    worker_dir / f"{stem}.stdout", completed_process.stdout.encode()
                )
                _write_artifact(
                    worker_dir / f"{stem}.stderr", completed_process.stderr.encode()
                )
                if completed_process.returncode != 0:
                    raise CampaignError(
                        f"worker failed for {run.key} with exit {completed_process.returncode}; see {stem}.stderr"
                    )
                normalized = validate_worker_records(_read_jsonl(result_path), request)
            _append_records(ledger_path, normalized)
            records_so_far.extend(normalized)
            request_path.unlink()
            if run.workload_id not in active_sessions:
                result_path.unlink()
                (worker_dir / f"{stem}.stdout").unlink()
                (worker_dir / f"{stem}.stderr").unlink()

            spend_after = _spend_summary(
                spec,
                records_so_far,
                stop_reason="in_progress",
            )
            exceeded = cast(Sequence[str], spend_after["threshold_exceeded_workloads"])
            if exceeded:
                stopped_workload = exceeded[0]
                _write_spend_stop_manifest(
                    output_dir=output_dir,
                    spec=spec,
                    frozen_spec=frozen_spec,
                    frozen_spec_path=frozen_spec_path,
                    source_context=source_context,
                    source_context_path=source_context_path,
                    schedule=schedule,
                    records=records_so_far,
                    portable_commands=portable_commands,
                    started=started,
                    workload_id=stopped_workload,
                )
                raise CampaignError(
                    f"{stopped_workload}: actual provider spend exceeded 125% "
                    "of the frozen cell estimate; campaign stopped"
                )

    records = _read_jsonl(ledger_path)
    validate_complete_ledger(records, schedule)
    unknown_scores = sum(
        record.get("official_score") is None
        for record in records
        if record["record_type"] == "task"
    )
    spend = _spend_summary(spec, records)
    if spend["threshold_exceeded"]:
        raise CampaignError(
            "actual provider spend exceeded 125% of the frozen estimate; "
            "the completed schedule cannot be sealed"
        )
    analysis = (
        analyze_diagnostic_campaign(spec, records)
        if "diagnostic_arms" in spec
        else analyze_campaign(spec, records)
    )
    analysis_path = output_dir / "analysis.json"
    _write_artifact(analysis_path, _canonical_bytes(analysis) + b"\n")
    report_path = output_dir / "report.md"
    _write_artifact(report_path, render_report(analysis).encode())
    protocol_copy = dict(cast(Mapping[str, Any], frozen_spec["protocol"]))

    manifest: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "joint_campaign_manifest",
        "campaign_id": spec["campaign_id"],
        "stage": spec["stage"],
        "paper_evidence": spec["paper_evidence"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": time.monotonic() - started,
        "agentc_git_commit": source_context["agentc_git_commit"],
        "agentc_git_dirty": source_context["agentc_git_dirty"],
        "agentc_source_state_sha256": source_context["agentc_source_state_sha256"],
        "runtime": source_context["runtime"],
        "worker_execution_modes": source_context["worker_execution_modes"],
        "protocol": protocol_copy,
        "workloads": _workload_manifest_entries(spec),
        "worker_commands": portable_commands,
        "schedule": _schedule_manifest(schedule),
        "spend": spend,
        "artifacts": {
            "campaign.json": digest_file(frozen_spec_path),
            "run-context.json": digest_file(source_context_path),
            "raw-records.jsonl": digest_file(ledger_path),
            "analysis.json": digest_file(analysis_path),
            "report.md": digest_file(report_path),
            "state": _digest_tree(output_dir / "state"),
        },
        "completeness": {
            "status": "diagnostic_incomplete"
            if unknown_scores
            else "diagnostic_complete"
            if "diagnostic_arms" in spec
            else "complete",
            "schedule_complete": True,
            "official_score_unknown_tasks": unknown_scores,
            "task_records": sum(record["record_type"] == "task" for record in records),
            "call_records": sum(record["record_type"] == "call" for record in records),
            "scheduled_runs": len(schedule),
            "distinct_unengineered_families": len(
                {
                    cast(str, workload["family"])
                    for workload in cast(Sequence[Mapping[str, Any]], spec["workloads"])
                    if bool(workload["unengineered_upstream"])
                }
            ),
        },
        "interpretation_limits": spec.get("interpretation_limits", []),
    }
    _write_artifact(manifest_path, _canonical_bytes(manifest) + b"\n")
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
        set.intersection(*(set(record[0] for record in by_arm[arm]) for arm in arms))
    )
    if not common_tasks:
        raise CampaignError(f"no paired tasks for bootstrap arms {arms}")

    def observed_sample() -> dict[str, list[Mapping[str, Any]]]:
        return {arm: [record for record in by_arm[arm].values()] for arm in arms}

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
            common_repetitions = set(repetitions_by_arm_task[(arms[0], task_id)])
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
    repetitions = sorted({repetition for _, repetition in both})
    discordant = ref_pass_candidate_fail + ref_fail_candidate_pass
    # Repeated outcomes for one task are a cluster, not independent pairs.
    # The hierarchical bootstrap is the overall inference; exact McNemar
    # diagnostics use one pair per task within each individual repetition.
    if len(repetitions) > 1:
        p_value = None
    elif discordant == 0:
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
        "independent_unit": "task",
        "pooled_repetitions": len(repetitions) > 1,
        "by_repetition": (
            {
                str(repetition): _mcnemar(
                    {key: reference[key] for key in both if key[1] == repetition},
                    {key: candidate[key] for key in both if key[1] == repetition},
                )
                for repetition in repetitions
            }
            if len(repetitions) > 1
            else {}
        ),
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
    completed = [
        record for record in task_records if record["task_status"] == "completed"
    ]
    unknown_scores = sum(record["official_score"] is None for record in task_records)
    scores = [
        float(record["official_score"])
        for record in task_records
        if record["official_score"] is not None
    ]
    latencies = [float(record["request_latency_ms"]) for record in call_records]
    total_input = sum(int(record["input_tokens"]) for record in call_records)

    def detail_sum(field: str) -> int | None:
        values = [record[field] for record in call_records]
        return (
            None
            if any(value is None for value in values)
            else sum(int(value) for value in values)
        )

    total_cached = detail_sum("cached_input_tokens")
    total_output = sum(int(record["output_tokens"]) for record in call_records)
    total_reasoning = detail_sum("reasoning_tokens")
    total_tool = detail_sum("tool_tokens")
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
        "official_score_unknown_tasks": unknown_scores,
        "quality_mean": None if unknown_scores else _mean(scores),
        "full_pass_rate": None
        if unknown_scores
        else _mean(float(score == 1.0) for score in scores),
        "total_cost_usd": total_cost,
        "mean_cost_per_task_usd": total_cost / len(task_records)
        if task_records
        else 0.0,
        "input_tokens": total_input,
        "cached_input_tokens": total_cached,
        "output_tokens": total_output,
        "reasoning_tokens": total_reasoning,
        "tool_tokens": total_tool,
        # Provider prompt/completion counts already include their breakdowns.
        "total_tokens": total_input + total_output,
        "token_breakdown_unknown_calls": {
            field: sum(record[field] is None for record in call_records)
            for field in ("cached_input_tokens", "reasoning_tokens", "tool_tokens")
        },
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
        "exploration_cost_usd": sum(
            float(record["cost_usd"]) for record in exploration
        ),
        "shadow_calls": sum(bool(record["is_shadow"]) for record in call_records),
        "retry_count": sum(int(record["retry_count"]) for record in call_records),
        "failed_calls": sum(bool(record["failed"]) for record in call_records),
        "dispatch_fallbacks": sum(
            bool(record["dispatch_fallback"]) for record in call_records
        ),
        "safety_failures": sum(
            bool(record["safety_failure"]) for record in task_records
        ),
        "safety_unknown_tasks": sum(
            record["safety_failure"] is None for record in task_records
        ),
    }


def _zero_opportunity_status(calls: Sequence[Mapping[str, Any]]) -> bool | None:
    decisions = [
        call
        for call in calls
        if call["eligible"] and not call["is_exploration"] and not call["is_shadow"]
    ]
    if any(
        call["candidate_count"] is not None and call["candidate_count"] > 1
        for call in decisions
    ):
        return False
    if not decisions or any(
        call["candidate_count"] is None or call.get("enumeration_complete") is False
        for call in decisions
    ):
        return None
    return True


def _safety_failure_status(summary: Mapping[str, Any]) -> bool | None:
    if summary["safety_failures"] > 0:
        return True
    return None if summary["safety_unknown_tasks"] else False


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


def _paired_total_statistic(
    field: str, reference_arm: str, candidate_arm: str
) -> Callable[[dict[str, list[Mapping[str, Any]]]], float]:
    def statistic(sample: dict[str, list[Mapping[str, Any]]]) -> float:
        reference = sample[reference_arm]
        candidate = sample[candidate_arm]
        return sum(float(record[field]) for record in candidate) - sum(
            float(record[field]) for record in reference
        )

    return statistic


def _median_task_effect(
    reference: Mapping[tuple[str, int], Mapping[str, Any]],
    candidate: Mapping[tuple[str, int], Mapping[str, Any]],
    field: str,
) -> float:
    task_ids = sorted(
        {task_id for task_id, _ in reference} & {task_id for task_id, _ in candidate}
    )
    effects: list[float] = []
    for task_id in task_ids:
        reference_repetitions = {
            repetition
            for candidate_task, repetition in reference
            if candidate_task == task_id
        }
        candidate_repetitions = {
            repetition
            for candidate_task, repetition in candidate
            if candidate_task == task_id
        }
        repetitions = sorted(reference_repetitions & candidate_repetitions)
        if not repetitions:
            continue
        reference_mean = _mean(
            float(reference[(task_id, repetition)][field]) for repetition in repetitions
        )
        candidate_mean = _mean(
            float(candidate[(task_id, repetition)][field]) for repetition in repetitions
        )
        effects.append(candidate_mean - reference_mean)
    if not effects:
        raise CampaignError("no paired task effects")
    return float(statistics.median(effects))


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


def _interaction_cost_statistic(sample: dict[str, list[Mapping[str, Any]]]) -> float:
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
    joint = _mean(float(record["_task_cost_usd"]) for record in sample["joint_guarded"])
    return (reference - joint) - (reference - routing) - (reference - rewrite)


def _cost_per_task(
    task_records: Sequence[Mapping[str, Any]],
    calls: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str, str, int], float]:
    totals: dict[tuple[str, str, str, int], float] = {
        (
            cast(str, record["workload_id"]),
            cast(str, record["arm"]),
            cast(str, record["task_id"]),
            cast(int, record["repetition"]),
        ): 0.0
        for record in task_records
    }
    for record in calls:
        key = (
            cast(str, record["workload_id"]),
            cast(str, record["arm"]),
            cast(str, record["task_id"]),
            cast(int, record["repetition"]),
        )
        totals[key] = totals.get(key, 0.0) + float(record["cost_usd"])
    return totals


def _resampled_best_control_statistic(
    quality_margin: float,
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
    if any(record.get("official_score") is None for record in task_records):
        raise CampaignError("primary analysis requires every assigned official score")
    call_records = [record for record in records if record["record_type"] == "call"]
    costs = _cost_per_task(task_records, call_records)
    enriched_tasks: list[JsonObject] = []
    for record in task_records:
        enriched = dict(record)
        enriched["_task_cost_usd"] = costs[
            (
                cast(str, record["workload_id"]),
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
            arm_task_records = [
                record for record in workload_tasks if record["arm"] == arm
            ]
            arm_call_records = [
                record for record in workload_calls if record["arm"] == arm
            ]
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
            cost_total_interval = _bootstrap_hierarchical(
                by_arm,
                (REFERENCE_ARM, arm),
                _paired_total_statistic("_task_cost_usd", REFERENCE_ARM, arm),
                resamples=resamples,
                seed=int.from_bytes(
                    hashlib.sha256(
                        f"cost-total\0{workload_id}\0{arm}".encode("utf-8")
                    ).digest()[:8],
                    "big",
                ),
            )
            latency_interval = _bootstrap_hierarchical(
                by_arm,
                (REFERENCE_ARM, arm),
                _paired_statistic("end_to_end_latency_ms", REFERENCE_ARM, arm),
                resamples=resamples,
                seed=int.from_bytes(
                    hashlib.sha256(
                        f"latency\0{workload_id}\0{arm}".encode("utf-8")
                    ).digest()[:8],
                    "big",
                ),
            )
            latency_total_interval = _bootstrap_hierarchical(
                by_arm,
                (REFERENCE_ARM, arm),
                _paired_total_statistic("end_to_end_latency_ms", REFERENCE_ARM, arm),
                resamples=resamples,
                seed=int.from_bytes(
                    hashlib.sha256(
                        f"latency-total\0{workload_id}\0{arm}".encode("utf-8")
                    ).digest()[:8],
                    "big",
                ),
            )
            comparisons[arm] = {
                "quality_delta_vs_reference": quality_interval.as_dict(),
                "quality_median_task_delta_vs_reference": _median_task_effect(
                    by_arm[REFERENCE_ARM], by_arm[arm], "official_score"
                ),
                "cost_delta_usd_vs_reference": cost_interval.as_dict(),
                "cost_median_task_delta_usd_vs_reference": _median_task_effect(
                    by_arm[REFERENCE_ARM], by_arm[arm], "_task_cost_usd"
                ),
                "cost_total_delta_usd_vs_reference": cost_total_interval.as_dict(),
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
                "latency_delta_ms_vs_reference": latency_interval.as_dict(),
                "latency_median_task_delta_ms_vs_reference": _median_task_effect(
                    by_arm[REFERENCE_ARM],
                    by_arm[arm],
                    "end_to_end_latency_ms",
                ),
                "latency_total_delta_ms_vs_reference": (
                    latency_total_interval.as_dict()
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
                "quality_point_within_margin": quality_interval.estimate
                > float(workload["quality_margin"]),
                "quality_interval_clears_margin_unadjusted": quality_interval.low
                > float(workload["quality_margin"]),
            }

        resampled_control_arms = tuple(
            dict.fromkeys((REFERENCE_ARM, *INTERACTION_CONTROLS, "joint_guarded"))
        )
        resampled_best_control = _bootstrap_hierarchical(
            by_arm,
            resampled_control_arms,
            _resampled_best_control_statistic(float(workload["quality_margin"])),
            resamples=resamples,
            seed=int.from_bytes(
                hashlib.sha256(
                    f"resampled-best-control\0{workload_id}".encode("utf-8")
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
                _paired_advantage_statistic("_task_cost_usd", control, "joint_guarded"),
                resamples=resamples,
                seed=int.from_bytes(
                    hashlib.sha256(
                        f"joint-control-cost\0{workload_id}\0{control}".encode("utf-8")
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
            "resampled_best_control_joint_cost_advantage_usd": (
                resampled_best_control.as_dict()
            ),
            "interaction": {
                "cost_synergy_usd_per_task": cost_synergy.as_dict(),
                "joint_vs_named_controls_cost_usd": {
                    arm: float(arm_summaries[arm]["mean_cost_per_task_usd"])
                    - joint_cost
                    for arm in INTERACTION_CONTROLS
                },
                "joint_vs_named_controls_intervals": named_control_intervals,
                "joint_beats_every_named_control_point": all(
                    joint_cost < float(arm_summaries[arm]["mean_cost_per_task_usd"])
                    for arm in INTERACTION_CONTROLS
                ),
            },
            "negative_regimes": {
                "zero_opportunity": _zero_opportunity_status(
                    [
                        record
                        for record in workload_calls
                        if record["arm"] == "joint_guarded"
                    ]
                ),
                "abstention_dominant": float(joint_summary["abstention_rate"]) >= 0.5,
                "no_joint_efficiency_gain": joint_cost >= reference_cost,
                "joint_quality_point_beyond_margin": float(
                    comparisons["joint_guarded"]["quality_delta_vs_reference"][
                        "estimate"
                    ]
                )
                <= float(workload["quality_margin"]),
                "interaction_not_positive": cost_synergy.estimate <= 0.0,
                "safety_failure_observed": _safety_failure_status(joint_summary),
            },
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "joint_campaign_analysis",
        "campaign_id": spec["campaign_id"],
        "stage": spec["stage"],
        "paper_evidence": spec["paper_evidence"],
        "interpretation_limits": list(spec.get("interpretation_limits", [])),
        "analysis_contract": {
            "intention_to_treat": True,
            "pairing": "workload task, then repetition",
            "bootstrap": "deterministic hierarchical percentile",
            "bootstrap_resamples": resamples,
            "selection_repeated_within_resample": True,
            "control_admissibility": "point quality delta; not a non-inferiority test",
            "inference_scope": "exploratory; intervals are not multiplicity-adjusted",
            "protocol_go_decision": False,
            "mean_median_and_total_paired_effects": True,
            "held_out_tuning": "forbidden; P/T require a calibration lock",
        },
        "workloads": result_workloads,
    }


def render_report(analysis: Mapping[str, Any]) -> str:
    if analysis["record_kind"] == "joint_campaign_diagnostic_analysis":
        lines = [
            f"# Campaign diagnostic: {analysis['campaign_id']}",
            "",
            "Stage E0; paper_evidence=false. This scheduled subset does not complete the primary campaign or support competitive claims.",
            "",
        ]
        for name, workload in analysis["workloads"].items():
            lines.extend(
                [
                    f"## {name}",
                    "",
                    "| Arm | Assigned tasks | Official mean | Unknown scores | Recorded cost | Unknown safety |",
                    "|---|---:|---:|---:|---:|---:|",
                ]
            )
            for arm, summary in workload["arms"].items():
                quality = (
                    "unavailable"
                    if summary["quality_mean"] is None
                    else f"{summary['quality_mean']:.4f}"
                )
                lines.append(
                    f"| {arm} | {summary['assigned_tasks']} | {quality} | {summary['official_score_unknown_tasks']} | {summary['total_cost_usd']:.6f} | {summary['safety_unknown_tasks']} |"
                )
            lines.append("")
        return "\n".join(lines)
    lines = [
        f"# Joint campaign: {analysis['campaign_id']}",
        "",
        f"Stage: `{analysis['stage']}`  ",
        f"Paper evidence: `{str(analysis['paper_evidence']).lower()}`",
        "",
    ]
    lines.extend(
        [
            "These summaries use unadjusted exploratory intervals. They do not "
            "establish protocol non-inferiority or a main-track GO decision.",
            "The best-control diagnostic reselects controls by their point quality "
            "delta in each resample; it is not a confirmatory comparison to a "
            "calibration-selected policy.",
            "",
        ]
    )
    limits = analysis.get("interpretation_limits", [])
    if limits:
        lines.extend(f"- {limit}" for limit in limits)
        lines.append("")
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
            Mapping[str, float],
            workload["resampled_best_control_joint_cost_advantage_usd"],
        )
        negatives = cast(Mapping[str, bool | None], workload["negative_regimes"])
        active_negatives = [name for name, active in negatives.items() if active]
        unknown_negatives = [
            name for name, active in negatives.items() if active is None
        ]
        lines.extend(
            [
                "",
                "Exploratory joint cost advantage over the resampled best "
                f"point-admissible named control: ${interval['estimate']:.6f} "
                f"[${interval['low']:.6f}, ${interval['high']:.6f}].",
                "",
                (
                    "Negative-regime flags: " + ", ".join(active_negatives)
                    if active_negatives
                    else "Negative-regime flags: none."
                ),
                "Unmeasured regime checks: " + ", ".join(unknown_negatives)
                if unknown_negatives
                else "All regime checks measured.",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def analyze_diagnostic_campaign(
    spec: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> JsonObject:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "joint_campaign_diagnostic_analysis",
        "campaign_id": spec["campaign_id"],
        "stage": "E0",
        "paper_evidence": False,
        "diagnostic_arms": list(spec["diagnostic_arms"]),
        "analysis_contract": {
            "protocol_go_decision": False,
            "competitive_comparisons": False,
            "intention_to_treat": True,
        },
        "workloads": {
            workload["workload_id"]: {
                "arms": {
                    arm: _aggregate_arm(
                        [
                            record
                            for record in records
                            if record["record_type"] == "task"
                            and record["workload_id"] == workload["workload_id"]
                            and record["arm"] == arm
                        ],
                        [
                            record
                            for record in records
                            if record["record_type"] == "call"
                            and record["workload_id"] == workload["workload_id"]
                            and record["arm"] == arm
                        ],
                    )
                    for arm in spec["diagnostic_arms"]
                }
            }
            for workload in spec["workloads"]
        },
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path, help="frozen campaign JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the frozen contract and summarize its schedule without executing workers",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume a partially completed directory after validating its ledger",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.validate_only:
            spec = load_campaign(args.campaign)
            schedule = build_schedule(spec)
            print(
                json.dumps(
                    {
                        "campaign_id": spec["campaign_id"],
                        "stage": spec["stage"],
                        "paper_evidence": spec["paper_evidence"],
                        "worker_execution": False,
                        "expected_spend_usd": spec["expected_spend_usd"],
                        "protocol_sha256": spec["protocol"]["sha256"],
                        "schedule": _schedule_manifest(schedule),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        manifest = run_campaign(args.campaign, args.output, resume=args.resume)
    except CampaignError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
