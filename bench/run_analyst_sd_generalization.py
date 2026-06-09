"""StateDrop generalization probe: analyst_qa on SQuAD (different domain).

Tests whether StateDrop's accuracy degradation (observed on research_planner)
reproduces on a different agent + corpus that also violates the read-window
assumption (context forwarded into the prompt but not state_read).

Configs: all-on, StateDrop-only, StateDrop-off (override via AQ_CONFIGS).
Shared baseline, per-config warmup ($W$=20) + isolated optimizer state, temp=0.

Outputs:
  bench/paper_results/analyst_qa_sd_gen.csv
  bench/paper_results/analyst_qa-sd-gen.per_task.csv   (paired_analysis input)
"""
from __future__ import annotations

import csv
import math
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "python"))

from bench.optimizer_bench import _find_agentc_binary, _aggregate_from_db, _parse_per_task_pass_fail
from bench.optimizer_ablation import _disable

AGENT = "bench.agents.analyst_qa"
W_TASKS = int(os.environ.get("AQ_W", "20"))
N_TASKS = int(os.environ.get("AQ_N", "150"))
COST_CEILING_USD = 5.0
ABORT_CEILING_USD = 8.0
PAPER_RESULTS = _REPO / "bench" / "paper_results"
STORAGE_ROOT = Path("/tmp/agentc-analyst-sd-gen")
OUT_PATH = PAPER_RESULTS / "analyst_qa_sd_gen.csv"
PER_TASK_PATH = PAPER_RESULTS / "analyst_qa-sd-gen.per_task.csv"

_ALL_RULES = [
    "CacheHit", "ContextCompress", "ParallelBranch", "ModelDowngrade", "StateDrop",
    "StructuredTruncation", "OutputBudget", "PromptDedup", "DeadOutputTruncation",
]
_CONFIGS = [c.strip() for c in os.environ.get(
    "AQ_CONFIGS", "all-on,StateDrop-only,StateDrop-off").split(",") if c.strip()]

_CSV_COLUMNS = [
    "config", "n_pass", "n_total", "acc_pct", "acc_delta_pp", "se_pp",
    "mcnemar_p", "BF", "FB",
    "baseline_cost_mUSD", "optimized_cost_mUSD", "cost_savings_mUSD",
    "input_tokens_baseline", "input_tokens_optimized", "input_token_savings_pct",
    "sd_fire_count", "total_calls",
]
_PER_TASK_COLS = ["agent_module", "config", "task_id", "baseline_passed", "optimized_passed"]


def _rules_off(config: str) -> list[str]:
    if config == "all-on":
        return []
    if config.endswith("-off"):
        return [config[:-4]]
    if config.endswith("-only"):
        rule = config[:-5]
        return [r for r in _ALL_RULES if r != rule]
    raise ValueError(config)


def _load_env() -> dict[str, str]:
    env = os.environ.copy()
    env_file = _REPO / ".env"
    if env_file.is_file():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
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
    return min(1.0, 2 * sum(_binom_pmf(n, k) for k in range(observed + 1)))


def _run_phase(storage_dir: Path, optimize: bool, n_tasks: int) -> tuple[list[tuple[str, bool]], float, int]:
    storage_dir.mkdir(parents=True, exist_ok=True)
    env = _load_env()
    env["AGENTC_OPTIMIZE"] = "1" if optimize else "0"
    env["BENCH_MAX_TASKS"] = str(n_tasks)
    env["BENCH_TASK_OFFSET"] = "0"
    env["PYTHONPATH"] = str(_REPO / "python")
    env["AGENTC_COMPOSE"] = "1"
    agentc_bin = _find_agentc_binary()
    cmd = [agentc_bin, "record", "--storage-path", str(storage_dir), "--", sys.executable, "-m", AGENT]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(f"{AGENT} failed (exit={proc.returncode})")
    per_task = _parse_per_task_pass_fail(proc.stdout)
    cost, _, tokens = _aggregate_from_db(storage_dir / "traces.db")
    return per_task, cost, tokens


def _reset_between_phases(opt_dir: Path) -> None:
    for fname in ["traces.db", "traces.db.lock", "optimizer_audit.db"]:
        p = opt_dir / fname
        if p.is_file():
            p.unlink()


