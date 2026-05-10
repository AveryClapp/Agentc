"""AutoGen bridge — activation probe for Agentc rules on multi-agent traces.

Phase 0 version: simulates a 2-agent ReAct trace (assistant + tool_caller)
using the existing long_context_qa fixture. Does NOT require AutoGen installed.
A later version will hook into real AutoGen conversations.

Simulated trace structure:
  Step 1 (autogen.reason): long-context reasoning with a structured tool
    result injected as state. Target for ContextCompress (long doc) and
    StateDrop (tool_result not re-read in step 2). The output of step 1
    is NOT forwarded to step 2 — dead output pattern.
  Step 2 (autogen.answer): long-context answer, same doc, same question.
    The user message references only the "label" key from the tool output,
    making raw_text / debug_trace unreferenced — StructuredTruncation target.

Outputs:
  - Per-task pass/fail
  - Accuracy summary
  - Pointer to optimizer audit DB for rule activation rates

Run (stub mode, no API key):
    BENCH_MAX_TASKS=5 python -m bench.agents.autogen_bridge

Run (live with activation logging):
    BENCH_MAX_TASKS=20 AGENTC_OPTIMIZE=1 python -m bench.agents.autogen_bridge
    sqlite3 ~/.agentc/optimizer_audit.db \\
      "SELECT rule, COUNT(*) FROM plan_audit GROUP BY rule"
"""

from __future__ import annotations

import json
import os

import agentc

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, load_tasks

AGENT_KEY = "autogen_bridge"

# Structured tool result injected in step 1.
# Only "label" is referenced in step 2 — raw_text and debug_trace are
# deliberate padding to activate StructuredTruncation when it exists.
_FAKE_TOOL_JSON = json.dumps({
    "result": "found",
    "entity": "PLACEHOLDER",
    "score": 0.97,
    "raw_text": "X" * 600,
    "debug_trace": "Y" * 400,
    "label": "answer",
})


def _model() -> str:
    return os.environ.get("BENCH_BASELINE_MODEL", "gpt-4o-mini-2024-07-18")


def _doc_messages(task: SyntheticTask) -> list[dict[str, str]]:
    paragraphs = (task.meta or {}).get("paragraphs") or []
    return [
        {
            "role": "user",
            "content": f"{p['title']}\n{' '.join(p.get('sentences', []))}",
        }
        for p in paragraphs
    ]


def _run_one(task: SyntheticTask) -> str:
    client = llm_client()

    # Step 1: ReAct reasoning over the long document + tool result.
    # Output is NOT forwarded to step 2 — this is the dead-output pattern
    # that DeadOutputTruncation will eventually fire on.
    with agentc.span("autogen.reason"):
        tool_tagged = agentc.state_write("tool_result", _FAKE_TOOL_JSON)
        messages_1: list[dict[str, str]] = [
            {"role": "system", "content": "You are a reasoning agent. Think step by step."},
            *_doc_messages(task),
            {"role": "user", "content": tool_tagged},
            {"role": "user", "content": f"Reason about: {task.prompt}"},
        ]
        if client:
            r1 = client.chat.completions.create(
                model=_model(), messages=messages_1, temperature=0
            )
            _reasoning = r1.choices[0].message.content or ""
        else:
            _reasoning = f"[stub reasoning for: {task.prompt[:40]}]"

    # Step 2: Final answer.  Same long document — ContextCompress target.
    # tool_result is NOT state_read here, so its state tag becomes a
    # StateDrop candidate once the call site is hot.
    # "label" is the only key from the tool JSON referenced here.
    with agentc.span("autogen.answer"):
        messages_2: list[dict[str, str]] = [
            {"role": "system", "content": "Answer concisely using only the context. One sentence."},
            *_doc_messages(task),
            {"role": "user", "content": f"Question: {task.prompt}"},
        ]
        if client:
            r2 = client.chat.completions.create(
                model=_model(), messages=messages_2, temperature=0
            )
            return r2.choices[0].message.content or ""
        else:
            gold = (task.meta or {}).get("gold_answer") or task.expected
            return f"[stub] {gold}"


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    tasks = load_tasks("long_context_qa", [])
    cap = int(os.environ.get("BENCH_MAX_TASKS", str(len(tasks))))
    results: list[AgentResult] = []
    for t in tasks[:cap]:
        answer = _run_one(t)
        passed = str(t.expected).lower() in answer.lower()
        results.append(AgentResult(t.task_id, answer, passed, t.expected))
    return results


if __name__ == "__main__":
    agentc.init()
    try:
        results = run()
        passed = sum(1 for r in results if r.passed)
        print(f"\n{passed}/{len(results)} accuracy")
        for r in results:
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  gold={r.expected!r}  got={r.answer[:60]!r}")
        print("\n--- V1 Rule Activation ---")
        print("Rerun with AGENTC_OPTIMIZE=1 to log rule activations, then:")
        print('  sqlite3 ~/.agentc/optimizer_audit.db "SELECT rule, COUNT(*) FROM plan_audit GROUP BY rule"')
    finally:
        agentc.shutdown()
