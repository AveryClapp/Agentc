"""Ordered concurrent map, provenance certificates, and real batch profiles.

The driver owns execution. Synthetic peer descriptors support the rule's
audit certificate; complete batch economics come from actual SDK receipts.
With an application version, complete provider-reported original-call batches
are profiled without enabling callback reordering or counterfactual replay.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Literal, TypeVar

from agentc._provenance import UserInput, as_json, tag
from agentc._batch_observation import (
    BatchObservation,
    ParallelObservation,
    comparison_fingerprint,
    GRAPH_COMPARISON_VERSION,
)

log = logging.getLogger(__name__)

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
    on_observation: Callable[[ParallelObservation], None] | None = None,
    comparison: bool | Literal["graph"] = False,
    graph_template_id: str | None = None,
    graph_pair_id: str | None = None,
    graph_admission: dict[str, Any] | None = None,
    graph_sampling: dict[str, Any] | None = None,
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

    Inside ``application_version(...)``, complete provider-reported SDK calls
    form an ordered batch or realized model-and-rewrite graph profile.
    Graphs retain every call and the opaque callback result contract; estimated
    costs retain an execution checkpoint without a measured-cost profile.
    ``on_observation`` receives a
    content-free receipt or an explicit abstention. Missing receipts, probes,
    streaming/bypassed calls and unsupported rewrites never qualify as complete
    billing. Observation callback failures cannot change the map's result or exception.

    ``comparison=True`` binds ordered JSON inputs and final JSON results to a
    durable native receipt. Compare two completed runs with
    ``compare_parallel_observations``; callbacks are never replayed by that API.
    Unsupported values retain ordinary economics and receive no comparison
    receipt. Comparison does not authorize learned scheduling.
    ``comparison="graph"`` binds the complete model-and-rewrite graph and its
    ordered final results, including original serial baselines. Compare saved
    runs with ``compare_parallel_observations``; graph pairing also works while
    billing is pending and reports no billed acquisition cost in that case.
    Pairing provides no scheduling or deployment authorization.

    Graph comparisons record a native workflow start before calling ``fn``.
    ``graph_template_id`` optionally freezes a saved graph execution's node
    plans, source controls, dependencies and runtime for another acquisition.
    A missing, stale or changed template raises before the affected SDK call;
    this option grants no learned scheduling or deployment permission.
    ``graph_admission`` requests native guarded deployment of that template.
    Admission runs before the callback, and a held decision raises without
    executing it. Upcoming requests are materialized and bound at each SDK
    call, including prompts that use earlier responses. ``on_observation``
    retains the original native decision as ``graph_decision``.
    ``graph_pair_id`` additionally freezes both evidence generations from a
    saved whole-result pair. Its ``graph_template_id`` must identify that pair's
    reference or candidate execution. A transition on either side holds the
    next dispatch and prevents the acquisition from qualifying for readmission.
    ``on_observation`` also reports ``graph_acquisition`` for prepared graph
    comparisons. This separate physical-cost report includes failed, fallback,
    validation, shadow and late exploration work even when graph publication
    abstains. Unknown billing remains held; the report does not grant admission.

    ``graph_sampling`` explicitly authorizes bounded callback replay for whole
    graph learning. It accepts an optional saved ``source_pair_id`` and requires a ``policy`` with
    a replay contract digest, sampling rate and lifetime workflow/sample limits.
    The optional ``max_lifetime_physical_invocations`` reserves the full
    per-workflow call allowance before callbacks start. Concurrent workflows
    share that allowance; finishing releases unused slots while every dispatched
    call, including failed or unbilled work, stays charged across restarts.
    Optional ``policy.commitments`` declares a monetary allowance per workflow,
    a shared lifetime allowance, and limits on open workflows and pending SDK
    invocations. It accepts billed USD or one pinned token-tariff snapshot.
    Open or incompletely priced workflows retain their full allowance; verified
    costs release only the unused part. An observed overrun blocks new calls.
    These are accounting commitments; provider-enforced price bounds are needed
    to guarantee that a pending call cannot exceed its declared allowance.
    Optional ``policy.campaign`` shares immutable monetary, workflow and physical
    limits across sampling drivers and independent guarded deployments in the
    same store. Use the same ``campaign_id`` and policy in ``graph_admission.campaign``
    to charge both paths. Original calls and unknown-cost holds survive restart;
    a changed policy cannot replenish the campaign. Once a driver joins, later
    prepared graph work must keep that campaign, including after
    restart or a change of explicit candidate pair. ``graph_sampling`` reports
    its campaign totals; ``ProviderReceiptBatch.graph_campaign_report`` reads
    shared totals without dispatching work.
    Without a saved pair, native discovery binds each selected instruction before
    SDK dispatch and retains a completed alternative graph pair. Later calls
    reuse that pair. Learned templates rediscover after evidence retirement;
    optional ``policy.refresh_interval_workflows`` rediscoveries run on eligible
    sampled workflows to learn changed topology. Refresh preserves the original
    driver, limits, costs and checkpoint history; explicit saved pairs stay fixed.
    Discovery uses the
    existing registered rules, catalog and hotness gates; it grants no visible
    candidate permission. Native sampling prepares both graphs before the visible reference
    starts. A sampled candidate runs afterward; its failure preserves the
    reference result. ``on_observation`` reports its paired result and all
    acquisition costs in ``graph_sampling``. Callbacks must be replay-safe;
    use isolated application state and tools for each invocation. Sampling
    creates evidence. Optional ``deployment`` supplies ``remaining_batches``
    and a guarded graph admission ``policy``. Sampled workflows keep the visible
    reference; unsampled workflows may return a natively admitted candidate.
    Admission runs before callbacks and freezes the selected template. Its
    forecast horizon is bounded by the remaining lifetime workflow allowance.
    The cost and latency forecast also includes the remaining sampling allowance,
    using the compatible observed reference envelope for future replays.
    Every candidate dispatch rechecks quality, economics, source controls and
    the shared physical allowance. An unadmitted candidate uses the reference.
    A visible candidate failure propagates without replaying application tools.

    ``max_workers`` defaults to ``min(32, len(items))`` (Python's
    ``ThreadPoolExecutor`` default behavior, capped to keep the
    OpenAI client's connection pool reasonable).
    """
    if graph_sampling is not None:
        if max_workers is not None:
            raise ValueError("graph_sampling uses the worker limits of its frozen templates")
        if comparison != "graph" or any(value is not None for value in (graph_template_id, graph_pair_id, graph_admission)):
            raise ValueError('graph_sampling requires comparison="graph" without separate graph options')
        from agentc._graph_sampling import sampled_map

        return sampled_map(fn, items, graph_sampling, on_observation)
    return _parallel_map(
        fn, items, max_workers=max_workers, on_observation=on_observation,
        comparison=comparison, graph_template_id=graph_template_id,
        graph_pair_id=graph_pair_id, graph_admission=graph_admission,
    )


