"""StateDrop generalization probe — a second, different-domain agent that
violates StateDrop's read-window assumption.

Domain: SQuAD reading comprehension (squad_qa.json) — a different task type and
corpus from the HotpotQA-derived fixtures used by research_planner. Answers are
spans grounded in a provided context paragraph, so an agent that loses the
context cannot answer; baseline accuracy is high, giving headroom to detect
degradation.

Structure: extract -> classify -> answer.
  Step 1 (extract):  pull the context sentences relevant to the question.
                     -> state_write("context_facts", ...)
  Step 2 (classify): label the question type (a side task on the question only).
                     -> state_write("qtype", ...)
  Step 3 (answer):   state_read("qtype") only. context_facts is forwarded into
                     the prompt (orchestrator passes prior state down the chain)
                     but is NOT state_read, so its key is absent from
                     window_state_reads -> StateDrop drops it. Since the answer
                     lives only in context_facts, dropping it removes the
                     answer-bearing content.

Same read-window violation as research_planner, on a different domain/agent, to
test whether StateDrop's accuracy degradation is a fixture-specific quirk or a
reproducible failure mode of the heuristic.
"""

from __future__ import annotations

import os
import re

import agentc

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, run_all

AGENT_KEY = "analyst_qa"

EXTRACT_SYSTEM = (
    "Extract verbatim the sentence(s) from the context that are relevant to "
    "answering the question. Output only those sentences."
)
CLASSIFY_SYSTEM = (
    "Classify the question into one word: who, what, when, where, which, "
    "number, or other. Output only the label."
)
ANSWER_SYSTEM = (
    "Answer the question with the shortest exact span (a word, name, number, or "
    "phrase) supported by the provided context. Output only the answer."
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(text).lower())).strip()


def _squad_check(answer: str, expected) -> bool:
    if not isinstance(expected, str):
        return False
    a, e = _normalize(answer), _normalize(expected)
    if not e:
        return False
    return e in a or a in e


def _run_one(task: SyntheticTask) -> str:
    model = os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini-2024-07-18"
    client = llm_client()
    if client is None:
        return str(task.expected)
    context = (task.meta or {}).get("context", "")

    # Step 1: extract relevant context -> state_write("context_facts")
    with agentc.span("analyst.extract"):
        e_msgs = [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": f"Context:\n{context}"},
            {"role": "user", "content": f"Question: {task.prompt}"},
        ]
        r1 = client.chat.completions.create(model=model, messages=e_msgs, temperature=0)
        facts_str = r1.choices[0].message.content or ""
        context_facts = agentc.state_write("context_facts", facts_str)

    # Step 2: classify question type -> state_write("qtype") (decoy read key)
    with agentc.span("analyst.classify"):
        c_msgs = [
            {"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": f"Question: {task.prompt}"},
        ]
        r2 = client.chat.completions.create(model=model, messages=c_msgs, temperature=0)
        qtype_str = r2.choices[0].message.content or ""
        qtype = agentc.state_write("qtype", qtype_str)

    # Step 3: answer -> state_read("qtype") only; context_facts forwarded but
    # NOT read (key absent from window_state_reads) -> StateDrop candidate.
    with agentc.span("analyst.answer"):
        qtype_in_window = agentc.state_read("qtype", qtype)
        a_msgs = [
            {"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user", "content": context_facts},     # state-tagged, out of window
            {"role": "user", "content": f"(question type: {qtype_in_window})"},
            {"role": "user", "content": f"Question: {task.prompt}"},
        ]
        r3 = client.chat.completions.create(model=model, messages=a_msgs, temperature=0)
        return r3.choices[0].message.content or ""


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    return run_all("squad_qa", [], _run_one, check=_squad_check)


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
