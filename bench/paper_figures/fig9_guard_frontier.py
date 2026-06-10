"""Figure 9: Selective accuracy-guard frontier (embedding divergence metric).

Two panels share the divergence-budget x-axis (tight -> loose -> off). The SAME
guard, driven by the SAME embedding-cosine divergence metric, produces opposite
but correct behavior on two rules:

  (a) Catastrophic StateDrop on analyst_qa (n=200): unguarded accuracy is
      -48 pp; tightening the budget makes the guard auto-disable the rule
      earlier, recovering accuracy to about -3 pp.
  (b) Benign ContextCompress on research_planner (n=50): accuracy delta stays
      positive and cost savings stay ~25-28% across the whole frontier -- the
      guard does not destroy a rule that is helping.

The shaded band (0.10-0.20) marks the operating region where the catastrophic
rule is caught AND the benign rule's savings are retained.

Data (read directly from bench/paper_results/):
  gsweep_embedding_{rp,an}_{0.05,0.10,0.20,0.30,0.50,off}.csv
  emb_ckpt_{rp,an}.csv                       (the 0.15 operating point)
"""

from pathlib import Path
import csv

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

OUT = Path(__file__).resolve().parent / "fig9_guard_frontier.pdf"
RESULTS = Path(__file__).resolve().parents[1] / "paper_results"

DARK   = "#2c3e50"
SAVE   = "#2e7d32"   # benign savings (green)
CATCH  = "#b03030"   # catastrophic accuracy (red)
GRID   = "#9a9a9a"
BAND   = "#cfe3cf"
EDGE   = "#1a242f"

# x-axis order, tight budget -> loose -> guard off.
THRESHOLDS = ["0.05", "0.10", "0.15", "0.20", "0.30", "0.50", "off"]
XLABELS    = ["0.05", "0.10", "0.15", "0.20", "0.30", "0.50", "off"]


def _read_row(path: Path) -> dict:
    with open(path) as f:
        return next(csv.DictReader(f))


def _series(short: str) -> tuple[list[float], list[float]]:
    """Return (acc_delta_pp, cost_savings_pct) over THRESHOLDS for one agent."""
    acc, save = [], []
    for t in THRESHOLDS:
        if t == "0.15":
            path = RESULTS / f"emb_ckpt_{short}.csv"
        else:
            path = RESULTS / f"gsweep_embedding_{short}_{t}.csv"
        row = _read_row(path)
        acc.append(float(row["acc_delta_pp"]))
        save.append(float(row["cost_savings_pct"]))
    return acc, save


def main() -> None:
    x = np.arange(len(THRESHOLDS))
    an_acc, _ = _series("an")          # catastrophic StateDrop
    rp_acc, rp_save = _series("rp")    # benign ContextCompress

    # Operating band 0.10-0.20 -> indices 1..3 inclusive.
    band_lo, band_hi = 0.5, 3.5

    fig, (axA, axB) = plt.subplots(1, 2, figsize=(7.4, 3.3))

    # ---- Panel A: catastrophic StateDrop accuracy recovery ----
    axA.axhspan(-100, -100, color="none")  # keep autoscale stable
    axA.axvspan(band_lo, band_hi, color=BAND, alpha=0.55, zorder=0)
    axA.axhline(0, color=GRID, lw=0.7, zorder=1)
    axA.plot(x, an_acc, "o-", color=CATCH, lw=1.6, ms=5, zorder=3,
             label="StateDrop accuracy $\\Delta$")
    axA.annotate(f"unguarded\n{an_acc[-1]:.0f} pp",
                 xy=(x[-1], an_acc[-1]), xytext=(x[-1] - 0.2, an_acc[-1] + 9),
                 ha="right", va="bottom", fontsize=7.5, color=CATCH, style="italic",
                 arrowprops=dict(arrowstyle="-", color=CATCH, lw=0.6))
    axA.annotate(f"caught\n{an_acc[3]:.1f} pp",
                 xy=(x[3], an_acc[3]), xytext=(x[3] + 0.15, an_acc[3] - 12),
                 ha="left", va="top", fontsize=7.5, color=EDGE, style="italic",
                 arrowprops=dict(arrowstyle="-", color=EDGE, lw=0.6))
    axA.set_title("(a) Catastrophic rule (StateDrop, $n$=200)")
    axA.set_ylabel("Accuracy $\\Delta$ (pp)")
    axA.set_ylim(-55, 12)
    axA.set_xticks(x); axA.set_xticklabels(XLABELS)
    axA.set_xlabel("Divergence budget $\\tau$  (tight $\\rightarrow$ off)")
    axA.spines["top"].set_visible(False); axA.spines["right"].set_visible(False)

    # ---- Panel B: benign ContextCompress preserved ----
    axB.axvspan(band_lo, band_hi, color=BAND, alpha=0.55, zorder=0,
                label="operating band")
    axB.axhline(0, color=GRID, lw=0.7, zorder=1)
    axB.plot(x, rp_acc, "o-", color=DARK, lw=1.6, ms=5, zorder=3,
             label="accuracy $\\Delta$ (pp)")
    axB.set_title("(b) Benign rule (ContextCompress, $n$=50)")
    axB.set_ylabel("Accuracy $\\Delta$ (pp)", color=DARK)
    axB.tick_params(axis="y", labelcolor=DARK)
    axB.set_ylim(-12, 12)
    axB.set_xticks(x); axB.set_xticklabels(XLABELS)
    axB.set_xlabel("Divergence budget $\\tau$  (tight $\\rightarrow$ off)")
    axB.spines["top"].set_visible(False)

    axB2 = axB.twinx()
    axB2.plot(x, rp_save, "s--", color=SAVE, lw=1.4, ms=4, zorder=3,
              label="cost savings (%)")
    axB2.set_ylabel("Cost savings (%)", color=SAVE)
    axB2.tick_params(axis="y", labelcolor=SAVE)
    axB2.set_ylim(0, 35)
    axB2.spines["top"].set_visible(False)

    # Combined legend for panel B.
    lines = (axB.get_lines()[1:2] + axB2.get_lines()[0:1])
    axB.legend(lines, [l.get_label() for l in lines], loc="lower center",
               fontsize=7.5, frameon=True, framealpha=0.9, edgecolor=EDGE)

    fig.tight_layout()
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
