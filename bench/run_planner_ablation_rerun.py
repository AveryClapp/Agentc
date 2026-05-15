"""Composition-QA planner ablation rerun — isolated state, n=50 (§6.7).

Reruns the four key configurations from planner_ablation.csv under strict
per-config isolation so no run-order contamination can leak between configs.

Configs:
  baseline   — AGENTC_OPTIMIZE=0 (shared reference)
  V2-CC      — compose=1, ContextCompress only
  V1-CC+OB   — compose=0, ContextCompress + OutputBudget
  V2-CC+OB   — compose=1, ContextCompress + OutputBudget

Outputs:
  bench/paper_results/planner_ablation_rerun.csv  — same format as planner_ablation.csv
"""

from __future__ import annotations

import csv
import math
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "python"))

from bench.optimizer_bench import (
    _find_agentc_binary,
    _aggregate_from_db,
    _parse_per_task_pass_fail,
)
from bench.optimizer_ablation import _disable

AGENT = "bench.agents.composition_qa"
N_TASKS = 50
PAPER_RESULTS = _REPO / "bench" / "paper_results"
STORAGE_ROOT = Path("/tmp/agentc-planner-ablation-rerun")
OUT_PATH = PAPER_RESULTS / "planner_ablation_rerun.csv"

# Rules NOT in the CC-only config are disabled for V2-CC.
_ALL_RULES = [
    "CacheHit", "ContextCompress", "ParallelBranch", "ModelDowngrade",
    "StateDrop", "StructuredTruncation", "OutputBudget", "PromptDedup",
    "DeadOutputTruncation",
]

_CONFIGS: list[tuple[str, str, list[str]]] = [
    # (label, compose_mode, rules_enabled)
    ("V2-CC",    "1", ["ContextCompress"]),
    ("V1-CC+OB", "0", ["ContextCompress", "OutputBudget"]),
    ("V2-CC+OB", "1", ["ContextCompress", "OutputBudget"]),
]

_CSV_COLUMNS = [
    "config", "compose_mode", "rules",
    "n_pass", "n_total", "acc_pct", "acc_delta_pp", "se_pp",
    "mcnemar_p", "BF", "FB",
    "savings_total_usd", "n_plans_fired",
]


def _load_env() -> dict[str, str]:
    env = os.environ.copy()
    env_file = _REPO / ".env"
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


def _binom_pmf(n: int, k: int, p: float = 0.5) -> float:
    if n < 0 or k < 0 or k > n:
        return 0.0
    log_c = sum(math.log(n - i) - math.log(i + 1) for i in range(k))
    return math.exp(log_c + k * math.log(p) + (n - k) * math.log(1 - p))


def mcnemar_exact(n_BF: int, n_FB: int) -> float:
    n = n_BF + n_FB
    if n == 0:
        return 1.0
    observed = min(n_BF, n_FB)
    p_val = sum(_binom_pmf(n, k) for k in range(observed + 1))
    return min(1.0, 2 * p_val)


