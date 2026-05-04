"""Single-run baseline driver — reports cost / tokens / accuracy as CSV.

Used for oracle-style agents (hotpot_oracle) where the manual filter
already does the work the optimizer would do, so the rule sweep adds
no information. We just want one number to compare against.

Usage:
    python -m bench.run_oracle_baseline bench.agents.hotpot_oracle \
        --storage-root /tmp/agentc-oracle \
        --out bench/results/oracle.csv
"""

from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

from bench.optimizer_bench import run_bench


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m bench.run_oracle_baseline")
    p.add_argument("agent_module")
    p.add_argument("--storage-root", default="/tmp/agentc-oracle")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    root = Path(args.storage_root) / args.agent_module.replace(".", "_")
    if root.exists():
        shutil.rmtree(root)

    result = run_bench(agent_module=args.agent_module, storage_root=root)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "agent_module",
            "n_tasks",
            "n_passed_baseline",
            "n_passed_optimized",
            "baseline_cost_usd",
            "optimized_cost_usd",
            "baseline_input_tokens",
            "optimized_input_tokens",
            "baseline_wall_clock_s",
            "optimized_wall_clock_s",
        ])
        w.writerow([
            args.agent_module,
            result.baseline.n_tasks,
            result.baseline.n_passed,
            result.optimized.n_passed,
            f"{result.baseline.total_cost_usd:.6f}",
            f"{result.optimized.total_cost_usd:.6f}",
            result.baseline.total_input_tokens,
            result.optimized.total_input_tokens,
            f"{result.baseline.wall_clock_s:.3f}",
            f"{result.optimized.wall_clock_s:.3f}",
        ])

    print(f"\nWrote {args.out}")
    print(f"  cost: ${result.baseline.total_cost_usd:.4f} (baseline) / "
          f"${result.optimized.total_cost_usd:.4f} (optimized)")
    print(f"  pass: {result.baseline.n_passed}/{result.baseline.n_tasks} (baseline) / "
          f"{result.optimized.n_passed}/{result.optimized.n_tasks} (optimized)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
