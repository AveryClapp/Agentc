"""Long-context QA — purpose-built ContextCompress rule benchmark.

Identical agent shape to ``hotpot_qa`` but loads a richer fixture:
``long_context_qa.json`` (built by ``build_long_context_fixture.py``)
contains ~20 paragraphs per task, yielding ~13-18 KB prompts that
clear the ContextCompress 8 KB activation gate by a wide margin.

Distractor paragraphs come from other tasks in the pool, so the
question's content tokens almost never appear in them — exactly the
signal the IDF-weighted attention proxy was designed to detect.

Expected: 30-50% input-token savings (the proxy drops most distractor
paragraphs); <=2pp accuracy delta (the supporting paragraphs survive
because their content words overlap heavily with the question).
"""

from __future__ import annotations

import os
import re

import agentc

from typing import Any

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, run_all

AGENT_KEY = "long_context_qa"

LONG_SYSTEM = (
    "Answer the question using only the provided paragraphs. Output only "
    "the answer — a single word, name, number, or short phrase. No "
    "explanation."
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(text).lower())).strip()


def _hotpot_check(answer: str, expected: Any) -> bool:
    """Same EM-with-tolerance scorer as hotpot_qa."""
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
    messages: list[dict[str, str]] = [{"role": "system", "content": LONG_SYSTEM}]
    for para in paragraphs:
        joined = " ".join(para.get("sentences", []))
        messages.append({"role": "user", "content": f"{para['title']}\n{joined}"})
    messages.append({"role": "user", "content": f"Question: {task.prompt}"})
    return messages


def _run_one(task: SyntheticTask) -> str:
    with agentc.span("long_context.answer"):
        model = os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini-2024-07-18"
        client = llm_client()
        if client is None:
            gold = (task.meta or {}).get("gold_answer", "") or task.expected
            return f"[stub:{model}] {gold}"
        messages = _build_messages(task)
        resp = client.chat.completions.create(model=model, messages=messages)
        return resp.choices[0].message.content or ""


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, [], _run_one, check=_hotpot_check)


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
