"""Figure 6: Composition orthogonality — horizontal bar chart.

Two horizontal bars normalised to % of additive ideal:
  - MD + CC (orthogonal drivers): 95.6%
  - CC + SD (same driver):        65.3%
Vertical dashed reference line at 100%. Value labels inside bars in white.
Annotation strings below each bar show the raw numbers.
"""

from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

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

TEAL   = "#1D9E75"
AMBER  = "#BA7517"
EDGE   = "#1a242f"
GRID   = "#9a9a9a"


def main() -> None:
    bars = [
        {
            "label": "Orthogonal drivers\nMD + CC",
            "value": 95.6,
            "color": TEAL,
            "note": "CC: 2.82 mUSD + MD: 7.51 mUSD  →  ideal: 10.33 mUSD  →  composed: 9.88 mUSD",
        },
        {
            "label": "Same driver\nCC + SD",
            "value": 65.3,
            "color": AMBER,
            "note": "CC: 33.1% + SD: 0.1% tok  →  ideal: 33.2%  →  composed: 21.7%",
        },
    ]

    fig, ax = plt.subplots(figsize=(6.0, 2.6))

    y_positions = [1.0, 0.0]
    bar_height  = 0.42

    for y, b in zip(y_positions, bars):
        ax.barh(
            y, b["value"], bar_height,
            color=b["color"], edgecolor=EDGE, linewidth=0.6,
            zorder=2,
        )
        # Value label inside bar, right-aligned, white.
        ax.text(
            b["value"] - 1.5, y,
            f'{b["value"]:.1f}%',
            ha="right", va="center",
            fontsize=9, color="white", fontweight="bold",
        )
        # Raw-number annotation below the bar.
        ax.text(
            1.0, y - bar_height / 2 - 0.07,
            b["note"],
            ha="left", va="top",
            fontsize=7, color=EDGE, style="italic",
            transform=ax.get_yaxis_transform(),
        )

    # Dashed reference line at 100%.
    ax.axvline(100, color=EDGE, lw=0.9, ls="--", zorder=1)
    ax.text(
        100.8, max(y_positions) + bar_height / 2 + 0.04,
        "additive ideal",
        ha="left", va="top",
        fontsize=7.5, color=EDGE, style="italic",
    )

    # Light vertical gridlines.
    for xg in (25, 50, 75):
        ax.axvline(xg, color=GRID, alpha=0.35, lw=0.5, zorder=0)

    ax.set_yticks(y_positions)
    ax.set_yticklabels([b["label"] for b in bars], fontsize=9)
    ax.set_xlabel("% of additive ideal")
    ax.set_xlim(0, 114)
    ax.set_ylim(-0.52, 1.52)
    ax.xaxis.set_major_formatter(mticker.FormatStrFormatter("%g%%"))
    ax.xaxis.set_major_locator(mticker.MultipleLocator(25))

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
