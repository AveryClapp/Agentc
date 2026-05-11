"""Figure 5: LLMLingua dual-regime comparison (double-column width).

Left panel: HotpotQA-distractor (n=100) — Agentc improves 68→100%,
LLMLingua-2 degrades 68→53%.
Right panel: Natural Wikipedia prose (n=39) — Agentc abstains (94.9→94.9%),
LLMLingua-2 compresses pointlessly (94.9→97.4%, 13.7s overhead).

Fixes vs v1:
- p-value annotations are anchored directly above their bar with an arrowhead
  pointing down to the bar top; value labels placed inside bars for annotated bars
- Right panel y-axis narrowed to 90–102% with axis-break marks so the 3pp
  difference is visible; left panel keeps 40–105%
- Removed floating "+32pp / LL2: −15pp" annotation
"""

from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
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

OUT = Path(__file__).resolve().parent / "fig5_llmlingua_dualregime.pdf"

C_BASE   = "#7f8c8d"
C_AGENTC = "#2c3e50"
C_LL2    = "#bcc6cf"
EDGE     = "#1a242f"
GRID     = "#9a9a9a"


def _is_dark(color):
    """True for dark bars (white label); false for light bars (dark label)."""
    return color in (C_AGENTC, C_BASE)


def draw_break_marks(ax):
    """Draw two diagonal slash marks at the bottom of the left spine."""
    d = 0.025
    gap = 0.022
    kw = dict(transform=ax.transAxes, color=EDGE, lw=0.9, clip_on=False)
    for dy in (0.0, gap):
        ax.plot([-d, d], [dy, dy + 2 * d], **kw)
    # White cover over spine at the break location so it looks like a gap.
    ax.plot([0, 0], [0, gap + 2 * d], transform=ax.transAxes,
            color="white", lw=2.5, clip_on=False, zorder=5)


def panel(ax, title, accuracies, overhead_labels,
          pval_agentc=None, pval_ll2=None,
          ylim=(40, 108), yticks=None, axis_break=False,
          overhead_pos=(0.97, 0.03), overhead_va="bottom",
          overhead_ha="right"):
    """Draw one comparison panel."""
    groups = ["Baseline", "Agentc V2\n(CC)", "LLMLingua-2"]
    colors = [C_BASE, C_AGENTC, C_LL2]
    x = np.arange(len(groups))
    width = 0.55

    ax.set_ylim(*ylim)
    y_lo, y_hi = ylim
    span = y_hi - y_lo

    # Gridlines.
    tick_vals = yticks if yticks else range(int(y_lo), int(y_hi) + 1, 10)
    for yg in tick_vals:
        if yg > y_lo:
            ax.axhline(yg, color=GRID, alpha=0.3, lw=0.5, zorder=0)

    # Bars.
    bars = []
    for i, (acc, col) in enumerate(zip(accuracies, colors)):
        b = ax.bar(x[i], acc, width, color=col, edgecolor=EDGE,
                   linewidth=0.6, zorder=2)
        bars.append(b[0])

    # Value labels: inside bar for annotated bars (Agentc, LL2 on left panel)
    # to leave headroom for p-values; above bar otherwise.
    annotated_indices = set()
    if pval_agentc:
        annotated_indices.add(1)
    if pval_ll2:
        annotated_indices.add(2)

    for i, (b, acc, col) in enumerate(zip(bars, accuracies, colors)):
        x_mid = b.get_x() + b.get_width() / 2
        if i in annotated_indices:
            # Label inside the bar, near the top.
            y_inside = acc - span * 0.06
            txt_col = "white" if _is_dark(col) else EDGE
            ax.text(x_mid, y_inside, f"{acc:.1f}%",
                    ha="center", va="top", fontsize=8, color=txt_col,
                    fontweight="bold")
        else:
            ax.annotate(f"{acc:.1f}%",
                        xy=(x_mid, acc),
                        xytext=(0, 3), textcoords="offset points",
                        ha="center", va="bottom", fontsize=8, color=EDGE)

    # P-value annotations: text above bar with downward arrowhead to bar top.
    def _pval_annotate(ax, bar, label):
        x_mid = bar.get_x() + bar.get_width() / 2
        y_bar = bar.get_height()
        y_text = min(y_bar + span * 0.14, y_hi - span * 0.02)
        ax.annotate(
            label,
            xy=(x_mid, y_bar),
            xytext=(x_mid, y_text),
            ha="center", va="bottom",
            fontsize=8, color=EDGE, style="italic",
            arrowprops=dict(
                arrowstyle="-|>",
                color=EDGE,
                lw=0.7,
                mutation_scale=6,
            ),
        )

    if pval_agentc:
        _pval_annotate(ax, bars[1], f"$p = 4.66 \\times 10^{{-10}}$")
    if pval_ll2:
        _pval_annotate(ax, bars[2], f"$p = 0.0013$")

    # Overhead box — abbreviated labels to keep the box narrow.
    oh_text = (
        "Overhead (ms)\n"
        f"Agentc: {overhead_labels[0]}\n"
        f"LL2: {overhead_labels[1]}"
    )
    ax.text(overhead_pos[0], overhead_pos[1], oh_text,
            transform=ax.transAxes,
            ha=overhead_ha, va=overhead_va,
            fontsize=7, color=EDGE,
            bbox=dict(boxstyle="round,pad=0.3", fc="white",
                      ec=EDGE, lw=0.5, alpha=0.88))

    ax.set_title(title, fontsize=10, pad=6)
    ax.set_xticks(x)
    ax.set_xticklabels(groups)
    ax.set_ylabel("Accuracy (%)")
    if yticks:
        ax.set_yticks(yticks)
    else:
        ax.set_yticks([t for t in range(int(y_lo), int(y_hi) + 1, 10) if t >= y_lo])
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    if axis_break:
        draw_break_marks(ax)


