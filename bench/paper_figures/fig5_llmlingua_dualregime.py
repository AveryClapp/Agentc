"""Figure 5: LLMLingua dual-regime comparison (double-column width).

Left panel: HotpotQA-distractor (n=100) — Agentc improves 68→100%,
LLMLingua-2 degrades 68→53%.
Right panel: Natural Wikipedia prose (n=39) — Agentc abstains (94.9→94.9%),
LLMLingua-2 compresses pointlessly (94.9→97.4%, 13.7s overhead).

McNemar p-values annotated on treatment bars (left panel only, where
significance differs). Overhead shown as text inset in each panel.
"""

from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
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

# Color scheme matching existing figures.
C_BASE   = "#7f8c8d"   # neutral gray — baseline
C_AGENTC = "#2c3e50"   # dark slate — Agentc
C_LL2    = "#bcc6cf"   # light steel — LLMLingua-2
EDGE     = "#1a242f"
GRID     = "#9a9a9a"


def annotate_pval(ax, bar, label, offset_y=2.5):
    x = bar.get_x() + bar.get_width() / 2
    y = bar.get_height()
    ax.annotate(
        label,
        xy=(x, y), xytext=(0, offset_y),
        textcoords="offset points",
        ha="center", va="bottom",
        fontsize=7, color=EDGE, style="italic",
    )


def add_value_label(ax, bar, val, offset_y=2.5):
    x = bar.get_x() + bar.get_width() / 2
    y = bar.get_height()
    ax.annotate(
        f"{val:.1f}%",
        xy=(x, y), xytext=(0, offset_y),
        textcoords="offset points",
        ha="center", va="bottom",
        fontsize=8, color=EDGE,
    )


def panel(ax, title, accuracies, overhead_labels, pval_agentc=None, pval_ll2=None):
    """Draw one panel. accuracies = [baseline, agentc, ll2]."""
    groups = ["Baseline", "Agentc V2\n(CC)", "LLMLingua-2"]
    colors = [C_BASE, C_AGENTC, C_LL2]
    x = np.arange(len(groups))
    width = 0.55

    for ax_grid_y in (50, 60, 70, 80, 90, 100):
        ax.axhline(ax_grid_y, color=GRID, alpha=0.3, lw=0.5, zorder=0)

    bars = []
    for i, (acc, col) in enumerate(zip(accuracies, colors)):
        b = ax.bar(
            x[i], acc, width,
            color=col, edgecolor=EDGE, linewidth=0.6, zorder=2,
        )
        bars.append(b[0])
        add_value_label(ax, b[0], acc, offset_y=3)

    # McNemar p-value annotations on Agentc and LL2 bars (left panel).
    if pval_agentc:
        annotate_pval(ax, bars[1], f"p={pval_agentc}", offset_y=14)
    if pval_ll2:
        annotate_pval(ax, bars[2], f"p={pval_ll2}", offset_y=14)

    # Overhead inset text box.
    oh_text = (
        f"Overhead\n"
        f"Agentc: {overhead_labels[0]}\n"
        f"LLMLingua-2: {overhead_labels[1]}"
    )
    ax.text(
        0.97, 0.03, oh_text,
        transform=ax.transAxes,
        ha="right", va="bottom",
        fontsize=7, color=EDGE,
        bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=EDGE, lw=0.5, alpha=0.85),
    )

    ax.set_title(title, fontsize=10, pad=6)
    ax.set_xticks(x)
    ax.set_xticklabels(groups)
    ax.set_ylabel("Accuracy (%)")
    ax.set_ylim(40, 108)
    ax.set_yticks([40, 50, 60, 70, 80, 90, 100])
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def main() -> None:
    fig, (ax_left, ax_right) = plt.subplots(1, 2, figsize=(7.0, 3.6), sharey=False)
    fig.subplots_adjust(wspace=0.38)

    # Left: HotpotQA-distractor (n=100)
    panel(
        ax_left,
        title="Distractor fixture  (HotpotQA, $n=100$)",
        accuracies=[68.0, 100.0, 53.0],
        overhead_labels=["<1 ms", "11,400 ms"],
        pval_agentc="4.66e-10",
        pval_ll2="0.0013",
    )

    # Right: Natural Wikipedia prose (n=39)
    panel(
        ax_right,
        title="Natural prose  (Wikipedia QA, $n=39$)",
        accuracies=[94.9, 94.9, 97.4],
        overhead_labels=["<1 ms", "13,678 ms"],
    )

    # Shared message: shaded difference regions (optional arrow annotation).
    ax_left.annotate(
        "Agentc: +32 pp\nLL2: −15 pp",
        xy=(1.05, 82), xycoords=("axes fraction", "data"),
        ha="left", va="center",
        fontsize=7.5, color=EDGE,
        style="italic",
    )

    # Legend shared at figure level.
    legend_handles = [
        mpatches.Patch(color=C_BASE,   ec=EDGE, lw=0.6, label="Baseline"),
        mpatches.Patch(color=C_AGENTC, ec=EDGE, lw=0.6, label="Agentc V2 (CC)"),
        mpatches.Patch(color=C_LL2,    ec=EDGE, lw=0.6, hatch="////", label="LLMLingua-2"),
    ]
    fig.legend(
        handles=legend_handles,
        loc="lower center",
        ncol=3,
        fontsize=8,
        frameon=True,
        framealpha=0.9,
        edgecolor=EDGE,
        bbox_to_anchor=(0.5, -0.04),
    )

    fig.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
