"""HotpotQA-distractor oracle agent — manual baseline for ContextCompress.

Identical to ``hotpot_qa`` except this agent drops every paragraph
labeled ``supporting=false`` *before* sending the call. The HotpotQA
dataset annotates which paragraphs contain the answer, so this agent
sees only the supporting context. It is the upper-bound manual
baseline ContextCompress competes against: how close can a learned
runtime rule come to dropping exactly what an oracle would drop.

Note: this leaks the gold supporting-paragraph labels into the
prompt, so accuracy here is an upper-bound, not a deployable system.
The point is the *cost* number — how cheap can the call be when only
the strictly necessary context is included — which is the fairest
comparison for ContextCompress.
"""

from __future__ import annotations

import os
import re

import agentc

from typing import Any

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, run_all

# Reuses the hotpot_distractor.json fixture (the dataset is the same;
# only the agent's prompt-construction differs).
AGENT_KEY = "hotpot_oracle"
FIXTURE_KEY = "hotpot_distractor"

ORACLE_SYSTEM = (
    "Answer the question using only the provided paragraphs. Output only "
    "the answer — a single word, name, number, or short phrase. No "
    "explanation."
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(text).lower())).strip()


def _hotpot_check(answer: str, expected: Any) -> bool:
    if not isinstance(expected, str):
        return False
    a, e = _normalize(answer), _normalize(expected)
    if not e:
        return False
    if a == e:
        return True
    return f" {e} " in f" {a} "


def _build_messages(task: SyntheticTask) -> list[dict[str, str]]:
    paragraphs = (task.meta or {}).get("paragraphs") or []
    # Oracle filter: only supporting paragraphs survive. Distractors are
    # what ContextCompress would (ideally) learn to drop.
    supporting = [p for p in paragraphs if p.get("supporting")]
    messages: list[dict[str, str]] = [{"role": "system", "content": ORACLE_SYSTEM}]
    for para in supporting:
        joined = " ".join(para.get("sentences", []))
        messages.append({"role": "user", "content": f"{para['title']}\n{joined}"})
    messages.append({"role": "user", "content": f"Question: {task.prompt}"})
    return messages


def _run_one(task: SyntheticTask) -> str:
    with agentc.span("hotpot_oracle.answer"):
        model = os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini"
        client = llm_client()
        if client is None:
            gold = (task.meta or {}).get("gold_answer", "") or task.expected
            return f"[stub:{model}] {gold}"
        messages = _build_messages(task)
        resp = client.chat.completions.create(model=model, messages=messages)
        return resp.choices[0].message.content or ""


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    return run_all(FIXTURE_KEY, [], _run_one, check=_hotpot_check)


if __name__ == "__main__":
    agentc.init()
    try:
        results = run()
        passed = sum(1 for r in results if r.passed)
        print(f"\n{passed}/{len(results)} EM accuracy (oracle: supporting-only context)")
        for r in results:
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  gold={r.expected!r}  got={r.answer[:60]!r}")
    finally:
        agentc.shutdown()