def _n_plans_fired(storage_dir: Path) -> int:
    """Count plan_audit rows with plan_kind='rewritten' from optimized side."""
    audit_db = storage_dir / "optimizer_audit.db"
    if not audit_db.is_file():
        return 0
    conn = sqlite3.connect(str(audit_db))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM plan_audit WHERE plan_kind='rewritten'"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _run_agent(
    storage_dir: Path,
    optimize: bool,
    extra_env: Optional[dict[str, str]] = None,
) -> list[tuple[str, bool]]:
    storage_dir.mkdir(parents=True, exist_ok=True)
    env = _load_env()
    env["AGENTC_OPTIMIZE"] = "1" if optimize else "0"
    env["BENCH_MAX_TASKS"] = str(N_TASKS)
    env["PYTHONPATH"] = str(_REPO / "python")
    if extra_env:
        env.update(extra_env)

    agentc_bin = _find_agentc_binary()
    cmd = [
        agentc_bin, "record",
        "--storage-path", str(storage_dir),
        "--",
        sys.executable, "-m", AGENT,
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(
            f"{AGENT} failed (exit={proc.returncode}, optimize={optimize})"
        )
    return _parse_per_task_pass_fail(proc.stdout)


def main() -> int:
    PAPER_RESULTS.mkdir(parents=True, exist_ok=True)
    if STORAGE_ROOT.exists():
        shutil.rmtree(STORAGE_ROOT)
    STORAGE_ROOT.mkdir(parents=True)

    # Write CSV header.
    with OUT_PATH.open("w", newline="") as f:
        csv.writer(f).writerow(_CSV_COLUMNS)

    def _append_row(row: dict) -> None:
        with OUT_PATH.open("a", newline="") as f:
            w = csv.writer(f)
            w.writerow([row[c] for c in _CSV_COLUMNS])
        print(f"  → wrote row '{row['config']}' to {OUT_PATH}")

    # --- Baseline ---
    print(f"\n{'='*60}")
    print(f"baseline (AGENTC_OPTIMIZE=0), n={N_TASKS}")
    print("="*60)
    baseline_dir = STORAGE_ROOT / "baseline"
    baseline_per = _run_agent(baseline_dir, optimize=False)
    n_baseline_pass = sum(1 for _, p in baseline_per if p)
    n_baseline_total = len(baseline_per)
    b_acc = 100.0 * n_baseline_pass / n_baseline_total if n_baseline_total else 0.0
    se = 100.0 * math.sqrt(
        (b_acc / 100.0) * (1 - b_acc / 100.0) / n_baseline_total
    ) if n_baseline_total else 0.0

    _append_row({
        "config": "baseline",
        "compose_mode": "N/A",
        "rules": "none",
        "n_pass": n_baseline_pass,
        "n_total": n_baseline_total,
        "acc_pct": f"{b_acc:.1f}",
        "acc_delta_pp": "0.0",
        "se_pp": f"{se:.1f}",
        "mcnemar_p": "1.0",
        "BF": 0,
        "FB": 0,
        "savings_total_usd": "0.0",
        "n_plans_fired": 0,
    })

    baseline_map = dict(baseline_per)

    # --- Optimized configs ---
    for label, compose_mode, rules_enabled in _CONFIGS:
        print(f"\n{'='*60}")
        print(f"{label} (compose={compose_mode}, rules={rules_enabled}), n={N_TASKS}")
        print("="*60)

        cfg_dir = STORAGE_ROOT / label
        cfg_dir.mkdir(parents=True)
        optimized_dir = cfg_dir / "optimized"
        optimized_dir.mkdir(parents=True)

        # Disable rules NOT in rules_enabled.
        rules_off = [r for r in _ALL_RULES if r not in rules_enabled]
        _disable(rules_off, optimized_dir)

        extra = {"AGENTC_COMPOSE": compose_mode}
        per_task = _run_agent(optimized_dir, optimize=True, extra_env=extra)

        n_pass = sum(1 for _, p in per_task if p)
        n_total = len(per_task)
        opt_map = dict(per_task)

        # Paired McNemar against baseline.
        n_BF = n_FB = 0
        for tid, bp in baseline_per:
            op = opt_map.get(tid, False)
            if bp and not op:
                n_BF += 1
            elif not bp and op:
                n_FB += 1

        p_val = mcnemar_exact(n_BF, n_FB)
        acc = 100.0 * n_pass / n_total if n_total else 0.0
        acc_delta = acc - b_acc
        se_opt = 100.0 * math.sqrt(
            (acc / 100.0) * (1 - acc / 100.0) / n_total
        ) if n_total else 0.0

        # Cost savings from optimized traces.db (vs baseline).
        b_cost, _, _ = _aggregate_from_db(baseline_dir / "traces.db")
        o_cost, _, _ = _aggregate_from_db(optimized_dir / "traces.db")
        savings = b_cost - o_cost

        n_fired = _n_plans_fired(optimized_dir)

        print(
            f"  baseline={n_baseline_pass}/{n_baseline_total}  "
            f"optimized={n_pass}/{n_total}  "
            f"BF={n_BF}  FB={n_FB}  p={p_val:.4f}"
        )

        _append_row({
            "config": label,
            "compose_mode": compose_mode,
            "rules": ",".join(rules_enabled),
            "n_pass": n_pass,
            "n_total": n_total,
            "acc_pct": f"{acc:.1f}",
            "acc_delta_pp": f"{acc_delta:.1f}",
            "se_pp": f"{se_opt:.1f}",
            "mcnemar_p": f"{p_val:.4f}",
            "BF": n_BF,
            "FB": n_FB,
            "savings_total_usd": f"{savings:.6f}",
            "n_plans_fired": n_fired,
        })

    print(f"\nPlanner ablation rerun complete. Results: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