def _parallel_map(
    fn: Callable[[T], R], items: Iterable[T], *, max_workers: int | None = None,
    on_observation: Callable[[ParallelObservation], None] | None = None,
    comparison: bool | Literal["graph"] = False, graph_template_id: str | None = None,
    graph_pair_id: str | None = None, graph_admission: dict[str, Any] | None = None,
    provider_receipt_batch_id: str | None = None,
    graph_sampling_lease_id: str | None = None,
    graph_deployment_decision: dict[str, Any] | None = None,
) -> list[R]:
    if type(comparison) is not bool and comparison != "graph":
        raise ValueError('comparison must be a bool or "graph"')
    if graph_template_id is not None and comparison != "graph":
        raise ValueError('graph_template_id requires comparison="graph"')
    if graph_pair_id is not None and graph_template_id is None:
        raise ValueError('graph_pair_id requires graph_template_id')
    if graph_admission is not None and (comparison != "graph" or graph_template_id is None or graph_pair_id is not None):
        raise ValueError('graph_admission requires a graph template and comparison="graph" without graph_pair_id')
    items_list = list(items)
    n = len(items_list)
    if n == 0:
        return []
    started = time.perf_counter()
    span_ids = [_fresh_span_id_hex() for _ in range(n)]
    for item, sid in zip(items_list, span_ids):
        tag(item, UserInput(span_id=sid))

    # Pair each call i with peer (i+1) mod n. Disjoint span ids
    # guarantee the rule's disjointness check passes.
    peers: list[dict[str, Any]] = []
    for i in range(n):
        peer_idx = (i + 1) % n
        peers.append(
            {
                # call_site_id / model / messages are optional; the rule
                # falls back to inheriting from the base call.
                "input_deps": [
                    as_json(UserInput(span_id=span_ids[peer_idx])),
                ],
            }
        )

    if n == 1:
        max_workers = 1
    elif max_workers is None:
        max_workers = min(32, n)

    from agentc._application_version import declaration

    version = declaration()
    batch = None
    if version is not None or on_observation is not None or comparison:
        callback_name = f"{getattr(fn, '__module__', type(fn).__module__)}.{getattr(fn, '__qualname__', type(fn).__qualname__)}"
        callback_version = hashlib.sha256(
            ("map-callback-v1\0" + callback_name).encode()
        ).hexdigest()
        batch = BatchObservation(
            application_version=None if version is None else version["sha256"],
            callback_version=callback_version,
            item_count=n,
            max_workers=min(max_workers, n),
            comparison_requested=bool(comparison),
            graph_comparison_requested=comparison == "graph",
            graph_template_id=graph_template_id,
            graph_pair_id=graph_pair_id,
            graph_admission=graph_admission,
            provider_receipt_batch_id=provider_receipt_batch_id,
            graph_sampling_lease_id=graph_sampling_lease_id,
            graph_deployment_decision=graph_deployment_decision,
            comparison_input_sha256=(
                comparison_fingerprint(items_list, version=GRAPH_COMPARISON_VERSION)
                if comparison == "graph"
                else comparison_fingerprint(items_list) if comparison else None
            ),
        )

    def _runner(idx: int) -> R:
        previous_peer = get_parallel_peer()
        _set_peer(peers[idx] if n > 1 else None)
        try:
            if batch is None:
                return fn(items_list[idx])
            with batch.item(idx):
                return fn(items_list[idx])
        finally:
            _set_peer(previous_peer)

    # If max_workers == 1 the user explicitly asked for serial. Honor
    # it (useful for debugging) — peers still get staged so the audit
    # records what *would* have parallelized.
    succeeded = False
    results: list[R] | None = None
    try:
        if n == 1:
            results = [_runner(0)]
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [
                    pool.submit(contextvars.copy_context().run, _runner, i)
                    for i in range(n)
                ]
                results = [future.result() for future in futures]
        succeeded = True
        return results
    finally:
        if batch is not None:
            try:
                observation = batch.finish(
                    wall_latency_ms=(time.perf_counter() - started) * 1000.0,
                    succeeded=succeeded,
                    results=results,
                )
                if on_observation is not None:
                    on_observation(observation)
            except BaseException:
                log.debug(
                    "parallel observation failed; preserving map outcome", exc_info=True
                )
