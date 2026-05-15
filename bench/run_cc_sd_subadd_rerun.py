"""CC+SD sub-additivity re-verification — isolated state, n=30 (§6.7).

Reruns the CC+SD composition experiment under strict per-config isolation.
Measures input-token savings per config and computes the sub-additivity
efficiency = actual_composed_savings / additive_ideal_savings.

Configs:
  baseline  — AGENTC_OPTIMIZE=0 (reference)
  CC-only   — ContextCompress only enabled
  SD-only   — StateDrop only enabled
  CC+SD     — both rules enabled

Outputs:
  bench/paper_results/cc_sd_subadd_rerun.csv
  bench/paper_results/cc_sd_subadd_rerun.summary.txt
"""

from __future__ import annotations

import csv
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
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

AGENT = "bench.agents.multirule_qa"
N_TASKS = 30
PAPER_RESULTS = _REPO / "bench" / "paper_results"
STORAGE_ROOT = Path("/tmp/agentc-cc-sd-subadd-rerun")
OUT_PATH = PAPER_RESULTS / "cc_sd_subadd_rerun.csv"
SUMMARY_PATH = PAPER_RESULTS / "cc_sd_subadd_rerun.summary.txt"

_CSV_COLUMNS = [
    "config",
    "n_pass", "n_total", "acc_pct", "acc_delta_pp", "se_pp",
    "mcnemar_p", "BF", "FB",
    "input_tokens", "output_tokens", "input_token_savings_pct",
]

_CONFIGS: list[tuple[str, list[str]]] = [
    ("CC-only", ["ContextCompress"]),
    ("SD-only", ["StateDrop"]),
    ("CC+SD",   ["ContextCompress", "StateDrop"]),
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


def _aggregate_tokens(storage_dir: Path) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) from traces.db."""
    db = storage_dir / "traces.db"
    if not db.is_file():
        return (0, 0)
    import sqlite3
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) FROM spans"
        ).fetchone()
        return (int(row[0]), int(row[1]))
    finally:
        conn.close()


def _run_agent(
    storage_dir: Path,
    optimize: bool,
    extra_env: Optional[dict[str, str]] = None,
) -> tuple[list[tuple[str, bool]], int, int]:
    """Returns (per_task, input_tokens, output_tokens)."""
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
    per_task = _parse_per_task_pass_fail(proc.stdout)
    in_tok, out_tok = _aggregate_tokens(storage_dir)
    return per_task, in_tok, out_tok


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
        print(f"  → wrote row '{row['config']}' to {OUT_PATH}")

    # Baseline
    print(f"\n{'='*60}")
    print(f"baseline (AGENTC_OPTIMIZE=0), n={N_TASKS}")
    print("="*60)
    baseline_dir = STORAGE_ROOT / "baseline"
    baseline_per, baseline_in_tok, baseline_out_tok = _run_agent(
        baseline_dir, optimize=False
    )
    n_base_pass = sum(1 for _, p in baseline_per if p)
    n_total = len(baseline_per)
    b_acc = 100.0 * n_base_pass / n_total if n_total else 0.0
    se_b = 100.0 * math.sqrt(b_acc / 100.0 * (1 - b_acc / 100.0) / n_total) if n_total else 0.0

    _append_row({
        "config": "baseline",
        "n_pass": n_base_pass, "n_total": n_total,
        "acc_pct": f"{b_acc:.1f}", "acc_delta_pp": "0.0", "se_pp": f"{se_b:.1f}",
        "mcnemar_p": "1.0", "BF": 0, "FB": 0,
        "input_tokens": baseline_in_tok,
        "output_tokens": baseline_out_tok,
        "input_token_savings_pct": "0.0",
    })
    print(f"  baseline: {n_base_pass}/{n_total}  {baseline_in_tok:,} input tokens")

    baseline_map = dict(baseline_per)
    tok_savings: dict[str, float] = {}

    for label, rules_enabled in _CONFIGS:
        print(f"\n{'='*60}")
        print(f"{label}, n={N_TASKS}")
        print("="*60)

        cfg_dir = STORAGE_ROOT / label
        cfg_dir.mkdir(parents=True)
        optimized_dir = cfg_dir / "optimized"
        optimized_dir.mkdir(parents=True)

        rules_off = [r for r in RULES if r not in rules_enabled]
        _disable(rules_off, optimized_dir)

        per_task, in_tok, out_tok = _run_agent(optimized_dir, optimize=True)
        n_pass = sum(1 for _, p in per_task if p)
        opt_map = dict(per_task)

        n_BF = n_FB = 0
        for tid, bp in baseline_per:
            op = opt_map.get(tid, False)
            if bp and not op: n_BF += 1
            elif not bp and op: n_FB += 1

        p_val = mcnemar_exact(n_BF, n_FB)
        acc = 100.0 * n_pass / n_total if n_total else 0.0
        acc_delta = acc - b_acc
        se = 100.0 * math.sqrt(acc / 100.0 * (1 - acc / 100.0) / n_total) if n_total else 0.0

        tok_saved_pct = (
            100.0 * (baseline_in_tok - in_tok) / baseline_in_tok
            if baseline_in_tok > 0 else 0.0
        )
        tok_savings[label] = tok_saved_pct

        print(f"  {n_pass}/{n_total}  BF={n_BF} FB={n_FB} p={p_val:.4f}  "
              f"tok_saved={tok_saved_pct:+.2f}%")

        _append_row({
            "config": label,
            "n_pass": n_pass, "n_total": n_total,
            "acc_pct": f"{acc:.1f}", "acc_delta_pp": f"{acc_delta:.1f}", "se_pp": f"{se:.1f}",
            "mcnemar_p": f"{p_val:.4f}", "BF": n_BF, "FB": n_FB,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "input_token_savings_pct": f"{tok_saved_pct:.2f}",
        })

    cc_tok = tok_savings.get("CC-only", 0.0)
    sd_tok = tok_savings.get("SD-only", 0.0)
    both_tok = tok_savings.get("CC+SD", 0.0)
    additive_ideal = cc_tok + sd_tok
    efficiency = 100.0 * both_tok / additive_ideal if additive_ideal > 0 else 0.0

    summary_lines = [
        f"CC+StateDrop sub-additivity rerun — multirule_qa, n={N_TASKS}, gpt-4o-mini base",
        "-" * 60,
        f"{'config':<12} {'tok_saved_pct':>14}",
        "-" * 60,
        f"{'baseline':<12} {'0.0%':>14}",
        f"{'CC-only':<12} {cc_tok:>13.2f}%",
        f"{'SD-only':<12} {sd_tok:>13.2f}%",
        f"{'CC+SD':<12} {both_tok:>13.2f}%",
        "-" * 60,
        f"Additivity: CC({cc_tok:.2f}%) + SD({sd_tok:.2f}%) = {additive_ideal:.2f}% additive ideal",
        f"CC+SD actual: {both_tok:.2f}% = {efficiency:.1f}% of additive ideal",
        "",
        f"SUB-ADDITIVITY EFFICIENCY: {efficiency:.1f}%  (target: ~65.3%)",
    ]

    summary_text = "\n".join(summary_lines)
    print("\n" + summary_text)
    SUMMARY_PATH.write_text(summary_text + "\n")

    print(f"\nResults: {OUT_PATH}")
    print(f"Summary: {SUMMARY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
