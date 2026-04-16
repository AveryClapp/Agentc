"""Overhead measurement: latency and memory delta with/without profiler.

Targets:
- Latency: <5ms per LLM call on calling thread
- Memory: <100MB resident with content capture, <50MB without
- Wall-clock: <5% overhead
"""

from __future__ import annotations

import os
import statistics
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import agentc
from agentc._lifecycle import _initialized, _shutdown_in_progress

from bench.harness import MockLLMCall, generate_pipeline_trace


@dataclass
class OverheadResult:
    """Result of an overhead measurement run."""

    task_id: str
    total_calls: int

    # Latency (nanoseconds)
    bare_wall_clock_ns: int
    instrumented_wall_clock_ns: int
    per_call_overhead_ns: list[int]

    # Memory (bytes)
    bare_peak_memory_bytes: int
    instrumented_peak_memory_bytes: int

    # Real-time pipeline duration (ms) for projected overhead calculation.
    # This is the sum of mock call latencies at 1x scale.
    real_pipeline_ms: float = 0.0

    @property
    def wall_clock_overhead_pct(self) -> float:
        """Projected wall-clock overhead against real (unscaled) pipeline duration.

        Since benchmarks use scaled-down sleeps, the measured wall-clock delta
        is noisy. Instead we project: total instrumentation overhead (per-call
        mean * n_calls) divided by the real pipeline duration.
        """
        if self.real_pipeline_ms <= 0 or not self.per_call_overhead_ns:
            return 0.0
        total_overhead_ms = (
            statistics.mean(self.per_call_overhead_ns) * self.total_calls / 1_000_000
        )
        return round((total_overhead_ms / self.real_pipeline_ms) * 100, 6)

    @property
    def mean_per_call_overhead_us(self) -> float:
        if not self.per_call_overhead_ns:
            return 0.0
        return statistics.mean(self.per_call_overhead_ns) / 1000

    @property
    def p99_per_call_overhead_us(self) -> float:
        if not self.per_call_overhead_ns:
            return 0.0
        sorted_vals = sorted(self.per_call_overhead_ns)
        idx = int(len(sorted_vals) * 0.99)
        return sorted_vals[min(idx, len(sorted_vals) - 1)] / 1000

    @property
    def memory_overhead_bytes(self) -> int:
        return self.instrumented_peak_memory_bytes - self.bare_peak_memory_bytes

    def passes_budget(self) -> dict[str, bool]:
        return {
            "latency_per_call_under_5ms": self.p99_per_call_overhead_us < 5000,
            "wall_clock_under_5pct": self.wall_clock_overhead_pct < 5.0,
            "memory_under_50mb": self.memory_overhead_bytes < 50 * 1024 * 1024,
        }


def _measure_bare_call_latency() -> int:
    """Measure latency of a bare function call (no instrumentation), in nanoseconds."""
    start = time.perf_counter_ns()
    # Simulate the work a span would do: generate IDs, timestamps, dict building
    time.perf_counter_ns()  # timestamp
    end = time.perf_counter_ns()
    return end - start


def _measure_instrumented_call_latency(tmp_storage: Path) -> int:
    """Measure latency of an instrumented span call, in nanoseconds."""
    start = time.perf_counter_ns()

    with agentc.span("benchmark-call", kind="chat"):
        time.perf_counter_ns()  # simulate timestamp read

    end = time.perf_counter_ns()
    return end - start


def measure_per_call_overhead(
    n_iterations: int = 200,
    tmp_storage: Path | None = None,
) -> list[int]:
    """Measure per-call instrumentation overhead.

    Returns list of overhead values in nanoseconds (instrumented - bare).
    """
    storage = tmp_storage or Path("/tmp/agentc-bench-overhead")

    # Measure bare latencies
    bare_latencies = []
    for _ in range(n_iterations):
        bare_latencies.append(_measure_bare_call_latency())

    # Initialize agentc for instrumented measurements
    was_initialized = agentc.is_initialized()
    if not was_initialized:
        with patch("agentc._lifecycle._apply_patches"):
            agentc.init(storage_path=str(storage))

    # Measure instrumented latencies
    @agentc.trace(name="overhead-benchmark")
    def instrumented_run() -> list[int]:
        latencies = []
        for _ in range(n_iterations):
            latencies.append(_measure_instrumented_call_latency(storage))
        return latencies

    with patch("agentc._span._write_root_span"):
        instrumented_latencies = instrumented_run()

    if not was_initialized:
        agentc.shutdown()
        _initialized.clear()
        _shutdown_in_progress.clear()

    # Compute per-call overhead
    bare_median = statistics.median(bare_latencies)
    overheads = [max(0, inst - int(bare_median)) for inst in instrumented_latencies]
    return overheads


