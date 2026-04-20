"""Hand-authored synthetic fixtures for the four reference agents.

These let the evaluation harness run end-to-end *today* — no SWE-bench
download, no GAIA auth, no RAG corpus. Each agent's real fixture loader
tries `bench/fixtures/<agent>.json` first and falls back to these.

Intent: they exercise the pipeline (record → optimize → export). They
do NOT produce ship-gate savings numbers — for that you need the real
datasets. The ship-gate runner refuses to emit pass/fail against
synthetic fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SyntheticTask:
    """A single synthetic evaluation task.

    ``prompt`` is the user-level input; ``expected`` is the substring (or
    structured-equality target) that a correct answer must contain. The
    per-agent ``check(answer, expected)`` function decides pass/fail —
    simple substring by default.
    """

    task_id: str
    prompt: str
    expected: Any
    meta: dict[str, Any] | None = None


SWEBENCH_PLANNER: list[SyntheticTask] = [
    SyntheticTask(
        "synth-swe-001",
        "Plan how to fix a TypeError raised when a list is compared to None.",
        expected="None",
    ),
    SyntheticTask(
        "synth-swe-002",
        "Plan how to add retry logic to an HTTP client without swallowing errors.",
        expected="retry",
    ),
    SyntheticTask(
        "synth-swe-003",
        "Plan how to migrate a synchronous queue consumer to asyncio.",
        expected="async",
    ),
]


GAIA_ROUTER: list[SyntheticTask] = [
    SyntheticTask("synth-gaia-001", "What is 2+2?", expected="4"),
    SyntheticTask(
        "synth-gaia-002", "Who wrote 'The Republic'?", expected="Plato"
    ),
    SyntheticTask(
        "synth-gaia-003",
        "What is the capital of the country whose name begins with 'Arg'?",
        expected="Buenos Aires",
    ),
]


RAG_SUMMARIZER: list[SyntheticTask] = [
    SyntheticTask(
        "synth-rag-001",
        "Summarize: 'The mitochondrion is the powerhouse of the cell; it "
        "produces ATP via oxidative phosphorylation.'",
        expected="mitochondrion",
    ),
    SyntheticTask(
        "synth-rag-002",
        "Summarize: 'Rust's borrow checker enforces memory safety at compile "
        "time without a garbage collector.'",
        expected="borrow",
    ),
    SyntheticTask(
        "synth-rag-003",
        "Summarize: 'HTTP/3 uses QUIC over UDP and drops head-of-line "
        "blocking present in HTTP/2.'",
        expected="QUIC",
    ),
]


MULTIAGENT_RESEARCH: list[SyntheticTask] = [
    SyntheticTask(
        "synth-multi-001",
        "Researcher-and-writer: explain what a ring buffer is in one paragraph.",
        expected="ring buffer",
    ),
    SyntheticTask(
        "synth-multi-002",
        "Researcher-and-writer: compare B-trees and LSM trees.",
        expected="LSM",
    ),
    SyntheticTask(
        "synth-multi-003",
        "Researcher-and-writer: what problem does a bloom filter solve?",
        expected="bloom",
    ),
]
