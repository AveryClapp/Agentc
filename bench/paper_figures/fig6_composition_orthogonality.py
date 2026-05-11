"""Figure 6: Composition orthogonality — horizontal bar chart with summary table.

Two horizontal bars normalised to % of additive ideal:
  - MD + CC (orthogonal drivers): 95.6%  — teal
  - CC + SD (same driver):        65.3%  — amber
Vertical dashed reference line at 100%.
Value labels inside bars in white.
Summary table of raw numbers below the bars.
"""

from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.gridspec as gridspec

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

OUT = Path(__file__).resolve().parent / "fig6_composition_orthogonality.pdf"

TEAL  = "#1D9E75"
AMBER = "#BA7517"
EDGE  = "#1a242f"
GRID  = "#9a9a9a"


def main() -> None:
    fig = plt.figure(figsize=(5.5, 3.5))
    gs = gridspec.GridSpec(2, 1, height_ratios=[2.2, 1.0], hspace=0.08,
                           figure=fig)
    ax_bars  = fig.add_subplot(gs[0])
    ax_table = fig.add_subplot(gs[1])

    # ── Horizontal bars ──────────────────────────────────────────────────
    bars = [
        {"label": "Orthogonal drivers\nMD + CC", "value": 95.6, "color": TEAL},
        {"label": "Same driver\nCC + SD",         "value": 65.3, "color": AMBER},
    ]

    y_pos      = [1.0, 0.0]
    bar_height = 0.42

    for yg in (25, 50, 75):
        ax_bars.axvline(yg, color=GRID, alpha=0.35, lw=0.5, zorder=0)

    ax_bars.axvline(100, color=EDGE, lw=0.9, ls="--", zorder=1)
    ax_bars.text(100.8, max(y_pos) + bar_height / 2,
                 "additive\nideal",
                 ha="left", va="center",
                 fontsize=7.5, color=EDGE, style="italic")

    for y, b in zip(y_pos, bars):
        ax_bars.barh(y, b["value"], bar_height,
                     color=b["color"], edgecolor=EDGE, linewidth=0.6, zorder=2)
        ax_bars.text(
            b["value"] - 1.8, y,
            f'{b["value"]:.1f}%',
            ha="right", va="center",
            fontsize=9.5, color="white", fontweight="bold",
        )

    ax_bars.set_yticks(y_pos)
    ax_bars.set_yticklabels([b["label"] for b in bars], fontsize=9)
    ax_bars.set_xlabel("% of additive ideal")
    ax_bars.set_xlim(0, 114)
    ax_bars.set_ylim(-0.5, 1.5)
    ax_bars.xaxis.set_major_formatter(mticker.FormatStrFormatter("%g%%"))
    ax_bars.xaxis.set_major_locator(mticker.MultipleLocator(25))
    ax_bars.spines["top"].set_visible(False)
    ax_bars.spines["right"].set_visible(False)

    # ── Summary table ─────────────────────────────────────────────────────
    ax_table.axis("off")

    col_labels = ["Pair", "Best solo", "Composed", "Ideal", "Efficiency"]
    row_data = [
        ["MD + CC", "7.51 mUSD", "9.88 mUSD", "10.33 mUSD", "95.6%"],
        ["CC + SD", "33.1% tok",  "21.7% tok",  "33.2% tok",  "65.3%"],
    ]

    # Column widths as fractions of axes width.
    col_widths = [0.18, 0.20, 0.20, 0.22, 0.20]

    tbl = ax_table.table(
        cellText=row_data,
        colLabels=col_labels,
        colWidths=col_widths,
        loc="upper center",
        cellLoc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8)

    # Style: header row in dark with white text; data rows white bg.
    for (row, col), cell in tbl.get_celld().items():
        cell.set_edgecolor(EDGE)
        cell.set_linewidth(0.5)
        if row == 0:
            cell.set_facecolor(EDGE)
            cell.set_text_props(color="white", fontweight="bold")
        elif row == 1:
            cell.set_facecolor("#e8f5f0")   # light teal tint for MD+CC row
        else:
            cell.set_facecolor("#fdf3e3")   # light amber tint for CC+SD row

    tbl.scale(1, 1.35)

    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
