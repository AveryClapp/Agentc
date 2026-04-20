"""Build ``bench/fixtures/multiagent_research.json`` from hand-authored
research topics.

No external dataset: the multi-agent research workload is novel enough
that there is no off-the-shelf benchmark with the right shape
(researcher-then-writer, factual-paragraph target). We author 30
topics in-line; each has a short reference answer that captures the
core fact the writer should produce.

Accuracy signal: ``expected`` is a short phrase that a correct answer
should contain. The ship-gate runner can upgrade to LLM-as-judge
scoring against ``meta.reference`` later — for now the substring
check gives a directional signal.
"""

from __future__ import annotations

from bench.build_fixtures._common import write_fixture

AGENT_KEY = "multiagent_research"

# (prompt, expected-substring, reference-paragraph) triples.
TOPICS: list[tuple[str, str, str]] = [
    (
        "Compare B-trees and LSM trees for database storage.",
        "write-heavy",
        "LSM trees optimize for write-heavy workloads via sequential log "
        "writes and background compaction; B-trees give lower read latency "
        "but suffer from random-write amplification.",
    ),
    (
        "What problem does a bloom filter solve?",
        "membership",
        "A bloom filter is a space-efficient probabilistic data structure "
        "that answers approximate set-membership queries with no false "
        "negatives and a tunable false-positive rate.",
    ),
    (
        "Explain CAP theorem in one paragraph.",
        "partition",
        "CAP theorem states that a distributed system can provide at most "
        "two of consistency, availability, and partition tolerance during "
        "a network partition; in practice, P is a given and systems trade "
        "C against A.",
    ),
    (
        "How does TCP congestion control work?",
        "congestion window",
        "TCP maintains a congestion window that grows exponentially in slow "
        "start and linearly in congestion avoidance; loss (timeout or "
        "duplicate ACK) triggers window reduction.",
    ),
    (
        "Why is Rust considered memory-safe without garbage collection?",
        "borrow checker",
        "Rust enforces memory safety at compile time via the borrow "
        "checker, which verifies that references cannot outlive the data "
        "they point to and that aliasing never coexists with mutation.",
    ),
    (
        "What is a monad in functional programming?",
        "sequencing",
        "A monad is an abstraction for sequencing computations that carry "
        "extra context; it provides unit (lift a value) and bind (thread "
        "the context through a sequence of steps).",
    ),
    (
        "Explain the difference between SQL isolation levels.",
        "serializable",
        "SQL defines read uncommitted, read committed, repeatable read, "
        "and serializable; each level prevents more concurrency anomalies "
        "(dirty reads, non-repeatable reads, phantom reads) at the cost "
        "of more contention.",
    ),
    (
        "How do HTTPS certificates establish trust?",
        "certificate authority",
        "A server's certificate is signed by a certificate authority whose "
        "public key is pre-installed as a trust anchor; the client "
        "verifies the signature chain back to a trusted root.",
    ),
    (
        "What does an operating system page fault do?",
        "page table",
        "When a virtual address is not mapped in the page table, the CPU "
        "traps to the kernel, which either loads the missing page from "
        "disk or terminates the process if the address is invalid.",
    ),
    (
        "Compare vector databases and traditional databases for AI workloads.",
        "similarity",
        "Vector databases index embeddings for approximate nearest-neighbor "
        "similarity search, typically via HNSW or IVF; traditional "
        "databases index scalar values for exact lookup.",
    ),
    (
        "What is the difference between a process and a thread?",
        "address space",
        "A process has its own virtual address space and file descriptors; "
        "threads share the process's address space and descriptors but "
        "have their own stack and registers.",
    ),
    (
        "Explain how a ring buffer works.",
        "wraparound",
        "A ring buffer is a fixed-size circular array with head and tail "
        "pointers that wrap around the array modulo its length; it allows "
        "constant-time enqueue and dequeue with bounded memory.",
    ),
    (
        "What is the purpose of a write-ahead log?",
        "durability",
        "A write-ahead log records intended changes to durable storage "
        "before those changes are applied, so a crash mid-write can be "
        "recovered by replaying the log.",
    ),
    (
        "Why do modern CPUs use branch prediction?",
        "pipeline",
        "Branch prediction speculatively fetches instructions past a "
        "conditional branch to keep the CPU pipeline full; a mispredict "
        "flushes the pipeline and costs tens of cycles.",
    ),
    (
        "Explain Byzantine fault tolerance.",
        "malicious",
        "Byzantine fault tolerance protects a distributed system against "
        "arbitrary (including malicious) failures by requiring "
        "supermajority agreement among non-faulty replicas.",
    ),
    (
        "What makes QUIC different from TCP?",
        "UDP",
        "QUIC runs over UDP and integrates TLS, multiplexed streams, and "
        "connection migration into one protocol, eliminating head-of-line "
        "blocking and shortening the handshake.",
    ),
    (
        "What is eventual consistency?",
        "converge",
        "Eventual consistency guarantees that, in the absence of new "
        "writes, replicas of a shared value converge to the same state "
        "given enough time and message delivery.",
    ),
    (
        "Explain the actor model.",
        "message",
        "In the actor model, concurrent computations are actors that "
        "communicate only by asynchronous messages; each actor has private "
        "state and processes one message at a time.",
    ),
    (
        "How does a hash map handle collisions?",
        "chaining",
        "Hash maps resolve collisions via chaining (each bucket holds a "
        "linked list of entries) or open addressing (probe alternative "
        "slots in the same array).",
    ),
    (
        "What is the difference between OLTP and OLAP?",
        "analytical",
        "OLTP systems handle many small transactional updates with low "
        "latency; OLAP systems serve analytical queries over large "
        "aggregates with columnar storage and batch-oriented execution.",
    ),
    (
        "Explain what a CRDT is.",
        "conflict-free",
        "A CRDT is a conflict-free replicated data type whose operations "
        "commute so concurrent updates at different replicas can be "
        "merged without coordination.",
    ),
    (
        "Why do caches use LRU eviction?",
        "recency",
        "LRU eviction exploits temporal locality by evicting the least "
        "recently used item, which empirically correlates with lowest "
        "future reuse probability.",
    ),
    (
        "What is the difference between a microservice and a monolith?",
        "deployment",
        "A monolith is deployed as one unit; microservices split the "
        "system into independently deployable services communicating over "
        "the network, trading operational complexity for team autonomy.",
    ),
    (
        "How does a vector database use HNSW?",
        "graph",
        "HNSW builds a hierarchical navigable graph where higher layers "
        "are sparse long-range links and lower layers are dense; greedy "
        "search across layers finds approximate nearest neighbors in "
        "logarithmic time.",
    ),
    (
        "Explain public-key cryptography.",
        "keypair",
        "Public-key cryptography uses a keypair: messages encrypted with "
        "the public key can only be decrypted with the private key, and "
        "signatures made with the private key can be verified with the "
        "public key.",
    ),
    (
        "What is tail latency and why does it matter?",
        "99th percentile",
        "Tail latency is the latency at high percentiles (p99, p999); in "
        "systems that fan out to many backends, overall latency is "
        "dominated by the slowest, so tail latency matters more than mean.",
    ),
    (
        "How does a garbage collector trace live objects?",
        "roots",
        "A tracing garbage collector starts from root references (stacks, "
        "globals) and transitively marks every reachable object as live; "
        "unmarked objects are swept.",
    ),
    (
        "What is the role of a load balancer?",
        "distribute",
        "A load balancer distributes incoming requests across a pool of "
        "backend servers, typically using round-robin, least-connections, "
        "or consistent hashing to preserve session affinity.",
    ),
    (
        "Explain what consensus means in distributed systems.",
        "agreement",
        "Consensus is the problem of multiple nodes reaching agreement on "
        "a single value despite failures; algorithms like Raft and Paxos "
        "solve it by electing a leader and replicating a log.",
    ),
    (
        "Why do databases use B+ trees for indexing?",
        "sequential",
        "B+ trees keep all values in the leaves with sibling pointers, "
        "giving efficient range scans and sequential access alongside "
        "logarithmic point lookups.",
    ),
]


def build() -> None:
    assert len(TOPICS) == 30, f"expected 30 topics, got {len(TOPICS)}"
    rows = []
    for idx, (prompt, expected, reference) in enumerate(TOPICS, start=1):
        rows.append(
            {
                "task_id": f"multi-{idx:03d}",
                "prompt": prompt,
                "expected": expected,
                "meta": {"reference": reference},
            }
        )
    write_fixture(AGENT_KEY, rows)


if __name__ == "__main__":
    build()
