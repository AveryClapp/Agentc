"""Figure 5: Regime transition — CC fire rate and token savings vs. distractor density.

Dual-axis line plot. Left axis: ContextCompress fire rate (%) as distractor
paragraphs are added on top of the base 10 HotpotQA paragraphs. Right axis:
input-token savings (%). A vertical dashed reference line marks the structural
8 KB threshold region (around extras=5–6) where the sharp transition occurs.

Data: bench/paper_results/density_sweep.csv
"""

from pathlib import Path

import csv

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
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

DATA = Path(__file__).resolve().parent.parent / "paper_results" / "density_sweep.csv"
OUT = Path(__file__).resolve().parent / "fig5_density_sweep.pdf"

DARK = "#2c3e50"
ACCENT = "#e74c3c"
GRID = "#9a9a9a"


def _load() -> tuple[list[int], list[float], list[float]]:
    extras, fire_rates, tok_savings = [], [], []
    with DATA.open() as f:
        for row in csv.DictReader(f):
            extras.append(int(row["extras"]))
            fire_rates.append(float(row["cc_fire_rate_pct"]))
            tok_savings.append(float(row["input_token_savings_pct"]))
    return extras, fire_rates, tok_savings


def main() -> None:
    extras, fire_rates, tok_savings = _load()
    total_paras = [10 + e for e in extras]

    fig, ax1 = plt.subplots(figsize=(5.5, 3.2))
    ax2 = ax1.twinx()

    # Light gridlines behind everything
    for y in (20, 40, 60, 80):
        ax1.axhline(y, color=GRID, alpha=0.25, lw=0.5, zorder=0)

    # Vertical threshold annotation — transition happens between extras=4 (14 paras)
    # and extras=6 (16 paras), i.e. total_paras ~15
    ax1.axvline(15, color=GRID, alpha=0.5, lw=0.8, linestyle="--", zorder=0)
    ax1.text(15.2, 82, "8 KB\ngate", fontsize=7, color=GRID,
             va="top", style="italic")

    line1, = ax1.plot(total_paras, fire_rates,
                      color=DARK, marker="o", markersize=4,
                      linewidth=1.4, label="CC fire rate", zorder=3)
    line2, = ax2.plot(total_paras, tok_savings,
                      color=ACCENT, marker="s", markersize=4,
                      linewidth=1.4, linestyle="--", label="Input-token savings", zorder=3)

    ax1.set_xlabel("Total paragraphs per task")
    ax1.set_ylabel("ContextCompress fire rate (%)", color=DARK)
    ax2.set_ylabel("Input-token savings (%)", color=ACCENT)

    ax1.set_ylim(0, 100)
    ax2.set_ylim(0, 50)
    ax1.set_xticks(total_paras)
    ax1.yaxis.set_major_formatter(mticker.FormatStrFormatter("%g%%"))
    ax2.yaxis.set_major_formatter(mticker.FormatStrFormatter("%g%%"))
    ax1.tick_params(axis="y", labelcolor=DARK)
    ax2.tick_params(axis="y", labelcolor=ACCENT)

    ax1.spines["top"].set_visible(False)

    lines = [line1, line2]
    labels = [l.get_label() for l in lines]
    ax1.legend(lines, labels, loc="upper left", frameon=True,
               framealpha=0.9, edgecolor=DARK, fontsize=8)

    fig.tight_layout()
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
