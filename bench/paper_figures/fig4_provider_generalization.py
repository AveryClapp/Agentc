"""Figure 4: Provider generalization (single-column width).

Two-panel bar chart showing ContextCompress and ModelDowngrade cost savings
across three LLM providers (OpenAI, HuggingFace, Anthropic).
"""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 8,
    "axes.labelsize": 8,
    "axes.titlesize": 9,
    "legend.fontsize": 7,
    "mathtext.fontset": "cm",
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "axes.linewidth": 0.7,
})

OUT_PDF = Path(__file__).resolve().parents[2] / "figures" / "fig4_provider_generalization.pdf"
OUT_PNG = Path(__file__).resolve().parents[2] / "figures" / "fig4_provider_generalization.png"

C_OPENAI = "#2c5f8a"
C_HF = "#5b9dc9"
C_ANTHROPIC = "#c8dff0"
EDGE = "#1a242f"
GRID = "#9a9a9a"

PROVIDERS = ["OpenAI", "HuggingFace", "Anthropic"]


def _annotate_bar(ax, bar, value_str, fire_str, is_hatched=False, arrow_label=None):
    h = bar.get_height()
    cx = bar.get_x() + bar.get_width() / 2

    if arrow_label:
        # Annotate with an arrow pointing into the bar area for special cases.
        ax.annotate(
            arrow_label,
            xy=(cx, 0.5),
            xytext=(cx + 0.55, 6.0),
            ha="center", va="bottom", fontsize=6, color=EDGE,
            style="italic",
            arrowprops=dict(arrowstyle="-", color=EDGE, lw=0.5),
        )
        return

    # Value label on top of bar.
    ax.annotate(
        value_str,
        xy=(cx, h),
        xytext=(0, 3), textcoords="offset points",
        ha="center", va="bottom", fontsize=7,
    )
    # Fire-rate annotation just below the value label.
    ax.annotate(
        fire_str,
        xy=(cx, h),
        xytext=(0, 11), textcoords="offset points",
        ha="center", va="bottom", fontsize=6, color="#555555",
    )


def main() -> None:
    fig, (ax_cc, ax_md) = plt.subplots(
        1, 2,
        figsize=(3.5, 2.8),
        sharey=True,
    )

    # Gridlines behind bars.
    for ax in (ax_cc, ax_md):
        for y_grid in (10, 20, 30, 40):
            ax.axhline(y_grid, color=GRID, alpha=0.3, lw=0.5, zorder=0)

    width = 0.5
    x_pos = np.arange(len(PROVIDERS))
    colors = [C_OPENAI, C_HF, C_ANTHROPIC]

    # ── (a) ContextCompress ──────────────────────────────────────────────────
    cc_values = [34.9, 34.0, 0.0]
    cc_fire = ["91% fire", "98% fire", ""]
    cc_hatches = [None, None, "////"]

    cc_bars = []
    for i, (v, color, hatch) in enumerate(zip(cc_values, colors, cc_hatches)):
        render_h = v if v > 0.0 else 0.8
        bar = ax_cc.bar(
            x_pos[i], render_h, width,
            color=color, edgecolor=EDGE, linewidth=0.6,
            hatch=hatch, zorder=2,
            label=PROVIDERS[i],
        )
        cc_bars.append(bar[0])

    for bar, v, fire in zip(cc_bars[:2], cc_values[:2], cc_fire[:2]):
        _annotate_bar(ax_cc, bar, f"{v:.1f}%", fire)

    # Anthropic bar: arrow annotation for correct-abstain.
    _annotate_bar(ax_cc, cc_bars[2], "", "", arrow_label="correct abstain\n(single-msg format)")

    ax_cc.set_title("(a) ContextCompress", fontsize=8, pad=4)
    ax_cc.set_xticks([])
    ax_cc.set_ylabel("Cost Savings (%)", fontsize=8)

    # ── (b) ModelDowngrade ───────────────────────────────────────────────────
    md_values = [35.3, 31.1, 14.7]
    md_fire = ["all calls", "34% fire", "20% fire"]

    md_bars = []
    for i, (v, color) in enumerate(zip(md_values, colors)):
        bar = ax_md.bar(
            x_pos[i], v, width,
            color=color, edgecolor=EDGE, linewidth=0.6,
            zorder=2,
        )
        md_bars.append(bar[0])

    for bar, v, fire in zip(md_bars, md_values, md_fire):
        _annotate_bar(ax_md, bar, f"{v:.1f}%", fire)

    ax_md.set_title("(b) ModelDowngrade", fontsize=8, pad=4)
    ax_md.set_xticks([])

    # Shared y-axis.
    for ax in (ax_cc, ax_md):
        ax.set_ylim(0, 46)
        ax.set_yticks([0, 10, 20, 30, 40])
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    ax_md.spines["left"].set_visible(False)
    ax_md.tick_params(left=False)

    # Shared legend at the bottom.
    handles = [
        plt.Rectangle((0, 0), 1, 1, facecolor=C_OPENAI, edgecolor=EDGE, linewidth=0.6),
        plt.Rectangle((0, 0), 1, 1, facecolor=C_HF, edgecolor=EDGE, linewidth=0.6),
        plt.Rectangle((0, 0), 1, 1, facecolor=C_ANTHROPIC, edgecolor=EDGE, linewidth=0.6,
                       hatch="////"),
    ]
    fig.legend(
        handles, PROVIDERS,
        loc="lower center",
        ncol=3,
        frameon=False,
        fontsize=7,
        bbox_to_anchor=(0.5, -0.02),
    )

    fig.tight_layout(rect=[0, 0.06, 1, 1])

    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PDF, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    fig.savefig(OUT_PNG, format="png", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT_PDF}")
    print(f"wrote {OUT_PNG}")


if __name__ == "__main__":
    main()
