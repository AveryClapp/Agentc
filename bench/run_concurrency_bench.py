"""Concurrency benchmark: latency and throughput under concurrent load.

Tests §3.6 claim: the optimizer does not become a serialization point
under concurrent load. Runs long_context_qa at 1×, 8×, and 32× concurrency
with and without agentc optimization, and measures wall-clock time,
per-task latency distributions (p50/p95/p99), QPS, and token savings.

Usage:
    python -m bench.run_concurrency_bench
    python -m bench.run_concurrency_bench --concurrency 1 8 32 --n-tasks 100
    python -m bench.run_concurrency_bench --concurrency 1 4 --n-tasks 20   # quick smoke

Outputs:
    bench/paper_results/concurrency_bench.csv          per-task rows
    bench/paper_results/concurrency_bench_summary.csv  per-condition summary
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "python"))

from bench.optimizer_bench import _find_agentc_binary

PAPER_RESULTS = _REPO / "bench" / "paper_results"
STORAGE_ROOT = Path("/tmp/agentc-concurrency-bench")
AGENT_MODULE = "bench.agents.long_context_qa_concurrent"

_TIMING_RE = re.compile(
    r"^TIMING\s+(\S+)\s+([\d.]+)\s+(\d+)\s+(\d+)$", re.MULTILINE
)
_PASS_FAIL_RE = re.compile(r"^(PASS|FAIL)\s+(\S+)", re.MULTILINE)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class TaskRow:
    concurrency: int
    condition: str
    task_id: str
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    passed: int  # 0 or 1


@dataclass
class ConditionSummary:
    concurrency: int
    condition: str
    n_tasks: int
    n_passed: int
    total_wall_s: float
    qps: float
    p50_latency_s: float
    p95_latency_s: float
    p99_latency_s: float
    mean_prompt_tokens: float
    token_savings_pct: float  # vs baseline at same concurrency; 0 for baseline rows
    stub_mode: int


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------


def _load_env(repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    env_file = repo / ".env"
    if env_file.is_file():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in env:
                env[k] = v
    return env


def run_condition(
    *,
    concurrency: int,
    condition: str,
    n_tasks: int,
    storage_dir: Path,
    verbose: bool = True,
) -> tuple[list[TaskRow], float]:
    """Run one condition (concurrency × baseline|optimized) via agentc record.

    Returns (task_rows, total_wall_clock_s).
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    env = _load_env(_REPO)
    env["AGENTC_OPTIMIZE"] = "1" if condition == "optimized" else "0"
    env["BENCH_CONCURRENCY"] = str(concurrency)
    env["BENCH_MAX_TASKS"] = str(n_tasks)
    env["PYTHONPATH"] = str(_REPO / "python")
    env["AGENTC_COMPOSE"] = "1"

    agentc_bin = _find_agentc_binary()
    cmd = [
        agentc_bin, "record",
        "--storage-path", str(storage_dir),
        "--",
        sys.executable, "-m", AGENT_MODULE,
    ]

    if verbose:
        label = f"[{condition}, {concurrency}x]"
        print(f"  {label} running {n_tasks} tasks...", end="", flush=True)

    import time
    t_start = time.perf_counter()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    total_wall_s = time.perf_counter() - t_start

    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(
            f"{AGENT_MODULE} failed "
            f"(exit={proc.returncode}, cond={condition}, conc={concurrency})"
        )

    # Parse TIMING lines
    pass_fail: dict[str, int] = {}
    for m in _PASS_FAIL_RE.finditer(proc.stdout):
        pass_fail[m.group(2)] = 1 if m.group(1) == "PASS" else 0

    rows: list[TaskRow] = []
    for m in _TIMING_RE.finditer(proc.stdout):
        task_id = m.group(1)
        rows.append(TaskRow(
            concurrency=concurrency,
            condition=condition,
            task_id=task_id,
            latency_s=float(m.group(2)),
            prompt_tokens=int(m.group(3)),
            completion_tokens=int(m.group(4)),
            passed=pass_fail.get(task_id, 0),
        ))

    if verbose:
        n_passed = sum(r.passed for r in rows)
        qps = len(rows) / total_wall_s if total_wall_s > 0 else 0
        print(
            f" done. {n_passed}/{len(rows)} passed, "
            f"{qps:.1f} QPS, {total_wall_s:.1f}s wall"
        )

    return rows, total_wall_s


# ---------------------------------------------------------------------------
# Summary computation
# ---------------------------------------------------------------------------


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(int(len(sorted_vals) * pct / 100), len(sorted_vals) - 1)
    return sorted_vals[idx]


