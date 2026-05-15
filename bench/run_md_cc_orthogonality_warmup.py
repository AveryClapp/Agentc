"""MD+CC orthogonality re-verification with warmup phase (section 6.7).

Two-phase protocol per config:
  Phase 1 (warmup): W tasks, AGENTC_OPTIMIZE=1, writes call_site_profile to cost_model.db.
  Between phases: traces.db deleted (resets cost/token accounting), cost_model.db kept.
  Phase 2 (measure): N tasks, AGENTC_OPTIMIZE=1, uses warm call_site_profile.

Baseline runs N measurement tasks only (AGENTC_OPTIMIZE=0, no warmup needed).
All configs use AGENTC_COMPOSE=1. Baseline model: gpt-4o (matches default route table).

Cost ceiling: $25 hard stop (expected ~$15).

Outputs:
  bench/paper_results/md_cc_orthogonality_warmup.csv
  bench/paper_results/md_cc_orthogonality_warmup.summary.txt
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

from bench.optimizer_bench import (
    _find_agentc_binary,
    _aggregate_from_db,
    _parse_per_task_pass_fail,
)
from bench.optimizer_ablation import _disable, RULES

AGENT = "bench.agents.long_context_qa"
W_TASKS = 30
N_TASKS = 20
COST_CEILING_USD = 25.0
PAPER_RESULTS = _REPO / "bench" / "paper_results"
STORAGE_ROOT = Path("/tmp/agentc-md-cc-orthogonality-warmup")
OUT_PATH = PAPER_RESULTS / "md_cc_orthogonality_warmup.csv"
SUMMARY_PATH = PAPER_RESULTS / "md_cc_orthogonality_warmup.summary.txt"

_CSV_COLUMNS = [
    "config",
    "n_pass", "n_total", "acc_pct", "acc_delta_pp", "se_pp",
    "mcnemar_p", "BF", "FB",
    "baseline_cost_mUSD", "optimized_cost_mUSD", "cost_savings_mUSD",
    "input_tokens_baseline", "input_tokens_optimized", "input_token_savings_pct",
    "cc_fire_count", "md_fire_count",
    "gpt4o_calls", "gpt4o_mini_calls", "md_fire_rate_pct",
]

_CONFIGS: list[tuple[str, list[str]]] = [
    ("CC-only", ["ContextCompress"]),
    ("MD-only", ["ModelDowngrade"]),
    ("CC+MD",   ["ContextCompress", "ModelDowngrade"]),
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


def _run_phase(
    storage_dir: Path,
    optimize: bool,
    task_offset: int,
    n_tasks: int,
    extra_env: Optional[dict[str, str]] = None,
) -> tuple[list[tuple[str, bool]], float, int]:
    """Returns (per_task, cost_usd, input_tokens)."""
    storage_dir.mkdir(parents=True, exist_ok=True)
    env = _load_env()
    env["AGENTC_OPTIMIZE"] = "1" if optimize else "0"
    env["BENCH_MAX_TASKS"] = str(n_tasks)
    env["BENCH_TASK_OFFSET"] = str(task_offset)
    env["PYTHONPATH"] = str(_REPO / "python")
    env["BENCH_BASELINE_MODEL"] = "gpt-4o"
    env["AGENTC_COMPOSE"] = "1"
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
        raise RuntimeError(f"{AGENT} failed (exit={proc.returncode}, optimize={optimize})")
    per_task = _parse_per_task_pass_fail(proc.stdout)
    cost, _, tokens = _aggregate_from_db(storage_dir / "traces.db")
    return per_task, cost, tokens


def _log_warmup_state(storage_dir: Path) -> int:
    """Log call_site_profile stats after warmup. Returns site count."""
    db = storage_dir / "cost_model.db"
    if not db.is_file():
        print("  warmup: cost_model.db not found")
        return 0
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT call_site_id, n_observations FROM call_site_profile"
        ).fetchall()
        if not rows:
            print("  warmup completed: 0 call sites observed (cold start)")
            return 0
        mean_obs = sum(n for _, n in rows) / len(rows)
        print(f"  warmup completed: {len(rows)} call sites observed, mean={mean_obs:.1f} observations each")
        for site_id, n_obs in rows:
            print(f"    {site_id}: n_obs={n_obs}")
        return len(rows)
    finally:
        conn.close()


def _query_rule_fires(storage_dir: Path) -> dict[str, int]:
    """Return {rule_name: fire_count} from plan_audit."""
    db = storage_dir / "optimizer_audit.db"
    if not db.is_file():
        return {}
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT rule, COUNT(*) FROM plan_audit WHERE plan_kind = 'rewritten' AND rule IS NOT NULL GROUP BY rule"
        ).fetchall()
        return {rule: count for rule, count in rows}
    finally:
        conn.close()


def _query_model_dispatch(storage_dir: Path) -> tuple[int, int]:
    """Return (gpt4o_calls, gpt4o_mini_calls) from spans table."""
    db = storage_dir / "traces.db"
    if not db.is_file():
        return (0, 0)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT model, COUNT(*) FROM spans GROUP BY model").fetchall()
        gpt4o = sum(cnt for m, cnt in rows if m and "gpt-4o" in m and "mini" not in m)
        mini = sum(cnt for m, cnt in rows if m and "mini" in m)
        return (gpt4o, mini)
    finally:
        conn.close()


def main() -> int:
    PAPER_RESULTS.mkdir(parents=True, exist_ok=True)
    if STORAGE_ROOT.exists():
        shutil.rmtree(STORAGE_ROOT)
    STORAGE_ROOT.mkdir(parents=True)

    with OUT_PATH.open("w", newline="") as f:
        csv.writer(f).writerow(_CSV_COLUMNS)

    def _append_row(row: dict) -> None:
        with OUT_PATH.open("a", newline="") as f:
            csv.writer(f).writerow([row[c] for c in _CSV_COLUMNS])
        print(f"  -> wrote row '{row['config']}' to {OUT_PATH}")

    cumulative_cost_usd = 0.0

    def _check_ceiling(label: str, cost: float) -> None:
        nonlocal cumulative_cost_usd
        cumulative_cost_usd += cost
        print(f"  cumulative cost: ${cumulative_cost_usd:.4f} USD (ceiling: ${COST_CEILING_USD})")
        if cumulative_cost_usd > COST_CEILING_USD:
            raise RuntimeError(
                f"COST CEILING EXCEEDED at '{label}': cumulative=${cumulative_cost_usd:.4f} > ${COST_CEILING_USD}"
            )

    print(f"\n{'='*60}")
    print(f"baseline (AGENTC_OPTIMIZE=0, tasks {W_TASKS}..{W_TASKS+N_TASKS-1})")
    print("="*60)
    baseline_dir = STORAGE_ROOT / "baseline"
    baseline_per, baseline_cost, baseline_tokens = _run_phase(
        baseline_dir, optimize=False, task_offset=W_TASKS, n_tasks=N_TASKS
    )
    _check_ceiling("baseline", baseline_cost)
    n_base_pass = sum(1 for _, p in baseline_per if p)
    n_total = len(baseline_per)
    b_acc = 100.0 * n_base_pass / n_total if n_total else 0.0
    se_b = 100.0 * math.sqrt(b_acc / 100.0 * (1 - b_acc / 100.0) / n_total) if n_total else 0.0

    _append_row({
        "config": "baseline",
        "n_pass": n_base_pass, "n_total": n_total,
        "acc_pct": f"{b_acc:.1f}", "acc_delta_pp": "0.0", "se_pp": f"{se_b:.1f}",
        "mcnemar_p": "1.0", "BF": 0, "FB": 0,
        "baseline_cost_mUSD": f"{baseline_cost*1000:.4f}",
        "optimized_cost_mUSD": f"{baseline_cost*1000:.4f}",
        "cost_savings_mUSD": "0.0",
        "input_tokens_baseline": baseline_tokens,
        "input_tokens_optimized": baseline_tokens,
        "input_token_savings_pct": "0.0",
        "cc_fire_count": 0, "md_fire_count": 0,
        "gpt4o_calls": 0, "gpt4o_mini_calls": 0, "md_fire_rate_pct": "0.0",
    })
    print(f"  baseline: {n_base_pass}/{n_total}  ${baseline_cost*1000:.4f} mUSD  {baseline_tokens:,} tokens")

    savings: dict[str, float] = {}
    tok_savings: dict[str, float] = {}

    for label, rules_enabled in _CONFIGS:
        print(f"\n{'='*60}")
        print(f"{label}  warmup={W_TASKS}  measure={N_TASKS}  compose=1")
        print("="*60)

        cfg_dir = STORAGE_ROOT / label
        cfg_dir.mkdir(parents=True)
        opt_dir = cfg_dir / "optimized"
        opt_dir.mkdir(parents=True)

        rules_off = [r for r in RULES if r not in rules_enabled]
        _disable(rules_off, opt_dir)

        # Phase 1: warmup
        print(f"  [warmup] tasks 0..{W_TASKS-1}")
        _run_phase(opt_dir, optimize=True, task_offset=0, n_tasks=W_TASKS)
        n_warmup_sites = _log_warmup_state(opt_dir)
        warmup_cost, _, _ = _aggregate_from_db(opt_dir / "traces.db")
        _check_ceiling(f"{label} warmup", warmup_cost)

        # Reset token accounting; preserve cost_model.db
        traces_db = opt_dir / "traces.db"
        lock_file = opt_dir / "traces.db.lock"
        if traces_db.is_file():
            traces_db.unlink()
        if lock_file.is_file():
            lock_file.unlink()

        # Phase 2: measurement
        print(f"  [measure] tasks {W_TASKS}..{W_TASKS+N_TASKS-1}")
        per_task, opt_cost, opt_tokens = _run_phase(
            opt_dir, optimize=True, task_offset=W_TASKS, n_tasks=N_TASKS
        )
        _check_ceiling(f"{label} measure", opt_cost)

        n_pass = sum(1 for _, p in per_task if p)
        opt_map = dict(per_task)

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
        se = 100.0 * math.sqrt(acc / 100.0 * (1 - acc / 100.0) / n_total) if n_total else 0.0

        cost_saved_musd = (baseline_cost - opt_cost) * 1000.0
        savings[label] = cost_saved_musd
        tok_saved_pct = (
            100.0 * (baseline_tokens - opt_tokens) / baseline_tokens
            if baseline_tokens > 0 else 0.0
        )
        tok_savings[label] = tok_saved_pct

        rule_fires = _query_rule_fires(opt_dir)
        cc_fires = rule_fires.get("ContextCompress", 0)
        md_fires = rule_fires.get("ModelDowngrade", 0)
        gpt4o_calls, gpt4o_mini_calls = _query_model_dispatch(opt_dir)
        total_calls = gpt4o_calls + gpt4o_mini_calls
        md_fire_rate = 100.0 * gpt4o_mini_calls / total_calls if total_calls > 0 else 0.0

        print(
            f"  {n_pass}/{n_total}  BF={n_BF} FB={n_FB} p={p_val:.4f}  "
            f"cost_saved={cost_saved_musd:+.4f} mUSD  tok_saved={tok_saved_pct:+.2f}%  "
            f"CC_fires={cc_fires}  MD_fires={md_fires}  "
            f"gpt4o={gpt4o_calls} mini={gpt4o_mini_calls} md_rate={md_fire_rate:.1f}%"
        )

        _append_row({
            "config": label,
            "n_pass": n_pass, "n_total": n_total,
            "acc_pct": f"{acc:.1f}", "acc_delta_pp": f"{acc_delta:.1f}", "se_pp": f"{se:.1f}",
            "mcnemar_p": f"{p_val:.4f}", "BF": n_BF, "FB": n_FB,
            "baseline_cost_mUSD": f"{baseline_cost*1000:.4f}",
            "optimized_cost_mUSD": f"{opt_cost*1000:.4f}",
            "cost_savings_mUSD": f"{cost_saved_musd:.4f}",
            "input_tokens_baseline": baseline_tokens,
            "input_tokens_optimized": opt_tokens,
            "input_token_savings_pct": f"{tok_saved_pct:.2f}",
            "cc_fire_count": cc_fires,
            "md_fire_count": md_fires,
            "gpt4o_calls": gpt4o_calls,
            "gpt4o_mini_calls": gpt4o_mini_calls,
            "md_fire_rate_pct": f"{md_fire_rate:.1f}",
        })

    cc_savings = savings.get("CC-only", 0.0)
    md_savings = savings.get("MD-only", 0.0)
    both_savings = savings.get("CC+MD", 0.0)
    additive_ideal = cc_savings + md_savings
    efficiency = 100.0 * both_savings / additive_ideal if additive_ideal != 0 else float("nan")

    cc_tok = tok_savings.get("CC-only", 0.0)
    md_tok = tok_savings.get("MD-only", 0.0)
    both_tok = tok_savings.get("CC+MD", 0.0)

    summary_lines = [
        f"MD+CC orthogonality warmup rerun -- long_context_qa, W={W_TASKS} warmup, N={N_TASKS} measure, gpt-4o base, compose=1",
        "-" * 70,
        f"{'config':<12} {'cost_saved_mUSD':>15} {'tok_saved_pct':>14} {'md_fire_rate':>13}",
        "-" * 70,
        f"{'CC-only':<12} {cc_savings:>15.4f} {cc_tok:>13.2f}% {'N/A':>13}",
        f"{'MD-only':<12} {md_savings:>15.4f} {md_tok:>13.2f}% {tok_savings.get('MD-only', 0.0):>12.2f}%",
        f"{'CC+MD':<12} {both_savings:>15.4f} {both_tok:>13.2f}% {'see_md_cols':>13}",
        "-" * 70,
        f"Additive ideal: CC({cc_savings:.4f}) + MD({md_savings:.4f}) = {additive_ideal:.4f} mUSD",
        f"CC+MD actual: {both_savings:.4f} mUSD = {efficiency:.1f}% of additive ideal",
        "",
        f"ORTHOGONALITY EFFICIENCY: {efficiency:.1f}%  (target: >=95.6%)",
        "",
        f"Total cumulative cost: ${cumulative_cost_usd:.4f} USD",
    ]

    summary_text = "\n".join(summary_lines)
    print("\n" + summary_text)
    SUMMARY_PATH.write_text(summary_text + "\n")

    print(f"\nResults: {OUT_PATH}")
    print(f"Summary: {SUMMARY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
