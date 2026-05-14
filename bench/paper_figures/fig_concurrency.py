"""Figure: end-to-end latency and throughput under concurrent load.

Reads bench/paper_results/concurrency_bench.csv and
bench/paper_results/concurrency_bench_summary.csv and produces a
3-panel figure:

  Panel A  Latency CDF — per-task latency distributions for baseline
           and optimized at each concurrency level.

  Panel B  Throughput and token savings vs. concurrency — shows QPS
           scales sub-linearly (API-bound) but optimization advantage
           holds across levels.

  Panel C  Latency slowdown ratio — optimized/baseline p50 latency
           at each concurrency level (should stay ≤ 1.0, ideally < 1.0
           once token savings kick in).

Usage:
    python bench/paper_figures/fig_concurrency.py
    python bench/paper_figures/fig_concurrency.py --out bench/paper_figures/fig_concurrency.pdf
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "python"))

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False

PAPER_RESULTS = _REPO / "bench" / "paper_results"
DETAIL_CSV = PAPER_RESULTS / "concurrency_bench.csv"
SUMMARY_CSV = PAPER_RESULTS / "concurrency_bench_summary.csv"

_COLORS = {
    "baseline": "#4878CF",
    "optimized": "#D65F5F",
}
_MARKERS = {1: "o", 8: "s", 32: "^"}
_LINESTYLES = {1: "-", 8: "--", 32: ":"}


def _load_detail() -> dict[tuple[int, str], list[float]]:
    """Returns {(concurrency, condition): [latency_s, ...]}"""
    data: dict[tuple[int, str], list[float]] = defaultdict(list)
    with DETAIL_CSV.open() as f:
        for row in csv.DictReader(f):
            key = (int(row["concurrency"]), row["condition"])
            data[key].append(float(row["latency_s"]))
    return data


def _load_summary() -> list[dict]:
    rows = []
    with SUMMARY_CSV.open() as f:
        for row in csv.DictReader(f):
            rows.append({
                "concurrency": int(row["concurrency"]),
                "condition": row["condition"],
                "qps": float(row["qps"]),
                "p50_latency_s": float(row["p50_latency_s"]),
                "p95_latency_s": float(row["p95_latency_s"]),
                "p99_latency_s": float(row["p99_latency_s"]),
                "mean_prompt_tokens": float(row["mean_prompt_tokens"]),
                "token_savings_pct": float(row["token_savings_pct"]),
                "stub_mode": int(row.get("stub_mode", "0")),
            })
    return rows


def plot(out_path: Path) -> None:
    if not _HAS_MPL:
        print("matplotlib not available — skipping figure generation")
        return
    if not DETAIL_CSV.exists() or not SUMMARY_CSV.exists():
        print(f"Missing CSVs under {PAPER_RESULTS} — run run_concurrency_bench.py first")
        return

    detail = _load_detail()
    summary = _load_summary()

    concurrency_levels = sorted({r["concurrency"] for r in summary})
    stub_mode = any(r["stub_mode"] for r in summary)

    fig, axes = plt.subplots(1, 3, figsize=(13, 4))
    fig.subplots_adjust(wspace=0.38)

    # ------------------------------------------------------------------
    # Panel A: Latency CDF
    # ------------------------------------------------------------------
    ax = axes[0]
    for cond in ["baseline", "optimized"]:
        for conc in concurrency_levels:
            lats = sorted(detail.get((conc, cond), []))
            if not lats:
                continue
            n = len(lats)
            cdf = [(i + 1) / n for i in range(n)]
            ax.plot(
                lats, cdf,
                color=_COLORS[cond],
                linestyle=_LINESTYLES.get(conc, "-"),
                linewidth=1.2,
                alpha=0.85,
                label=f"{cond} {conc}×",
            )

    ax.set_xlabel("Task latency (s)")
    ax.set_ylabel("CDF")
    ax.set_title("A  Latency CDF")
    ax.set_ylim(0, 1.05)
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=7, ncol=2)
    if stub_mode:
        ax.text(0.5, 0.5, "STUB MODE", transform=ax.transAxes,
                ha="center", va="center", color="red", alpha=0.4, fontsize=14)

    # ------------------------------------------------------------------
    # Panel B: QPS + token savings vs. concurrency
    # ------------------------------------------------------------------
    ax = axes[1]
    ax2 = ax.twinx()

    for cond in ["baseline", "optimized"]:
        xs = []
        qps_vals = []
        tok_vals = []
        for r in sorted(summary, key=lambda r: r["concurrency"]):
            if r["condition"] != cond:
                continue
            xs.append(r["concurrency"])
            qps_vals.append(r["qps"])
            tok_vals.append(r["token_savings_pct"])
        if not xs:
            continue
        ax.plot(
            xs, qps_vals,
            color=_COLORS[cond],
            marker="o",
            linewidth=1.5,
            label=f"{cond} QPS",
        )
        if cond == "optimized":
            ax2.plot(
                xs, tok_vals,
                color="#4DAF4A",
                marker="D",
                linestyle="--",
                linewidth=1.2,
                label="token savings %",
            )

    ax.set_xlabel("Concurrency")
    ax.set_ylabel("QPS")
    ax2.set_ylabel("Token savings (%)", color="#4DAF4A")
    ax2.tick_params(axis="y", labelcolor="#4DAF4A")
    ax.set_title("B  Throughput & savings")
    ax.set_xticks(concurrency_levels)
    ax.grid(True, alpha=0.25)
    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, fontsize=7)

    # ------------------------------------------------------------------
    # Panel C: Latency slowdown ratio (optimized p50 / baseline p50)
    # ------------------------------------------------------------------
    ax = axes[2]

    # Build dicts for easy lookup
    base_p50 = {r["concurrency"]: r["p50_latency_s"]
                for r in summary if r["condition"] == "baseline"}
    opt_p50  = {r["concurrency"]: r["p50_latency_s"]
                for r in summary if r["condition"] == "optimized"}
    base_p99 = {r["concurrency"]: r["p99_latency_s"]
                for r in summary if r["condition"] == "baseline"}
    opt_p99  = {r["concurrency"]: r["p99_latency_s"]
                for r in summary if r["condition"] == "optimized"}

    xs = sorted(set(base_p50) & set(opt_p50))
    ratios_p50 = [opt_p50[c] / base_p50[c] if base_p50[c] > 0 else 1.0 for c in xs]
    ratios_p99 = [opt_p99.get(c, 0) / base_p99.get(c, 1) if base_p99.get(c, 0) > 0 else 1.0
                  for c in xs]

    ax.plot(xs, ratios_p50, color="#7B2D8B", marker="o", linewidth=1.5, label="p50 ratio")
    ax.plot(xs, ratios_p99, color="#7B2D8B", marker="^", linestyle="--",
            linewidth=1.2, label="p99 ratio")
    ax.axhline(1.0, color="gray", linewidth=0.8, linestyle=":")
    ax.axhline(0.9, color="green", linewidth=0.6, linestyle=":", alpha=0.5)

    ax.set_xlabel("Concurrency")
    ax.set_ylabel("Optimized / Baseline latency")
    ax.set_title("C  Latency ratio (optimized÷baseline)")
    ax.set_xticks(xs)
    ax.set_ylim(0, 1.5)
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8)

    if stub_mode:
        fig.text(0.5, 0.01, "NOTE: stub mode — latency numbers not meaningful",
                 ha="center", fontsize=8, color="red")

    fig.savefig(str(out_path), bbox_inches="tight", dpi=150)
    print(f"Saved {out_path}")
    plt.close(fig)


def _print_summary_table() -> None:
    if not SUMMARY_CSV.exists():
        print("No summary CSV yet.")
        return
    summary = _load_summary()
    print(f"\n{'conc':>5}  {'condition':>12}  {'QPS':>7}  {'p50':>7}  "
          f"{'p95':>7}  {'p99':>7}  {'tokens':>8}  {'savings':>8}")
    print("-" * 72)
    for r in sorted(summary, key=lambda r: (r["concurrency"], r["condition"])):
        print(
            f"{r['concurrency']:>5}  {r['condition']:>12}  "
            f"{r['qps']:>7.2f}  {r['p50_latency_s']:>7.3f}  "
            f"{r['p95_latency_s']:>7.3f}  {r['p99_latency_s']:>7.3f}  "
            f"{r['mean_prompt_tokens']:>8.0f}  {r['token_savings_pct']:>7.1f}%"
        )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "fig_concurrency.pdf",
    )
    args = ap.parse_args(argv)
    _print_summary_table()
    plot(args.out)


if __name__ == "__main__":
    main()
