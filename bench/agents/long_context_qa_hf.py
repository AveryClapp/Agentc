"""Long-context QA — Hugging Face Inference API (OpenAI-compat) variant.

Uses ``https://router.huggingface.co/v1/`` with the OpenAI SDK so the
existing OpenAI patch intercepts calls automatically. Model names use
the HF canonical form without the "Meta-" prefix.

Key differences from the OpenAI variant:
  1. Uses ``openai_compat_client()`` pointed at the HF router.
  2. Default model: ``meta-llama/Llama-3.3-70B-Instruct`` (70B baseline).
  3. ModelDowngrade route: 70B → ``meta-llama/Llama-3.1-8B-Instruct``.
  4. ContextCompress CAN fire (multi-message format, same as OpenAI variant).

Expected results:
  - ContextCompress: ~30-40% input-token savings (same fixture/rule).
  - ModelDowngrade (70B → 8B): ~92% cost savings when rule warms up.
  - Accuracy: lower than GPT variants due to smaller open-source models.
"""

from __future__ import annotations

import os
import re
from typing import Any

import agentc

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, openai_compat_client, run_all

AGENT_KEY = "long_context_qa_hf"

HF_BASE_URL = "https://router.huggingface.co/v1/"

SYSTEM = (
    "Answer the question using only the provided paragraphs. Output only "
    "the answer — a single word, name, number, or short phrase. No explanation."
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
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM}]
    for para in paragraphs:
        joined = " ".join(para.get("sentences", []))
        messages.append({"role": "user", "content": f"{para['title']}\n{joined}"})
    messages.append({"role": "user", "content": f"Question: {task.prompt}"})
    return messages


def _run_one(task: SyntheticTask) -> str:
    with agentc.span("long_context_qa_hf.answer"):
        model = os.environ.get("BENCH_BASELINE_MODEL") or "meta-llama/Llama-3.3-70B-Instruct"
        api_key = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY") or ""
        client = openai_compat_client(HF_BASE_URL, api_key) if api_key else None
        if client is None:
            gold = (task.meta or {}).get("gold_answer", "") or task.expected
            return f"[stub:{model}] {gold}"
        messages = _build_messages(task)
        resp = client.chat.completions.create(model=model, messages=messages)
        return resp.choices[0].message.content or ""


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    # Shares the long_context_qa fixture — same HotpotQA distractor tasks.
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
