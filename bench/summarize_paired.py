"""Quick summary printer for the paired ablation results.

Reads the aggregate CSV and the per-task sidecar, runs paired_analysis,
and prints a single table per agent suitable for pasting into the
paper draft.

Usage:
    python -m bench.summarize_paired bench/paper_results/iterative_refiner-statedrop-n50-paired.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from bench.paired_analysis import _load, summarize


def _load_aggregate(path: Path) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            out[(row["agent_module"], row["config"])] = row
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.summarize_paired")
    ap.add_argument("aggregate_csv", help="path to ablation aggregate CSV (.csv)")
    args = ap.parse_args(argv)

    agg_path = Path(args.aggregate_csv)
    per_task_path = agg_path.with_suffix(".per_task.csv")
    if not agg_path.is_file():
        print(f"missing: {agg_path}", file=sys.stderr)
        return 1
    if not per_task_path.is_file():
        print(f"missing: {per_task_path}", file=sys.stderr)
        return 1

    agg = _load_aggregate(agg_path)
    grouped = _load(per_task_path)

    # Stable config ordering matches optimizer_ablation.py.
    rule_order = ["CacheHit", "ContextCompress", "ParallelBranch",
                  "ModelDowngrade", "StateDrop"]
    config_order = ["all-on"]
    config_order += [f"{r}-off" for r in rule_order]
    config_order += [f"{r}-only" for r in rule_order]

    print(
        f"{'config':25s}  "
        f"{'cost Δ%':>8s}  "
        f"{'in-tok Δ%':>10s}  "
        f"{'acc Δ pp':>9s}  "
        f"{'McNemar p':>10s}  "
        f"{'95% CI (pp)':>16s}  "
        f"{'BB':>3s} {'BF':>3s} {'FB':>3s} {'FF':>3s}"
    )
    print("-" * 110)

    for (agent, config), agg_row in sorted(
        agg.items(),
        key=lambda kv: (kv[0][0], config_order.index(kv[0][1]) if kv[0][1] in config_order else 99),
    ):
        per_task_rows = grouped.get((agent, config), [])
        if per_task_rows:
            s = summarize(agent, config, per_task_rows)
            mcn = f"{s.mcnemar_p:.4f}"
            ci = f"[{s.boot_ci_low_pp:>+5.2f},{s.boot_ci_high_pp:>+5.2f}]"
            counts = f"{s.n_BB:>3d} {s.n_BF:>3d} {s.n_FB:>3d} {s.n_FF:>3d}"
        else:
            mcn = "—"
            ci = "—"
            counts = "—"
        print(
            f"{config:25s}  "
            f"{float(agg_row['cost_savings_pct']):>+7.2f}%  "
            f"{float(agg_row['input_token_savings_pct']):>+9.2f}%  "
            f"{float(agg_row['accuracy_delta_pp']):>+8.2f}  "
            f"{mcn:>10s}  "
            f"{ci:>16s}  "
            f"{counts}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
