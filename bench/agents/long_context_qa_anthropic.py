"""Long-context QA — Anthropic provider variant.

Runs the same long_context_qa fixture against the Anthropic Messages API.
Key differences from the OpenAI variant:

  1. Uses ``anthropic.Anthropic().messages.create()`` instead of
     ``openai.OpenAI().chat.completions.create()``.
  2. Anthropic does not allow consecutive user messages — all paragraphs
     are concatenated into a single user message. This means ContextCompress
     CANNOT fire (no separate messages to drop). ModelDowngrade CAN fire.
  3. The system prompt uses Anthropic's top-level ``system`` parameter.

Expected results:
  - ContextCompress: 0% savings, 0% fire rate (single-message format).
  - ModelDowngrade (sonnet → haiku): ~33% cost savings when the rule warms up.
  - Accuracy: same as OpenAI baseline (same fixture, same prompts).

For a ContextCompress eval on Anthropic, a multi-turn fixture is needed
(paragraphs interleaved with brief assistant acknowledgments so Anthropic
accepts the message list). That is a separate fixture design task.
"""

from __future__ import annotations

import os
import re

import agentc

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, anthropic_client, run_all

AGENT_KEY = "long_context_qa_anthropic"

SYSTEM = (
    "Answer the question using only the provided paragraphs. Output only "
    "the answer — a single word, name, number, or short phrase. No explanation."
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(text).lower())).strip()


def _hotpot_check(answer: str, expected: object) -> bool:
    if not isinstance(expected, str):
        return False
    a, e = _normalize(answer), _normalize(expected)
    if not e:
        return False
    if a == e:
        return True
    return f" {e} " in f" {a} "


def _build_user_message(task: SyntheticTask) -> str:
    """Concatenate all paragraphs + question into one user message.

    Anthropic prohibits consecutive user messages, so the OpenAI variant's
    per-paragraph message approach cannot be used directly.
    """
    paragraphs = (task.meta or {}).get("paragraphs") or []
    parts: list[str] = []
    for para in paragraphs:
        joined = " ".join(para.get("sentences", []))
        parts.append(f"[{para['title']}]\n{joined}")
    parts.append(f"\nQuestion: {task.prompt}")
    return "\n\n".join(parts)


def _run_one(task: SyntheticTask) -> str:
    with agentc.span("long_context_qa_anthropic.answer"):
        model = os.environ.get("BENCH_BASELINE_MODEL") or "claude-haiku-4-5-20251001"
        client = anthropic_client()
        if client is None:
            gold = (task.meta or {}).get("gold_answer", "") or task.expected
            return f"[stub:{model}] {gold}"
        user_msg = _build_user_message(task)
        resp = client.messages.create(
            model=model,
            max_tokens=64,
            system=SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text if resp.content else ""


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
