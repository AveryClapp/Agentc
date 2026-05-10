"""Multi-rule QA — purpose-built end-to-end workload (EXP-006).

A 3-step iterative answer-refinement chain over a long supporting
document. The combined prompt structure activates three rewrite rules
on the same trace:

- ``ContextCompress``: per-step prompts include 20 paragraphs of
  Wikipedia-derived context, exceeding the 8 KB activation gate. Most
  paragraphs are distractors irrelevant to the question, so the
  IDF-weighted attention proxy can drop them safely.
- ``StateDrop``: each refinement step's previous revisions are state-
  tagged with their version key, but only the latest revision is
  state-read into the current window. Older revisions therefore become
  eligible for removal.
- ``ModelDowngrade``: when ``BENCH_BASELINE_MODEL=gpt-4o`` is set, the
  call site has a downgrade route to ``gpt-4o-mini`` configured. After
  the hot-threshold is crossed, the optimizer can route the call to
  the cheaper model when accuracy budget permits.

Reuses the existing ``long_context_qa.json`` fixture (20 paragraphs per
task, ~13-18 KB raw context). Per task we issue 3 LLM calls
(initial + 2 refinements). At n=20 that is 60 calls per ablation
configuration.

Expected behavior under all-on:
- ContextCompress drops most distractor paragraphs at each step.
- StateDrop drops v0..v(k-2) once v(k-1) is the only state-read revision.
- ModelDowngrade can route once the call site is hot
  (``hot_threshold`` defaults to 3; the third step is the first
  candidate).
"""

from __future__ import annotations

import os
import re
from typing import Any

import agentc

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, run_all

AGENT_KEY = "multirule_qa"

ANSWER_SYSTEM = (
    "Answer the question using only the provided paragraphs. Output only "
    "the answer — a single word, name, number, or short phrase. No "
    "explanation."
)

REFINE_SYSTEM = (
    "Given the prior revisions and the supporting paragraphs, produce a "
    "refined answer. If the prior revisions look correct, repeat the "
    "best one verbatim. Output only the answer — a single word, name, "
    "number, or short phrase. No explanation."
)

_NUM_REFINEMENTS = 2  # initial + 2 refinements = 3 LLM calls per task


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(text).lower())).strip()


def _hotpot_check(answer: str, expected: Any) -> bool:
    """EM-with-tolerance scorer matching long_context_qa."""
    if not isinstance(expected, str):
        return False
    a, e = _normalize(answer), _normalize(expected)
    if not e:
        return False
    if a == e:
        return True
    return f" {e} " in f" {a} "


def _doc_messages(task: SyntheticTask) -> list[dict[str, str]]:
    """The 20-paragraph supporting document, one message per paragraph."""
    paragraphs = (task.meta or {}).get("paragraphs") or []
    out: list[dict[str, str]] = []
    for para in paragraphs:
        joined = " ".join(para.get("sentences", []))
        out.append({"role": "user", "content": f"{para['title']}\n{joined}"})
    return out


def _model() -> str:
    return os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini-2024-07-18"


def _initial_answer(task: SyntheticTask) -> str:
    """Step 0: initial pass over the document. Long-context single-shot."""
    with agentc.span("multirule.initial"):
        client = llm_client()
        if client is None:
            gold = (task.meta or {}).get("gold_answer", "") or task.expected
            return f"[stub:{_model()}] {gold}"
        messages = [{"role": "system", "content": ANSWER_SYSTEM}]
        messages.extend(_doc_messages(task))
        messages.append({"role": "user", "content": f"Question: {task.prompt}"})
        resp = client.chat.completions.create(
            model=_model(), messages=messages, temperature=0
        )
        return resp.choices[0].message.content or ""


def _refine_step(
    task: SyntheticTask,
    older_revisions: list[str],
    latest_in_window: str,
) -> str:
    """One refinement step.

    ``older_revisions`` are state-tagged but NOT re-read here, so their
    keys are not in ``window_state_reads`` for this call.
    ``latest_in_window`` was just state_read by the caller, so its key
    IS in the window — protecting the most recent revision from
    StateDrop.
    """
    with agentc.span("multirule.refine"):
        client = llm_client()
        if client is None:
            return latest_in_window  # stub: don't change the answer

        messages: list[dict[str, str]] = [
            {"role": "system", "content": REFINE_SYSTEM},
        ]
        messages.extend(_doc_messages(task))
        # Older revisions: pass the state-tagged strings DIRECTLY as
        # message content. Wrapping them in an f-string ("Prior revision:
        # {prior}") would create a new string and lose the State(v_k)
        # provenance tag, preventing StateDrop from recognizing them.
        # This matches the iterative_refiner pattern.
        for prior in older_revisions:
            messages.append({"role": "user", "content": prior})
        # Latest revision: same — pass the tagged string directly so its
        # State(v_{k-1}) tag is preserved and matched against
        # window_state_reads.
        messages.append({"role": "user", "content": latest_in_window})
        # The question must remain the LAST role=user message: the IDF
        # attention proxy uses the last user message as the salient
        # signal for ContextCompress scoring (see §3.4). If a generic
        # meta-instruction is appended afterward, the salient signal
        # collapses to instruction tokens and CC drops the supporting
        # paragraphs indiscriminately.
        messages.append({"role": "user", "content": f"Question: {task.prompt}"})
        resp = client.chat.completions.create(
            model=_model(), messages=messages, temperature=0
        )
        return resp.choices[0].message.content or ""


def _run_one(task: SyntheticTask) -> str:
    # Step 0: initial answer (no state yet).
    a0 = _initial_answer(task)
    # Tag the initial answer with State(v0) for downstream calls.
    a0_tagged = agentc.state_write("v0", a0)
    revisions: list[str] = [a0_tagged]

    for step in range(1, _NUM_REFINEMENTS + 1):
        latest_key = f"v{step - 1}"
        latest_val = revisions[-1]
        # Mark the latest as fresh-read for THIS call's window.
        latest_in_window = agentc.state_read(latest_key, latest_val)

        new_answer = _refine_step(
            task,
            older_revisions=revisions[:-1],  # tagged but out of window
            latest_in_window=latest_in_window,
        )
        new_key = f"v{step}"
        revisions.append(agentc.state_write(new_key, new_answer))

    return revisions[-1]


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
