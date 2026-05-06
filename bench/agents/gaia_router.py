"""GAIA router reference agent.

Routes a GAIA-style question through classify → answer. Two LLM calls
per task — the dual-call structure is what exercises the optimizer's
``ParallelBranch`` and ``StateDrop`` rules.

Target per ``specs/optimizer.md``: savings ≥ 35%, accuracy floor 69.0%.

Fixture file: ``bench/fixtures/gaia_router.json``; falls back to the
hand-authored synthetic tasks when absent.
"""

from __future__ import annotations

import agentc

from bench.agents._fixtures import GAIA_ROUTER, SyntheticTask
from bench.agents._runtime import AgentResult, call_llm, run_all

AGENT_KEY = "gaia_router"
CLASSIFIER_SYSTEM = (
    "Classify the user's question into one of: [factual, reasoning, "
    "multi-hop]. Reply with just the label."
)
ANSWER_SYSTEM = (
    "Answer the user's question concisely. Output only the answer, no "
    "prose around it."
)


@agentc.memoize(model="gpt-4o-mini-2024-07-18")
def _classify(prompt: str) -> str:
    with agentc.span("gaia.classify"):
        return call_llm(prompt, model="gpt-4o-mini-2024-07-18", system=CLASSIFIER_SYSTEM)


def _answer(prompt: str, category: str) -> str:
    with agentc.span("gaia.answer"):
        return call_llm(
            f"[class={category}] {prompt}",
            model="gpt-4o-mini-2024-07-18",
            system=ANSWER_SYSTEM,
        )


def _run_one(task: SyntheticTask) -> str:
    category = _classify(task.prompt)
    return _answer(task.prompt, category)


@agentc.trace(name="gaia_router")
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, GAIA_ROUTER, _run_one)


if __name__ == "__main__":
    agentc.init()
    try:
        for r in run():
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  {r.answer[:80]}")
    finally:
        agentc.shutdown()
