"""Generalization evaluation runner — Experiments 1 and 2.

Runs shared-baseline ablation for three agents:
  - rag_summarizer   (Experiment 1a: real agent, ours)
  - autogen_bridge   (Experiment 1b: real agent, ours)
  - support_qa       (Experiment 2: cold agent — never designed for Agentc)

For each agent:
  1. Baseline run (AGENTC_OPTIMIZE=0) — one shared run
  2. All-on optimized run (AGENTC_OPTIMIZE=1)
  3. McNemar exact test on per-task pass/fail discordant pairs
  4. Rule activation rates from optimizer_audit.db

Outputs written to bench/paper_results/:
  generalization_evals.csv          — per-agent summary row
  generalization_evals.summary.txt  — human-readable
  rule_activation_rates.csv         — per-agent per-rule activation rates
"""

from __future__ import annotations

import csv
import math
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Ensure python/ SDK is importable when running directly.
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "python"))

from bench.optimizer_bench import _find_agentc_binary, _parse_per_task_pass_fail

PAPER_RESULTS = _REPO / "bench" / "paper_results"
STORAGE_ROOT = Path("/tmp/agentc-generalization")


# ---------------------------------------------------------------------------
# McNemar exact test (two-sided binomial on discordant pairs)
# ---------------------------------------------------------------------------

def _binom_pmf(n: int, k: int, p: float = 0.5) -> float:
    if n < 0 or k < 0 or k > n:
        return 0.0
    log_c = sum(math.log(n - i) - math.log(i + 1) for i in range(k))
    return math.exp(log_c + k * math.log(p) + (n - k) * math.log(1 - p))


def mcnemar_exact(n_BF: int, n_FB: int) -> float:
    """Two-sided exact McNemar p-value."""
    n = n_BF + n_FB
    if n == 0:
        return 1.0
    observed = min(n_BF, n_FB)
    p_val = sum(_binom_pmf(n, k) for k in range(observed + 1))
    return min(1.0, 2 * p_val)


# ---------------------------------------------------------------------------
# Run one side (baseline or optimized) via agentc record
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    n_tasks: int
    n_passed: int
    cost_usd: float
    input_tokens: int
    per_task: list[tuple[str, bool]]  # (task_id, passed)
    stub_mode: bool


def _run_side(
    agent_module: str,
    storage_dir: Path,
    optimize: bool,
    n_tasks: int,
) -> RunResult:
    storage_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    # Load .env manually so subprocess inherits the API key.
    env_file = _REPO / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in env:
                env[k] = v

    env["AGENTC_OPTIMIZE"] = "1" if optimize else "0"
    env["BENCH_MAX_TASKS"] = str(n_tasks)
    env["PYTHONPATH"] = str(_REPO / "python")
    env["AGENTC_COMPOSE"] = "1"

    agentc_bin = _find_agentc_binary()
    cmd = [
        agentc_bin, "record",
        "--storage-path", str(storage_dir),
        "--",
        sys.executable, "-m", agent_module,
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(
            f"{agent_module} failed (exit={proc.returncode}, opt={optimize})"
        )

    per_task = _parse_per_task_pass_fail(proc.stdout)
    n_passed = sum(1 for _, p in per_task)

    db = storage_dir / "traces.db"
    cost = 0.0
    tokens = 0
    if db.is_file():
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(input_tokens),0) FROM spans"
            ).fetchone()
            cost, tokens = float(row[0]), int(row[1])
        finally:
            conn.close()

    stub_mode = not env.get("OPENAI_API_KEY")
    return RunResult(
        n_tasks=len(per_task),
        n_passed=n_passed,
        cost_usd=cost,
        input_tokens=tokens,
        per_task=per_task,
        stub_mode=stub_mode,
    )


