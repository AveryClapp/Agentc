"""Figure 7: complete optimizer-call scaling by request size and concurrency.

The source is the committed Stage E0 artifact produced by
``bench.optimizer_e2e_scaling``. The figure shows the admitted joint-rewrite
path because it is the production path with the most planning work. Values are
pooled order statistics across five randomized 1,024-call replications per
cell. The JSON retains replication-level intervals and the guarded-reference
control.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt


plt.rcParams.update(
    {
        "font.family": "serif",
        "font.size": 8,
        "axes.labelsize": 8,
        "axes.titlesize": 9,
        "legend.fontsize": 7.5,
        "mathtext.fontset": "cm",
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "axes.linewidth": 0.7,
    }
)

OUT = Path(__file__).resolve().parent / "fig7_overhead_scaling.pdf"
DATA = (
    Path(__file__).resolve().parents[1]
    / "repro"
    / "optimizer-e2e-scaling-2026-09-04.json"
)

COLORS = {1: "#174a52", 8: "#4f7f86", 32: "#b45f3c"}
MARKERS = {1: "o", 8: "s", 32: "^"}
GRID = "#9a9a9a"
REFERENCE = "#8e3b46"


def _load_rows() -> list[dict[str, Any]]:
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    if payload.get("paper_evidence") is not False:
        raise RuntimeError("Figure 7 expects the explicitly diagnostic Stage E0 run")
    rows = [
        row
        for row in payload["aggregate_measurements_us_and_throughput"]
        if row["scenario"] == "joint_admitted_rewrite"
    ]
    if len(rows) != 15:
        raise RuntimeError(
            f"expected 15 admitted-rewrite matrix cells, found {len(rows)}"
        )
    return rows


def main() -> None:
    rows = _load_rows()
    sizes = sorted({int(row["target_call_json_bytes"]) // 1_024 for row in rows})
    concurrencies = sorted({int(row["concurrency"]) for row in rows})

    fig, axes = plt.subplots(1, 2, figsize=(5.5, 2.75), sharex=True, sharey=True)
    for axis, percentile in zip(axes, ("p50_us", "p99_us"), strict=True):
        for concurrency in concurrencies:
            by_size = {
                int(row["target_call_json_bytes"]) // 1_024: row
                for row in rows
                if int(row["concurrency"]) == concurrency
            }
            latency_ms = [
                float(by_size[size]["e2e"][percentile]) / 1_000 for size in sizes
            ]
            axis.plot(
                range(len(sizes)),
                latency_ms,
                marker=MARKERS[concurrency],
                color=COLORS[concurrency],
                linewidth=1.35,
                markersize=4.2,
                label=(
                    f"{concurrency} caller"
                    if concurrency == 1
                    else f"{concurrency} callers"
                ),
                zorder=3,
            )
        axis.axhline(
            1.0,
            color=REFERENCE,
            alpha=0.7,
            linewidth=0.75,
            linestyle="--",
            zorder=1,
        )
        axis.set_yscale("log")
        axis.set_ylim(0.08, 100)
        axis.set_xticks(range(len(sizes)))
        axis.set_xticklabels([str(size) for size in sizes])
        axis.set_xlabel("Serialized call size (KiB)")
        axis.grid(axis="y", which="major", color=GRID, alpha=0.28, linewidth=0.5)
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)

    axes[0].set_title("Median (p50)")
    axes[1].set_title("Tail (p99)")
    axes[0].set_ylabel("Complete-call latency (ms, log scale)")
    axes[0].text(
        0.03,
        1.08,
        "1 ms",
        color=REFERENCE,
        fontsize=7,
        transform=axes[0].get_yaxis_transform(),
    )
    axes[0].legend(loc="upper left", frameon=False, ncol=1)

    fig.tight_layout(w_pad=1.0)
    fig.savefig(OUT, format="pdf", dpi=300, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
