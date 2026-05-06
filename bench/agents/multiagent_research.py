"""Multi-agent research reference agent.

Three cooperating roles in a ReAct-shaped loop: a researcher produces
bullet notes, a critic reviews them, and a refiner produces the final
paragraph from history. Inter-agent messages carry provenance tags
(``LlmOutput``, ``State``) via the optimizer glue so ``ParallelBranch``
and ``StateDrop`` can both reason about the workload.

Targets per ``specs/optimizer.md``: savings ≥ 25%, accuracy floor
7.1/10 quality. Synthetic fixtures fall back to substring-match; the
real quality scorer lives in the ship-gate runner.

State tagging: notes and critique are both explicitly declared as
dependencies of the refiner call via ``state_read``, so StateDrop keeps
both. This workload is too short (3 steps) for StateDrop to fire
meaningfully — the rule is validated on longer-horizon pipelines where
early state becomes genuinely stale.
"""

from __future__ import annotations

import os

import agentc

from bench.agents._fixtures import MULTIAGENT_RESEARCH, SyntheticTask
from bench.agents._runtime import AgentResult, call_llm, llm_client, run_all

AGENT_KEY = "multiagent_research"
RESEARCHER_SYSTEM = (
    "You are a researcher. Given a topic, produce 3-4 factual bullet "
    "points. No prose."
)
CRITIC_SYSTEM = (
    "You are a critic. Given research notes, identify the single most "
    "important gap or correction in one short sentence. No prose around it."
)
REFINER_SYSTEM = (
    "You are a writer. Given conversation history with research notes "
    "and a critic's correction, produce one paragraph that synthesizes "
    "them. Output only the paragraph."
)


@agentc.memoize(model="gpt-4o-mini-2024-07-18")
def _research(prompt: str) -> str:
    with agentc.span("multi.researcher"):
        return call_llm(prompt, model="gpt-4o-mini-2024-07-18", system=RESEARCHER_SYSTEM)


def _critique(notes: str) -> str:
    with agentc.span("multi.critic"):
        return call_llm(notes, model="gpt-4o-mini-2024-07-18", system=CRITIC_SYSTEM)


def _refine(notes_msg: str, critique_msg: str, final_prompt: str) -> str:
    """Final pass — sees notes + critique as history, then a fresh user
    prompt. ``notes_msg`` and ``critique_msg`` are pre-tagged with
    ``State`` provenance by the caller; the SDK interceptor reads those
    tags via object identity, so they must reach this call as the same
    objects (no string concatenation, no f-string interpolation)."""
    with agentc.span("multi.refiner"):
        # Mirror ``call_llm`` so the ablation's ``BENCH_BASELINE_MODEL``
        # override flows through here too.
        model = os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini-2024-07-18"

        client = llm_client()
        messages = [
            {"role": "system", "content": REFINER_SYSTEM},
            {"role": "user", "content": notes_msg},
            {"role": "user", "content": critique_msg},
            {"role": "user", "content": final_prompt},
        ]
        if client is None:
            # Stub: keep the substring expected by fixtures (the topic
            # itself) so accuracy_delta_pp is interpretable in stub runs.
            return f"[stub:{model}] {final_prompt}"
        resp = client.chat.completions.create(model=model, messages=messages)
        return resp.choices[0].message.content or ""


def _run_one(task: SyntheticTask) -> str:
    notes = agentc.state_write("notes", _research(task.prompt))
    critique = agentc.state_write(
        "critique", _critique(agentc.state_read("notes", notes))
    )
    # Explicitly re-read notes so StateDrop sees window={"notes","critique"}
    # at refiner time and keeps both messages. The refiner genuinely needs
    # both the research content and the critic's correction.
    refined = _refine(
        agentc.state_read("notes", notes),
        agentc.state_read("critique", critique),
        f"Compose the final paragraph for: {task.prompt}",
    )
    return refined


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