def _query_activation(storage_dir: Path) -> list[dict]:
    """Return per-rule firing counts from optimizer_audit.db."""
    audit_db = storage_dir / "optimizer_audit.db"
    if not audit_db.is_file():
        return []
    conn = sqlite3.connect(str(audit_db))
    try:
        rows = conn.execute(
            "SELECT rule, plan_kind, COUNT(*) as n_fired "
            "FROM plan_audit "
            "GROUP BY rule, plan_kind "
            "ORDER BY n_fired DESC"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM plan_audit").fetchone()[0]
    finally:
        conn.close()
    out = []
    for rule, plan_kind, n_fired in rows:
        out.append({
            "rule": rule or "none",
            "plan_kind": plan_kind or "pass_through",
            "n_fired": n_fired,
            "n_total": total,
            "fire_rate_pct": round(100.0 * n_fired / total, 1) if total else 0.0,
        })
    return out


# ---------------------------------------------------------------------------
# Main evaluation loop
# ---------------------------------------------------------------------------

@dataclass
class EvalConfig:
    agent_module: str
    label: str
    n_tasks: int


AGENTS = [
    EvalConfig("bench.agents.rag_summarizer",  "rag_summarizer",  30),
    EvalConfig("bench.agents.autogen_bridge",  "autogen_bridge",  30),
    EvalConfig("bench.agents.support_qa",      "support_qa",      39),
]


def run_all() -> None:
    PAPER_RESULTS.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    activation_rows = []

    for cfg in AGENTS:
        print(f"\n{'='*60}")
        print(f"Agent: {cfg.label}  (n={cfg.n_tasks})")
        print("="*60)

        storage = STORAGE_ROOT / cfg.label
        if storage.exists():
            shutil.rmtree(storage)

        baseline_dir = storage / "baseline"
        optimized_dir = storage / "optimized"

        print(f"\n[1/2] Baseline run...")
        baseline = _run_side(cfg.agent_module, baseline_dir, optimize=False,
                             n_tasks=cfg.n_tasks)

        print(f"\n[2/2] Optimized run...")
        optimized = _run_side(cfg.agent_module, optimized_dir, optimize=True,
                              n_tasks=cfg.n_tasks)

        # Paired pass/fail for McNemar.
        b_map = dict(baseline.per_task)
        o_map = dict(optimized.per_task)
        task_ids = [t for t, _ in baseline.per_task]
        n_BB = n_BF = n_FB = n_FF = 0
        for tid in task_ids:
            bp = b_map.get(tid, False)
            op = o_map.get(tid, False)
            if bp and op:       n_BB += 1
            elif bp and not op: n_BF += 1
            elif not bp and op: n_FB += 1
            else:               n_FF += 1

        mcnemar_p = mcnemar_exact(n_BF, n_FB)

        # Cost / token deltas.
        b_acc = baseline.n_passed / baseline.n_tasks if baseline.n_tasks else 0
        o_acc = optimized.n_passed / optimized.n_tasks if optimized.n_tasks else 0
        acc_delta = (o_acc - b_acc) * 100.0

        cost_delta = (
            100.0 * (baseline.cost_usd - optimized.cost_usd) / baseline.cost_usd
            if baseline.cost_usd > 0 else 0.0
        )
        tok_delta = (
            100.0 * (baseline.input_tokens - optimized.input_tokens) / baseline.input_tokens
            if baseline.input_tokens > 0 else 0.0
        )

        row = dict(
            agent=cfg.label,
            n=cfg.n_tasks,
            baseline_acc_pct=round(b_acc * 100, 1),
            optimized_acc_pct=round(o_acc * 100, 1),
            acc_delta_pp=round(acc_delta, 1),
            cost_delta_pct=round(cost_delta, 1),
            input_tok_delta_pct=round(tok_delta, 1),
            baseline_cost_usd=round(baseline.cost_usd, 4),
            optimized_cost_usd=round(optimized.cost_usd, 4),
            n_BB=n_BB, n_BF=n_BF, n_FB=n_FB, n_FF=n_FF,
            mcnemar_p=round(mcnemar_p, 4),
            stub_mode=int(baseline.stub_mode),
        )
        summary_rows.append(row)

        # Activation rates.
        for act in _query_activation(optimized_dir):
            activation_rows.append({"agent": cfg.label, **act})

        print(f"\n--- {cfg.label} ---")
        print(f"  Baseline:  {baseline.n_passed}/{baseline.n_tasks} "
              f"({b_acc*100:.1f}%)  ${baseline.cost_usd:.4f}  "
              f"{baseline.input_tokens:,} tok")
        print(f"  Optimized: {optimized.n_passed}/{optimized.n_tasks} "
              f"({o_acc*100:.1f}%)  ${optimized.cost_usd:.4f}  "
              f"{optimized.input_tokens:,} tok")
        print(f"  Cost Δ: {cost_delta:+.1f}%  Input-tok Δ: {tok_delta:+.1f}%  "
              f"Acc Δ: {acc_delta:+.1f}pp")
        print(f"  McNemar: BB={n_BB} BF={n_BF} FB={n_FB} FF={n_FF}  p={mcnemar_p:.4f}")

    # Write CSVs.
    csv_path = PAPER_RESULTS / "generalization_evals.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        w.writeheader()
        w.writerows(summary_rows)
    print(f"\nWrote {csv_path}")

    act_path = PAPER_RESULTS / "generalization_activation.csv"
    if activation_rows:
        with act_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(activation_rows[0].keys()))
            w.writeheader()
            w.writerows(activation_rows)
        print(f"Wrote {act_path}")

    # Write summary text.
    txt_path = PAPER_RESULTS / "generalization_evals.summary.txt"
    lines = [
        "Generalization evaluation — real agents (Exp 1) + cold agent (Exp 2)",
        "=" * 70,
        f"{'Agent':<20} {'n':>4}  {'Base%':>6}  {'Opt%':>6}  {'AccΔ':>6}  "
        f"{'CostΔ%':>7}  {'TokΔ%':>7}  {'McNemar p':>10}",
        "-" * 70,
    ]
    for r in summary_rows:
        lines.append(
            f"{r['agent']:<20} {r['n']:>4}  {r['baseline_acc_pct']:>6.1f}  "
            f"{r['optimized_acc_pct']:>6.1f}  {r['acc_delta_pp']:>+6.1f}  "
            f"{r['cost_delta_pct']:>+7.1f}  {r['input_tok_delta_pct']:>+7.1f}  "
            f"{r['mcnemar_p']:>10.4f}"
        )
    lines.append("-" * 70)
    if summary_rows[0]["stub_mode"]:
        lines.append("NOTE: STUB MODE — no API key set, cost/token figures are 0.")

    if activation_rows:
        lines += ["", "Rule activation rates (optimized runs):"]
        cur_agent = None
        for a in activation_rows:
            if a["agent"] != cur_agent:
                cur_agent = a["agent"]
                lines.append(f"\n  {cur_agent}:")
            lines.append(
                f"    {a['rule']:<22} {a['plan_kind']:<16} "
                f"n={a['n_fired']:>4}  rate={a['fire_rate_pct']:>5.1f}%"
            )

    txt_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote {txt_path}")


if __name__ == "__main__":
    run_all()
