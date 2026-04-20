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

from bench.agents._fixtures import RAG_SUMMARIZER, SyntheticTask
from bench.agents._runtime import AgentResult, call_llm, run_all

AGENT_KEY = "rag_summarizer"
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


def _run_one(task: SyntheticTask) -> str:
    # Synthetic corpus: treat the whole prompt as one chunk. Real
    # fixtures will supply a list under ``meta["chunks"]``.
    chunks: list[str] = []
    if task.meta and isinstance(task.meta.get("chunks"), list):
        chunks = [str(c) for c in task.meta["chunks"]]
    if not chunks:
        chunks = [task.prompt]

    partials = [_summarize_chunk(c) for c in chunks]
    return _combine(partials)


@agentc.trace(name="rag_summarizer")
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, RAG_SUMMARIZER, _run_one)


if __name__ == "__main__":
    agentc.init()
    try:
        for r in run():
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  {r.answer[:80]}")
    finally:
        agentc.shutdown()
