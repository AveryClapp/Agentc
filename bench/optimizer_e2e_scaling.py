"""Measure complete optimizer-call scaling across input size and concurrency.

This is the scaling companion to :mod:`bench.optimizer_e2e_overhead`. It times
the complete ``_native.optimize_plan`` FFI call while varying the exact
serialized call size and the number of Python threads issuing calls against one
hot call site. Every timed call carries a replication-unique span ID, which
pairs its outer wall-clock duration with the exact
``plan_audit.overhead_us`` row written by that call even when completion order
differs from audit order.

No provider is called. Build the extension in release mode before producing a
committed Stage E0 artifact::

    maturin develop --release -m crates/agentc-profiler/Cargo.toml
    python -m bench.optimizer_e2e_scaling \
      --build-profile release \
      --output bench/repro/optimizer-e2e-scaling-offpath-audit-2026-09-04.json \
      --raw-output bench/repro/optimizer-e2e-scaling-offpath-audit-2026-09-04.csv.gz
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import platform
import random
import sqlite3
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from agentc import _native

from bench import optimizer_e2e_overhead as e2e


_DEFAULT_CONTEXT_KIB = (4, 8, 16, 32, 64)
_DEFAULT_CONCURRENCY = (1, 8, 32)
_DEFAULT_CALLS_PER_CELL = 1_024
_DEFAULT_WARMUP = 100
_DEFAULT_REPLICATIONS = 5
_DEFAULT_BOOTSTRAP_RESAMPLES = 10_000
_DEFAULT_MAX_INFLIGHT_PLANS = 4
_MATRIX_SEED = 20_260_904
_SCENARIOS = ("joint_reference", "joint_admitted_rewrite")
_CONTENT_PATTERN = "agent systems context token "


def _load_average() -> list[float] | None:
    try:
        return list(os.getloadavg())
    except (AttributeError, OSError):
        return None


def _sized_call_json(site: str, *, target_bytes: int, span_number: int) -> str:
    """Return a valid call whose compact UTF-8 JSON encoding is exactly sized."""
    if target_bytes <= 0:
        raise ValueError("target_bytes must be positive")
    if not 0 <= span_number <= (2**64 - 1):
        raise ValueError("span_number must fit in an unsigned 64-bit span ID")

    call = e2e._call(site)
    call["span_id"] = f"{span_number:016x}"
    call["messages"] = [{"role": "user", "content": ""}]
    route_context = call["parameters"]["extra"]["agentc_route_context"]
    route_context["input_tokens_upper_bound"] = math.ceil(target_bytes / 4)

    empty = json.dumps(call, separators=(",", ":"), sort_keys=True)
    empty_bytes = len(empty.encode("utf-8"))
    if empty_bytes > target_bytes:
        raise ValueError(
            f"target_bytes={target_bytes} is smaller than call metadata ({empty_bytes})"
        )
    content_bytes = target_bytes - empty_bytes
    repeated = _CONTENT_PATTERN * math.ceil(content_bytes / len(_CONTENT_PATTERN))
    call["messages"][0]["content"] = repeated[:content_bytes]
    encoded = json.dumps(call, separators=(",", ":"), sort_keys=True)
    actual_bytes = len(encoded.encode("utf-8"))
    if actual_bytes != target_bytes:
        raise RuntimeError(
            f"sized-call construction drifted: expected {target_bytes}, got {actual_bytes}"
        )
    return encoded


def _settings(
    *,
    scenario: str,
    max_overhead_ms: float,
    max_inflight_plans: int = _DEFAULT_MAX_INFLIGHT_PLANS,
) -> dict[str, str]:
    if scenario not in _SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    return {
        "AGENTC_COMPOSE": "1",
        "AGENTC_ENABLED_RULES": "OutputBudget",
        "AGENTC_EVAL_PLANNER_MODE": "joint_guarded",
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": str(max_overhead_ms),
        "AGENTC_OPTIMIZE_MAX_INFLIGHT_PLANS": str(max_inflight_plans),
        "AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE": str(e2e._PAIRED_EVIDENCE),
        "AGENTC_OPTIMIZE_OBJECTIVE": "cost",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_PROVIDER": "openai",
    }


def _validated_admission_stats(*, expected_attempted: int) -> dict[str, int]:
    value = json.loads(_native.optimize_admission_stats())
    required = {
        "attempted",
        "admitted",
        "rejected_saturated",
        "inflight",
        "max_observed_inflight",
        "limit",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise RuntimeError("native planner-admission stats are incomplete")
    stats = {key: int(value[key]) for key in required}
    if stats["attempted"] != expected_attempted:
        raise RuntimeError(
            "planner-admission attempt count drifted: "
            f"expected {expected_attempted}, found {stats['attempted']}"
        )
    if stats["attempted"] != stats["admitted"] + stats["rejected_saturated"]:
        raise RuntimeError("planner-admission accounting does not conserve attempts")
    if stats["inflight"] != 0:
        raise RuntimeError("planner-admission permits leaked after timed calls")
    if not 0 <= stats["max_observed_inflight"] <= stats["limit"]:
        raise RuntimeError("planner-admission high-water mark exceeded its limit")
    return stats


def _read_correlated_audit_rows(
    audit_path: Path, after_id: int
) -> dict[str, tuple[int, str, str | None]]:
    with sqlite3.connect(audit_path) as connection:
        rows = connection.execute(
            "SELECT lower(hex(span_id)), overhead_us, plan_kind, "
            "planner_diagnostics_json, runtime_fallback_reason "
            "FROM plan_audit WHERE audit_id > ? ORDER BY audit_id",
            (after_id,),
        ).fetchall()
    correlated: dict[str, tuple[int, str, str | None]] = {}
    for span_id, overhead_us, plan_kind, diagnostics_json, runtime_reason in rows:
        key = str(span_id)
        if key in correlated:
            raise RuntimeError(f"duplicate measured span ID in plan_audit: {key}")
        fallback_reason: str | None = None
        if runtime_reason is not None:
            fallback_reason = str(runtime_reason)
        elif diagnostics_json is not None:
            diagnostics = json.loads(str(diagnostics_json))
            reason = diagnostics.get("fallback_reason")
            if isinstance(reason, str):
                fallback_reason = reason
        correlated[key] = (int(overhead_us), str(plan_kind), fallback_reason)
    return correlated


def _plan_result(plan_json: str) -> tuple[str, str | None]:
    value = json.loads(plan_json)
    kind = value.get("kind")
    if not isinstance(kind, str):
        raise RuntimeError("optimizer returned a plan without a string kind")
    fallback_reason: str | None = None
    diagnostics = value.get("agentc_planner_diagnostics")
    if isinstance(diagnostics, dict):
        reason = diagnostics.get("fallback_reason")
        if isinstance(reason, str):
            fallback_reason = reason
    runtime_fallback = value.get("agentc_runtime_fallback")
    if isinstance(runtime_fallback, dict):
        reason = runtime_fallback.get("fallback_reason")
        if isinstance(reason, str):
            fallback_reason = reason
    return kind, fallback_reason


def _partition_calls(
    call_jsons: Sequence[tuple[int, str]], concurrency: int
) -> list[list[tuple[int, str]]]:
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    if len(call_jsons) < concurrency:
        raise ValueError("calls_per_cell must be at least concurrency")
    return [list(call_jsons[worker::concurrency]) for worker in range(concurrency)]


def _measure_concurrent_calls(
    call_jsons: Sequence[tuple[int, str]], *, concurrency: int
) -> tuple[int, list[dict[str, Any]]]:
    """Time calls from ready workers; validate returned plans after the clock."""
    partitions = _partition_calls(call_jsons, concurrency)
    ready = threading.Barrier(concurrency + 1, timeout=30.0)
    start = threading.Event()

    def worker(
        worker_id: int, assigned: Sequence[tuple[int, str]]
    ) -> list[dict[str, Any]]:
        ready.wait()
        if not start.wait(timeout=30.0):
            raise RuntimeError("timed-call start signal was not delivered")
        measurements: list[dict[str, Any]] = []
        for worker_iteration, (sequence, call_json) in enumerate(assigned, start=1):
            started_at = time.perf_counter_ns()
            thread_cpu_started_at = time.thread_time_ns()
            plan_json = _native.optimize_plan(call_json)
            thread_cpu_ns = time.thread_time_ns() - thread_cpu_started_at
            elapsed_ns = time.perf_counter_ns() - started_at
            # The wall interval encloses the thread-CPU interval. A positive
            # difference is time the calling thread was runnable or blocked
            # but not executing, including scheduler delay, lock waits, and
            # GIL reacquisition after ``allow_threads`` returns.
            off_cpu_ns = max(0, elapsed_ns - thread_cpu_ns)
            measurements.append(
                {
                    "sequence": sequence,
                    "worker": worker_id,
                    "worker_iteration": worker_iteration,
                    "span_id": f"{sequence:016x}",
                    "e2e_ns": elapsed_ns,
                    "thread_cpu_ns": thread_cpu_ns,
                    "off_cpu_ns": off_cpu_ns,
                    "plan_json": plan_json,
                }
            )
        return measurements

    with ThreadPoolExecutor(
        max_workers=concurrency, thread_name_prefix="agentc-e2e"
    ) as executor:
        futures = [
            executor.submit(worker, worker_id, assigned)
            for worker_id, assigned in enumerate(partitions)
        ]
        ready.wait()
        group_started_at = time.perf_counter_ns()
        start.set()
        worker_measurements = [future.result() for future in futures]
        group_elapsed_ns = time.perf_counter_ns() - group_started_at

    measurements = [row for worker_rows in worker_measurements for row in worker_rows]
    measurements.sort(key=lambda row: int(row["sequence"]))
    return group_elapsed_ns, measurements


def _measure_replication(
    *,
    root: Path,
    scenario: str,
    target_bytes: int,
    concurrency: int,
    replication: int,
    calls_per_cell: int,
    warmup: int,
    max_overhead_ms: float,
    max_inflight_plans: int = _DEFAULT_MAX_INFLIGHT_PLANS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    storage = root / (
        f"{replication:02d}-{scenario}-{target_bytes}-bytes-c{concurrency}"
    )
    site = f"bench.optimizer-e2e-scaling:{scenario}:{target_bytes}"
    setup_call_json = _sized_call_json(site, target_bytes=target_bytes, span_number=0)
    reference_outcome_json = json.dumps(
        e2e._outcome(site, candidate=False), separators=(",", ":"), sort_keys=True
    )
    candidate_outcome_json = json.dumps(
        e2e._outcome(site, candidate=True), separators=(",", ":"), sort_keys=True
    )

    with (
        e2e._reset_native_optimizer(),
        e2e._isolated_agentc_environment(
            _settings(
                scenario=scenario,
                max_overhead_ms=max_overhead_ms,
                max_inflight_plans=max_inflight_plans,
            )
        ),
    ):
        configured_at = time.perf_counter_ns()
        _native.optimize_configure(str(storage))
        configure_ns = time.perf_counter_ns() - configured_at

        first_at = time.perf_counter_ns()
        first_plan = _native.optimize_plan(setup_call_json)
        first_call_ns = time.perf_counter_ns() - first_at
        if e2e._plan_kind(first_plan) != "pass_through":
            raise RuntimeError("the first cold call was not pass-through")
        first_token = str(_native.optimize_observe(first_plan, reference_outcome_json))
        if not first_token:
            raise RuntimeError(
                "the first cold call did not produce an observation token"
            )
        for _ in range(2):
            plan_json = _native.optimize_plan(setup_call_json)
            token = str(_native.optimize_observe(plan_json, reference_outcome_json))
            if not token:
                raise RuntimeError(
                    "reference warmup did not produce an observation token"
                )

        if scenario == "joint_admitted_rewrite":
            e2e._admit_joint_rewrite(setup_call_json, candidate_outcome_json)
        elif e2e._plan_kind(_native.optimize_plan(setup_call_json)) != "pass_through":
            raise RuntimeError(
                "joint reference scenario unexpectedly admitted a candidate"
            )

        expected_kind = "pass_through" if scenario == "joint_reference" else "rewritten"
        for _ in range(warmup):
            if e2e._plan_kind(_native.optimize_plan(setup_call_json)) != expected_kind:
                raise RuntimeError(f"{scenario} changed plan kind during warmup")

        audit_path = storage / "optimizer_audit.db"
        _native.optimize_flush()
        setup_attempted_rows = warmup + (
            24 if scenario == "joint_admitted_rewrite" else 4
        )
        audit_writer_before_measurement = e2e._validated_audit_stats(
            expected_attempted_rows=setup_attempted_rows
        )
        admission_before_measurement = _validated_admission_stats(
            expected_attempted=setup_attempted_rows
        )
        first_measured_audit_id = int(audit_writer_before_measurement["written_rows"])
        call_jsons = [
            (
                sequence,
                _sized_call_json(site, target_bytes=target_bytes, span_number=sequence),
            )
            for sequence in range(1, calls_per_cell + 1)
        ]
        group_elapsed_ns, measured = _measure_concurrent_calls(
            call_jsons, concurrency=concurrency
        )

        _native.optimize_flush()
        audit_writer = e2e._validated_audit_stats(
            expected_attempted_rows=setup_attempted_rows + calls_per_cell
        )
        admission_after_measurement = _validated_admission_stats(
            expected_attempted=setup_attempted_rows + calls_per_cell
        )
        measured_rejected_saturated = (
            admission_after_measurement["rejected_saturated"]
            - admission_before_measurement["rejected_saturated"]
        )

        # Close the native writer before reading its WAL through Python's
        # separately linked SQLite driver. Under high thread counts that reader
        # can otherwise observe the pre-measurement snapshot until the native
        # connection closes/checkpoints. Reset is outside both measured clocks;
        # the surrounding context manager keeps failure cleanup idempotent.
        _native.optimize_reset()
        audit_rows = _read_correlated_audit_rows(audit_path, first_measured_audit_id)
        if len(audit_rows) != calls_per_cell:
            raise RuntimeError(
                "audit lost measured calls: "
                f"expected {calls_per_cell}, found {len(audit_rows)}"
            )

        raw: list[dict[str, Any]] = []
        for row in measured:
            returned_kind, returned_fallback_reason = _plan_result(
                str(row.pop("plan_json"))
            )
            span_id = str(row["span_id"])
            if span_id not in audit_rows:
                raise RuntimeError(f"measured span missing from plan_audit: {span_id}")
            overhead_us, audit_kind, audit_fallback_reason = audit_rows[span_id]
            if returned_kind != audit_kind:
                raise RuntimeError(
                    f"returned/audited plan kind mismatch for {span_id}: "
                    f"returned={returned_kind}, audited={audit_kind}"
                )
            if returned_fallback_reason != audit_fallback_reason:
                raise RuntimeError(
                    f"returned/audited fallback mismatch for {span_id}: "
                    f"returned={returned_fallback_reason}, "
                    f"audited={audit_fallback_reason}"
                )
            allowed_kinds = (
                {"pass_through"}
                if scenario == "joint_reference"
                else {"rewritten", "pass_through"}
            )
            if returned_kind not in allowed_kinds:
                raise RuntimeError(
                    f"{scenario} returned unsupported plan kind: {returned_kind}"
                )
            internal_ns = overhead_us * 1_000
            e2e_ns = int(row["e2e_ns"])
            thread_cpu_ns = int(row["thread_cpu_ns"])
            off_cpu_ns = int(row["off_cpu_ns"])
            residual_ns = e2e_ns - internal_ns
            if residual_ns < 0:
                raise RuntimeError(
                    "internal planner clock exceeded its enclosing FFI call"
                )
            raw.append(
                {
                    "scenario": scenario,
                    "target_call_json_bytes": target_bytes,
                    "concurrency": concurrency,
                    "replication": replication,
                    "sequence": int(row["sequence"]),
                    "worker": int(row["worker"]),
                    "worker_iteration": int(row["worker_iteration"]),
                    "span_id": span_id,
                    "expected_plan_kind": expected_kind,
                    "plan_kind": returned_kind,
                    "fell_back_from_expected": returned_kind != expected_kind,
                    "planner_fallback_reason": returned_fallback_reason,
                    "e2e_ns": e2e_ns,
                    "thread_cpu_ns": thread_cpu_ns,
                    "off_cpu_ns": off_cpu_ns,
                    "internal_pre_audit_ns": internal_ns,
                    "boundary_state_audit_residual_ns": residual_ns,
                }
            )

        journal_mode = e2e._journal_mode(audit_path)
        if journal_mode != "wal":
            raise RuntimeError(f"optimizer audit DB is not in WAL mode: {journal_mode}")
        e2e_ns_values = [int(row["e2e_ns"]) for row in raw]
        thread_cpu_ns_values = [int(row["thread_cpu_ns"]) for row in raw]
        off_cpu_ns_values = [int(row["off_cpu_ns"]) for row in raw]
        internal_ns_values = [int(row["internal_pre_audit_ns"]) for row in raw]
        residual_ns_values = [
            int(row["boundary_state_audit_residual_ns"]) for row in raw
        ]
        plan_kind_counts = Counter(str(row["plan_kind"]) for row in raw)
        fallback_reason_counts = Counter(
            (
                str(row["planner_fallback_reason"])
                if row["planner_fallback_reason"] is not None
                else "unattributed"
            )
            for row in raw
            if row["fell_back_from_expected"]
        )
        fallback_count = sum(bool(row["fell_back_from_expected"]) for row in raw)
        correlated_saturation_count = sum(
            row["planner_fallback_reason"] == "optimizer_saturated" for row in raw
        )
        if correlated_saturation_count != measured_rejected_saturated:
            raise RuntimeError(
                "correlated saturation fallbacks disagree with admission counters: "
                f"plans={correlated_saturation_count}, "
                f"counter={measured_rejected_saturated}"
            )
        summary = {
            "scenario": scenario,
            "target_call_json_bytes": target_bytes,
            "concurrency": concurrency,
            "replication": replication,
            "sample_count": len(raw),
            "expected_plan_kind": expected_kind,
            "plan_kind_counts": dict(sorted(plan_kind_counts.items())),
            "fallback_reason_counts": dict(sorted(fallback_reason_counts.items())),
            "fallback_count": fallback_count,
            "fallback_rate_pct": 100.0 * fallback_count / len(raw),
            "configure_us": configure_ns / 1_000.0,
            "first_cold_call_us": first_call_ns / 1_000.0,
            "audit_journal_mode": journal_mode,
            "audit_writer_before_measurement": audit_writer_before_measurement,
            "audit_writer": audit_writer,
            "admission_before_measurement": admission_before_measurement,
            "admission_after_measurement": admission_after_measurement,
            "measured_rejected_saturated": measured_rejected_saturated,
            "group_elapsed_ms": group_elapsed_ns / 1_000_000.0,
            "throughput_calls_per_second": (
                calls_per_cell * 1_000_000_000.0 / group_elapsed_ns
            ),
            "measured_thread_cpu_core_equivalents": (
                sum(thread_cpu_ns_values) / group_elapsed_ns
            ),
            "e2e": e2e._summarize_ns(e2e_ns_values),
            "thread_cpu": e2e._summarize_ns(thread_cpu_ns_values),
            "off_cpu": e2e._summarize_ns(off_cpu_ns_values),
            "internal_pre_audit": e2e._summarize_ns(internal_ns_values),
            "boundary_state_audit_residual": e2e._summarize_ns(residual_ns_values),
        }
    return summary, raw


def _aggregate_cells(
    replications: Sequence[dict[str, Any]],
    raw: Sequence[dict[str, Any]],
    *,
    bootstrap_resamples: int,
) -> list[dict[str, Any]]:
    aggregate: list[dict[str, Any]] = []
    for scenario_index, scenario in enumerate(_SCENARIOS):
        target_sizes = sorted(
            {int(row["target_call_json_bytes"]) for row in replications}
        )
        concurrencies = sorted({int(row["concurrency"]) for row in replications})
        for target_bytes in target_sizes:
            for concurrency in concurrencies:
                cell_replications = [
                    row
                    for row in replications
                    if row["scenario"] == scenario
                    and row["target_call_json_bytes"] == target_bytes
                    and row["concurrency"] == concurrency
                ]
                cell_raw = [
                    row
                    for row in raw
                    if row["scenario"] == scenario
                    and row["target_call_json_bytes"] == target_bytes
                    and row["concurrency"] == concurrency
                ]
                if not cell_replications or not cell_raw:
                    raise RuntimeError(
                        f"missing matrix cell: {scenario}/{target_bytes}/{concurrency}"
                    )
                seed = (
                    _MATRIX_SEED
                    + scenario_index * 1_000_000
                    + target_bytes * 10
                    + concurrency
                )
                e2e_summary = e2e._summarize_ns(
                    [int(row["e2e_ns"]) for row in cell_raw]
                )
                thread_cpu_summary = e2e._summarize_ns(
                    [int(row["thread_cpu_ns"]) for row in cell_raw]
                )
                off_cpu_summary = e2e._summarize_ns(
                    [int(row["off_cpu_ns"]) for row in cell_raw]
                )
                internal_summary = e2e._summarize_ns(
                    [int(row["internal_pre_audit_ns"]) for row in cell_raw]
                )
                residual_summary = e2e._summarize_ns(
                    [int(row["boundary_state_audit_residual_ns"]) for row in cell_raw]
                )
                expected_kind = (
                    "pass_through" if scenario == "joint_reference" else "rewritten"
                )
                plan_kind_counts = Counter(str(row["plan_kind"]) for row in cell_raw)
                fallback_reason_counts = Counter(
                    (
                        str(row["planner_fallback_reason"])
                        if row["planner_fallback_reason"] is not None
                        else "unattributed"
                    )
                    for row in cell_raw
                    if row["fell_back_from_expected"]
                )
                fallback_count = sum(
                    bool(row["fell_back_from_expected"]) for row in cell_raw
                )
                saturation_count = sum(
                    row["planner_fallback_reason"] == "optimizer_saturated"
                    for row in cell_raw
                )
                aggregate.append(
                    {
                        "scenario": scenario,
                        "target_call_json_bytes": target_bytes,
                        "concurrency": concurrency,
                        "sample_count": len(cell_raw),
                        "replication_count": len(cell_replications),
                        "expected_plan_kind": expected_kind,
                        "plan_kind_counts": dict(sorted(plan_kind_counts.items())),
                        "fallback_reason_counts": dict(
                            sorted(fallback_reason_counts.items())
                        ),
                        "fallback_count": fallback_count,
                        "fallback_rate_pct": 100.0 * fallback_count / len(cell_raw),
                        "saturation_fallback_count": saturation_count,
                        "saturation_fallback_rate_pct": (
                            100.0 * saturation_count / len(cell_raw)
                        ),
                        "e2e": e2e_summary,
                        "thread_cpu": thread_cpu_summary,
                        "off_cpu": off_cpu_summary,
                        "internal_pre_audit": internal_summary,
                        "boundary_state_audit_residual": residual_summary,
                        "mean_residual_share_pct": (
                            100.0 * residual_summary["mean_us"] / e2e_summary["mean_us"]
                        ),
                        "mean_off_cpu_share_pct": (
                            100.0 * off_cpu_summary["mean_us"] / e2e_summary["mean_us"]
                        ),
                        "replication_mean_p50_us": e2e._bootstrap_mean_ci(
                            [float(row["e2e"]["p50_us"]) for row in cell_replications],
                            resamples=bootstrap_resamples,
                            seed=seed,
                        ),
                        "replication_mean_p99_us": e2e._bootstrap_mean_ci(
                            [float(row["e2e"]["p99_us"]) for row in cell_replications],
                            resamples=bootstrap_resamples,
                            seed=seed + 1,
                        ),
                        "replication_mean_throughput_calls_per_second": (
                            e2e._bootstrap_mean_ci(
                                [
                                    float(row["throughput_calls_per_second"])
                                    for row in cell_replications
                                ],
                                resamples=bootstrap_resamples,
                                seed=seed + 2,
                            )
                        ),
                        "replication_mean_thread_cpu_core_equivalents": (
                            e2e._bootstrap_mean_ci(
                                [
                                    float(row["measured_thread_cpu_core_equivalents"])
                                    for row in cell_replications
                                ],
                                resamples=bootstrap_resamples,
                                seed=seed + 3,
                            )
                        ),
                    }
                )

    by_key = {
        (
            str(row["scenario"]),
            int(row["target_call_json_bytes"]),
            int(row["concurrency"]),
        ): row
        for row in aggregate
    }
    for row in aggregate:
        baseline = by_key[(str(row["scenario"]), int(row["target_call_json_bytes"]), 1)]
        throughput = float(
            row["replication_mean_throughput_calls_per_second"]["estimate"]
        )
        baseline_throughput = float(
            baseline["replication_mean_throughput_calls_per_second"]["estimate"]
        )
        concurrency = int(row["concurrency"])
        speedup = throughput / baseline_throughput
        row["throughput_speedup_vs_c1"] = speedup
        row["throughput_efficiency_pct"] = 100.0 * speedup / concurrency
        row["p50_inflation_vs_c1"] = float(row["e2e"]["p50_us"]) / float(
            baseline["e2e"]["p50_us"]
        )
        row["p99_inflation_vs_c1"] = float(row["e2e"]["p99_us"]) / float(
            baseline["e2e"]["p99_us"]
        )
    return aggregate


def run(
    *,
    context_kib: Sequence[int],
    concurrencies: Sequence[int],
    calls_per_cell: int,
    warmup: int,
    replications: int,
    bootstrap_resamples: int,
    max_overhead_ms: float,
    build_profile: str,
    max_inflight_plans: int = _DEFAULT_MAX_INFLIGHT_PLANS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not context_kib or not concurrencies:
        raise ValueError("context_kib and concurrencies must be non-empty")
    if min(*context_kib, *concurrencies, calls_per_cell, warmup, replications) <= 0:
        raise ValueError("matrix dimensions and sample counts must be positive")
    if bootstrap_resamples <= 0:
        raise ValueError("bootstrap_resamples must be positive")
    if calls_per_cell < max(concurrencies):
        raise ValueError("calls_per_cell must be at least the maximum concurrency")
    if 1 not in concurrencies:
        raise ValueError("concurrencies must include 1 for scaling baselines")
    if len(set(context_kib)) != len(context_kib) or len(set(concurrencies)) != len(
        concurrencies
    ):
        raise ValueError("matrix dimensions must not contain duplicates")
    if not math.isfinite(max_overhead_ms) or max_overhead_ms <= 0:
        raise ValueError("max_overhead_ms must be finite and positive")
    if max_inflight_plans <= 0:
        raise ValueError("max_inflight_plans must be positive")

    load_average_before = _load_average()
    cells = [
        (scenario, size_kib * 1_024, concurrency)
        for scenario in _SCENARIOS
        for size_kib in context_kib
        for concurrency in concurrencies
    ]
    summaries: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    realized_orders: list[list[str]] = []
    with tempfile.TemporaryDirectory(prefix="agentc-optimizer-e2e-scaling-") as temp:
        root = Path(temp).resolve()
        for replication in range(1, replications + 1):
            ordered_cells = list(cells)
            random.Random(_MATRIX_SEED + replication).shuffle(ordered_cells)
            realized_orders.append(
                [
                    f"{scenario}/{size}/{concurrency}"
                    for scenario, size, concurrency in ordered_cells
                ]
            )
            for scenario, target_bytes, concurrency in ordered_cells:
                summary, samples = _measure_replication(
                    root=root,
                    scenario=scenario,
                    target_bytes=target_bytes,
                    concurrency=concurrency,
                    replication=replication,
                    calls_per_cell=calls_per_cell,
                    warmup=warmup,
                    max_overhead_ms=max_overhead_ms,
                    max_inflight_plans=max_inflight_plans,
                )
                summaries.append(summary)
                raw.extend(samples)

    aggregate = _aggregate_cells(
        summaries, raw, bootstrap_resamples=bootstrap_resamples
    )
    load_average_after = _load_average()
    native_path = Path(str(_native.__file__))
    clock = time.get_clock_info("perf_counter")
    thread_clock = time.get_clock_info("thread_time")
    result = {
        "schema_version": 2,
        "experiment_kind": "optimizer_end_to_end_size_concurrency_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "agentc_git_commit": e2e._git_commit(),
        "agentc_git_dirty": e2e._git_dirty(),
        "paper_evidence": False,
        "network_calls": 0,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpu_count": os.cpu_count(),
            "load_average_before": load_average_before,
            "load_average_after": load_average_after,
            "sqlite": sqlite3.sqlite_version,
            "native_extension": native_path.name,
            "native_extension_sha256": e2e._sha256(native_path),
            "operator_attested_build_profile": build_profile,
            "perf_counter": {
                "implementation": clock.implementation,
                "monotonic": clock.monotonic,
                "adjustable": clock.adjustable,
                "resolution_seconds": clock.resolution,
            },
            "thread_time": {
                "implementation": thread_clock.implementation,
                "monotonic": thread_clock.monotonic,
                "adjustable": thread_clock.adjustable,
                "resolution_seconds": thread_clock.resolution,
            },
        },
        "settings": {
            "target_call_json_kib": list(context_kib),
            "concurrency": list(concurrencies),
            "calls_per_cell_per_replication": calls_per_cell,
            "warmup_calls_per_cell": warmup,
            "replications": replications,
            "scenario_order": "seeded shuffle per replication",
            "scenario_order_seed": _MATRIX_SEED,
            "realized_cell_order": realized_orders,
            "bootstrap_resamples": bootstrap_resamples,
            "max_overhead_ms": max_overhead_ms,
            "max_inflight_plans": max_inflight_plans,
            "enabled_rules": ["OutputBudget"],
            "exploration_enabled": False,
            "shadow_rate": 0.0,
            "objective": "cost",
            "paired_evidence_for_admitted_scenario": e2e._PAIRED_EVIDENCE,
            "site_layout": "one hot call site per matrix cell",
        },
        "aggregate_measurements_us_and_throughput": aggregate,
        "replications": summaries,
        "timed_scope": [
            "Python call into _native.optimize_plan",
            "native optimizer-state lookup",
            "JSON decode and encode",
            "complete-plan enumeration, guard lookup, and selection",
            "plan_audit row construction and bounded non-blocking enqueue",
            "conversion and return of the Python plan string",
        ],
        "throughput_scope": [
            "ready worker release through completion of every optimize_plan call",
            "Python thread loop and result-list bookkeeping",
            "excludes thread-pool construction and post-clock plan validation",
        ],
        "paired_internal_clock_scope": [
            "native planning after optimizer-state lookup",
            "optional exploration reservation (disabled in this experiment)",
            "excludes enqueue_plan_audit and the outer FFI boundary",
        ],
        "interpretation_limits": [
            "This is a repeated single-machine Stage E0 systems microbenchmark, not task-quality or savings evidence.",
            "Inputs are fixed-structure synthetic ASCII JSON records sized by serialized UTF-8 bytes; this isolates request-byte scaling, not tokenizer or semantic complexity.",
            "Threads share one hot call site and one bounded optimizer audit queue per cell; this exposes request-path contention without placing the ordered SQLite flush inside the clock.",
            "Inputs and outcomes are deterministic synthetic records; no provider or network is invoked.",
            "The paired residual combines boundary, state lookup, audit-row construction/enqueue, clock quantization, and return conversion; it is not an audit-only timer.",
            "Per-call thread CPU is enclosed by the wall clock; wall minus thread CPU estimates off-CPU delay but cannot distinguish scheduler delay, lock waiting, and GIL reacquisition.",
            "The build profile is operator-attested; the native extension hash pins the measured binary.",
            "The ordered audit flush and SQLite commit are outside the request-path clock; normal host scheduling remains inside it.",
            "Host load averages are recorded before and after the matrix; overlapping external workloads can inflate off-CPU tails and invalidate a canonical run.",
            "The harness derives its audit-id boundary from flushed native counters and opens Python's separately linked SQLite only after closing the native writer.",
        ],
    }
    return result, raw


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _positive_int_list(value: str) -> tuple[int, ...]:
    try:
        parsed = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be comma-separated integers") from error
    if not parsed or min(parsed) <= 0 or len(set(parsed)) != len(parsed):
        raise argparse.ArgumentTypeError("must be unique positive integers")
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--context-kib",
        type=_positive_int_list,
        default=_DEFAULT_CONTEXT_KIB,
        help="comma-separated exact serialized call sizes in KiB",
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int_list,
        default=_DEFAULT_CONCURRENCY,
        help="comma-separated Python thread counts; must include 1",
    )
    parser.add_argument(
        "--calls-per-cell", type=_positive_int, default=_DEFAULT_CALLS_PER_CELL
    )
    parser.add_argument("--warmup", type=_positive_int, default=_DEFAULT_WARMUP)
    parser.add_argument(
        "--replications", type=_positive_int, default=_DEFAULT_REPLICATIONS
    )
    parser.add_argument(
        "--bootstrap-resamples",
        type=_positive_int,
        default=_DEFAULT_BOOTSTRAP_RESAMPLES,
    )
    parser.add_argument("--max-overhead-ms", type=_positive_float, default=5.0)
    parser.add_argument(
        "--max-inflight-plans",
        type=_positive_int,
        default=_DEFAULT_MAX_INFLIGHT_PLANS,
    )
    parser.add_argument(
        "--build-profile", choices=("debug", "release", "unknown"), default="unknown"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--raw-output", type=Path)
    return parser.parse_args(argv)


_RAW_FIELDS = (
    "scenario",
    "target_call_json_bytes",
    "concurrency",
    "replication",
    "sequence",
    "worker",
    "worker_iteration",
    "span_id",
    "expected_plan_kind",
    "plan_kind",
    "fell_back_from_expected",
    "planner_fallback_reason",
    "e2e_ns",
    "thread_cpu_ns",
    "off_cpu_ns",
    "internal_pre_audit_ns",
    "boundary_state_audit_residual_ns",
)


def _write_raw(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".gz":
        with path.open("wb") as compressed:
            with gzip.GzipFile(
                fileobj=compressed, mode="wb", filename="", mtime=0
            ) as stream:
                with io.TextIOWrapper(stream, encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, fieldnames=_RAW_FIELDS, lineterminator="\n"
                    )
                    writer.writeheader()
                    writer.writerows(rows)
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_RAW_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result, raw = run(
        context_kib=args.context_kib,
        concurrencies=args.concurrency,
        calls_per_cell=args.calls_per_cell,
        warmup=args.warmup,
        replications=args.replications,
        bootstrap_resamples=args.bootstrap_resamples,
        max_overhead_ms=args.max_overhead_ms,
        build_profile=args.build_profile,
        max_inflight_plans=args.max_inflight_plans,
    )
    if args.raw_output is not None:
        _write_raw(args.raw_output, raw)
        result["raw_samples"] = {
            "path": args.raw_output.name,
            "rows": len(raw),
            "sha256": e2e._sha256(args.raw_output),
            "compression": "gzip" if args.raw_output.suffix == ".gz" else "none",
        }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
