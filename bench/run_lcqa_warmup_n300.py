"""Section 6.1 stronger-n: long_context_qa, N=300, W=30 warmup, gpt-4o-mini.

11-config ablation (all-on, 5x-off, 5x-only) under warmup-corrected harness.
Drops binomial SE on accuracy from ±5.0pp (n=100) to ±2.9pp (n=300) and
eliminates most baseline-drift artifacts. CC is stateless so warmup only
ensures cost_model.db is populated; no methodology subtlety.

Builds bench/fixtures/long_context_qa_n300.json on demand from
bench/fixtures/hotpot_distractor.json (500 tasks available).

Cost estimate: ~$3-6. Ceiling: $25 hard stop ($40 surface-and-abort).

Outputs:
  bench/paper_results/long_context_qa_warmup_n300.csv
  bench/paper_results/long_context_qa_warmup_n300_warmup_stats.csv
  bench/fixtures/long_context_qa_n300.json  (built on demand)
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "python"))

from bench.optimizer_bench import _find_agentc_binary, _aggregate_from_db, _parse_per_task_pass_fail
from bench.optimizer_ablation import _disable

AGENT = "bench.agents.long_context_qa"
W_TASKS = 30
N_TASKS = 300
COST_CEILING_USD = 25.0
ABORT_CEILING_USD = 40.0
PAPER_RESULTS = _REPO / "bench" / "paper_results"
STORAGE_ROOT = Path("/tmp/agentc-lcqa-warmup-n300")
FIXTURE_N300 = _REPO / "bench" / "fixtures" / "long_context_qa_n300.json"
FIXTURE_SRC = _REPO / "bench" / "fixtures" / "hotpot_distractor.json"
OUT_PATH = PAPER_RESULTS / "long_context_qa_warmup_n300.csv"
WARMUP_STATS_PATH = PAPER_RESULTS / "long_context_qa_warmup_n300_warmup_stats.csv"

_ABLATION_RULES = ["CacheHit", "ContextCompress", "ParallelBranch", "ModelDowngrade", "StateDrop"]
_ALL_RULES = [
    "CacheHit", "ContextCompress", "ParallelBranch", "ModelDowngrade", "StateDrop",
    "StructuredTruncation", "OutputBudget", "PromptDedup", "DeadOutputTruncation",
]
_CONFIGS: list[str] = (
    ["all-on"]
    + [f"{r}-off" for r in _ABLATION_RULES]
    + [f"{r}-only" for r in _ABLATION_RULES]
)
_CSV_COLUMNS = [
    "config", "n_pass", "n_total", "acc_pct", "acc_delta_pp", "se_pp",
    "mcnemar_p", "BF", "FB",
    "baseline_cost_mUSD", "optimized_cost_mUSD", "cost_savings_mUSD",
    "input_tokens_baseline", "input_tokens_optimized", "input_token_savings_pct",
    "cc_fire_count",
]
_WARMUP_COLS = [
    "config", "call_site_id", "n_observations",
    "n_call_sites_in_config", "mean_obs_in_config",
    "warmup_tasks", "measurement_tasks", "overlap_tasks", "overlap_fraction",
]


def _build_fixture_n300() -> None:
    if FIXTURE_N300.is_file():
        tasks = json.loads(FIXTURE_N300.read_text())
        if len(tasks) >= N_TASKS:
            print(f"  fixture: {FIXTURE_N300} ({len(tasks)} tasks, reusing)")
            return
    if not FIXTURE_SRC.is_file():
        raise RuntimeError(f"Source fixture missing: {FIXTURE_SRC}")
    src = json.loads(FIXTURE_SRC.read_text())
    pool: list = []
    seen: set[str] = set()
    for task in src:
        for para in (task.get("meta") or {}).get("paragraphs", []):
            if para.get("supporting"):
                continue
            title = para.get("title", "")
            if title and title not in seen:
                seen.add(title)
                pool.append(para)
    rng = random.Random(42)
    extras = 10
    out: list = []
    for task in src[:N_TASKS]:
        meta = dict(task.get("meta") or {})
        paragraphs = list(meta.get("paragraphs", []))
        existing = {p.get("title", "") for p in paragraphs}
        added: list = []
        attempts = 0
        while len(added) < extras and attempts < 200:
            cand = rng.choice(pool)
            t = cand.get("title", "")
            if t and t not in existing:
                added.append({"title": t, "sentences": cand.get("sentences", []), "supporting": False})
                existing.add(t)
            attempts += 1
        new_paras = paragraphs + added
        rng.shuffle(new_paras)
        new_task = dict(task)
        new_task["meta"] = {**meta, "paragraphs": new_paras}
        out.append(new_task)
    FIXTURE_N300.write_text(json.dumps(out, indent=2) + "\n")
    sizes = [sum(len(p["title"]) + sum(len(s) for s in p.get("sentences", [])) for p in t["meta"]["paragraphs"]) for t in out]
    over_8k = sum(1 for s in sizes if s > 8192)
    print(f"  fixture: built {len(out)} tasks, {over_8k}/{len(out)} above 8 KB gate → {FIXTURE_N300}")


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


def _run_phase(
    storage_dir: Path,
    optimize: bool,
    n_tasks: int,
    extra_env: Optional[dict[str, str]] = None,
) -> tuple[list[tuple[str, bool]], float, int]:
    storage_dir.mkdir(parents=True, exist_ok=True)
    env = _load_env()
    env["AGENTC_OPTIMIZE"] = "1" if optimize else "0"
    env["BENCH_MAX_TASKS"] = str(n_tasks)
    env["BENCH_TASK_OFFSET"] = "0"
    env["PYTHONPATH"] = str(_REPO / "python")
    env["AGENTC_COMPOSE"] = "1"
    env["BENCH_FIXTURE_OVERRIDE"] = str(FIXTURE_N300)
    if extra_env:
        env.update(extra_env)
    agentc_bin = _find_agentc_binary()
    py = str(_REPO / ".venv" / "bin" / "python")
    cmd = [agentc_bin, "record", "--storage-path", str(storage_dir), "--", py, "-m", AGENT]
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


def _log_warmup_state(storage_dir: Path) -> list[tuple[str, int]]:
    db = storage_dir / "cost_model.db"
    if not db.is_file():
        print("  warmup: cost_model.db not found")
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT call_site_id, n_observations FROM call_site_profile").fetchall()
    finally:
        conn.close()
    if not rows:
        print("  warmup completed: 0 call sites (cold)")
        return []
    mean_obs = sum(n for _, n in rows) / len(rows)
    print(f"  warmup completed: {len(rows)} call sites, mean={mean_obs:.1f} obs each")
    for sid, n in rows:
        print(f"    {sid}: n_obs={n}")
    return list(rows)


def _query_cc_fires(storage_dir: Path) -> int:
    db = storage_dir / "optimizer_audit.db"
    if not db.is_file():
        return 0
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM plan_audit WHERE rule='ContextCompress' AND plan_kind='rewritten'"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def main() -> int:
    PAPER_RESULTS.mkdir(parents=True, exist_ok=True)

    print("\n=== Building/verifying n=300 fixture ===")
    _build_fixture_n300()

    if STORAGE_ROOT.exists():
        shutil.rmtree(STORAGE_ROOT)
    STORAGE_ROOT.mkdir(parents=True)

    with OUT_PATH.open("w", newline="") as f:
        csv.writer(f).writerow(_CSV_COLUMNS)
    with WARMUP_STATS_PATH.open("w", newline="") as f:
        csv.writer(f).writerow(_WARMUP_COLS)

    cumulative_cost_usd = 0.0

    def _check_ceiling(label: str, cost: float) -> None:
        nonlocal cumulative_cost_usd
        cumulative_cost_usd += cost
        print(f"  cumulative=${cumulative_cost_usd:.4f} (warn=${COST_CEILING_USD} abort=${ABORT_CEILING_USD})")
        if cumulative_cost_usd > ABORT_CEILING_USD:
            raise RuntimeError(f"ABORT CEILING at '{label}': ${cumulative_cost_usd:.4f}")
        if cumulative_cost_usd > COST_CEILING_USD:
            print(f"WARNING: cost ceiling ${COST_CEILING_USD} exceeded at '{label}' -- continuing to abort ceiling")

    print(f"\n{'='*60}\nbaseline (AGENTC_OPTIMIZE=0, N={N_TASKS})\n{'='*60}")
    baseline_dir = STORAGE_ROOT / "baseline"
    baseline_per, baseline_cost, baseline_tokens = _run_phase(baseline_dir, optimize=False, n_tasks=N_TASKS)
    _check_ceiling("baseline", baseline_cost)
    n_base_pass = sum(1 for _, p in baseline_per if p)
    b_acc = 100.0 * n_base_pass / N_TASKS
    print(f"  baseline: {n_base_pass}/{N_TASKS}  ${baseline_cost*1000:.4f} mUSD  {baseline_tokens:,} tok")

    def _append_row(row: dict) -> None:
        with OUT_PATH.open("a", newline="") as f:
            csv.writer(f).writerow([row[c] for c in _CSV_COLUMNS])
        print(f"  -> wrote '{row['config']}' to {OUT_PATH.name}")

    def _append_warmup(config: str, sites: list[tuple[str, int]]) -> None:
        overlap_str = f"{W_TASKS}/{N_TASKS}={100.0*W_TASKS/N_TASKS:.1f}%"
        n = len(sites)
        mean = sum(obs for _, obs in sites) / n if n else 0.0
        with WARMUP_STATS_PATH.open("a", newline="") as f:
            w = csv.writer(f)
            for sid, obs in sites:
                w.writerow([config, sid, obs, n, f"{mean:.1f}", W_TASKS, N_TASKS, W_TASKS, overlap_str])

    for config in _CONFIGS:
        print(f"\n{'='*60}\n{config}  W={W_TASKS}  N={N_TASKS}\n{'='*60}")
        rules_off = _rules_off(config)
        opt_dir = STORAGE_ROOT / config / "optimized"
        opt_dir.mkdir(parents=True)
        _disable(rules_off, opt_dir)

        print(f"  [warmup] tasks 0..{W_TASKS-1}")
        _run_phase(opt_dir, optimize=True, n_tasks=W_TASKS)
        warmup_cost, _, _ = _aggregate_from_db(opt_dir / "traces.db")
        _check_ceiling(f"{config} warmup", warmup_cost)
        sites = _log_warmup_state(opt_dir)
        _append_warmup(config, sites)

        if sites:
            mean_obs = sum(obs for _, obs in sites) / len(sites)
            if mean_obs < 3.0:
                print(f"  WARNING: mean_obs={mean_obs:.1f} < 3.0, re-running warmup with W=50")
                _reset_between_phases(opt_dir)
                _run_phase(opt_dir, optimize=True, n_tasks=50)
                warmup_cost50, _, _ = _aggregate_from_db(opt_dir / "traces.db")
                _check_ceiling(f"{config} warmup-w50", warmup_cost50)
                sites = _log_warmup_state(opt_dir)
                _append_warmup(config + "-w50", sites)

        _reset_between_phases(opt_dir)

        print(f"  [measure] tasks 0..{N_TASKS-1}")
        per_task, opt_cost, opt_tokens = _run_phase(opt_dir, optimize=True, n_tasks=N_TASKS)
        _check_ceiling(f"{config} measure", opt_cost)

        n_pass = sum(1 for _, p in per_task if p)
        opt_map = dict(per_task)
        n_BF = n_FB = 0
        for tid, bp in baseline_per:
            op = opt_map.get(tid, False)
            if bp and not op: n_BF += 1
            elif not bp and op: n_FB += 1

        p_val = mcnemar_exact(n_BF, n_FB)
        acc = 100.0 * n_pass / N_TASKS
        se = 100.0 * math.sqrt(acc / 100.0 * (1 - acc / 100.0) / N_TASKS)
        cost_saved = (baseline_cost - opt_cost) * 1000.0
        tok_saved_pct = 100.0 * (baseline_tokens - opt_tokens) / baseline_tokens if baseline_tokens > 0 else 0.0
        cc_fires = _query_cc_fires(opt_dir)

        print(f"  {n_pass}/{N_TASKS}  BF={n_BF} FB={n_FB} p={p_val:.4f}  "
              f"cost_saved={cost_saved:+.4f} mUSD  tok_saved={tok_saved_pct:+.2f}%  CC_fires={cc_fires}")
        _append_row({
            "config": config, "n_pass": n_pass, "n_total": N_TASKS,
            "acc_pct": f"{acc:.1f}", "acc_delta_pp": f"{acc - b_acc:.1f}", "se_pp": f"{se:.1f}",
            "mcnemar_p": f"{p_val:.4f}", "BF": n_BF, "FB": n_FB,
            "baseline_cost_mUSD": f"{baseline_cost*1000:.4f}",
            "optimized_cost_mUSD": f"{opt_cost*1000:.4f}",
            "cost_savings_mUSD": f"{cost_saved:.4f}",
            "input_tokens_baseline": baseline_tokens,
            "input_tokens_optimized": opt_tokens,
            "input_token_savings_pct": f"{tok_saved_pct:.2f}",
            "cc_fire_count": cc_fires,
        })

    print(f"\nDone. Total cost: ${cumulative_cost_usd:.4f} USD")
    print(f"Results: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
