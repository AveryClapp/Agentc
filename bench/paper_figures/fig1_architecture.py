"""Figure 1: Agentc system architecture (graphviz, left-to-right).

Pipeline: Agent Code → SDK Interceptor → DAG Builder/Provenance Tagger →
Optimizer (with three numbered sub-steps) → Executor (with four plan types)
→ LLM Provider. A dashed feedback edge from LLM Provider back to Optimizer
labeled "optimize_observe()". A separate dashed-border subgraph cluster
labeled "DepSource" lists the five provenance tags.
"""

from pathlib import Path

from graphviz import Digraph

OUT_DIR = Path(__file__).resolve().parent
OUT_NAME = "fig1_architecture"


def main() -> None:
    g = Digraph("arch", format="pdf")
    g.attr(
        rankdir="LR",
        fontname="serif",
        fontsize="10",
        nodesep="0.35",
        ranksep="0.55",
        bgcolor="white",
    )
    g.attr(
        "node",
        shape="box",
        style="rounded",
        fontname="serif",
        fontsize="10",
        margin="0.15,0.10",
    )
    g.attr("edge", fontname="serif", fontsize="9")

    # Main pipeline nodes.
    g.node("agent", "Agent Code")
    g.node("sdk", "SDK Interceptor")
    g.node("dag", "DAG Builder\nProvenance Tagger")

    # Optimizer: HTML table with title + three numbered sub-steps.
    opt_label = (
        '<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="3" CELLPADDING="2">'
        '<TR><TD ALIGN="CENTER"><B>Optimizer</B></TD></TR>'
        '<TR><TD ALIGN="LEFT">1. CallSiteProfile + hot_threshold gate</TD></TR>'
        '<TR><TD ALIGN="LEFT">2. applies() filter (all enabled rules)</TD></TR>'
        '<TR><TD ALIGN="LEFT">3. propose() + safety_check &#8594; winning Plan</TD></TR>'
        "</TABLE>>"
    )
    g.node(
        "opt",
        label=opt_label,
        shape="box",
        style="rounded,filled",
        fillcolor="#f0f0f0",
    )

    # Executor: HTML table with title + four plan types.
    exec_label = (
        '<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="3" CELLPADDING="2">'
        '<TR><TD ALIGN="CENTER"><B>Executor</B></TD></TR>'
        '<TR><TD ALIGN="CENTER">Plan::Cached | Plan::Rewritten</TD></TR>'
        '<TR><TD ALIGN="CENTER">Plan::Parallel | Plan::PassThrough</TD></TR>'
        "</TABLE>>"
    )
    g.node("exec", label=exec_label)

    g.node("llm", "LLM Provider")

    # Forward edges.
    g.edge("agent", "sdk")
    g.edge("sdk", "dag")
    g.edge("dag", "opt")
    g.edge("opt", "exec")
    g.edge("exec", "llm")

    # Feedback edge: LLM Provider back to Optimizer.
    g.edge(
        "llm", "opt",
        label="optimize_observe() → cost model",
        style="dashed",
        constraint="false",
    )

    # DepSource cluster: dashed-border subgraph with the five tags.
    with g.subgraph(name="cluster_depsource") as c:
        c.attr(
            label="DepSource",
            style="dashed",
            fontname="serif",
            fontsize="10",
            color="gray40",
            fontcolor="gray20",
            margin="10",
        )
        c.attr("node", shape="plaintext", fontname="monospace",
               fontsize="9", margin="0.02,0.02")
        for tag in ["Literal", "UserInput", "ToolOutput",
                    "LlmOutput", "State"]:
            c.node(f"dep_{tag.lower()}", tag)
        # Stack tags vertically inside the cluster.
        c.edge("dep_literal", "dep_userinput", style="invis")
        c.edge("dep_userinput", "dep_tooloutput", style="invis")
        c.edge("dep_tooloutput", "dep_llmoutput", style="invis")
        c.edge("dep_llmoutput", "dep_state", style="invis")

    g.render(
        filename=OUT_NAME,
        directory=str(OUT_DIR),
        cleanup=True,
    )
    out = OUT_DIR / f"{OUT_NAME}.pdf"
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
