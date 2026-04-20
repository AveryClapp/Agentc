"""Shared runtime bits used by every reference agent.

Keeps the per-agent modules short: each agent only has to supply its
fixtures, its prompt shape, and (optionally) an accuracy checker.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from bench.agents._fixtures import SyntheticTask


FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "fixtures"


@dataclass
class AgentResult:
    """Outcome of a single agent run on one task."""

    task_id: str
    answer: str
    passed: bool
    expected: Any


def load_tasks(
    agent_key: str, synthetic_fallback: list[SyntheticTask]
) -> list[SyntheticTask]:
    """Return tasks from ``bench/fixtures/<agent_key>.json`` if present,
    else the hand-authored synthetic fallback.

    Fixture JSON shape: ``[{"task_id": "...", "prompt": "...", "expected": ...}, ...]``
    """
    path = FIXTURES_ROOT / f"{agent_key}.json"
    if path.is_file():
        data = json.loads(path.read_text())
        return [
            SyntheticTask(
                task_id=row["task_id"],
                prompt=row["prompt"],
                expected=row["expected"],
                meta=row.get("meta"),
            )
            for row in data
        ]
    return synthetic_fallback


def default_check(answer: str, expected: Any) -> bool:
    """Default pass/fail: case-insensitive substring match on ``expected``.

    Overridden per-agent when the dataset demands something richer
    (SWE-bench ``resolved`` flag, GAIA exact-match, ROUGE-L, etc.)."""
    if not isinstance(expected, str):
        return False
    return str(expected).lower() in str(answer).lower()


def llm_client():
    """Return an OpenAI client if ``OPENAI_API_KEY`` is set and the SDK
    is importable; otherwise ``None``. All four reference agents use the
    same entry point so the harness can centrally decide whether to run
    for real or return a deterministic stub."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError:
        return None
    return OpenAI()


def call_llm(
    prompt: str,
    model: str = "gpt-4o-mini",
    system: Optional[str] = None,
) -> str:
    """One-shot chat completion. Returns a deterministic stub when no
    API key is available — the harness still exercises the optimizer's
    interception path, just without real cost numbers.

    Stub shape: ``f"[stub:{model}] {prompt[:80]}"`` — includes part of
    the prompt so the fixture ``expected`` substring can still match."""
    client = llm_client()
    if client is None:
        return f"[stub:{model}] {prompt}"
    messages = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = client.chat.completions.create(model=model, messages=messages)
    return resp.choices[0].message.content or ""


def run_all(
    agent_key: str,
    synthetic_fallback: list[SyntheticTask],
    run_one: Callable[[SyntheticTask], str],
    check: Callable[[str, Any], bool] = default_check,
) -> list[AgentResult]:
    """Boilerplate loop shared by all four agents. Each agent's
    ``main()`` calls this; it is not meant to be invoked directly."""
    tasks = load_tasks(agent_key, synthetic_fallback)
    results: list[AgentResult] = []
    for t in tasks:
        answer = run_one(t)
        results.append(
            AgentResult(
                task_id=t.task_id,
                answer=answer,
                passed=check(answer, t.expected),
                expected=t.expected,
            )
        )
    return results
