"""Concurrent variant of long_context_qa for throughput benchmarking.

Same QA logic but runs tasks in a ThreadPoolExecutor so the parent
can control concurrency level via BENCH_CONCURRENCY.

For each task, prints a structured timing line to stdout:
    TIMING <task_id> <latency_s> <prompt_tokens> <completion_tokens>

The concurrency bench runner parses these alongside the normal
PASS/FAIL lines to build per-task latency distributions.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import agentc
from openai import RateLimitError

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, load_tasks
from bench.agents.long_context_qa import AGENT_KEY, _build_messages, _hotpot_check

CONCURRENCY = int(os.environ.get("BENCH_CONCURRENCY", "1"))
_MAX_RETRIES = 8
_RETRY_BASE_S = 2.0


def _run_one_timed(task: SyntheticTask) -> tuple[AgentResult, float, int, int]:
    model = os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini-2024-07-18"
    client = llm_client()

    t0 = time.perf_counter()

    with agentc.span("long_context.answer"):
        if client is None:
            gold = (task.meta or {}).get("gold_answer", "") or task.expected
            answer = f"[stub:{model}] {gold}"
            prompt_tokens = 0
            completion_tokens = 0
        else:
            messages = _build_messages(task)
            for attempt in range(_MAX_RETRIES):
                try:
                    resp = client.chat.completions.create(model=model, messages=messages)
                    break
                except RateLimitError:
                    if attempt == _MAX_RETRIES - 1:
                        raise
                    time.sleep(_RETRY_BASE_S * (2**attempt))
            answer = resp.choices[0].message.content or ""
            prompt_tokens = resp.usage.prompt_tokens if resp.usage else 0
            completion_tokens = resp.usage.completion_tokens if resp.usage else 0

    latency_s = time.perf_counter() - t0
    passed = _hotpot_check(answer, task.expected)

    result = AgentResult(
        task_id=task.task_id,
        answer=answer,
        passed=passed,
        expected=task.expected,
    )
    return result, round(latency_s, 4), prompt_tokens, completion_tokens


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    tasks = load_tasks(AGENT_KEY, [])
    cap = os.environ.get("BENCH_MAX_TASKS")
    if cap:
        tasks = tasks[: int(cap)]

    results: list[AgentResult] = []

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(_run_one_timed, t): t for t in tasks}
        for fut in as_completed(futures):
            result, latency_s, prompt_tokens, completion_tokens = fut.result()
            results.append(result)
            print(
                f"TIMING {result.task_id} {latency_s} {prompt_tokens} {completion_tokens}"
            )

    return results


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