def _query_sd_fires(storage_dir: Path) -> tuple[int, int]:
    db = storage_dir / "optimizer_audit.db"
    if not db.is_file():
        return (0, 0)
    conn = sqlite3.connect(str(db))
    try:
        sd = conn.execute(
            "SELECT COUNT(*) FROM plan_audit WHERE rule='StateDrop' AND plan_kind IN ('rewritten','composed')"
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM plan_audit").fetchone()
        return (int(sd[0]) if sd else 0, int(total[0]) if total else 0)
    finally:
        conn.close()


def main() -> int:
    PAPER_RESULTS.mkdir(parents=True, exist_ok=True)
    if STORAGE_ROOT.exists():
        shutil.rmtree(STORAGE_ROOT)
    STORAGE_ROOT.mkdir(parents=True)

    with OUT_PATH.open("w", newline="") as f:
        csv.writer(f).writerow(_CSV_COLUMNS)
    with PER_TASK_PATH.open("w", newline="") as f:
        csv.writer(f).writerow(_PER_TASK_COLS)

    cumulative = 0.0

    def _check(label: str, cost: float) -> None:
        nonlocal cumulative
        cumulative += cost
        print(f"  cumulative=${cumulative:.4f} (warn=${COST_CEILING_USD} abort=${ABORT_CEILING_USD})")
        if cumulative > ABORT_CEILING_USD:
            raise RuntimeError(f"ABORT CEILING at '{label}': ${cumulative:.4f}")

    print(f"\n{'='*60}\nbaseline (OPTIMIZE=0, N={N_TASKS})\n{'='*60}")
    baseline_dir = STORAGE_ROOT / "baseline"
    baseline_per, baseline_cost, baseline_tokens = _run_phase(baseline_dir, optimize=False, n_tasks=N_TASKS)
    _check("baseline", baseline_cost)
    n_base_pass = sum(1 for _, p in baseline_per if p)
    b_acc = 100.0 * n_base_pass / len(baseline_per) if baseline_per else 0.0
    print(f"  baseline: {n_base_pass}/{len(baseline_per)}  {baseline_tokens:,} tok")

    for config in _CONFIGS:
        print(f"\n{'='*60}\n{config}  W={W_TASKS} N={N_TASKS}\n{'='*60}")
        opt_dir = STORAGE_ROOT / config / "optimized"
        opt_dir.mkdir(parents=True)
        _disable(_rules_off(config), opt_dir)

        print(f"  [warmup] 0..{W_TASKS-1}")
        _run_phase(opt_dir, optimize=True, n_tasks=W_TASKS)
        wcost, _, _ = _aggregate_from_db(opt_dir / "traces.db")
        _check(f"{config} warmup", wcost)
        _reset_between_phases(opt_dir)

        print(f"  [measure] 0..{N_TASKS-1}")
        per_task, opt_cost, opt_tokens = _run_phase(opt_dir, optimize=True, n_tasks=N_TASKS)
        _check(f"{config} measure", opt_cost)

        opt_map = dict(per_task)
        n_pass = sum(1 for _, p in per_task if p)
        n = len(per_task)
        n_BF = sum(1 for tid, bp in baseline_per if bp and not opt_map.get(tid, False))
        n_FB = sum(1 for tid, bp in baseline_per if not bp and opt_map.get(tid, False))
        p_val = mcnemar_exact(n_BF, n_FB)
        acc = 100.0 * n_pass / n if n else 0.0
        se = 100.0 * math.sqrt(acc / 100.0 * (1 - acc / 100.0) / n) if n else 0.0
        cost_saved = (baseline_cost - opt_cost) * 1000.0
        tok_saved = 100.0 * (baseline_tokens - opt_tokens) / baseline_tokens if baseline_tokens else 0.0
        sd_fires, total_calls = _query_sd_fires(opt_dir)

        print(f"  {n_pass}/{n}  BF={n_BF} FB={n_FB} p={p_val:.4f}  "
              f"tok_saved={tok_saved:+.2f}%  SD_fires={sd_fires}/{total_calls}")

        with OUT_PATH.open("a", newline="") as f:
            csv.writer(f).writerow([
                config, n_pass, n, f"{acc:.1f}", f"{acc-b_acc:.1f}", f"{se:.1f}",
                f"{p_val:.4f}", n_BF, n_FB,
                f"{baseline_cost*1000:.4f}", f"{opt_cost*1000:.4f}", f"{cost_saved:.4f}",
                baseline_tokens, opt_tokens, f"{tok_saved:.2f}", sd_fires, total_calls,
            ])
        with PER_TASK_PATH.open("a", newline="") as f:
            w = csv.writer(f)
            for tid, bp in baseline_per:
                w.writerow([AGENT, config, tid, int(bp), int(opt_map.get(tid, False))])

    print(f"\nDone. cumulative=${cumulative:.4f}")
    print(f"Results: {OUT_PATH}\nPer-task: {PER_TASK_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
