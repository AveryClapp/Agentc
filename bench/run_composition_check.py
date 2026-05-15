"""Composition planner bug check for autogen_bridge (§6.7).

Runs autogen_bridge n=20 under two planner modes:
  - AGENTC_COMPOSE=0  (first-match): OutputBudget fires first, nullifies CC
  - AGENTC_COMPOSE=1  (composition, default): both rules apply correctly

Expects the composition planner to show higher savings than first-match.
Results written to bench/paper_results/autogen_bridge_composition_check.csv.
"""

from __future__ import annotations

import csv
import os
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "python"))

from bench.optimizer_ablation import AblationRow, _CSV_COLUMNS, _run_config, write_header
from bench.optimizer_bench import run_bench, render_result

PAPER_RESULTS = _REPO / "bench" / "paper_results"
STORAGE_ROOT = Path("/tmp/agentc-composition-check")
OUT_PATH = PAPER_RESULTS / "autogen_bridge_composition_check.csv"
AGENT = "bench.agents.autogen_bridge"
N_TASKS = 20

_TOGETHER_BASE_URL = "https://api.together.xyz/v1"
_TOGETHER_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"


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


def main() -> int:
    env = _load_env(_REPO)
    together_key = env.get("TOGETHER_API_KEY", "")
    if not together_key:
        print("ERROR: TOGETHER_API_KEY not set", file=sys.stderr)
        return 1

    base_extra: dict[str, str] = {
        "BENCH_OPENAI_BASE_URL": _TOGETHER_BASE_URL,
        "BENCH_BASELINE_MODEL": _TOGETHER_MODEL,
        "TOGETHER_API_KEY": together_key,
        "BENCH_MAX_TASKS": str(N_TASKS),
    }

    PAPER_RESULTS.mkdir(parents=True, exist_ok=True)
    if STORAGE_ROOT.exists():
        shutil.rmtree(STORAGE_ROOT)
    STORAGE_ROOT.mkdir(parents=True)

    # Write CSV header manually (two custom rows, not AblationRow format)
    with OUT_PATH.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "agent_module", "planner_mode", "config",
            "cost_savings_pct", "input_token_savings_pct",
            "accuracy_delta_pp", "baseline_cost_usd", "optimized_cost_usd",
            "n_tasks", "n_passed_optimized",
        ])

    for compose_val, label in [("0", "first-match"), ("1", "composition")]:
        extra = dict(base_extra)
        extra["AGENTC_COMPOSE"] = compose_val

        print(f"\n{'='*60}")
        print(f"AGENTC_COMPOSE={compose_val} ({label}), n={N_TASKS}")
        print(f"{'='*60}")

        storage_dir = STORAGE_ROOT / label
        result = run_bench(
            agent_module=AGENT,
            storage_root=storage_dir,
            extra_env=extra,
        )
        print(render_result(result))

        b = result.baseline
        o = result.optimized
        if b.total_cost_usd > 0:
            cost_savings_pct = 100.0 * (b.total_cost_usd - o.total_cost_usd) / b.total_cost_usd
        else:
            cost_savings_pct = 0.0
        if b.total_input_tokens > 0:
            tok_savings_pct = 100.0 * (b.total_input_tokens - o.total_input_tokens) / b.total_input_tokens
        else:
            tok_savings_pct = 0.0
        acc_delta = (o.n_passed - b.n_passed) if b.n_tasks > 0 else 0

        with OUT_PATH.open("a", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                AGENT, label, "all-on",
                f"{cost_savings_pct:.3f}", f"{tok_savings_pct:.3f}",
                f"{acc_delta:.3f}", f"{b.total_cost_usd:.6f}", f"{o.total_cost_usd:.6f}",
                o.n_tasks, o.n_passed,
            ])
        print(f"  → wrote {label} row to {OUT_PATH}")

    print(f"\nComposition check done. Results: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
