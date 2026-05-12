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
    "axes.titlesize": 8,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "mathtext.fontset": "cm",
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "axes.linewidth": 0.7,
})

OUT_PDF = Path(__file__).resolve().parents[2] / "figures" / "fig4_provider_generalization.pdf"
OUT_PNG = Path(__file__).resolve().parents[2] / "figures" / "fig4_provider_generalization.png"

C_OPENAI    = "#2c5f8a"
C_HF        = "#5b9dc9"
C_ANTHROPIC = "#c8dff0"
EDGE  = "#1a242f"
GRID  = "#9a9a9a"
ANNOT = "#555555"

WIDTH = 0.55
X = np.arange(3)


def _bar(ax, i, h, color, hatch=None):
    return ax.bar(
        X[i], h, WIDTH,
        color=color, edgecolor=EDGE, linewidth=0.6,
        hatch=hatch, zorder=2,
    )[0]


def _val_label(ax, bar, val):
    """Value % just above bar."""
    h = bar.get_height()
    cx = bar.get_x() + bar.get_width() / 2
    ax.annotate(
        f"{val:.1f}%",
        xy=(cx, h), xytext=(0, 3), textcoords="offset points",
        ha="center", va="bottom", fontsize=7,
    )


def _fire_label(ax, bar, text):
    """Fire-rate note below the x-axis baseline (avoids clip-box issues).

    Uses annotation_clip=False so the text renders outside the axes viewport.
    """
    cx = bar.get_x() + bar.get_width() / 2
    ax.annotate(
        text,
        xy=(cx, 0),
        xytext=(0, -7), textcoords="offset points",
        ha="center", va="top", fontsize=6,
        color=ANNOT, style="italic",
        annotation_clip=False,
    )


def _shared_style(ax):
    for y in (10, 20, 30, 40):
        ax.axhline(y, color=GRID, alpha=0.3, lw=0.5, zorder=0)
    ax.set_xticks([])
    ax.set_xlim(-0.55, 2.55)
    ax.set_ylim(0, 48)
    ax.set_yticks([0, 10, 20, 30, 40])
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def main() -> None:
    fig, (ax_cc, ax_md) = plt.subplots(
        1, 2, figsize=(3.5, 2.8), sharey=True,
    )
    fig.subplots_adjust(left=0.15, right=0.97, top=0.91, bottom=0.20, wspace=0.08)

    # ── (a) ContextCompress ──────────────────────────────────────────────────
    b0 = _bar(ax_cc, 0, 34.9, C_OPENAI)
    b1 = _bar(ax_cc, 1, 34.0, C_HF)
    b2 = _bar(ax_cc, 2, 0.75, C_ANTHROPIC, hatch="////")

    _val_label(ax_cc, b0, 34.9)
    _val_label(ax_cc, b1, 34.0)

    _fire_label(ax_cc, b0, "91% fire")
    _fire_label(ax_cc, b1, "98% fire")
    # Anthropic: "correct abstain" callout replaces a fire-rate label.
    _fire_label(ax_cc, b2, "—")

    _shared_style(ax_cc)

    # "correct abstain" callout: float above the bar tops (both bars 0/1 end
    # below y=35%) directly in bar 2's column. Text is shifted 0.45 units left
    # of bar 2 center so the wider second line stays within xlim; the resulting
    # arrow is ~7° from vertical so still reads as pointing straight down.
    ax_cc.annotate(
        "correct abstain\n(single-msg format)",
        xy=(X[2], 0.75),
        xytext=(1.55, 38.5),
        ha="center", va="bottom", fontsize=6, color=EDGE, style="italic",
        arrowprops=dict(arrowstyle="-", color=EDGE, lw=0.5),
    )
    ax_cc.set_title("(a) ContextCompress", fontsize=8, pad=3)
    ax_cc.set_ylabel("Input-Token Savings (%)", fontsize=8)

    # ── (b) ModelDowngrade ───────────────────────────────────────────────────
    b3 = _bar(ax_md, 0, 35.3, C_OPENAI)
    b4 = _bar(ax_md, 1, 31.1, C_HF)
    b5 = _bar(ax_md, 2, 14.7, C_ANTHROPIC)

    _val_label(ax_md, b3, 35.3)
    _val_label(ax_md, b4, 31.1)
    _val_label(ax_md, b5, 14.7)

    _fire_label(ax_md, b3, "all calls")
    _fire_label(ax_md, b4, "34% fire")
    _fire_label(ax_md, b5, "20% fire")

    _shared_style(ax_md)
    ax_md.set_title("(b) ModelDowngrade", fontsize=8, pad=3)
    ax_md.spines["left"].set_visible(False)
    ax_md.tick_params(left=False)
    ax_md.yaxis.set_label_position("right")
    ax_md.set_ylabel("Cost Savings (%)", fontsize=8)

    # ── shared legend ─────────────────────────────────────────────────────────
    handles = [
        plt.Rectangle((0, 0), 1, 1, facecolor=C_OPENAI, edgecolor=EDGE, linewidth=0.6),
        plt.Rectangle((0, 0), 1, 1, facecolor=C_HF, edgecolor=EDGE, linewidth=0.6),
        plt.Rectangle((0, 0), 1, 1, facecolor=C_ANTHROPIC, edgecolor=EDGE,
                       linewidth=0.6, hatch="////"),
    ]
    fig.legend(
        handles, ["OpenAI", "HuggingFace", "Anthropic"],
        loc="lower center", ncol=3, frameon=False, fontsize=7,
        bbox_to_anchor=(0.5, 0.0),
    )

    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PDF, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    fig.savefig(OUT_PNG, format="png", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT_PDF}")
    print(f"wrote {OUT_PNG}")


if __name__ == "__main__":
    main()
