"""Batch classifier — purpose-built CacheHit rule benchmark.

The CacheHit rule fires when (a) the call site has at least one prior
observation in the profile and (b) the cache returns an exact-match
hit on the call key. Within a single ablation run, both conditions
become satisfied after the *first* occurrence of each unique prompt:
``observe_outcome`` increments ``n_observations`` and seeds the cache,
so subsequent identical prompts trigger CacheHit.

Fixture: 20 unique classification prompts × 10 repetitions = 200 tasks
through a single call site. After warmup (~20 calls), the remaining
~180 should hit the cache.

Expected: ~85-90% cost savings; 0pp accuracy delta (cached responses
are identical to baseline).
"""

from __future__ import annotations

import os

import agentc

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, run_all

AGENT_KEY = "batch_classifier"

CLASSIFY_SYSTEM = (
    "Classify the following short text into exactly one of these "
    "categories: sports, politics, technology, health, business, "
    "science, entertainment, education, environment, travel. "
    "Output only the single-word category name."
)

# 20 unique items, each repeated 10x. All go through the same call site
# (the classifier helper below) so they share a profile and cache.
_ITEMS: list[tuple[str, str]] = [
    ("The Lakers defeated the Celtics in overtime by a score of 112 to 108.", "sports"),
    ("Senate passes new budget bill after weeks of negotiation between parties.", "politics"),
    ("Apple unveils new processor architecture for next-generation laptops.", "technology"),
    ("Researchers find link between sleep duration and cardiovascular health.", "health"),
    ("Quarterly earnings beat analyst expectations across major retailers.", "business"),
    ("Astronomers detect water vapor in the atmosphere of an exoplanet.", "science"),
    ("Streaming platform announces new fantasy series adaptation for next year.", "entertainment"),
    ("School district adopts new mathematics curriculum for elementary grades.", "education"),
    ("Coastal wetland restoration project completes its first phase ahead of schedule.", "environment"),
    ("Airlines announce expanded routes between major Asian and European cities.", "travel"),
    ("Star quarterback signs a five-year contract extension with his current team.", "sports"),
    ("Mayor proposes amendment to municipal zoning regulations for downtown.", "politics"),
    ("Open-source database project releases major version with vector search support.", "technology"),
    ("New vaccine candidate enters phase three clinical trials at multiple sites.", "health"),
    ("Tech startup secures Series B funding round led by venture capital firm.", "business"),
    ("Particle physicists report unexpected results from collider data analysis.", "science"),
    ("Director announces sequel to award-winning independent film from last decade.", "entertainment"),
    ("University launches scholarship program for first-generation college students.", "education"),
    ("Reforestation initiative plants its millionth tree across affected regions.", "environment"),
    ("Resort destination reports record visitor numbers during the winter season.", "travel"),
]

_REPETITIONS = 10

_SYNTHETIC: list[SyntheticTask] = []
for i, (text, label) in enumerate(_ITEMS):
    for rep in range(_REPETITIONS):
        _SYNTHETIC.append(
            SyntheticTask(
                task_id=f"cls-{i:02d}-rep{rep:02d}",
                prompt=text,
                expected=label,
            )
        )


def _classify(text: str) -> str:
    """Single shared call site — all 200 tasks route through here.

    The CacheHit rule keys on ``call_site_id`` plus the prompt hash,
    so identical prompts hitting this exact frame yield a cache hit.
    """
    with agentc.span("classify"):
        model = os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini"
        client = llm_client()
        if client is None:
            # Stub: emit a deterministic label so the harness still
            # exercises the cache path.
            return "[stub] sports"
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": text},
            ],
        )
        return resp.choices[0].message.content or ""


def _run_one(task: SyntheticTask) -> str:
    return _classify(task.prompt)


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, _SYNTHETIC, _run_one)


if __name__ == "__main__":
    agentc.init()
    try:
        results = run()
        passed = sum(1 for r in results if r.passed)
        print(f"\n{passed}/{len(results)} accuracy")
        for r in results:
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  expected={r.expected!r}  got={r.answer[:40]!r}")
    finally:
        agentc.shutdown()
