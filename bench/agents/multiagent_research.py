"""Multi-agent research reference agent.

Two cooperating agents: a researcher that produces bullet notes, and a
writer that turns the notes into a paragraph. This is the adapter
integration workload — inter-agent messages carry ``LlmOutput`` tags
via the autogen/crewai adapter so ``ParallelBranch`` can reason about
researcher spawns.

Target per ``specs/optimizer.md``: savings ≥ 25%, accuracy floor 7.1/10
quality. Synthetic fixtures fall back to substring-match; the real
quality scorer lives in the ship-gate runner.
"""

from __future__ import annotations

import agentc

from bench.agents._fixtures import MULTIAGENT_RESEARCH, SyntheticTask
from bench.agents._runtime import AgentResult, call_llm, run_all

AGENT_KEY = "multiagent_research"
RESEARCHER_SYSTEM = (
    "You are a researcher. Given a topic, produce 3-4 factual bullet "
    "points. No prose."
)
WRITER_SYSTEM = (
    "You are a writer. Given bullet points, produce one paragraph that "
    "synthesizes them. Output only the paragraph."
)


def _research(prompt: str) -> str:
    with agentc.span("multi.researcher"):
        return call_llm(prompt, model="gpt-4o-mini", system=RESEARCHER_SYSTEM)


def _write(notes: str) -> str:
    with agentc.span("multi.writer"):
        return call_llm(notes, model="gpt-4o-mini", system=WRITER_SYSTEM)


def _run_one(task: SyntheticTask) -> str:
    notes = _research(task.prompt)
    return _write(notes)


@agentc.trace(name="multiagent_research")
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, MULTIAGENT_RESEARCH, _run_one)


if __name__ == "__main__":
    agentc.init()
    try:
        for r in run():
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  {r.answer[:80]}")
    finally:
        agentc.shutdown()
