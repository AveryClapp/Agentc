"""Paired McNemar / bootstrap analysis on per-task ablation data.

Reads a ``.per_task.csv`` sidecar produced by ``bench.optimizer_ablation``
and produces, per (agent, config), a paired test of accuracy preservation:

- McNemar exact two-sided p-value on the discordant-pair count.
- 95% bootstrap CI on the accuracy delta (optimized - baseline pass rate).

Usage:

    python -m bench.paired_analysis bench/paper_results/iterative_refiner-statedrop-n50-paired.per_task.csv

Output is a printed table; pass --out PATH to also write a CSV.
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PairedSummary:
    agent_module: str
    config: str
    n: int
    n_baseline_pass: int
    n_optimized_pass: int
    # Paired counts: (baseline=B, optimized=O)
    n_BB: int  # both pass — concordant
    n_FF: int  # both fail — concordant
    n_BF: int  # baseline pass, optimized fail — discordant
    n_FB: int  # baseline fail, optimized pass — discordant
    delta_pp: float  # optimized_rate - baseline_rate, in percentage points
    mcnemar_p: float  # exact binomial two-sided p-value on discordant pairs
    boot_ci_low_pp: float
    boot_ci_high_pp: float


def _load(path: Path) -> dict[tuple[str, str], list[tuple[str, bool, bool]]]:
    grouped: dict[tuple[str, str], list[tuple[str, bool, bool]]] = defaultdict(list)
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            key = (row["agent_module"], row["config"])
            grouped[key].append((
                row["task_id"],
                bool(int(row["baseline_passed"])),
                bool(int(row["optimized_passed"])),
            ))
    return grouped


def _binomial_two_sided_pmf(n: int, k: int, p: float = 0.5) -> float:
    """Two-sided exact binomial p-value: prob of |X - n*p| >= |k - n*p|."""
    if n == 0:
        return 1.0
    target = abs(k - n * p)
    total = 0.0
    for i in range(n + 1):
        if abs(i - n * p) >= target - 1e-12:
            total += math.comb(n, i) * (p**i) * ((1 - p) ** (n - i))
    return min(1.0, total)


def _bootstrap_delta_ci(
    pairs: list[tuple[bool, bool]],
    n_iter: int = 5000,
    rng_seed: int = 0,
) -> tuple[float, float]:
    """95% percentile bootstrap CI for (optimized_rate - baseline_rate) in pp."""
    if not pairs:
        return (0.0, 0.0)
    rng = random.Random(rng_seed)
    n = len(pairs)
    deltas: list[float] = []
    for _ in range(n_iter):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        b_pass = sum(1 for b, _ in sample if b)
        o_pass = sum(1 for _, o in sample if o)
        deltas.append(100.0 * (o_pass - b_pass) / n)
    deltas.sort()
    lo = deltas[int(0.025 * n_iter)]
    hi = deltas[int(0.975 * n_iter) - 1]
    return (lo, hi)


def summarize(
    agent_module: str,
    config: str,
    rows: list[tuple[str, bool, bool]],
) -> PairedSummary:
    n = len(rows)
    n_BB = sum(1 for _, b, o in rows if b and o)
    n_FF = sum(1 for _, b, o in rows if not b and not o)
    n_BF = sum(1 for _, b, o in rows if b and not o)
    n_FB = sum(1 for _, b, o in rows if not b and o)
    n_baseline_pass = n_BB + n_BF
    n_optimized_pass = n_BB + n_FB
    discordant = n_BF + n_FB
    p = _binomial_two_sided_pmf(discordant, n_FB) if discordant > 0 else 1.0
    delta_pp = 100.0 * (n_optimized_pass - n_baseline_pass) / n if n else 0.0
    pairs = [(b, o) for _, b, o in rows]
    lo, hi = _bootstrap_delta_ci(pairs)
    return PairedSummary(
        agent_module=agent_module,
        config=config,
        n=n,
        n_baseline_pass=n_baseline_pass,
        n_optimized_pass=n_optimized_pass,
        n_BB=n_BB,
        n_FF=n_FF,
        n_BF=n_BF,
        n_FB=n_FB,
        delta_pp=delta_pp,
        mcnemar_p=p,
        boot_ci_low_pp=lo,
        boot_ci_high_pp=hi,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.paired_analysis")
    ap.add_argument("per_task_csv", help="path to .per_task.csv sidecar")
    ap.add_argument(
        "--out",
        help="optional output CSV path for the summary table",
    )
    args = ap.parse_args(argv)
    grouped = _load(Path(args.per_task_csv))
    summaries: list[PairedSummary] = []
    for (agent, config), rows in sorted(grouped.items()):
        summaries.append(summarize(agent, config, rows))

    print(
        f"{'config':30s}  "
        f"{'n':>3s}  "
        f"{'base':>4s}  {'opt':>4s}  "
        f"{'BB':>4s}  {'BF':>4s}  {'FB':>4s}  {'FF':>4s}  "
        f"{'Δpp':>7s}  "
        f"{'95% CI (pp)':>16s}  "
        f"{'McNemar p':>9s}"
    )
    for s in summaries:
        print(
            f"{s.config:30s}  "
            f"{s.n:>3d}  "
            f"{s.n_baseline_pass:>4d}  {s.n_optimized_pass:>4d}  "
            f"{s.n_BB:>4d}  {s.n_BF:>4d}  {s.n_FB:>4d}  {s.n_FF:>4d}  "
            f"{s.delta_pp:>+7.2f}  "
            f"[{s.boot_ci_low_pp:>+5.2f},{s.boot_ci_high_pp:>+5.2f}]  "
            f"{s.mcnemar_p:>9.4f}"
        )

    if args.out:
        out = Path(args.out)
        with out.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "agent_module", "config", "n",
                "n_baseline_pass", "n_optimized_pass",
                "n_BB", "n_BF", "n_FB", "n_FF",
                "delta_pp", "boot_ci_low_pp", "boot_ci_high_pp",
                "mcnemar_p",
            ])
            for s in summaries:
                w.writerow([
                    s.agent_module, s.config, s.n,
                    s.n_baseline_pass, s.n_optimized_pass,
                    s.n_BB, s.n_BF, s.n_FB, s.n_FF,
                    f"{s.delta_pp:.3f}",
                    f"{s.boot_ci_low_pp:.3f}",
                    f"{s.boot_ci_high_pp:.3f}",
                    f"{s.mcnemar_p:.6f}",
                ])
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
