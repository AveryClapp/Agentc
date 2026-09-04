"""Attribute optimizer concurrency tails against minimal PyO3 controls.

The production planner releases the GIL while Rust runs, then reacquires it to
return a Python string. This no-network Stage E0 diagnostic compares two small
native calls with a Python no-op under the same ready-worker protocol used by
``optimizer_e2e_scaling``. Per-call wall and thread-CPU clocks separate work
executed by the calling thread from time spent off CPU.

The controls intentionally do not call ``optimize_plan``: the scaling harness
measures that path. ``native_audit_stats`` covers GIL release/reacquisition,
optimizer-state lookup, atomic counter reads, JSON encoding, and return
conversion. ``native_model_catalog`` substitutes immutable catalog traversal
and encoding for the counter reads.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import platform
import random
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from agentc import _native

from bench import optimizer_e2e_overhead as e2e
from bench import optimizer_e2e_scaling as scaling


_OPERATIONS = ("python_noop", "native_audit_stats", "native_model_catalog")
_DEFAULT_CONCURRENCY = (1, 2, 4, 8, 16, 32)
_DEFAULT_CALLS_PER_CELL = 1_024
_DEFAULT_WARMUP = 100
_DEFAULT_REPLICATIONS = 5
_DEFAULT_BOOTSTRAP_RESAMPLES = 10_000
_MATRIX_SEED = 20_260_904


def _load_average() -> list[float] | None:
    try:
        return list(os.getloadavg())
    except (AttributeError, OSError):
        return None


def _invoke(operation: str) -> str:
    if operation == "python_noop":
        return "noop"
    if operation == "native_audit_stats":
        return str(_native.optimize_audit_stats())
    if operation == "native_model_catalog":
        return str(_native.optimize_model_catalog())
    raise ValueError(f"unknown operation: {operation}")


def _measure_concurrent_operation(
    operation: str, *, calls: int, concurrency: int
) -> tuple[int, list[dict[str, int]]]:
    if operation not in _OPERATIONS:
        raise ValueError(f"unknown operation: {operation}")
    if calls < concurrency:
        raise ValueError("calls must be at least concurrency")
    partitions = [
        list(range(worker, calls, concurrency)) for worker in range(concurrency)
    ]
    ready = threading.Barrier(concurrency + 1, timeout=30.0)
    start = threading.Event()

    def worker(worker_id: int, assigned: Sequence[int]) -> list[dict[str, int]]:
        ready.wait()
        if not start.wait(timeout=30.0):
            raise RuntimeError("probe start signal was not delivered")
        rows: list[dict[str, int]] = []
        for worker_iteration, sequence in enumerate(assigned, start=1):
            wall_started_at = time.perf_counter_ns()
            thread_cpu_started_at = time.thread_time_ns()
            result = _invoke(operation)
            thread_cpu_ns = time.thread_time_ns() - thread_cpu_started_at
            wall_ns = time.perf_counter_ns() - wall_started_at
            if not result:
                raise RuntimeError(f"{operation} returned an empty result")
            rows.append(
                {
                    "sequence": sequence,
                    "worker": worker_id,
                    "worker_iteration": worker_iteration,
                    "wall_ns": wall_ns,
                    "thread_cpu_ns": thread_cpu_ns,
                    "off_cpu_ns": max(0, wall_ns - thread_cpu_ns),
                }
            )
        return rows

    with ThreadPoolExecutor(
        max_workers=concurrency, thread_name_prefix="agentc-boundary"
    ) as executor:
        futures = [
            executor.submit(worker, worker_id, assigned)
            for worker_id, assigned in enumerate(partitions)
        ]
        ready.wait()
        group_started_at = time.perf_counter_ns()
        start.set()
        worker_rows = [future.result() for future in futures]
        group_ns = time.perf_counter_ns() - group_started_at

    rows = [row for partition in worker_rows for row in partition]
    rows.sort(key=lambda row: row["sequence"])
    return group_ns, rows


def _measure_replication(
    *, operation: str, concurrency: int, replication: int, calls: int, warmup: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    for _ in range(warmup):
        _invoke(operation)
    group_ns, measured = _measure_concurrent_operation(
        operation, calls=calls, concurrency=concurrency
    )
    raw: list[dict[str, Any]] = [
        {
            "operation": operation,
            "concurrency": concurrency,
            "replication": replication,
            **row,
        }
        for row in measured
    ]
    wall_values = [int(row["wall_ns"]) for row in raw]
    cpu_values = [int(row["thread_cpu_ns"]) for row in raw]
    off_cpu_values = [int(row["off_cpu_ns"]) for row in raw]
    return (
        {
            "operation": operation,
            "concurrency": concurrency,
            "replication": replication,
            "sample_count": len(raw),
            "group_elapsed_ms": group_ns / 1_000_000.0,
            "throughput_calls_per_second": calls * 1_000_000_000.0 / group_ns,
            "measured_thread_cpu_core_equivalents": sum(cpu_values) / group_ns,
            "wall": e2e._summarize_ns(wall_values),
            "thread_cpu": e2e._summarize_ns(cpu_values),
            "off_cpu": e2e._summarize_ns(off_cpu_values),
        },
        raw,
    )


def _aggregate(
    replications: Sequence[dict[str, Any]],
    raw: Sequence[dict[str, Any]],
    *,
    bootstrap_resamples: int,
) -> list[dict[str, Any]]:
    aggregate: list[dict[str, Any]] = []
    concurrencies = sorted({int(row["concurrency"]) for row in replications})
    for operation_index, operation in enumerate(_OPERATIONS):
        for concurrency in concurrencies:
            cell_replications = [
                row
                for row in replications
                if row["operation"] == operation and row["concurrency"] == concurrency
            ]
            cell_raw = [
                row
                for row in raw
                if row["operation"] == operation and row["concurrency"] == concurrency
            ]
            if not cell_replications or not cell_raw:
                raise RuntimeError(f"missing probe cell: {operation}/{concurrency}")
            seed = _MATRIX_SEED + operation_index * 1_000 + concurrency
            wall = e2e._summarize_ns([int(row["wall_ns"]) for row in cell_raw])
            thread_cpu = e2e._summarize_ns(
                [int(row["thread_cpu_ns"]) for row in cell_raw]
            )
            off_cpu = e2e._summarize_ns([int(row["off_cpu_ns"]) for row in cell_raw])
            aggregate.append(
                {
                    "operation": operation,
                    "concurrency": concurrency,
                    "sample_count": len(cell_raw),
                    "replication_count": len(cell_replications),
                    "wall": wall,
                    "thread_cpu": thread_cpu,
                    "off_cpu": off_cpu,
                    "mean_off_cpu_share_pct": (
                        100.0 * off_cpu["mean_us"] / wall["mean_us"]
                    ),
                    "replication_mean_p50_us": e2e._bootstrap_mean_ci(
                        [float(row["wall"]["p50_us"]) for row in cell_replications],
                        resamples=bootstrap_resamples,
                        seed=seed,
                    ),
                    "replication_mean_p99_us": e2e._bootstrap_mean_ci(
                        [float(row["wall"]["p99_us"]) for row in cell_replications],
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
        (str(row["operation"]), int(row["concurrency"])): row for row in aggregate
    }
    for row in aggregate:
        baseline = by_key[(str(row["operation"]), 1)]
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
        row["p50_inflation_vs_c1"] = float(row["wall"]["p50_us"]) / float(
            baseline["wall"]["p50_us"]
        )
        row["p99_inflation_vs_c1"] = float(row["wall"]["p99_us"]) / float(
            baseline["wall"]["p99_us"]
        )
    return aggregate


def run(
    *,
    concurrencies: Sequence[int],
    calls_per_cell: int,
    warmup: int,
    replications: int,
    bootstrap_resamples: int,
    build_profile: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not concurrencies:
        raise ValueError("concurrencies must be non-empty")
    if min(*concurrencies, calls_per_cell, warmup, replications) <= 0:
        raise ValueError("matrix dimensions and sample counts must be positive")
    if calls_per_cell < max(concurrencies):
        raise ValueError("calls_per_cell must be at least maximum concurrency")
    if 1 not in concurrencies:
        raise ValueError("concurrencies must include 1")
    if len(set(concurrencies)) != len(concurrencies):
        raise ValueError("concurrencies must not contain duplicates")
    if bootstrap_resamples <= 0:
        raise ValueError("bootstrap_resamples must be positive")

    load_average_before = _load_average()
    cells = [
        (operation, concurrency)
        for operation in _OPERATIONS
        for concurrency in concurrencies
    ]
    summaries: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    realized_orders: list[list[str]] = []
    settings = scaling._settings(scenario="joint_reference", max_overhead_ms=5.0)
    with (
        tempfile.TemporaryDirectory(prefix="agentc-optimizer-boundary-") as temp,
        e2e._reset_native_optimizer(),
        e2e._isolated_agentc_environment(settings),
    ):
        _native.optimize_configure(str(Path(temp).resolve()))
        for replication in range(1, replications + 1):
            ordered_cells = list(cells)
            random.Random(_MATRIX_SEED + replication).shuffle(ordered_cells)
            realized_orders.append(
                [
                    f"{operation}/{concurrency}"
                    for operation, concurrency in ordered_cells
                ]
            )
            for operation, concurrency in ordered_cells:
                summary, rows = _measure_replication(
                    operation=operation,
                    concurrency=concurrency,
                    replication=replication,
                    calls=calls_per_cell,
                    warmup=warmup,
                )
                summaries.append(summary)
                raw.extend(rows)

    aggregate = _aggregate(summaries, raw, bootstrap_resamples=bootstrap_resamples)
    load_average_after = _load_average()
    native_path = Path(str(_native.__file__))
    wall_clock = time.get_clock_info("perf_counter")
    thread_clock = time.get_clock_info("thread_time")
    result = {
        "schema_version": 1,
        "experiment_kind": "optimizer_boundary_scheduler_attribution_preflight",
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
            "native_extension": native_path.name,
            "native_extension_sha256": e2e._sha256(native_path),
            "operator_attested_build_profile": build_profile,
            "perf_counter": {
                "implementation": wall_clock.implementation,
                "resolution_seconds": wall_clock.resolution,
            },
            "thread_time": {
                "implementation": thread_clock.implementation,
                "resolution_seconds": thread_clock.resolution,
            },
        },
        "settings": {
            "operations": list(_OPERATIONS),
            "concurrency": list(concurrencies),
            "calls_per_cell_per_replication": calls_per_cell,
            "warmup_calls_per_cell": warmup,
            "replications": replications,
            "bootstrap_resamples": bootstrap_resamples,
            "scenario_order": "seeded shuffle per replication",
            "scenario_order_seed": _MATRIX_SEED,
            "realized_cell_order": realized_orders,
        },
        "aggregate_measurements_us_and_throughput": aggregate,
        "replications": summaries,
        "interpretation_limits": [
            "This is a single-machine Stage E0 runtime diagnostic, not task-quality or savings evidence.",
            "Wall minus calling-thread CPU estimates off-CPU time but cannot distinguish scheduler delay, lock waiting, and GIL reacquisition.",
            "The Python no-op does not release the GIL; it is a clock and worker-loop floor, not a native-boundary substitute.",
            "The two native controls release and reacquire the GIL but do not construct or enqueue a plan-audit row.",
            "Inputs are deterministic and no provider or network is invoked.",
            "Host load averages are recorded before and after the matrix; overlapping external workloads can invalidate a canonical run.",
        ],
    }
    return result, raw


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
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
        "--concurrency", type=_positive_int_list, default=_DEFAULT_CONCURRENCY
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
    parser.add_argument(
        "--build-profile", choices=("debug", "release", "unknown"), default="unknown"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--raw-output", type=Path)
    return parser.parse_args(argv)


_RAW_FIELDS = (
    "operation",
    "concurrency",
    "replication",
    "sequence",
    "worker",
    "worker_iteration",
    "wall_ns",
    "thread_cpu_ns",
    "off_cpu_ns",
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
        concurrencies=args.concurrency,
        calls_per_cell=args.calls_per_cell,
        warmup=args.warmup,
        replications=args.replications,
        bootstrap_resamples=args.bootstrap_resamples,
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
