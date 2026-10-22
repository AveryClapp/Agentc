"""Complete-plan request control loop; mechanism diagram, no result data."""

from pathlib import Path
import shutil
import subprocess
import sys

from graphviz import Digraph


def main() -> None:
    graph = Digraph("joint_request_control")
    graph.attr(rankdir="LR", nodesep="0.25", ranksep="0.35", bgcolor="white")
    graph.attr(
        "node",
        shape="box",
        style="rounded",
        fontname="Times New Roman",
        fontsize="14",
        margin="0.10,0.08",
    )
    graph.attr("edge", fontname="Times New Roman", fontsize="11")
    graph.node("app", "Agent call\nSDK / HTTP")
    graph.node("plans", "Candidates\nModel + rewrites\nCompatibility")
    graph.node(
        "select",
        "Admission\nExact-plan evidence\nGuard + benefit",
        style="rounded,filled",
        fillcolor="#e4efef",
    )
    graph.node("dispatch", "Dispatch\nReference or plan\nResponse to agent")
    graph.edge("app", "plans")
    graph.edge("plans", "select")
    graph.edge("select", "dispatch")
    graph.node("leases", "Cold leases\nReference visible")
    graph.node("profiles", "Profiles\nUsage + paired divergence\nDurable exposure")
    graph.edge("plans", "leases", style="dashed")
    graph.edge("leases", "profiles", style="dashed")
    graph.edge("dispatch", "profiles", style="dashed")
    graph.edge("profiles", "select", style="dashed", constraint="false")
    with graph.subgraph() as lower:
        lower.attr(rank="same")
        lower.node("leases")
        lower.node("plans")
    with graph.subgraph() as lower:
        lower.attr(rank="same")
        lower.node("profiles")
        lower.node("select")

    here = Path(__file__).resolve().parent
    output = here / "fig_joint_architecture.pdf"
    target = "pdf:quartz:quartz" if sys.platform == "darwin" else "pdf"
    subprocess.run(
        ["dot", f"-T{target}", "-o", str(output)],
        input=graph.source,
        text=True,
        check=True,
    )
    shutil.copyfile(output, here.parents[1] / "figures" / output.name)


if __name__ == "__main__":
    main()
