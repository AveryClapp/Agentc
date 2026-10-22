"""Prospective whole-graph learning with optional guarded candidate deployment."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from copy import deepcopy
from dataclasses import replace
from typing import Any, Callable, Iterable, TypeVar

from agentc._batch_observation import (
    GRAPH_COMPARISON_VERSION,
    ParallelObservation,
    comparison_fingerprint,
)

log = logging.getLogger(__name__)
T = TypeVar("T")
R = TypeVar("R")


def _native_command(command: dict[str, Any]) -> dict[str, Any]:
    from agentc import _native

    value = json.loads(_native.optimize_provider_graph_sampling(
        json.dumps(command, allow_nan=False)
    ))
    if value.get("status") != "recorded":
        raise RuntimeError(value.get("reason", "graph sampling was rejected"))
    result = value["sampling"]
    if not isinstance(result, dict):
        raise RuntimeError("native graph sampling returned an invalid result")
    return result


def sampled_map(
    fn: Callable[[T], R],
    items: Iterable[T],
    request: dict[str, Any],
    on_observation: Callable[[ParallelObservation], None] | None,
) -> list[R]:
    from agentc._application_version import declaration
    from agentc._optimization_scope import decide_optimization, optimization_scope
    from agentc._parallel import _parallel_map
    from agentc._provider_receipts import ProviderReceiptBatch

    if "policy" not in request or not set(request) <= {"source_pair_id", "policy", "deployment"}:
        raise ValueError("graph_sampling requires policy and optional source_pair_id and deployment")
    version = declaration()
    scope = decide_optimization(record=False)
    if version is None or not scope.eligible:
        raise ValueError("graph sampling requires application_version and an eligible scope")
    values = list(items)
    if not values:
        return []
    input_hash = comparison_fingerprint(values, version=GRAPH_COMPARISON_VERSION)
    if input_hash is None:
        raise ValueError("graph sampling requires supported ordered JSON inputs")
    candidate_values = deepcopy(values)
    name = f"{getattr(fn, '__module__', type(fn).__module__)}.{getattr(fn, '__qualname__', type(fn).__qualname__)}"
    callback = hashlib.sha256(("map-callback-v1\0" + name).encode()).hexdigest()
    batches = [
        {"version": "provider-dispatch-receipts-v1", "batch_id": uuid.uuid4().hex,
         "application_version": version["sha256"], "callback_version": callback,
         "item_count": len(values)}
        for _ in range(2)
    ]
    prepared = _native_command({"operation": "begin", "request": {
        **request, "reference_batch": batches[0], "candidate_batch": batches[1],
        "input_sha256": input_hash,
    }})
    ticket = prepared["ticket"]
    observed: list[ParallelObservation] = []
    candidate_observed: list[ParallelObservation] = []
    candidate_started = False
    candidate_error: str | None = None
    visible_error: str | None = None
    try:
        deployed = prepared.get("deployment")
        if deployed is not None:
            return _parallel_map(
                fn, values, max_workers=deployed["max_workers"],
                on_observation=observed.append, comparison="graph",
                graph_template_id=deployed["template"]["execution_id"],
                provider_receipt_batch_id=deployed["batch_id"],
                graph_sampling_lease_id=deployed["lease_id"],
                graph_deployment_decision=prepared["decision"],
            )
        reference = prepared["reference"]
        with optimization_scope(scope.scope, optimize=False):
            result = _parallel_map(
                fn, values, max_workers=reference["max_workers"],
                on_observation=observed.append, comparison="graph",
                graph_template_id=reference["template"]["execution_id"] if reference["template"] else None,
                graph_pair_id=request.get("source_pair_id") or ticket.get("discovered_pair_id"),
                provider_receipt_batch_id=reference["batch_id"],
                graph_sampling_lease_id=reference["lease_id"],
            )
        candidate = prepared["candidate"]
        if candidate is not None and observed and observed[-1].graph_execution_id:
            candidate_started = True
            try:
                _parallel_map(
                    fn, candidate_values, max_workers=candidate["max_workers"],
                    on_observation=candidate_observed.append, comparison="graph",
                    graph_template_id=candidate["template"]["execution_id"] if candidate["template"] else None,
                    graph_pair_id=request.get("source_pair_id") or ticket.get("discovered_pair_id"),
                    provider_receipt_batch_id=candidate["batch_id"],
                    graph_sampling_lease_id=candidate["lease_id"],
                )
            except Exception as exc:
                candidate_error = type(exc).__name__
        return result
    except BaseException as exc:
        visible_error = type(exc).__name__
        raise
    finally:
        report: dict[str, Any] = {
            "ticket": ticket, "completion": {"status": "abstained"},
            "visible_role": "candidate" if ticket.get("deployed") else "reference",
            "visible_error_type": visible_error,
            "candidate_error_type": candidate_error,
            "candidate_execution_id": candidate_observed[-1].graph_execution_id if candidate_observed else None,
        }
        if candidate_observed and not candidate_observed[-1].graph_execution_id:
            report["candidate_publication_reason"] = candidate_observed[-1].reason
        try:
            if prepared["candidate"] is not None and not candidate_started:
                # A failed or unsupported reference never authorizes replay.
                unused = ProviderReceiptBatch(
                    version["sha256"], callback, len(values),
                    batch_id=prepared["candidate"]["batch_id"],
                )
                unused.graph_preparation_id = prepared["candidate"]["lease_id"]
                unused.failed = True
                unused.seal([0] * len(values))
                unused.finish_economics(0.0)
            finished = _native_command({"operation": "finish", "ticket_id": ticket["ticket_id"]})
            report["completion"] = finished
            if request["policy"].get("campaign") is not None:
                report["campaign"] = ProviderReceiptBatch.graph_campaign_report(
                    request["policy"]["campaign"]["campaign_id"],
                )
            if request["policy"].get("commitments") is not None:
                report["commitments"] = _native_command({
                    "operation": "commitments", "ticket_id": ticket["ticket_id"],
                })["commitments"]
            if observed and observed[-1].graph_acquisition:
                from agentc import _native

                driver = observed[-1].graph_acquisition["driver_id"]
                costs = json.loads(_native.optimize_record_provider_receipt(json.dumps({
                    "operation": "graph_acquisition_costs", "driver_id": driver,
                })))
                if costs.get("status") == "recorded":
                    report["acquisition"] = costs["acquisition"]
        except BaseException as exc:
            report["finalization_error_type"] = type(exc).__name__
            log.debug("graph sampling finalization failed; preserving the reference outcome", exc_info=True)
        if observed and on_observation is not None:
            try:
                on_observation(replace(observed[-1], graph_sampling=report))
            except BaseException:
                log.debug("graph sampling observer failed; preserving the reference outcome", exc_info=True)