def _call_sleep_seconds(call: MockLLMCall, scale: float) -> float:
    """Convert a mock call's latency to a sleep duration in seconds.

    scale=1.0 means real-time (e.g. 1000ms call sleeps 1s).
    scale=0.001 means 1000x speedup (1000ms call sleeps 1ms).
    Default benchmarks use 0.01 (100x speedup) so a typical pipeline
    with ~20s total latency completes in ~200ms — fast enough for CI
    but slow enough that microsecond-level instrumentation overhead
    doesn't dominate wall-clock measurements.
    """
    return call.latency_ms / 1000.0 * scale


def measure_pipeline_overhead(
    task_id: str,
    tmp_storage: Path | None = None,
    seed: int | None = None,
    time_scale: float = 0.01,
) -> OverheadResult:
    """Measure full pipeline overhead for a single task.

    Runs the pipeline twice: once bare, once instrumented.

    Args:
        time_scale: Multiplier for mock call latencies. 1.0 = real-time,
            0.01 = 100x speedup (default). Lower values run faster but
            produce noisier wall-clock overhead measurements.
    """
    storage = tmp_storage or Path("/tmp/agentc-bench-overhead")
    trace = generate_pipeline_trace(task_id, seed=seed)

    # --- Bare run ---
    tracemalloc.start()
    bare_start = time.perf_counter_ns()

    for step in trace.steps:
        for call in step.calls:
            time.sleep(_call_sleep_seconds(call, time_scale))

    bare_elapsed = time.perf_counter_ns() - bare_start
    _, bare_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # --- Instrumented run ---
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(storage))

    tracemalloc.start()
    inst_start = time.perf_counter_ns()

    for step in trace.steps:

        @agentc.trace(name=f"{step.agent_name}-{step.step_name}")
        def run_step(calls: list[MockLLMCall] = step.calls) -> None:
            for call in calls:
                with agentc.span(f"llm-{call.model}", kind="chat"):
                    time.sleep(_call_sleep_seconds(call, time_scale))

        with patch("agentc._span._write_root_span"):
            run_step()

    inst_elapsed = time.perf_counter_ns() - inst_start
    _, inst_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()

    # Per-call overhead
    per_call_overhead = measure_per_call_overhead(
        n_iterations=100, tmp_storage=storage
    )

    return OverheadResult(
        task_id=task_id,
        total_calls=trace.total_calls,
        bare_wall_clock_ns=bare_elapsed,
        instrumented_wall_clock_ns=inst_elapsed,
        per_call_overhead_ns=per_call_overhead,
        bare_peak_memory_bytes=bare_peak,
        instrumented_peak_memory_bytes=inst_peak,
        real_pipeline_ms=trace.wall_clock_ms,
    )


def format_overhead_report(results: list[OverheadResult]) -> str:
    """Format overhead measurement results as a human-readable report."""
    lines = [
        "OVERHEAD MEASUREMENT REPORT",
        "=" * 60,
        "",
        f"Tasks measured: {len(results)}",
        "",
    ]

    all_per_call = []
    all_wall_pct = []
    all_mem = []

    for r in results:
        all_per_call.extend(r.per_call_overhead_ns)
        all_wall_pct.append(r.wall_clock_overhead_pct)
        all_mem.append(r.memory_overhead_bytes)

        lines.append(f"  {r.task_id}:")
        lines.append(f"    Calls: {r.total_calls}")
        lines.append(
            f"    Per-call overhead: mean={r.mean_per_call_overhead_us:.1f}us "
            f"p99={r.p99_per_call_overhead_us:.1f}us"
        )
        lines.append(f"    Wall-clock overhead: {r.wall_clock_overhead_pct:.4f}%")
        lines.append(
            f"    Memory overhead: {r.memory_overhead_bytes / 1024 / 1024:.1f}MB"
        )

        budget = r.passes_budget()
        for check, passed in budget.items():
            status = "PASS" if passed else "FAIL"
            lines.append(f"    [{status}] {check}")
        lines.append("")

    # Aggregate
    lines.append("AGGREGATE")
    lines.append("-" * 40)
    if all_per_call:
        mean_us = statistics.mean(all_per_call) / 1000
        p99_sorted = sorted(all_per_call)
        p99_us = p99_sorted[int(len(p99_sorted) * 0.99)] / 1000
        lines.append(f"  Per-call overhead: mean={mean_us:.1f}us p99={p99_us:.1f}us")
    if all_wall_pct:
        lines.append(
            f"  Wall-clock overhead: mean={statistics.mean(all_wall_pct):.4f}% "
            f"max={max(all_wall_pct):.4f}%"
        )
    if all_mem:
        lines.append(
            f"  Memory overhead: mean={statistics.mean(all_mem) / 1024 / 1024:.1f}MB "
            f"max={max(all_mem) / 1024 / 1024:.1f}MB"
        )

    # Budget pass/fail
    all_pass = all(
        all(r.passes_budget().values()) for r in results
    )
    lines.append("")
    lines.append(f"OVERALL: {'PASS' if all_pass else 'FAIL'}")

    return "\n".join(lines)
