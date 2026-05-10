"""Figure 4: Methodology validation, temp=1 noise vs temp=0 signal.

Same rule (StateDrop), same workload (iterative_refiner), two sampling
conditions. Cost delta is unstable across the two conditions; input-token
delta is identical. A double-headed arrow connects the two input-token bars
to emphasize their equivalence; the negative cost bar is shaded distinctly
(rust red) to flag it as a negative result. A thicker zero baseline and an
"output-token noise" call-out on the negative cost bar drive the
interpretation.
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

OUT = Path(__file__).resolve().parent / "fig4_methodology.pdf"

DARK = "#2c3e50"
LIGHT = "#bcc6cf"
EDGE = "#1a242f"
NEGATIVE = "#a64036"   # rust-red, prints distinctly in grayscale


def main() -> None:
    groups = ["Temperature 1\n(single trial)", "Temperature 0\n(controlled)"]
    cost_delta = [-7.0, 1.9]
    input_delta = [6.7, 6.7]

    x = np.arange(len(groups))
    width = 0.34

    fig, ax = plt.subplots(figsize=(3.33, 3.2))

    # Cost bars colored individually so the negative result stands out.
    cost_colors = [NEGATIVE if v < 0 else DARK for v in cost_delta]
    bars_cost = ax.bar(
        x - width / 2, cost_delta, width,
        label="Cost savings",
        color=cost_colors, edgecolor=EDGE, linewidth=0.6, zorder=2,
    )
    bars_tok = ax.bar(
        x + width / 2, input_delta, width,
        label="Input-token savings",
        color=LIGHT, edgecolor=EDGE, linewidth=0.6,
        hatch="////", zorder=2,
    )

    # Thick zero baseline.
    ax.axhline(0, color="black", lw=1.2, zorder=1)

    # Cost-bar value labels.
    cost_labels = ["-7.0%", "+1.9%"]
    for b, lbl in zip(bars_cost, cost_labels):
        h = b.get_height()
        offset = 4 if h >= 0 else -4
        va = "bottom" if h >= 0 else "top"
        ax.annotate(
            lbl,
            xy=(b.get_x() + b.get_width() / 2, h),
            xytext=(0, offset), textcoords="offset points",
            ha="center", va=va, fontsize=8,
        )

    # Input-token bar value labels.
    for b in bars_tok:
        h = b.get_height()
        ax.annotate(
            f"+{h:.1f}%",
            xy=(b.get_x() + b.get_width() / 2, h),
            xytext=(0, 4), textcoords="offset points",
            ha="center", va="bottom", fontsize=8,
        )

    # Double-headed arrow between the two input-token bars to emphasize
    # equivalence.
    x_left = x[0] + width / 2
    x_right = x[1] + width / 2
    y_arrow = 9.2
    ax.annotate(
        "",
        xy=(x_right, y_arrow), xytext=(x_left, y_arrow),
        arrowprops=dict(arrowstyle="<->", color="black", lw=0.9),
    )
    ax.text((x_left + x_right) / 2, y_arrow + 0.4,
            "equal (+6.7%)",
            ha="center", va="bottom", fontsize=8, style="italic",
            color="black")

    # Output-token-noise call-out pointing at the -7.0% bar. Placed in the
    # gap between groups so it stays inside the axis area and clear of the
    # x-tick labels.
    neg_bar = bars_cost[0]
    neg_x = neg_bar.get_x() + neg_bar.get_width() / 2
    ax.annotate(
        "output-token\nnoise",
        xy=(neg_x + neg_bar.get_width() / 2, -6.5),
        xytext=(0.55, -3.5),
        ha="center", va="center", fontsize=7.5, color=NEGATIVE,
        style="italic",
        arrowprops=dict(arrowstyle="->", color=NEGATIVE, lw=0.7),
    )

    ax.set_xticks(x)
    ax.set_xticklabels(groups, fontsize=8.5)
    ax.set_ylabel("Cost / Input-Token Savings (%)")
    ax.set_ylim(-10, 12)
    ax.set_yticks([-10, -5, 0, 5, 10])

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    ax.legend(loc="lower right", frameon=True, framealpha=0.9,
              edgecolor=EDGE, fontsize=7.5)

    fig.tight_layout()
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight",
                pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
