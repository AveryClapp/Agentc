"""Figure 2: Two-gate rule pipeline (graphviz, top-to-bottom).

Vertical flow: Call arrives → applies() (cheap gate) → propose() (expensive
projection) → rank by projected_savings_usd → safety_check() → final Plan.
A dashed gray reject edge runs from applies() to a PassThrough fast-path
node. Two plaintext annotation nodes label the cheap gate and expensive
projection on the left side.
"""

from pathlib import Path

from graphviz import Digraph

OUT_DIR = Path(__file__).resolve().parent
OUT_NAME = "fig2_twogatepipeline"

GREY_FILL = "#e8e8e8"
GREY_TEXT = "gray35"


def main() -> None:
    g = Digraph("twogate", format="pdf")
    g.attr(
        rankdir="TB",
        fontname="serif",
        fontsize="10",
        nodesep="0.45",
        ranksep="0.45",
        bgcolor="white",
    )
    g.attr(
        "node",
        shape="box",
        style="rounded",
        fontname="serif",
        fontsize="10",
        margin="0.18,0.12",
    )
    g.attr("edge", fontname="serif", fontsize="9")

    # Main flow nodes.
    g.node("call", "Call arrives")
    g.node("applies", "applies()\n(cheap: byte count, fields present)")
    g.node(
        "passthru", "PassThrough",
        style="rounded,filled", fillcolor=GREY_FILL,
    )
    g.node("propose", "propose()\n(expensive: IDF attention, cost projection)")
    g.node("rank", "rank by\nprojected_savings_usd")
    g.node("safety", "safety_check()")
    g.node(
        "plan",
        "Plan: Cached | Rewritten\nParallel | PassThrough",
        style="rounded,filled", fillcolor=GREY_FILL,
    )

    # Annotation nodes (no border) for the side labels.
    g.node(
        "anno_cheap", "cheap gate",
        shape="plaintext", fontcolor=GREY_TEXT, fontsize="9",
    )
    g.node(
        "anno_exp", "expensive\nprojection",
        shape="plaintext", fontcolor=GREY_TEXT, fontsize="9",
    )

    # Main forward flow.
    g.edge("call", "applies")
    g.edge("applies", "propose", label="passes")
    g.edge("propose", "rank", label="proposals")
    g.edge("rank", "safety")
    g.edge("safety", "plan", label="passes")

    # Reject path → PassThrough (dashed gray).
    g.edge(
        "applies", "passthru",
        label="reject",
        style="dashed", color="gray", fontcolor="gray35",
    )

    # Pin the annotation nodes to the same horizontal level as the gates.
    with g.subgraph() as s:
        s.attr(rank="same")
        s.node("anno_cheap")
        s.node("applies")
    with g.subgraph() as s:
        s.attr(rank="same")
        s.node("anno_exp")
        s.node("propose")

    # Visible dashed connectors from each annotation to the gate it labels.
    # arrowhead=none + dashed reads as an unambiguous "label points here"
    # without adding an arrow that would suggest a flow step. Constraint
    # is disabled so these edges don't perturb the main vertical layout.
    g.edge(
        "anno_cheap", "applies",
        style="dashed", arrowhead="none",
        color="gray", constraint="false",
    )
    g.edge(
        "anno_exp", "propose",
        style="dashed", arrowhead="none",
        color="gray", constraint="false",
    )

    g.render(
        filename=OUT_NAME,
        directory=str(OUT_DIR),
        cleanup=True,
    )
    out = OUT_DIR / f"{OUT_NAME}.pdf"
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
