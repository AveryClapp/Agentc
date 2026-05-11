"""Iterative refiner — StateDrop negative control.

Identical chain to ``iterative_refiner`` except every prior revision is
explicitly ``state_read`` before being passed to the LLM. Because every
state write has a corresponding read in the same call window, the
StateDrop rule has no unread state to drop and should fire 0% of the time.

This is the structural precondition test: StateDrop fires on unread state,
not on message count or prompt length. If it fires here, the precondition
check is broken.

Expected results vs iterative_refiner:
  - StateDrop fire rate: 0% (all state is read)
  - Input-token savings: 0% (rule abstains correctly)
  - Accuracy: same as baseline (no mutations applied)
"""

from __future__ import annotations

import os

import agentc

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, run_all
from bench.agents.iterative_refiner import REFINER_SYSTEM, _NUM_STEPS, _SYNTHETIC

AGENT_KEY = "iterative_refiner_allread"


def _refine_step_allread(
    task_prompt: str,
    all_versions_read: list,
) -> str:
    """One refinement turn — ALL revisions are state_read in this window."""
    with agentc.span("refiner_allread.step"):
        model = os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini-2024-07-18"
        client = llm_client()
        if client is None:
            return f"[stub:{model}] {task_prompt}"

        messages: list[dict[str, str]] = [
            {"role": "system", "content": REFINER_SYSTEM},
            {"role": "user", "content": f"Task: {task_prompt}"},
        ]
        for v in all_versions_read:
            messages.append({"role": "user", "content": v})
        messages.append({"role": "user", "content": "Produce the next revision now."})

        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0
        )
        return resp.choices[0].message.content or ""


def _run_one(task: SyntheticTask) -> str:
    v0 = agentc.state_write("v0", f"Initial idea: {task.prompt}")
    versions: list[str] = [v0]

    for step in range(1, _NUM_STEPS + 1):
        # state_read EVERY revision — no unread state exists in this window.
        # SD has no candidates to drop.
        all_read = [
            agentc.state_read(f"v{i}", v)
            for i, v in enumerate(versions)
        ]

        new_revision = _refine_step_allread(task.prompt, all_read)
        tagged_new = agentc.state_write(f"v{step}", new_revision)
        versions.append(tagged_new)

    return versions[-1]


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, _SYNTHETIC, _run_one)


if __name__ == "__main__":
    agentc.init()
    try:
        results = run()
        passed = sum(1 for r in results if r.passed)
        print(f"\n{passed}/{len(results)} accuracy (substring match)")
        for r in results:
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  {r.answer[:60]}")
    finally:
        agentc.shutdown()