def main() -> None:
    fig, (ax_left, ax_right) = plt.subplots(1, 2, figsize=(7.0, 3.8), sharey=False)
    fig.subplots_adjust(wspace=0.42)

    # Left panel: full range to show the 68→100 vs 68→53 spread.
    # Overhead anchored above the LL2 bar (x≈2, xlim≈[-0.4,2.4], so axes x≈88%)
    # at y=43% axes (data≈71%) — above LL2 top (53%), clear of p-value (32% up).
    panel(
        ax_left,
        title="Distractor fixture  (HotpotQA, $n=100$)",
        accuracies=[68.0, 100.0, 53.0],
        overhead_labels=["<1 ms", "11,400 ms"],
        pval_agentc=True,
        pval_ll2=True,
        ylim=(40, 113),
        yticks=[40, 50, 60, 70, 80, 90, 100],
        axis_break=False,
        # Upper-left quadrant: above the Baseline bar (68%), left of Agentc bar.
        # Axes x=[3%,31%] sits between left spine and Agentc left edge (~40%).
        # Axes y=73% = data 93.3% — clear above baseline bar top (38% axes).
        overhead_pos=(0.03, 0.73),
        overhead_va="top",
        overhead_ha="left",
    )

    # Right panel: narrow range to show the 94.9 vs 97.4 difference.
    panel(
        ax_right,
        title="Natural prose  (Wikipedia QA, $n=39$)",
        accuracies=[94.9, 94.9, 97.4],
        overhead_labels=["<1 ms", "13,678 ms"],
        ylim=(90, 101.5),
        yticks=[90, 92, 94, 96, 98, 100],
        axis_break=True,
        overhead_pos=(0.97, 0.97),
        overhead_va="top",
    )

    # Shared legend.
    legend_handles = [
        mpatches.Patch(color=C_BASE,   ec=EDGE, lw=0.6, label="Baseline"),
        mpatches.Patch(color=C_AGENTC, ec=EDGE, lw=0.6, label="Agentc V2 (CC)"),
        mpatches.Patch(color=C_LL2,    ec=EDGE, lw=0.6, label="LLMLingua-2"),
    ]
    fig.legend(
        handles=legend_handles,
        loc="lower center",
        ncol=3,
        fontsize=8,
        frameon=True,
        framealpha=0.9,
        edgecolor=EDGE,
        bbox_to_anchor=(0.5, -0.02),
    )

    fig.tight_layout(rect=[0, 0.07, 1, 1])
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
