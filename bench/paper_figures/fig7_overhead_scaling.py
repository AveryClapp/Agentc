"""Figure 7: Planner overhead scaling vs prompt size.

Line chart with p50, p95, p99 percentile lines across five prompt-size
buckets: 4KB, 8KB, 16KB, 32KB, 64KB.

Data from bench/paper_results/overhead_scaling.csv:
  size_label,bytes,n_messages,mean_ms,p50_ms,p95_ms,p99_ms
  4KB,4096,4,0.384,0.333,0.619,0.721
  8KB,8192,8,0.560,0.368,0.704,5.273  ← cold-start SQLite load visible in p99
  16KB,16384,12,0.460,0.457,0.590,0.597
  32KB,32768,16,0.595,0.526,0.870,1.685
  64KB,65536,24,0.769,0.710,1.105,1.263

Key message: overhead is flat/sub-linear; stays well below 2 ms even at
64 KB. The 8 KB p99 spike reflects cold-start SQLite cost-model loading
(not prompt-size-dependent).
"""

from pathlib import Path
import csv

import matplotlib.pyplot as plt
import numpy as np

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 9,
    "axes.labelsize": 9,
    "axes.titlesize": 10,
    "legend.fontsize": 8,
    "mathtext.fontset": "cm",
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "axes.linewidth": 0.7,
})

OUT = Path(__file__).resolve().parent / "fig7_overhead_scaling.pdf"
DATA = Path(__file__).resolve().parents[1] / "paper_results" / "overhead_scaling.csv"

DARK  = "#2c3e50"
MED   = "#7f8c8d"
LIGHT = "#bcc6cf"
EDGE  = "#1a242f"
GRID  = "#9a9a9a"


def main() -> None:
    rows = []
    with open(DATA) as f:
        for row in csv.DictReader(f):
            rows.append({
                "label": row["size_label"],
                "p50": float(row["p50_ms"]),
                "p95": float(row["p95_ms"]),
                "p99": float(row["p99_ms"]),
            })

    labels = [r["label"] for r in rows]
    p50 = [r["p50"] for r in rows]
    p95 = [r["p95"] for r in rows]
    p99 = [r["p99"] for r in rows]
    x = np.arange(len(labels))

    fig, ax = plt.subplots(figsize=(5.5, 3.6))

    for y_grid in (1, 2, 3, 4, 5):
        ax.axhline(y_grid, color=GRID, alpha=0.3, lw=0.5, zorder=0)

    # 2 ms reference line.
    ax.axhline(2.0, color="firebrick", alpha=0.55, lw=0.8, ls="--", zorder=1,
               label="2 ms reference")

    ax.plot(x, p50, "o-", color=DARK,  lw=1.4, ms=5, label="p50",  zorder=3)
    ax.plot(x, p95, "s--", color=MED,  lw=1.2, ms=4, label="p95",  zorder=3)
    ax.plot(x, p99, "^:",  color=LIGHT, lw=1.2, ms=4, label="p99",  zorder=3,
            markeredgecolor=EDGE, markeredgewidth=0.5)

    # Annotate the 8KB p99 cold-start spike.
    spike_idx = 1  # 8KB
    ax.annotate(
        "cold-start\nSQLite load",
        xy=(x[spike_idx], p99[spike_idx]),
        xytext=(x[spike_idx] + 0.5, p99[spike_idx] - 0.6),
        ha="left", va="top",
        fontsize=7.5, color=EDGE, style="italic",
        arrowprops=dict(arrowstyle="-", color=EDGE, lw=0.6),
    )

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_xlabel("Prompt size")
    ax.set_ylabel("Planner overhead (ms)")
    ax.set_ylim(0, 6.2)
    ax.set_yticks([0, 1, 2, 3, 4, 5, 6])
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    ax.legend(loc="upper left", fontsize=8, frameon=True,
              framealpha=0.9, edgecolor=EDGE)

    fig.tight_layout()
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
