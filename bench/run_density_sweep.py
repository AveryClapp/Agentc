"""Distractor density sweep — regime transition experiment.

Varies the number of distractor paragraphs on top of the base 10
HotpotQA paragraphs and measures how ContextCompress fire rate and
savings shift across the 8 KB activation threshold.

Density levels (extras added on top of the base 10 paragraphs):
  0, 2, 4, 6, 10, 14, 18

At extras=0 (10 total paragraphs, ~7–9 KB), roughly half of tasks fall
below the 8 KB gate and CC cannot fire. At extras=10+ (20+ paragraphs,
13+ KB), nearly all tasks clear the gate. The transition is sharp and
structurally determined — it doesn't depend on semantic content.

Output: bench/paper_results/density_sweep.csv

Usage:
    python -m bench.run_density_sweep                  # n=20 per level
    python -m bench.run_density_sweep --n 30 --seed 99
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


DENSITY_LEVELS = [0, 2, 4, 6, 10, 14, 18]
AGENT_MODULE = "bench.agents.long_context_qa"
RESULTS_DIR = Path("bench/paper_results")


@dataclass
class DensityRow:
    extras: int
    total_paragraphs: int
    n_tasks: int
    n_passed_baseline: int
    n_passed_optimized: int
    accuracy_baseline_pct: float
    accuracy_delta_pp: float
    cc_fires: int
    total_calls: int
    cc_fire_rate_pct: float
    base_input_tokens: int
    opt_input_tokens: int
    input_token_savings_pct: float
    base_cost_usd: float
    opt_cost_usd: float
    cost_savings_pct: float


def _build_fixture(extras: int, n_tasks: int, seed: int, out_path: Path) -> int:
    """Build a fixture at the given distractor density. Returns paragraph count."""
    from bench.build_long_context_fixture import build
    tasks = build(total=n_tasks, extras=extras, seed=seed)
    out_path.write_text(json.dumps(tasks, indent=2) + "\n")
    if tasks:
        return len(tasks[0].get("meta", {}).get("paragraphs", []))
    return 10 + extras


def _find_agentc_binary() -> str:
    from bench.optimizer_bench import _find_agentc_binary
    return _find_agentc_binary()


def _run_side(
    *,
    storage_dir: Path,
    fixture_path: Path,
    optimize: bool,
    n_tasks: int,
) -> str:
    storage_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["AGENTC_OPTIMIZE"] = "1" if optimize else "0"
    env["BENCH_MAX_TASKS"] = str(n_tasks)
    env["BENCH_FIXTURE_OVERRIDE"] = str(fixture_path)

    agentc_bin = _find_agentc_binary()
    cmd = [
        agentc_bin, "record",
        "--storage-path", str(storage_dir),
        "--",
        sys.executable, "-m", AGENT_MODULE,
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(
            f"agent failed (extras run, optimize={optimize}, exit={proc.returncode})"
        )
    return proc.stdout


_PF_RE = re.compile(r"^(PASS|FAIL)\s+\S+", re.MULTILINE)


def _parse_pass_fail(stdout: str) -> tuple[int, int]:
    n_total = n_passed = 0
    for m in _PF_RE.finditer(stdout):
        n_total += 1
        if m.group(1) == "PASS":
            n_passed += 1
    return n_total, n_passed


def _query_fire_rate(audit_db: Path) -> tuple[int, int]:
    """Return (cc_fires, total_plan_decisions) from plan_audit."""
    if not audit_db.is_file():
        return (0, 0)
    conn = sqlite3.connect(str(audit_db))
    try:
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN plan_kind='rewritten' AND rule='ContextCompress' "
            "           THEN 1 ELSE 0 END), "
            "  COUNT(*) "
            "FROM plan_audit"
        ).fetchone()
        return (int(row[0] or 0), int(row[1] or 0))
    finally:
        conn.close()


def _query_tokens(traces_db: Path) -> int:
    if not traces_db.is_file():
        return 0
    conn = sqlite3.connect(str(traces_db))
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) FROM spans"
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _query_cost(traces_db: Path) -> float:
    if not traces_db.is_file():
        return 0.0
    conn = sqlite3.connect(str(traces_db))
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM spans"
        ).fetchone()
        return float(row[0] or 0.0)
    finally:
        conn.close()


def run_sweep(n_tasks: int, seed: int) -> list[DensityRow]:
    rows: list[DensityRow] = []
    with tempfile.TemporaryDirectory(prefix="agentc_density_") as tmpdir:
        tmp = Path(tmpdir)
        for extras in DENSITY_LEVELS:
            print(f"\n=== extras={extras} ({10 + extras} total paragraphs) ===")
            fixture_path = tmp / f"fixture_d{extras}.json"
            total_paras = _build_fixture(extras, n_tasks, seed, fixture_path)

            baseline_dir = tmp / f"base_{extras}"
            optimized_dir = tmp / f"opt_{extras}"

            base_stdout = _run_side(
                storage_dir=baseline_dir,
                fixture_path=fixture_path,
                optimize=False,
                n_tasks=n_tasks,
            )
            opt_stdout = _run_side(
                storage_dir=optimized_dir,
                fixture_path=fixture_path,
                optimize=True,
                n_tasks=n_tasks,
            )

            n_total_b, n_passed_b = _parse_pass_fail(base_stdout)
            n_total_o, n_passed_o = _parse_pass_fail(opt_stdout)
            n_total = n_total_o or n_tasks

            cc_fires, total_calls = _query_fire_rate(
                optimized_dir / "optimizer_audit.db"
            )
            base_tokens = _query_tokens(baseline_dir / "traces.db")
            opt_tokens = _query_tokens(optimized_dir / "traces.db")
            base_cost = _query_cost(baseline_dir / "traces.db")
            opt_cost = _query_cost(optimized_dir / "traces.db")

            fire_rate = 100.0 * cc_fires / total_calls if total_calls else 0.0
            tok_savings = (
                100.0 * (base_tokens - opt_tokens) / base_tokens
                if base_tokens else 0.0
            )
            cost_savings = (
                100.0 * (base_cost - opt_cost) / base_cost
                if base_cost else 0.0
            )
            acc_base = 100.0 * n_passed_b / (n_total_b or n_tasks)
            acc_delta = 100.0 * (n_passed_o - n_passed_b) / (n_total_b or n_tasks)

            row = DensityRow(
                extras=extras,
                total_paragraphs=total_paras,
                n_tasks=n_total,
                n_passed_baseline=n_passed_b,
                n_passed_optimized=n_passed_o,
                accuracy_baseline_pct=acc_base,
                accuracy_delta_pp=acc_delta,
                cc_fires=cc_fires,
                total_calls=total_calls,
                cc_fire_rate_pct=fire_rate,
                base_input_tokens=base_tokens,
                opt_input_tokens=opt_tokens,
                input_token_savings_pct=tok_savings,
                base_cost_usd=base_cost,
                opt_cost_usd=opt_cost,
                cost_savings_pct=cost_savings,
            )
            rows.append(row)
            print(
                f"  cc_fire_rate={fire_rate:.1f}%  "
                f"tok_savings={tok_savings:.1f}%  "
                f"cost_savings={cost_savings:.1f}%  "
                f"accuracy_delta={acc_delta:+.1f}pp"
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m bench.run_density_sweep",
        description="Distractor density sweep for regime transition figure.",
    )
    p.add_argument(
        "--n", type=int, default=20,
        help="Tasks per density level (default: 20)",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--out",
        default=str(RESULTS_DIR / "density_sweep.csv"),
    )
    args = p.parse_args(argv)

    rows = run_sweep(n_tasks=args.n, seed=args.seed)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out)
    fieldnames = [
        "extras", "total_paragraphs", "n_tasks",
        "n_passed_baseline", "n_passed_optimized",
        "accuracy_baseline_pct", "accuracy_delta_pp",
        "cc_fires", "total_calls", "cc_fire_rate_pct",
        "base_input_tokens", "opt_input_tokens", "input_token_savings_pct",
        "base_cost_usd", "opt_cost_usd", "cost_savings_pct",
    ]
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "extras": r.extras,
                "total_paragraphs": r.total_paragraphs,
                "n_tasks": r.n_tasks,
                "n_passed_baseline": r.n_passed_baseline,
                "n_passed_optimized": r.n_passed_optimized,
                "accuracy_baseline_pct": f"{r.accuracy_baseline_pct:.2f}",
                "accuracy_delta_pp": f"{r.accuracy_delta_pp:.2f}",
                "cc_fires": r.cc_fires,
                "total_calls": r.total_calls,
                "cc_fire_rate_pct": f"{r.cc_fire_rate_pct:.2f}",
                "base_input_tokens": r.base_input_tokens,
                "opt_input_tokens": r.opt_input_tokens,
                "input_token_savings_pct": f"{r.input_token_savings_pct:.2f}",
                "base_cost_usd": f"{r.base_cost_usd:.6f}",
                "opt_cost_usd": f"{r.opt_cost_usd:.6f}",
                "cost_savings_pct": f"{r.cost_savings_pct:.2f}",
            })
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
