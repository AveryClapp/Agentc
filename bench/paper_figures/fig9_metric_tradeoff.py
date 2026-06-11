"""Figure 10: Why the divergence metric matters -- lexical alone is insufficient.

A single scatter on two sampling-rate-independent behavioral axes:

  x = benign rule retained (%)        = guarded CC fires / unguarded CC fires
  y = catastrophic damage prevented (%) = (|unguarded SD acc| - |guarded SD acc|)
                                          / |unguarded SD acc|

Each point is one (metric, divergence-budget tau) configuration; the benign
coordinate comes from research_planner/ContextCompress (n=200) and the
catastrophic coordinate from analyst_qa/StateDrop (n=200), at the SAME metric
and tau. Three groups tell the whole story:

  - unguarded (no shadow guard): keeps the benign rule fully (x=100) but
    prevents zero catastrophe (y=0)  -> bottom-right.
  - lexical (raw Jaccard) metric: catches the catastrophe (y~92) but disables
    the benign rule at EVERY tau, so retention is low  -> top-left.
  - normalized (containment) metric: catches the catastrophe AND keeps the
    benign rule at every tau  -> top-right, the desired region.

No lexical operating point reaches the top-right region across a 10x range of
budgets; the normalized metric occupies it at every budget. The naive metric
cannot preserve a benign output-changing rule; the selective metric can.

Data (read directly from bench/paper_results/):
  gsweep_tradeoff_{lexical,normalized}_{rp,an}_{0.10,0.20,0.30,0.50}.csv
  gsweep_tradeoff_off_{rp,an}.csv        (unguarded references)
"""

from pathlib import Path
import csv

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

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

OUT = Path(__file__).resolve().parents[2] / "figures" / "fig9_metric_tradeoff.pdf"
RESULTS = Path(__file__).resolve().parents[1] / "paper_results"

LEX   = "#b03030"   # lexical (red)
NORM  = "#2e7d32"   # normalized (green)
GREY  = "#7a7a7a"   # unguarded reference
BAND  = "#cfe3cf"   # desired-region shading
EDGE  = "#1a242f"
GRID  = "#c8c8c8"

THRESHOLDS = ["0.10", "0.20", "0.30", "0.50"]


def _row(path: Path) -> dict:
    with open(path) as f:
        return next(csv.DictReader(f))


def main() -> None:
    # Unguarded references.
    off_rp = _row(RESULTS / "gsweep_tradeoff_off_rp.csv")
    off_an = _row(RESULTS / "gsweep_tradeoff_off_an.csv")
    cc_unguarded = float(off_rp["cc_fire_count"])          # 191
    sd_dmg_unguarded = abs(float(off_an["acc_delta_pp"]))  # 49.5

    def points(metric: str):
        xs, ys, taus = [], [], []
        for t in THRESHOLDS:
            rp = _row(RESULTS / f"gsweep_tradeoff_{metric}_rp_{t}.csv")
            an = _row(RESULTS / f"gsweep_tradeoff_{metric}_an_{t}.csv")
            retention = 100.0 * float(rp["cc_fire_count"]) / cc_unguarded
            dmg_prev = 100.0 * (sd_dmg_unguarded - abs(float(an["acc_delta_pp"]))) \
                / sd_dmg_unguarded
            xs.append(retention); ys.append(dmg_prev); taus.append(t)
        return xs, ys, taus

    lx, ly, lt = points("lexical")
    nx, ny, nt = points("normalized")

    fig, ax = plt.subplots(figsize=(5.0, 4.0))

    # Desired operating region: keep most of the benign rule AND prevent most damage.
    ax.add_patch(Rectangle((80, 85), 25, 18, facecolor=BAND, alpha=0.6,
                           edgecolor="none", zorder=0))
    ax.text(92, 100.5, "desired", ha="center", va="top", fontsize=8,
            color="#2e7d32", style="italic")

    ax.axhline(0, color=GRID, lw=0.7, zorder=1)

    # Unguarded reference (keeps benign fully, prevents no catastrophe).
    ax.scatter([100], [0], marker="X", s=70, color=GREY, zorder=4,
               label="unguarded (no guard)")
    ax.annotate("unguarded", xy=(100, 0), xytext=(97, 7), ha="right",
                fontsize=7.5, color=GREY, style="italic")

    # Lexical: catches catastrophe but disables benign at every tau.
    ax.scatter(lx, ly, marker="o", s=46, color=LEX, zorder=4,
               edgecolor=EDGE, linewidth=0.4,
               label="lexical (raw Jaccard)")
    for x, y, t in zip(lx, ly, lt):
        ax.annotate(rf"$\tau$={t}", xy=(x, y), xytext=(x + 1.5, y - 2.2),
                    fontsize=6.8, color=LEX)

    # Normalized: catches catastrophe AND keeps benign at every tau. The four
    # points cluster tightly at retention~=96-100%, so label the cluster once
    # instead of overlapping per-point tau tags.
    ax.scatter(nx, ny, marker="s", s=46, color=NORM, zorder=5,
               edgecolor=EDGE, linewidth=0.4,
               label="normalized (containment)")
    ax.annotate(r"all $\tau$ (0.10-0.50)", xy=(min(nx), sum(ny) / len(ny)),
                xytext=(min(nx) - 3, 79), ha="right", va="top",
                fontsize=7.0, color=NORM, style="italic",
                arrowprops=dict(arrowstyle="-", color=NORM, lw=0.6))

    ax.set_xlabel("Benign rule retained (\\%)")
    ax.set_ylabel("Catastrophic damage prevented (\\%)")
    ax.set_xlim(0, 108)
    ax.set_ylim(-6, 106)
    ax.set_title("Metric choice governs the benign/safety tradeoff")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="center left", fontsize=7.6, frameon=True, framealpha=0.92,
              edgecolor=EDGE)

    fig.tight_layout()
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"wrote {OUT}")
    print(f"  unguarded: CC fires={cc_unguarded:.0f}, SD damage={sd_dmg_unguarded:.1f}pp")
    print(f"  lexical    retention%={[round(v,1) for v in lx]}  dmg_prev%={[round(v,1) for v in ly]}")
    print(f"  normalized retention%={[round(v,1) for v in nx]}  dmg_prev%={[round(v,1) for v in ny]}")


if __name__ == "__main__":
    main()
