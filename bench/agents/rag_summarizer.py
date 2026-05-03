"""RAG summarizer reference agent.

Three-stage pipeline: retrieve (stub) → chunk-summarize (fan-out) →
combine. The fan-out gives the optimizer a ParallelBranch opportunity;
the retrieve-output → chunk-summarize step exercises ContextCompress.

Target per ``specs/optimizer.md``: savings ≥ 40%, accuracy floor 0.82
ROUGE-L. Synthetic fixtures use substring-match instead; a real ROUGE-L
checker lives in the ship-gate runner once the corpus is available.
"""

from __future__ import annotations

import agentc

from typing import Any

from bench.agents._fixtures import RAG_SUMMARIZER, SyntheticTask
from bench.agents._runtime import AgentResult, call_llm, run_all

AGENT_KEY = "rag_summarizer"


def _rag_check(answer: str, expected: Any) -> bool:
    """Looser scorer for free-form summaries.

    The default substring check fails on the CNN/DailyMail fixture
    because the reference is a multi-line bullet-point distillation
    while the model produces a single fluent paragraph. Score by
    per-line overlap: a summary passes if it covers at least half of
    the reference's content lines (each line is treated as one
    factual claim; we use a substring of its first ~5 words to allow
    paraphrasing of the rest)."""
    if not isinstance(expected, str):
        return False
    answer_l = str(answer).lower()
    lines = [line.strip() for line in str(expected).splitlines() if line.strip()]
    if not lines:
        return False
    hits = 0
    for line in lines:
        # First 5 words of the reference line is the "claim head".
        head = " ".join(line.lower().split()[:5])
        if head and head in answer_l:
            hits += 1
    return hits * 2 >= len(lines)
CHUNK_SYSTEM = "Summarize the passage in one sentence. Output only the sentence."
COMBINE_SYSTEM = (
    "You are given several per-chunk summaries. Produce one final "
    "summary that captures the key point. Output only the summary."
)


def _summarize_chunk(chunk: str) -> str:
    with agentc.span("rag.chunk_summary"):
        return call_llm(chunk, model="gpt-4o-mini", system=CHUNK_SYSTEM)


def _combine(partials: list[str]) -> str:
    with agentc.span("rag.combine"):
        return call_llm(
            "\n".join(f"- {p}" for p in partials),
            model="gpt-4o-mini",
            system=COMBINE_SYSTEM,
        )


_TARGET_CHUNKS = 2


def _split_into_chunks(text: str, target: int = _TARGET_CHUNKS) -> list[str]:
    """Split a single article into ``target`` roughly-equal sub-chunks at
    sentence boundaries. Map-Reduce summarization needs multiple
    sub-chunks to fan out — fixtures often store one big article per
    task, so we split at runtime."""
    if target <= 1:
        return [text]
    sentences = [s.strip() for s in text.split(". ") if s.strip()]
    if len(sentences) <= 1:
        return [text]
    if len(sentences) < target:
        target = len(sentences)
    per = max(1, len(sentences) // target)
    out: list[str] = []
    for i in range(target):
        start = i * per
        end = (i + 1) * per if i < target - 1 else len(sentences)
        if start >= len(sentences):
            break
        piece = ". ".join(sentences[start:end])
        if piece and not piece.endswith("."):
            piece += "."
        out.append(piece)
    return out or [text]


def _run_one(task: SyntheticTask) -> str:
    raw_chunks: list[str] = []
    if task.meta and isinstance(task.meta.get("chunks"), list):
        raw_chunks = [str(c) for c in task.meta["chunks"]]
    if not raw_chunks:
        raw_chunks = [task.prompt]

    # Map-Reduce: each top-level chunk is split into _TARGET_CHUNKS
    # sub-chunks; the sub-summaries fan out and the combine step
    # reduces. Multiple sub-chunks are required for ParallelBranch to
    # have anything to pair.
    chunks: list[str] = []
    for c in raw_chunks:
        chunks.extend(_split_into_chunks(c))

    # Fan-out: each chunk summarizes independently, so the optimizer's
    # ``ParallelBranchRule`` can fire. ``agentc.parallel_map`` tags
    # each chunk with a fresh ``UserInput`` provenance and stages a
    # parallel-peer descriptor on the per-call thread-local.
    partials = agentc.parallel_map(_summarize_chunk, chunks)
    return _combine(partials)


@agentc.trace(name="rag_summarizer")
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, RAG_SUMMARIZER, _run_one, check=_rag_check)


if __name__ == "__main__":
    agentc.init()
    try:
        for r in run():
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  {r.answer[:80]}")
    finally:
        agentc.shutdown()
