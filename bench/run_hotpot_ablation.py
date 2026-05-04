"""HotpotQA-distractor 3-arm ablation runner (bd-9qe.4).

Three configurations targeting ContextCompress evaluation:

- ``baseline``: ``AGENTC_OPTIMIZE=0`` — no agentc rewriting at all.
- ``agentc-full``: ``AGENTC_OPTIMIZE=1`` with every rule enabled.
- ``agentc-no-compress``: ``AGENTC_OPTIMIZE=1`` with ``ContextCompress``
  disabled — isolates the contribution of the proxy.

Per-arm metrics:
- total input tokens, total output tokens (from spans table)
- total cost USD (from spans.cost_usd)
- EM accuracy (from agent stdout PASS/FAIL)
- accuracy delta vs baseline (pp)
- compression fire count (from plan_audit, agentc-full only)

Ship gate (per design doc): ``accuracy_delta_pp <= 1.5pp`` between
``agentc-full`` and ``baseline``.

Usage:

    python -m bench.run_hotpot_ablation
    python -m bench.run_hotpot_ablation --max-tasks 10  # smoke
    python -m bench.run_hotpot_ablation --out bench/results/hotpot_ablation.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bench.optimizer_ablation import _disable
from bench.optimizer_bench import (
    _find_agentc_binary,
    _parse_pass_fail,
)


AGENT_MODULE = "bench.agents.hotpot_qa"


@dataclass
class ArmStats:
    arm: str  # "baseline" | "agentc-full" | "agentc-no-compress"
    n_tasks: int
    n_passed: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    wall_clock_s: float
    compression_fire_count: int

    @property
    def em_accuracy(self) -> float:
        return (self.n_passed / self.n_tasks) if self.n_tasks else 0.0


def _aggregate_spans(db_path: Path) -> tuple[int, int, float, float]:
    """Return ``(input_tokens, output_tokens, cost_usd, wall_clock_s)``
    aggregated across all spans in ``traces.db``.

    Missing DB → all zeros.
    """
    if not db_path.is_file():
        return (0, 0, 0.0, 0.0)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0), "
            "       COALESCE(SUM(output_tokens), 0), "
            "       COALESCE(SUM(cost_usd), 0.0), "
            "       COALESCE(MAX(COALESCE(end_time, start_time)) - "
            "                MIN(start_time), 0) "
            "FROM spans"
        ).fetchone()
    finally:
        conn.close()
    in_tok, out_tok, cost, dur_us = row
    return (int(in_tok or 0), int(out_tok or 0), float(cost or 0.0), float(dur_us or 0) / 1_000_000.0)


def _compression_fires(audit_db: Path) -> int:
    """Count rewritten-plan rows for ``ContextCompress`` in plan_audit."""
    if not audit_db.is_file():
        return 0
    conn = sqlite3.connect(str(audit_db))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM plan_audit "
            "WHERE rule = 'ContextCompress' AND plan_kind = 'rewritten'"
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0)


def _run_arm(
    *,
    arm: str,
    storage_dir: Path,
    optimize: bool,
    rules_disabled: Optional[list[str]] = None,
    max_tasks: Optional[int] = None,
) -> ArmStats:
    if storage_dir.exists():
        shutil.rmtree(storage_dir)
    storage_dir.mkdir(parents=True)

    if optimize and rules_disabled:
        _disable(rules_disabled, storage_dir)

    env = os.environ.copy()
    env["AGENTC_OPTIMIZE"] = "1" if optimize else "0"
    if max_tasks is not None:
        env["BENCH_MAX_TASKS"] = str(max_tasks)

    agentc_bin = _find_agentc_binary()
    cmd = [
        agentc_bin,
        "record",
        "--storage-path",
        str(storage_dir),
        "--",
        sys.executable,
        "-m",
        AGENT_MODULE,
    ]
    print(f"\n=== arm: {arm} ===")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(f"arm {arm} failed (exit={proc.returncode})")

    n_total, n_passed = _parse_pass_fail(proc.stdout)
    in_tok, out_tok, cost, wall = _aggregate_spans(storage_dir / "traces.db")
    fires = _compression_fires(storage_dir / "optimizer_audit.db") if optimize else 0
    return ArmStats(
        arm=arm,
        n_tasks=n_total,
        n_passed=n_passed,
        total_input_tokens=in_tok,
        total_output_tokens=out_tok,
        total_cost_usd=cost,
        wall_clock_s=wall,
        compression_fire_count=fires,
    )


def render(arms: list[ArmStats]) -> str:
    """Pretty-print a 3-arm ablation summary."""
    by_arm = {a.arm: a for a in arms}
    baseline = by_arm.get("baseline")
    lines = [
        f"Agent: {AGENT_MODULE}",
        "",
        f"  {'arm':<22} {'n':<5} {'EM':<7} {'Δpp':<8} "
        f"{'in_tok':<10} {'out_tok':<10} {'cost_usd':<12} {'fires':<6}",
        "  " + "-" * 90,
    ]
    for a in arms:
        delta = ""
        if baseline is not None and a.arm != "baseline" and baseline.n_tasks:
            delta_pp = 100.0 * (a.em_accuracy - baseline.em_accuracy)
            delta = f"{delta_pp:+.2f}"
        lines.append(
            f"  {a.arm:<22} "
            f"{a.n_tasks:<5d} "
            f"{a.em_accuracy:.3f}   "
            f"{delta:<8} "
            f"{a.total_input_tokens:<10d} "
            f"{a.total_output_tokens:<10d} "
            f"${a.total_cost_usd:<10.4f} "
            f"{a.compression_fire_count:<6d}"
        )
    if baseline is not None:
        full = by_arm.get("agentc-full")
        if full is not None and baseline.total_cost_usd > 0:
            savings = 100.0 * (baseline.total_cost_usd - full.total_cost_usd) / baseline.total_cost_usd
            lines.append("")
            lines.append(f"  agentc-full cost savings vs baseline: {savings:+.1f}%")
    return "\n".join(lines)


_CSV_COLUMNS = [
    "arm",
    "n_tasks",
    "n_passed",
    "em_accuracy",
    "total_input_tokens",
    "total_output_tokens",
    "total_cost_usd",
    "wall_clock_s",
    "compression_fire_count",
]


def _row_values(a: ArmStats) -> list:
    return [
        a.arm,
        a.n_tasks,
        a.n_passed,
        f"{a.em_accuracy:.4f}",
        a.total_input_tokens,
        a.total_output_tokens,
        f"{a.total_cost_usd:.6f}",
        f"{a.wall_clock_s:.3f}",
        a.compression_fire_count,
    ]


def write_csv(arms: list[ArmStats], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_CSV_COLUMNS)
        for a in arms:
            w.writerow(_row_values(a))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m bench.run_hotpot_ablation",
        description="HotpotQA 3-arm ablation: baseline / agentc-full / agentc-no-compress.",
    )
    p.add_argument(
        "--storage-root",
        default="/tmp/agentc-hotpot-ablation",
        help="Root dir for per-arm storage (default: /tmp/agentc-hotpot-ablation)",
    )
    p.add_argument(
        "--out",
        default="bench/results/hotpot_ablation.csv",
        help="Output CSV path (default: bench/results/hotpot_ablation.csv)",
    )
    p.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Cap tasks per arm via BENCH_MAX_TASKS (default: full fixture)",
    )
    args = p.parse_args(argv)

    root = Path(args.storage_root)

    arms: list[ArmStats] = []
    arms.append(_run_arm(
        arm="baseline",
        storage_dir=root / "baseline",
        optimize=False,
        max_tasks=args.max_tasks,
    ))
    arms.append(_run_arm(
        arm="agentc-full",
        storage_dir=root / "agentc-full",
        optimize=True,
        rules_disabled=None,
        max_tasks=args.max_tasks,
    ))
    arms.append(_run_arm(
        arm="agentc-no-compress",
        storage_dir=root / "agentc-no-compress",
        optimize=True,
        rules_disabled=["ContextCompress"],
        max_tasks=args.max_tasks,
    ))

    print("\n" + render(arms))
    write_csv(arms, Path(args.out))
    print(f"\nWrote {len(arms)} arms to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
