"""Figure 6: Composition orthogonality validation.

Normalized bar chart (% of additive ideal) for two rule pairs:
  - MD + CC (orthogonal drivers: ModelPrice × InputTokens) → 95.6% of ideal
  - CC + SD (same driver: InputTokens × InputTokens)       → 65.3% of ideal

Three bars per group: Solo Best, Composed, Additive Ideal (reference).
Normalizing to % of additive ideal puts both groups on the same axis
despite different underlying units (mUSD vs input-token %).

Raw numbers:
  MD+CC: CC_solo=2.82 mUSD, MD_solo=7.51 mUSD,
         additive_ideal=10.33 mUSD, composed=9.88 mUSD → 95.6%
  CC+SD: CC_solo=33.1% tok, SD_solo=0.1% tok,
         additive_ideal=33.2% tok, composed=21.7% tok → 65.3%
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

OUT = Path(__file__).resolve().parent / "fig6_composition_orthogonality.pdf"

DARK  = "#2c3e50"
MED   = "#7f8c8d"
LIGHT = "#bcc6cf"
REF   = "#e8ecef"
EDGE  = "#1a242f"
GRID  = "#9a9a9a"


def main() -> None:
    # Normalize everything to % of additive ideal.
    # MD+CC: ideal = 10.33 mUSD
    mdcc_ideal = 10.33
    mdcc_solo_best = 7.51 / mdcc_ideal * 100     # MD wins solo
    mdcc_composed  = 9.88 / mdcc_ideal * 100     # 95.6%
    mdcc_ideal_pct = 100.0

    # CC+SD: ideal = 33.2% tokens
    ccsd_ideal = 33.2
    ccsd_solo_best = 33.1 / ccsd_ideal * 100     # CC wins solo (99.7%)
    ccsd_composed  = 21.7 / ccsd_ideal * 100     # 65.3%
    ccsd_ideal_pct = 100.0

    groups = [
        ("Orthogonal\n(MD + CC)", mdcc_solo_best, mdcc_composed, mdcc_ideal_pct),
        ("Same driver\n(CC + SD)", ccsd_solo_best, ccsd_composed, ccsd_ideal_pct),
    ]

    labels = ["Solo best rule", "Composed", "Additive ideal"]
    colors = [MED, DARK, REF]
    hatches = ["", "", "////"]

    x = np.arange(len(groups))
    width = 0.22
    offsets = [-width, 0, width]

    fig, ax = plt.subplots(figsize=(5.5, 3.6))

    for y_grid in (25, 50, 75, 100):
        ax.axhline(y_grid, color=GRID, alpha=0.3, lw=0.5, zorder=0)
    ax.axhline(100, color=EDGE, alpha=0.6, lw=0.8, ls="--", zorder=1)

    bar_objs = {lbl: [] for lbl in labels}
    for grp_idx, (grp_name, solo, composed, ideal) in enumerate(groups):
        vals = [solo, composed, ideal]
        for bar_idx, (lbl, col, hatch, val) in enumerate(
            zip(labels, colors, hatches, vals)
        ):
            bx = x[grp_idx] + offsets[bar_idx]
            b = ax.bar(
                bx, val, width,
                color=col, edgecolor=EDGE, linewidth=0.6,
                hatch=hatch, zorder=2,
            )
            bar_objs[lbl].append(b[0])
            # Value labels on top.
            ax.annotate(
                f"{val:.0f}%",
                xy=(bx + width / 2, val),
                xytext=(0, 3), textcoords="offset points",
                ha="center", va="bottom", fontsize=7.5,
            )

    # "% of ideal" call-out on composed bars.
    callouts = [
        (x[0] + offsets[1], mdcc_composed, "95.6%\nof ideal"),
        (x[1] + offsets[1], ccsd_composed, "65.3%\nof ideal"),
    ]
    for (bx, val, txt) in callouts:
        ax.annotate(
            txt,
            xy=(bx + width / 2, val),
            xytext=(18, -16), textcoords="offset points",
            ha="left", va="top", fontsize=7.5, color=DARK,
            style="italic",
            arrowprops=dict(arrowstyle="-", color=EDGE, lw=0.6),
        )

    ax.set_xticks(x)
    ax.set_xticklabels([g[0] for g in groups])
    ax.set_ylabel("% of Additive Ideal")
    ax.set_ylim(0, 115)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    legend_handles = [
        mpatches.Patch(color=MED,  ec=EDGE, lw=0.6, label="Solo best rule"),
        mpatches.Patch(color=DARK, ec=EDGE, lw=0.6, label="Composed"),
        mpatches.Patch(color=REF,  ec=EDGE, lw=0.6, hatch="////", label="Additive ideal"),
    ]
    ax.legend(
        handles=legend_handles,
        loc="lower right",
        fontsize=8,
        frameon=True,
        framealpha=0.9,
        edgecolor=EDGE,
    )

    # Subtitle annotation.
    ax.text(
        0.02, 0.97,
        "Orthogonal pairs compose near-additively;\nsame-driver pairs compose sub-additively.",
        transform=ax.transAxes,
        ha="left", va="top",
        fontsize=7.5, color=EDGE, style="italic",
    )

    fig.tight_layout()
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
