"""Iterative refiner — purpose-built StateDrop rule benchmark.

Ten-step refinement chain. Each step's LLM call sees a *growing*
message list — every prior revision is included as context — but only
the latest revision is fresh in the window of state reads. By step N,
the call carries N state-tagged messages, but ``window_state_reads``
contains only the most recent key. The ``StateDrop`` rule drops the
older messages whose keys aren't in the window, subject to a 50%
retention floor.

By step 10, the call has 1 system + 10 state messages = 11 total. The
rule's 50% floor means it keeps at least 6, so it drops the 5 oldest
state-tagged messages (v0..v4). That's where the savings come from:
half the input tokens at the deepest steps disappear.

Expected: 25-40% input-token savings on long chains; <=2pp accuracy
delta (the most recent revision and the system prompt are always
preserved, which is what the refiner actually needs).
"""

from __future__ import annotations

import os

import agentc

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, run_all

AGENT_KEY = "iterative_refiner"

REFINER_SYSTEM = (
    "You are an iterative writer. You will receive prior revisions of a "
    "short paragraph and must produce a slightly improved next revision. "
    "Make small focused edits — don't rewrite from scratch. Output only "
    "the new paragraph (one paragraph, no bullets, no preface)."
)

_NUM_STEPS = 10

# Synthetic fixture: 50 topics. Effective n is ``min(BENCH_MAX_TASKS,
# len(_TOPICS))`` — setting BENCH_MAX_TASKS above 50 doesn't grow the
# task count, it caps at this list's length.
_TOPICS: list[tuple[str, str]] = [
    ("Write a paragraph explaining what a binary search tree is.", "tree"),
    ("Write a paragraph explaining the concept of a hash table.", "hash"),
    ("Write a paragraph explaining how a queue differs from a stack.", "queue"),
    ("Write a paragraph explaining what recursion means in programming.", "recursion"),
    ("Write a paragraph explaining the role of a compiler.", "compiler"),
    ("Write a paragraph explaining what virtual memory is.", "memory"),
    ("Write a paragraph explaining how DNS resolves domain names.", "domain"),
    ("Write a paragraph explaining what a database index does.", "index"),
    ("Write a paragraph explaining the difference between TCP and UDP.", "tcp"),
    ("Write a paragraph explaining what a cache miss is.", "cache"),
    ("Write a paragraph explaining what an operating system kernel does.", "kernel"),
    ("Write a paragraph explaining the publish-subscribe messaging pattern.", "publish"),
    ("Write a paragraph explaining what a foreign key constraint means.", "foreign"),
    ("Write a paragraph explaining the role of a load balancer.", "balancer"),
    ("Write a paragraph explaining what container orchestration does.", "container"),
    ("Write a paragraph explaining the concept of a pure function.", "function"),
    ("Write a paragraph explaining what a race condition is.", "race"),
    ("Write a paragraph explaining the role of a garbage collector.", "garbage"),
    ("Write a paragraph explaining what an API rate limit is.", "rate"),
    ("Write a paragraph explaining how a B-tree differs from a binary tree.", "b-tree"),
    ("Write a paragraph explaining what a context switch costs.", "context"),
    ("Write a paragraph explaining what an SSL certificate authenticates.", "certificate"),
    ("Write a paragraph explaining what a SQL injection attack is.", "injection"),
    ("Write a paragraph explaining what a microservice architecture means.", "microservice"),
    ("Write a paragraph explaining the role of a write-ahead log.", "log"),
    ("Write a paragraph explaining what a thread pool manages.", "thread"),
    ("Write a paragraph explaining what an LRU cache evicts.", "lru"),
    ("Write a paragraph explaining what an event loop is.", "event"),
    ("Write a paragraph explaining what a reverse proxy does.", "proxy"),
    ("Write a paragraph explaining what a stack overflow signals.", "overflow"),
    ("Write a paragraph explaining the concept of immutability.", "immutable"),
    ("Write a paragraph explaining what idempotence means in HTTP.", "idempotent"),
    ("Write a paragraph explaining what eventual consistency means.", "consistency"),
    ("Write a paragraph explaining how Merkle trees verify data.", "merkle"),
    ("Write a paragraph explaining what a Bloom filter approximates.", "bloom"),
    ("Write a paragraph explaining what consistent hashing distributes.", "hashing"),
    ("Write a paragraph explaining what a memory leak does to a process.", "leak"),
    ("Write a paragraph explaining what schema migration involves.", "migration"),
    ("Write a paragraph explaining the producer-consumer pattern.", "producer"),
    ("Write a paragraph explaining the role of a service mesh.", "mesh"),
    ("Write a paragraph explaining what a hot path means in performance.", "hot"),
    ("Write a paragraph explaining what circuit breaking protects against.", "circuit"),
    ("Write a paragraph explaining how feature flags decouple rollouts.", "flag"),
    ("Write a paragraph explaining the concept of backpressure.", "backpressure"),
    ("Write a paragraph explaining what observability provides beyond logs.", "observability"),
    ("Write a paragraph explaining what zero-downtime deployment requires.", "deployment"),
    ("Write a paragraph explaining the role of a message broker.", "broker"),
    ("Write a paragraph explaining what idempotent retries enable.", "retry"),
    ("Write a paragraph explaining what blue-green deployment means.", "blue-green"),
    ("Write a paragraph explaining what a graph database optimizes for.", "graph"),
]

