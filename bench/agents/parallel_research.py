"""Parallel research — purpose-built ParallelBranch rule benchmark.

ParallelBranch fires when ``parameters.extra.parallel_peer`` is set
(staged by ``agentc.parallel_map``) AND the current call's
``input_deps`` are disjoint from the peer's. Each ``parallel_map``
item gets a fresh ``UserInput`` provenance tag, so siblings are
automatically disjoint.

Workflow per task: research 3 independent aspects of a topic
concurrently via ``parallel_map``, then synthesize. The 3 aspects are
worded so they share no content with each other (origin / impact /
example) — disjointness is structural.

Expected: latency reduction (the rule's primary metric is wall-clock,
not USD). Cost stays flat because we still issue 3 calls per task,
just in parallel rather than sequentially.
"""

from __future__ import annotations

import agentc

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, call_llm, run_all

AGENT_KEY = "parallel_research"

ASPECT_SYSTEM = (
    "Answer the question in one sentence. Be specific and factual. "
    "Output only the sentence."
)
SYNTHESIZE_SYSTEM = (
    "You will receive three short factual statements about the same "
    "topic. Combine them into one paragraph. Output only the paragraph."
)


_TOPICS: list[tuple[str, str]] = [
    ("the Roman Empire", "rome"),
    ("the Industrial Revolution", "industrial"),
    ("the printing press", "press"),
    ("the steam engine", "steam"),
    ("the Renaissance", "renaissance"),
    ("the French Revolution", "french"),
    ("the discovery of penicillin", "penicillin"),
    ("the Internet", "internet"),
    ("the moon landing", "moon"),
    ("the Wright Brothers", "wright"),
    ("the Manhattan Project", "manhattan"),
    ("the Berlin Wall", "berlin"),
    ("the Silk Road", "silk"),
    ("the Magna Carta", "magna"),
    ("the Apollo program", "apollo"),
    ("the Cold War", "cold"),
    ("the Great Depression", "depression"),
    ("the discovery of DNA structure", "dna"),
    ("the invention of the telephone", "telephone"),
    ("the assembly line", "assembly"),
    ("the transcontinental railroad", "railroad"),
    ("the Hubble Space Telescope", "hubble"),
    ("the Panama Canal", "panama"),
    ("the World Wide Web", "web"),
    ("the eradication of smallpox", "smallpox"),
    ("the Olympics", "olympics"),
    ("the United Nations", "united"),
    ("the European Union", "european"),
    ("the Suez Canal", "suez"),
    ("the Manhattan skyline", "manhattan"),
    ("the personal computer", "personal"),
    ("the smartphone revolution", "smartphone"),
    ("the green revolution in agriculture", "green"),
    ("the discovery of electricity", "electricity"),
    ("the theory of relativity", "relativity"),
    ("the Wright Flyer", "flyer"),
    ("the telegraph", "telegraph"),
    ("the Hoover Dam", "hoover"),
    ("the Apollo 11 mission", "apollo"),
    ("the World Health Organization", "health"),
    ("modern vaccines", "vaccine"),
    ("the Voyager probes", "voyager"),
    ("the Eiffel Tower", "eiffel"),
    ("the Great Wall of China", "wall"),
    ("the pyramids of Giza", "pyramid"),
    ("the Colosseum", "colosseum"),
    ("the Vietnam War", "vietnam"),
    ("the Marshall Plan", "marshall"),
    ("the discovery of the New World", "world"),
    ("the construction of the Channel Tunnel", "channel"),
]

_SYNTHETIC: list[SyntheticTask] = [
    SyntheticTask(task_id=f"par-{i:03d}", prompt=topic, expected=token)
    for i, (topic, token) in enumerate(_TOPICS)
]


def _research_aspect(question: str) -> str:
    """Per-aspect research call. Wrapped in ``parallel_map`` below so
    sibling calls stage parallel-peer descriptors for each other."""
    with agentc.span("research.aspect"):
        return call_llm(question, model="gpt-4o-mini", system=ASPECT_SYSTEM)


def _synthesize(partials: list[str]) -> str:
    with agentc.span("research.synthesize"):
        return call_llm(
            "\n".join(f"- {p}" for p in partials),
            model="gpt-4o-mini",
            system=SYNTHESIZE_SYSTEM,
        )


def _run_one(task: SyntheticTask) -> str:
    aspects = [
        f"What were the origins of {task.prompt}?",
        f"What was the lasting impact of {task.prompt}?",
        f"Name one notable figure or event associated with {task.prompt}.",
    ]
    # Fan out: each aspect runs concurrently. agentc.parallel_map tags
    # each with its own UserInput dep and stages a parallel_peer
    # descriptor on the per-thread context, which the optimizer glue
    # surfaces to ParallelBranch via parameters.extra.parallel_peer.
    summaries = agentc.parallel_map(_research_aspect, aspects)
    return _synthesize(summaries)


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, _SYNTHETIC, _run_one)


if __name__ == "__main__":
    agentc.init()
    try:
        results = run()
        passed = sum(1 for r in results if r.passed)
        print(f"\n{passed}/{len(results)} accuracy (substring match)")
        for r in results:
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  {r.answer[:60]}")
    finally:
        agentc.shutdown()