def compute_summary(
    rows: list[TaskRow],
    total_wall_s: float,
    *,
    baseline_mean_tokens: float = 0.0,
) -> ConditionSummary:
    latencies = sorted(r.latency_s for r in rows)
    tokens = [r.prompt_tokens for r in rows]
    mean_tokens = statistics.mean(tokens) if tokens else 0.0
    savings_pct = (
        100.0 * (baseline_mean_tokens - mean_tokens) / baseline_mean_tokens
        if baseline_mean_tokens > 0 else 0.0
    )
    stub_mode = int(not os.environ.get("OPENAI_API_KEY"))
    return ConditionSummary(
        concurrency=rows[0].concurrency,
        condition=rows[0].condition,
        n_tasks=len(rows),
        n_passed=sum(r.passed for r in rows),
        total_wall_s=round(total_wall_s, 3),
        qps=round(len(rows) / total_wall_s, 3) if total_wall_s > 0 else 0.0,
        p50_latency_s=round(_percentile(latencies, 50), 3),
        p95_latency_s=round(_percentile(latencies, 95), 3),
        p99_latency_s=round(_percentile(latencies, 99), 3),
        mean_prompt_tokens=round(mean_tokens, 1),
        token_savings_pct=round(savings_pct, 1),
        stub_mode=stub_mode,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m bench.run_concurrency_bench",
        description="Latency and throughput under concurrent load (§3.6).",
    )
    p.add_argument(
        "--concurrency", nargs="+", type=int, default=[1, 8, 32],
        help="Concurrency levels to test (default: 1 8 32)",
    )
    p.add_argument(
        "--n-tasks", type=int, default=100,
        help="Tasks per condition (default: 100)",
    )
    p.add_argument(
        "--storage-root", type=Path, default=STORAGE_ROOT,
        help=f"Storage root for agentc DBs (default: {STORAGE_ROOT})",
    )
    args = p.parse_args(argv)

    if args.storage_root.exists():
        shutil.rmtree(args.storage_root)

    PAPER_RESULTS.mkdir(parents=True, exist_ok=True)

    all_rows: list[TaskRow] = []
    summaries: list[ConditionSummary] = []

    # baseline_tokens_by_concurrency[c] = mean prompt tokens for baseline at level c
    baseline_tokens: dict[int, float] = {}

    conditions = ["baseline", "optimized"]
    total_conditions = len(args.concurrency) * len(conditions)
    done = 0

    for concurrency in args.concurrency:
        for condition in conditions:
            done += 1
            print(f"\n[{done}/{total_conditions}] concurrency={concurrency} condition={condition}")
            storage_dir = args.storage_root / f"{condition}_{concurrency}x"
            rows, wall = run_condition(
                concurrency=concurrency,
                condition=condition,
                n_tasks=args.n_tasks,
                storage_dir=storage_dir,
            )
            if not rows:
                print(f"  WARNING: no TIMING lines parsed for {condition} {concurrency}x")
                continue

            all_rows.extend(rows)
            baseline_mean = baseline_tokens.get(concurrency, 0.0)
            s = compute_summary(rows, wall, baseline_mean_tokens=baseline_mean)
            summaries.append(s)

            if condition == "baseline":
                baseline_tokens[concurrency] = s.mean_prompt_tokens

    if not all_rows:
        print("ERROR: no rows collected", file=sys.stderr)
        return 1

    # Write detail CSV
    detail_path = PAPER_RESULTS / "concurrency_bench.csv"
    detail_fields = [
        "concurrency", "condition", "task_id",
        "latency_s", "prompt_tokens", "completion_tokens", "passed",
    ]
    with detail_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=detail_fields)
        w.writeheader()
        for r in all_rows:
            w.writerow({
                "concurrency": r.concurrency,
                "condition": r.condition,
                "task_id": r.task_id,
                "latency_s": r.latency_s,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "passed": r.passed,
            })
    print(f"\nWrote {detail_path}")

    # Write summary CSV
    summary_path = PAPER_RESULTS / "concurrency_bench_summary.csv"
    summary_fields = [
        "concurrency", "condition", "n_tasks", "n_passed",
        "total_wall_s", "qps",
        "p50_latency_s", "p95_latency_s", "p99_latency_s",
        "mean_prompt_tokens", "token_savings_pct", "stub_mode",
    ]
    with summary_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=summary_fields)
        w.writeheader()
        for s in summaries:
            w.writerow({
                "concurrency": s.concurrency,
                "condition": s.condition,
                "n_tasks": s.n_tasks,
                "n_passed": s.n_passed,
                "total_wall_s": s.total_wall_s,
                "qps": s.qps,
                "p50_latency_s": s.p50_latency_s,
                "p95_latency_s": s.p95_latency_s,
                "p99_latency_s": s.p99_latency_s,
                "mean_prompt_tokens": s.mean_prompt_tokens,
                "token_savings_pct": s.token_savings_pct,
                "stub_mode": s.stub_mode,
            })
    print(f"Wrote {summary_path}")

    # Print summary table
    print("\n" + "=" * 88)
    print(
        f"{'conc':>5}  {'condition':>12}  {'n':>4}  "
        f"{'QPS':>7}  {'p50':>7}  {'p95':>7}  {'p99':>7}  "
        f"{'tokens':>8}  {'savings':>8}"
    )
    print("-" * 88)
    for s in summaries:
        print(
            f"{s.concurrency:>5}  {s.condition:>12}  {s.n_tasks:>4}  "
            f"{s.qps:>7.2f}  {s.p50_latency_s:>7.3f}  "
            f"{s.p95_latency_s:>7.3f}  {s.p99_latency_s:>7.3f}  "
            f"{s.mean_prompt_tokens:>8.0f}  {s.token_savings_pct:>7.1f}%"
        )

    if summaries and summaries[0].stub_mode:
        print("\nNOTE: STUB MODE — latency/token figures are not meaningful")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
