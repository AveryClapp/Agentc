"""Composition QA — purpose-built multi-rule workload (EXP-007).

Designed to fire: ContextCompress + StateDrop + OutputBudget + StructuredTruncation.

- ContextCompress: long context (20 paragraphs, >8KB), most paragraphs are
  distractors → IDF proxy drops them.
- StateDrop: prior revision tagged State(v0), not re-read → dropped.
- OutputBudget: no max_tokens cap → after warmup, rule applies p99 cap.
- StructuredTruncation: JSON tool output with 4 keys; only "label" referenced
  in the user message → other 3 keys are projected out.
"""
from __future__ import annotations

import json
import os
from typing import Any

import agentc
from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, load_tasks, run_all

AGENT_KEY = "composition_qa"
_TOOL_JSON = json.dumps({
    "entity": "PLACEHOLDER",
    "score": 0.95,
    "metadata": "M" * 400,   # irrelevant, not referenced
    "debug": "D" * 300,      # irrelevant
    "label": "answer_key",   # this IS referenced: "label" appears in user msg
})


def _run_one(task: SyntheticTask) -> str:
    client = llm_client()
    model = os.environ.get("BENCH_BASELINE_MODEL", "gpt-4o-mini-2024-07-18")
    paragraphs = (task.meta or {}).get("paragraphs") or []

    with agentc.span("composition_qa"):
        if client is None:
            gold = (task.meta or {}).get("gold_answer") or task.expected
            return f"[stub] {gold}"

        # Build messages: system + 20 paragraphs + tool output (State-tagged)
        # + prior revision (State-tagged, not re-read) + question
        messages: list[dict[str, Any]] = [
            {"role": "system",
             "content": "Answer using the context. Output only the answer, no explanation."}
        ]
        for para in paragraphs:
            messages.append({
                "role": "user",
                "content": f"{para['title']}\n{' '.join(para.get('sentences', []))}",
            })

        tool_tagged = agentc.state_write("tool_result", _TOOL_JSON)
        messages.append({"role": "tool", "content": tool_tagged})

        # Prior revision (State-tagged, creates StateDrop opportunity on 2nd+ call)
        if hasattr(task, "_prev_answer") and task._prev_answer:
            messages.append({"role": "user", "content": task._prev_answer})

        messages.append({
            "role": "user",
            "content": f"Question: {task.prompt} Use the label from the tool output.",
        })

        # No max_tokens → OutputBudget fires after warmup
        resp = client.chat.completions.create(model=model, messages=messages, temperature=0)
        return resp.choices[0].message.content or ""


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, [], _run_one)


if __name__ == "__main__":
    agentc.init()
    try:
        results = run()
        passed = sum(r.passed for r in results)
        print(f"\n{passed}/{len(results)} passed")
        for r in results:
            print(
                f"{'PASS' if r.passed else 'FAIL'}  {r.task_id}  "
                f"gold={r.expected!r}  got={r.answer[:60]!r}"
            )
    finally:
        agentc.shutdown()
