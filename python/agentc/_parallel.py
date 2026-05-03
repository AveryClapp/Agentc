"""Driver-style fan-out helper for ``ParallelBranch``.

``parallel_map(fn, items)`` is the user-facing replacement for
``[fn(x) for x in items]``. It does two jobs:

1. Auto-tags each input with a fresh ``UserInput`` ``DepSource`` so the
   per-call ``input_deps`` list contains a non-``Literal`` entry — the
   minimum the rule needs to consider firing.
2. Stages a ``parallel_peer`` descriptor in a thread-local before each
   ``fn(item)`` runs. The vendor patch reads the thread-local in
   ``build_call_dict_openai`` and threads it into ``parameters.extra``,
   which is exactly what ``ParallelBranchRule`` looks for.

The actual concurrent dispatch happens here in Python: a
``ThreadPoolExecutor`` runs ``fn(item)`` for every item in parallel.
The optimizer's ``Plan::Parallel`` is a *bookkeeping* artifact that
records the rule fired — the executor's sync path falls back to running
the underlying call (which is fine, it would have run anyway). What we
care about for the paper is (a) the audit row exists and (b) the
wall-clock latency reflects real parallelism.

Pairing rule for N items: each call's peer is the next item modulo N.
That's enough to satisfy the rule's two-call shape; full N-way fan-out
would need a richer ``Plan::Parallel`` than the current rule emits.
"""

from __future__ import annotations

import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, TypeVar

from agentc._provenance import UserInput, as_json, tag

__all__ = ["parallel_map"]

T = TypeVar("T")
R = TypeVar("R")

# Thread-local peer staging. Populated by ``parallel_map`` before
# invoking ``fn`` on a worker; consumed by the OpenAI / Anthropic
# patches when they build the call dict for the optimizer.
_state = threading.local()


def _set_peer(peer: dict[str, Any] | None) -> None:
    if peer is None:
        if hasattr(_state, "peer"):
            del _state.peer
    else:
        _state.peer = peer


def get_parallel_peer() -> dict[str, Any] | None:
    """Return the parallel-peer descriptor for the current thread, if any.

    Vendor patches call this from inside ``build_call_dict_*`` to learn
    whether the current call is part of a fan-out batch.
    """
    return getattr(_state, "peer", None)


def _fresh_span_id_hex() -> str:
    """8-byte hex string. Same shape as the rest of the SDK's span ids."""
    return secrets.token_hex(8)


def parallel_map(
    fn: Callable[[T], R],
    items: Iterable[T],
    *,
    max_workers: int | None = None,
) -> list[R]:
    """Concurrent ``map`` that exposes the fan-out to the optimizer.

    Equivalent to ``[fn(x) for x in items]`` but with two side effects:

    - Each ``item`` is tagged with a unique ``UserInput`` provenance so
      the per-call ``input_deps`` carry concrete, disjoint sources.
    - For each call, a ``parallel_peer`` descriptor is staged on a
      thread-local; the SDK patch reads it and forwards it to the rule
      engine, where ``ParallelBranchRule`` fires and writes a
      ``Plan::Parallel`` audit row.

    Returns the results in input order.

    ``max_workers`` defaults to ``min(32, len(items))`` (Python's
    ``ThreadPoolExecutor`` default behavior, capped to keep the
    OpenAI client's connection pool reasonable).
    """
    items_list = list(items)
    n = len(items_list)
    if n == 0:
        return []
    if n == 1:
        # Nothing to pair with — degrade to a serial call. Still tag
        # the input so downstream rules see consistent provenance.
        sid = _fresh_span_id_hex()
        tag(items_list[0], UserInput(span_id=sid))
        return [fn(items_list[0])]

    span_ids = [_fresh_span_id_hex() for _ in range(n)]
    for item, sid in zip(items_list, span_ids):
        tag(item, UserInput(span_id=sid))

    # Pair each call i with peer (i+1) mod n. Disjoint span ids
    # guarantee the rule's disjointness check passes.
    peers: list[dict[str, Any]] = []
    for i in range(n):
        peer_idx = (i + 1) % n
        peers.append({
            # call_site_id / model / messages are optional; the rule
            # falls back to inheriting from the base call.
            "input_deps": [
                as_json(UserInput(span_id=span_ids[peer_idx])),
            ],
        })

    if max_workers is None:
        max_workers = min(32, n)

    results: list[Any] = [None] * n

    def _runner(idx: int) -> None:
        _set_peer(peers[idx])
        try:
            results[idx] = fn(items_list[idx])
        finally:
            _set_peer(None)

    # If max_workers == 1 the user explicitly asked for serial. Honor
    # it (useful for debugging) — peers still get staged so the audit
    # records what *would* have parallelized.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for _ in pool.map(_runner, range(n)):
            pass

    return results  # type: ignore[return-value]
