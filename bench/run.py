#!/usr/bin/env python3
"""Run the full Agentc benchmark suite and produce a report.

Usage:
    python -m bench.run                    # full suite (20 calibration, 20 validation, 10 overhead)
    python -m bench.run --quick            # quick smoke run (2 tasks per category)
    python -m bench.run --output results/  # custom output directory
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from bench.calibrate import run_calibration
from bench.harness import TaskSplit
from bench.overhead import measure_pipeline_overhead
from bench.report import BenchmarkReport, generate_report, save_report
from bench.validate import run_validation


def run_suite(
    *,
    n_calibration: int = 20,
    n_validation: int = 20,
    n_overhead: int = 10,
    seed: int = 42,
    output_dir: Path = Path("bench/results"),
) -> BenchmarkReport:
    """Run the full benchmark suite."""

    split = TaskSplit.create(seed=seed)
    tmp_storage = Path("/tmp/agentc-bench-run")
    tmp_storage.mkdir(parents=True, exist_ok=True)

    report = BenchmarkReport()

    # --- Calibration ---
    print(f"[1/3] Calibration ({n_calibration} tasks) ...")
    t0 = time.monotonic()
    report.calibration = run_calibration(
        n_calibration_tasks=n_calibration, seed=seed
    )
    elapsed = time.monotonic() - t0
    status = "PASS" if report.calibration.passed else "FAIL"
    print(f"      {status} ({elapsed:.1f}s)")

    # --- Validation ---
    print(f"[2/3] Validation ({n_validation} tasks) ...")
    t0 = time.monotonic()
    val_tasks = split.validation[:n_validation]
    for i, task_id in enumerate(val_tasks, 1):
        task_storage = tmp_storage / f"validate-{task_id}"
        task_storage.mkdir(parents=True, exist_ok=True)
        vr = run_validation(task_id, task_storage, seed=seed)
        report.validation_results.append(vr)
        marker = "." if vr.passed else "F"
        print(f"      [{i}/{n_validation}] {task_id}: {marker}")

    elapsed = time.monotonic() - t0
    status = "PASS" if report.validation_passed else "FAIL"
    print(f"      {status} ({elapsed:.1f}s)")

    # --- Overhead ---
    print(f"[3/3] Overhead ({n_overhead} tasks) ...")
    t0 = time.monotonic()
    overhead_tasks = split.overhead[:n_overhead]
    for i, task_id in enumerate(overhead_tasks, 1):
        task_storage = tmp_storage / f"overhead-{task_id}"
        task_storage.mkdir(parents=True, exist_ok=True)
        result = measure_pipeline_overhead(task_id, task_storage, seed=seed)
        report.overhead_results.append(result)
        budget = result.passes_budget()
        all_ok = all(budget.values())
        marker = "." if all_ok else "F"
        p99_us = result.p99_per_call_overhead_us
        print(f"      [{i}/{n_overhead}] {task_id}: {marker}  (p99={p99_us:.0f}us, wall={result.wall_clock_overhead_pct:.1f}%, mem={result.memory_overhead_bytes / 1024 / 1024:.1f}MB)")

    elapsed = time.monotonic() - t0
    status = "PASS" if report.overhead_passed else "FAIL"
    print(f"      {status} ({elapsed:.1f}s)")

    # --- Report ---
    text = generate_report(report)
    print()
    print(text)
    print()

    report_path = save_report(report, output_dir)
    print(f"Report saved to: {report_path}")
    print(f"JSON summary:    {output_dir / 'benchmark_summary.json'}")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Agentc benchmark suite")
    parser.add_argument(
        "--quick", action="store_true",
        help="Quick smoke run (2 tasks per category)",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("bench/results"),
        help="Output directory for reports (default: bench/results)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed (default: 42)",
    )
    args = parser.parse_args()

    if args.quick:
        n_cal, n_val, n_over = 2, 2, 2
    else:
        n_cal, n_val, n_over = 20, 20, 10

    report = run_suite(
        n_calibration=n_cal,
        n_validation=n_val,
        n_overhead=n_over,
        seed=args.seed,
        output_dir=args.output,
    )

    sys.exit(0 if report.all_passed else 1)


if __name__ == "__main__":
    main()
