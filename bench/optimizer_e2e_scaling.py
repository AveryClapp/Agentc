"""Measure complete optimizer-call scaling across input size and concurrency.

This is the scaling companion to :mod:`bench.optimizer_e2e_overhead`. It times
the complete ``_native.optimize_plan`` FFI call while varying the exact
serialized call size and the number of Python threads issuing calls against one
hot call site. Every timed call carries a unique span ID, which pairs its outer
wall-clock duration with the exact ``plan_audit.overhead_us`` row written by
that call even when completion order differs from audit order.

No provider is called. Build the extension in release mode before producing a
committed Stage E0 artifact::

    maturin develop --release -m crates/agentc-profiler/Cargo.toml
    python -m bench.optimizer_e2e_scaling \
      --build-profile release \
      --output bench/repro/optimizer-e2e-scaling-2026-09-04.json \
      --raw-output bench/repro/optimizer-e2e-scaling-2026-09-04.csv.gz
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import platform
import random
import sqlite3
import tempfile
import threading
import time
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
_MATRIX_SEED = 20_260_904
_SCENARIOS = ("joint_reference", "joint_admitted_rewrite")
_CONTENT_PATTERN = "agent systems context token "


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


def _settings(*, scenario: str, max_overhead_ms: float) -> dict[str, str]:
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
        "AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE": str(e2e._PAIRED_EVIDENCE),
        "AGENTC_OPTIMIZE_OBJECTIVE": "cost",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_PROVIDER": "openai",
    }


def _read_correlated_audit_rows(
    audit_path: Path, after_id: int
) -> dict[str, tuple[int, str]]:
    with sqlite3.connect(audit_path) as connection:
        rows = connection.execute(
            "SELECT lower(hex(span_id)), overhead_us, plan_kind "
            "FROM plan_audit WHERE audit_id > ? ORDER BY audit_id",
            (after_id,),
        ).fetchall()
    correlated: dict[str, tuple[int, str]] = {}
    for span_id, overhead_us, plan_kind in rows:
        key = str(span_id)
        if key in correlated:
            raise RuntimeError(f"duplicate measured span ID in plan_audit: {key}")
        correlated[key] = (int(overhead_us), str(plan_kind))
    return correlated


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
            plan_json = _native.optimize_plan(call_json)
            elapsed_ns = time.perf_counter_ns() - started_at
            measurements.append(
                {
                    "sequence": sequence,
                    "worker": worker_id,
                    "worker_iteration": worker_iteration,
                    "span_id": f"{sequence:016x}",
                    "e2e_ns": elapsed_ns,
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
            _settings(scenario=scenario, max_overhead_ms=max_overhead_ms)
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
        first_measured_audit_id = e2e._max_audit_id(audit_path)
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
            returned_kind = e2e._plan_kind(str(row.pop("plan_json")))
            span_id = str(row["span_id"])
            if span_id not in audit_rows:
                raise RuntimeError(f"measured span missing from plan_audit: {span_id}")
            overhead_us, audit_kind = audit_rows[span_id]
            if returned_kind != expected_kind or audit_kind != expected_kind:
                raise RuntimeError(
                    f"{scenario} plan kind drifted for {span_id}: "
                    f"returned={returned_kind}, audit={audit_kind}"
                )
            internal_ns = overhead_us * 1_000
            e2e_ns = int(row["e2e_ns"])
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
                    "plan_kind": expected_kind,
                    "e2e_ns": e2e_ns,
                    "internal_pre_audit_ns": internal_ns,
                    "boundary_state_audit_residual_ns": residual_ns,
                }
            )

        journal_mode = e2e._journal_mode(audit_path)
        if journal_mode != "wal":
            raise RuntimeError(f"optimizer audit DB is not in WAL mode: {journal_mode}")
        e2e_ns_values = [int(row["e2e_ns"]) for row in raw]
        internal_ns_values = [int(row["internal_pre_audit_ns"]) for row in raw]
        residual_ns_values = [
            int(row["boundary_state_audit_residual_ns"]) for row in raw
        ]
        summary = {
            "scenario": scenario,
            "target_call_json_bytes": target_bytes,
            "concurrency": concurrency,
            "replication": replication,
            "sample_count": len(raw),
            "plan_kind": expected_kind,
            "configure_us": configure_ns / 1_000.0,
            "first_cold_call_us": first_call_ns / 1_000.0,
            "audit_journal_mode": journal_mode,
            "group_elapsed_ms": group_elapsed_ns / 1_000_000.0,
            "throughput_calls_per_second": (
                calls_per_cell * 1_000_000_000.0 / group_elapsed_ns
            ),
            "e2e": e2e._summarize_ns(e2e_ns_values),
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
                internal_summary = e2e._summarize_ns(
                    [int(row["internal_pre_audit_ns"]) for row in cell_raw]
                )
                residual_summary = e2e._summarize_ns(
                    [int(row["boundary_state_audit_residual_ns"]) for row in cell_raw]
                )
                aggregate.append(
                    {
                        "scenario": scenario,
                        "target_call_json_bytes": target_bytes,
                        "concurrency": concurrency,
                        "sample_count": len(cell_raw),
                        "replication_count": len(cell_replications),
                        "e2e": e2e_summary,
                        "internal_pre_audit": internal_summary,
                        "boundary_state_audit_residual": residual_summary,
                        "mean_residual_share_pct": (
                            100.0 * residual_summary["mean_us"] / e2e_summary["mean_us"]
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
                )
                summaries.append(summary)
                raw.extend(samples)

    aggregate = _aggregate_cells(
        summaries, raw, bootstrap_resamples=bootstrap_resamples
    )
    native_path = Path(str(_native.__file__))
    clock = time.get_clock_info("perf_counter")
    result = {
        "schema_version": 1,
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
            "synchronous plan_audit serialization and SQLite commit",
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
            "excludes write_plan_audit and the outer FFI boundary",
        ],
        "interpretation_limits": [
            "This is a repeated single-machine Stage E0 systems microbenchmark, not task-quality or savings evidence.",
            "Inputs are fixed-structure synthetic ASCII JSON records sized by serialized UTF-8 bytes; this isolates request-byte scaling, not tokenizer or semantic complexity.",
            "Threads share one hot call site and one optimizer audit connection per cell; this intentionally exposes production-path lock contention.",
            "Inputs and outcomes are deterministic synthetic records; no provider or network is invoked.",
            "The paired residual combines boundary, state lookup, audit serialization/commit, clock quantization, and return conversion; it is not an audit-only timer.",
            "The build profile is operator-attested; the native extension hash pins the measured binary.",
            "WAL checkpoint tails and normal host scheduling remain part of the production-path measurement.",
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
    "plan_kind",
    "e2e_ns",
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
