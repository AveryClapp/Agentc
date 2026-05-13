"""Multi-step research planner — realistic multi-agent workload.

A 3-step pipeline that mirrors how a retrieval-augmented research agent
naturally operates over a corpus of retrieved documents:

  Step 1 (filter):    Read all retrieved documents; identify the most
                      relevant passages for the question.
                      → state_write("filter_result", ...)
                      Target: ContextCompress (long retrieval dump, same
                      ~14 KB Wikipedia context as long_context_qa)

  Step 2 (synthesize): Compact the relevant passages into a draft answer.
                       → state_read("filter_result")
                       → state_write("synthesis", ...)

  Step 3 (answer):    Produce the final answer from the synthesis.
                      The raw filter result is passed down (common
                      framework pattern — orchestrator passes all prior
                      state), but is NOT state_read in this step.
                      → state_read("synthesis")
                      → filter_result in messages but out of window
                      Target: StateDrop (stale filter state pruned)

This agent is not designed to trigger specific rules. The 3-step
pipeline reflects a natural research agent design; the optimizer fires
on the structural properties it observes at runtime.

Fixture: long_context_qa.json — same Wikipedia retrieval corpus.
The multi-step framing is what distinguishes this from long_context_qa,
not the underlying data.
"""

from __future__ import annotations

import os
import re

import agentc

from typing import Any

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, run_all

AGENT_KEY = "research_planner"

FILTER_SYSTEM = (
    "You are a research assistant reviewing retrieved documents. "
    "Identify the 2-3 most relevant paragraphs for answering the question "
    "and quote their key sentences. Output only the relevant content, "
    "prefixed with each paragraph's title."
)

SYNTH_SYSTEM = (
    "You are a synthesis assistant. Given the relevant passages and the "
    "question, write a single-sentence draft answer. Output only the sentence."
)

ANSWER_SYSTEM = (
    "Answer the question in one word, name, number, or short phrase. "
    "Use only the provided synthesis. No explanation."
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(text).lower())).strip()


def _hotpot_check(answer: str, expected: Any) -> bool:
    if not isinstance(expected, str):
        return False
    a, e = _normalize(answer), _normalize(expected)
    if not e:
        return False
    return a == e or f" {e} " in f" {a} "


def _run_one(task: SyntheticTask) -> str:
    model = os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini-2024-07-18"
    client = llm_client()
    paragraphs = (task.meta or {}).get("paragraphs") or []

    # ── Step 1: filter retrieved documents ──────────────────────────────────
    # All paragraphs injected as separate user messages, the way a retrieval
    # framework would feed search results into an agent. Same ~14 KB context
    # as long_context_qa — ContextCompress target.
    with agentc.span("research.filter"):
        if client is None:
            gold = (task.meta or {}).get("gold_answer", "") or task.expected
            filter_result_str = f"[stub] relevant: {gold}"
        else:
            filter_msgs: list[dict[str, str]] = [
                {"role": "system", "content": FILTER_SYSTEM},
            ]
            for para in paragraphs:
                body = " ".join(para.get("sentences", []))
                filter_msgs.append(
                    {"role": "user", "content": f"{para['title']}\n{body}"}
                )
            filter_msgs.append(
                {"role": "user", "content": f"Question: {task.prompt}"}
            )
            r1 = client.chat.completions.create(
                model=model, messages=filter_msgs, temperature=0
            )
            filter_result_str = r1.choices[0].message.content or ""

        filter_result = agentc.state_write("filter_result", filter_result_str)

    # ── Step 2: synthesize relevant passages into a draft ───────────────────
    with agentc.span("research.synthesize"):
        filter_in_window = agentc.state_read("filter_result", filter_result)

        if client is None:
            synthesis_str = f"[stub synthesis] {task.expected}"
        else:
            synth_msgs: list[dict[str, str]] = [
                {"role": "system", "content": SYNTH_SYSTEM},
                {"role": "user", "content": filter_in_window},
                {"role": "user", "content": f"Question: {task.prompt}"},
            ]
            r2 = client.chat.completions.create(
                model=model, messages=synth_msgs, temperature=0
            )
            synthesis_str = r2.choices[0].message.content or ""

        synthesis = agentc.state_write("synthesis", synthesis_str)

    # ── Step 3: final answer from synthesis ─────────────────────────────────
    # filter_result included in messages (orchestrator passes all prior state
    # down the chain — common in LangChain/LangGraph-style pipelines).
    # NOT state_read here → key absent from window_state_reads → StateDrop.
    with agentc.span("research.answer"):
        synthesis_in_window = agentc.state_read("synthesis", synthesis)

        if client is None:
            return str(task.expected)

        answer_msgs: list[dict[str, str]] = [
            {"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user", "content": filter_result},   # state-tagged, out of window
            {"role": "user", "content": synthesis_in_window},
            {"role": "user", "content": f"Question: {task.prompt}"},
        ]
        r3 = client.chat.completions.create(
            model=model, messages=answer_msgs, temperature=0
        )
        return r3.choices[0].message.content or ""


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    # Load the same Wikipedia corpus as long_context_qa. The multi-step
    # research pipeline is what differs, not the underlying retrieval data.
    return run_all("long_context_qa", [], _run_one, check=_hotpot_check)


if __name__ == "__main__":
    agentc.init()
    try:
        results = run()
        passed = sum(1 for r in results if r.passed)
        print(f"\n{passed}/{len(results)} EM accuracy")
        for r in results:
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  gold={r.expected!r}  got={r.answer[:60]!r}")
    finally:
        agentc.shutdown()