_SYNTHETIC: list[SyntheticTask] = [
    SyntheticTask(task_id=f"refine-{i:03d}", prompt=topic, expected=token)
    for i, (topic, token) in enumerate(_TOPICS)
]


def _refine_step(
    task_prompt: str,
    versions: list,
    latest: str,
) -> str:
    """One refinement turn.

    ``versions`` carries every prior revision as a *state-tagged*
    string — these objects retain their ``State(key=v_i)`` tag from
    when they were originally written, but are NOT re-read here, so
    their keys do NOT enter the current window.

    ``latest`` is freshly state-read inside this call, so its key
    DOES enter the window — protecting the most recent revision from
    being dropped.
    """
    with agentc.span("refiner.step"):
        model = os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini-2024-07-18"
        client = llm_client()
        if client is None:
            return f"[stub:{model}] {task_prompt}"

        # Each state value is its own user message. The optimizer glue
        # builds parameters.extra.message_deps from tag_of(content) for
        # each — so each carries State(key=v_i). Only the latest key
        # appears in window_state_reads (set by state_read above).
        messages: list[dict[str, str]] = [
            {"role": "system", "content": REFINER_SYSTEM},
            {"role": "user", "content": f"Task: {task_prompt}"},
        ]
        for v in versions:
            messages.append({"role": "user", "content": v})
        messages.append({"role": "user", "content": latest})
        messages.append({
            "role": "user",
            "content": "Produce the next revision now.",
        })

        # temperature=0 collapses LLM output stochasticity. The
        # iterative chain compounds variance across 10 steps (each
        # step's input depends on the previous step's output), so
        # default-temperature runs at n=50 produced cost deltas
        # within the LLM's own noise floor. Deterministic sampling
        # makes the rule's effect distinguishable from sampling.
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0
        )
        return resp.choices[0].message.content or ""


def _run_one(task: SyntheticTask) -> str:
    # Step 0: seed with the task itself (no LLM call). Tag it so it
    # carries State(v0) when the first refiner call uses it.
    v0 = agentc.state_write("v0", f"Initial idea: {task.prompt}")

    # Track every revision; each is a State-tagged string we can pass
    # back into messages without re-reading.
    versions: list[str] = [v0]

    for step in range(1, _NUM_STEPS + 1):
        latest_key = f"v{step - 1}"
        latest_val = versions[-1]
        # Mark the latest revision as fresh-read for THIS call's window.
        # Older revisions in ``versions[:-1]`` are intentionally NOT
        # state_read, so their keys won't appear in window_state_reads.
        latest_in_window = agentc.state_read(latest_key, latest_val)

        new_revision = _refine_step(
            task.prompt,
            versions[:-1],  # older revisions, tagged but out of window
            latest_in_window,
        )
        # Tag the new revision so it carries State(v_step) into the
        # next call, where it will become an "older" message.
        new_key = f"v{step}"
        tagged_new = agentc.state_write(new_key, new_revision)
        versions.append(tagged_new)

    return versions[-1]


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
