"""Figure 3: Headline savings (double-column width).

Grouped bar chart with three rule families. Two bars per group: cost savings
(solid darker) and input-token savings (hatched). Annotates the
ModelDowngrade input-token bar as a structural zero ("price-ratio rule"), and
the StateDrop group as output-dominated. Light horizontal gridlines at 10,
20, 30, 40 help readers estimate values; the structural-zero input-tok bar is
rendered as a thin sliver (height=0.3) so it remains visible.
"""

from pathlib import Path

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

OUT = Path(__file__).resolve().parent / "fig3_headline_savings.pdf"

DARK = "#2c3e50"
LIGHT = "#bcc6cf"
EDGE = "#1a242f"
GRID = "#9a9a9a"


def main() -> None:
    rules = [
        "ContextCompress\n(n=300)",
        "ModelDowngrade\n(n=127)",
        "StateDrop\n(n=50, within-run)",
    ]
    cost = [33.9, 11.4, 6.1]
    input_tok = [34.0, 0.0, 10.8]

    # Render the structural-zero bar as a thin visible sliver instead of
    # a height-zero bar that disappears entirely.
    input_tok_render = [v if v > 0.0 else 0.3 for v in input_tok]

    x = np.arange(len(rules))
    width = 0.35

    fig, ax = plt.subplots(figsize=(7.0, 3.6))

    # Light horizontal gridlines drawn before the bars so they sit behind.
    for y_grid in (10, 20, 30, 40):
        ax.axhline(y_grid, color=GRID, alpha=0.3, lw=0.5, zorder=0)

    bars_cost = ax.bar(
        x - width / 2, cost, width,
        label="Cost savings",
        color=DARK, edgecolor=EDGE, linewidth=0.6, zorder=2,
    )
    bars_tok = ax.bar(
        x + width / 2, input_tok_render, width,
        label="Input-token savings",
        color=LIGHT, edgecolor=EDGE, linewidth=0.6,
        hatch="////", zorder=2,
    )

    # Numeric labels on top of each bar.
    for b, v in zip(bars_cost, cost):
        h = b.get_height()
        ax.annotate(f"{v:.1f}%",
                    xy=(b.get_x() + b.get_width() / 2, h),
                    xytext=(0, 3), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8)
    for b, v_actual in zip(bars_tok, input_tok):
        h = b.get_height()
        ax.annotate(f"{v_actual:.1f}%",
                    xy=(b.get_x() + b.get_width() / 2, h),
                    xytext=(0, 3), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8)

    # ModelDowngrade input-tok bar: short call-out placed above the bar,
    # not overlapping it.
    md_idx = 1
    md_x = x[md_idx] + width / 2
    ax.annotate(
        "price-ratio rule",
        xy=(md_x + 0.17, 0.3), xytext=(md_x + 0.05, 11.0),
        ha="left", va="center", fontsize=8, color=EDGE,
        style="italic",
        arrowprops=dict(arrowstyle="-", color=EDGE, lw=0.6),
    )

    # StateDrop: short call-out above the group.
    sd_x = x[2]
    ax.annotate(
        "output-dominated",
        xy=(sd_x, 10.8), xytext=(sd_x, 18.0),
        ha="center", va="center", fontsize=8, color=EDGE,
        style="italic",
        arrowprops=dict(arrowstyle="-", color=EDGE, lw=0.6),
    )

    ax.set_xticks(x)
    ax.set_xticklabels(rules)
    ax.set_ylabel("Cost / Input-Token Savings (%)")
    ax.set_ylim(0, 42)
    ax.set_yticks([0, 10, 20, 30, 40])

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    ax.legend(loc="upper right", frameon=True, framealpha=0.9,
              edgecolor=EDGE, fontsize=8)

    fig.tight_layout()
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight",
                pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
