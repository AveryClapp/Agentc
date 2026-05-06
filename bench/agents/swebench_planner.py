"""SWE-bench planner reference agent.

Produces a short plan for resolving a code-repair task. Target per
``specs/optimizer.md``: savings ≥ 30%, accuracy floor 80% resolve rate.

Dataset: ``princeton-nlp/SWE-bench_Lite``. Drop a preprocessed fixture
at ``bench/fixtures/swebench_planner.json`` (see ``_runtime.load_tasks``
for shape). Without the fixture the agent runs against three
hand-authored synthetic tasks so the harness is end-to-end runnable.
"""

from __future__ import annotations

import agentc

from bench.agents._fixtures import SWEBENCH_PLANNER, SyntheticTask
from bench.agents._runtime import AgentResult, call_llm, run_all

AGENT_KEY = "swebench_planner"
SYSTEM = (
    "You are a senior software engineer. Given a bug description, outline "
    "a plan in 3-6 bullet points. Keep it terse; no code."
)


def _plan(task: SyntheticTask) -> str:
    with agentc.span("swebench.plan"):
        return call_llm(task.prompt, model="gpt-4o-mini-2024-07-18", system=SYSTEM)


@agentc.trace(name="swebench_planner")
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, SWEBENCH_PLANNER, _plan)


if __name__ == "__main__":
    agentc.init()
    try:
        for r in run():
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  {r.answer[:80]}")
    finally:
        agentc.shutdown()
