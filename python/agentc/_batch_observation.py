"""Content-free observations of real SDK calls inside an ordered map.

The collector follows driver slots across copied contexts. A planned call
without a completed outcome, a bypassed/streamed SDK call, or an auxiliary
probe prevents a partial batch from masquerading as complete economics.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Iterator, Literal


@dataclass(frozen=True, slots=True)
class ParallelObservation:
    status: Literal["recorded", "abstained", "pending_billing"]
    reason: str | None
    sdk_calls: int
    observed_calls: int
    wall_latency_ms: float
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    call_site_version: str | None = None
    execution_plan_id: str | None = None
    comparison_token: str | None = field(default=None, repr=False)
    provider_receipt_batch_id: str | None = None
    provider_receipts: dict[str, Any] | None = None
    cost_source: str | None = None
    graph_execution_id: str | None = None
    graph_comparison_token: str | None = field(default=None, repr=False)
    graph_acquisition: dict[str, Any] | None = None
    graph_decision: dict[str, Any] | None = None
    graph_sampling: dict[str, Any] | None = None


COMPARISON_VERSION = "exact-ordered-map-results-v1"
GRAPH_COMPARISON_VERSION = "exact-provider-graph-results-v1"


def refresh_parallel_observation(
    observation: ParallelObservation,
) -> ParallelObservation:
    """Finish the retained original execution after its costs are verified.

    This reads the durable checkpoint only. It performs no SDK dispatch,
    metadata lookup, scalar observation, callback, or quality comparison.
    The returned comparison token remains bound to the original results.
    Combined graph checkpoints remain pending until graph billing publication
    is supported; they cannot use the original-request publication contract.
    """
    if (
        observation.status != "pending_billing"
        or not observation.provider_receipt_batch_id
        or observation.graph_execution_id is not None
    ):
        return observation
    from agentc import _native

    try:
        receipt = json.loads(
            _native.optimize_record_provider_receipt(
                json.dumps(
                    {
                        "operation": "publish_batch",
                        "batch_id": observation.provider_receipt_batch_id,
                    }
                )
            )
        )
        if receipt["status"] == "recorded":
            totals, key = receipt["totals"], receipt["key"]
            return replace(
                observation,
                status="recorded",
                reason=None,
                comparison_token=receipt.get("comparison_token"),
                cost_usd=totals["cost_usd"],
                input_tokens=totals["input_tokens"],
                output_tokens=totals["output_tokens"],
                wall_latency_ms=totals["wall_latency_ms"],
                call_site_version=key["call_site_version"],
                execution_plan_id=key["execution_plan_id"],
                cost_source=receipt.get("cost_source"),
            )
        if receipt["status"] == "pending_billing":
            return observation
        if receipt.get("retired") is not True:
            return replace(observation, reason="native_publication_unavailable")
        return replace(
            observation,
            status="abstained",
            reason=receipt.get("reason", "native_receipt_rejected"),
        )
    except Exception:
        return replace(observation, reason="native_publication_unavailable")


def comparison_fingerprint(value: Any, *, version: str = COMPARISON_VERSION) -> str | None:
    """Only ordinary JSON values have this exact, versioned result contract."""

    def supported(item: Any, active: set[int]) -> bool:
        if type(item) in (str, int, float, bool, type(None)):
            return True
        if type(item) not in (dict, list) or id(item) in active:
            return False
        active.add(id(item))
        try:
            if type(item) is dict:
                return all(
                    type(key) is str and supported(child, active)
                    for key, child in item.items()
                )
            return all(supported(child, active) for child in item)
        finally:
            active.remove(id(item))

    try:
        if not supported(value, set()):
            return None
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(
            (version + "\0" + encoded).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        return None


@dataclass(frozen=True, slots=True)
class ParallelComparison:
    status: Literal["recorded", "already_recorded", "abstained"]
    reason: str | None = None
    pair_id: str | None = None
    divergence: float | None = None
    acquisition_cost_usd: float | None = None
    acquisition_latency_ms: float | None = None
    graph_quality: dict[str, Any] | None = None


def compare_parallel_observations(
    reference: ParallelObservation,
    candidate: ParallelObservation,
) -> ParallelComparison:
    """Compare two completed runs without executing or replaying a callback.

    Both runs need the same comparison mode (``True`` for original maps or
    ``"graph"`` for whole model-and-rewrite graphs), ordered JSON inputs and
    result contract. The reference must have one worker and original calls.
    Native receipts bind
    the actual request graph, runtime, metrics and final result fingerprint.
    One receipt belongs to one pair; retries cannot create extra evidence or
    charge acquisition twice. This creates no scheduling authorization.
    Graph result pairing is available before billing; acquisition USD remains
    unknown until both original executions have measured cost evidence.
    ``graph_quality`` reports the persistent whole-result window and exposure
    guard. Its disable survives restart and cohort changes; fresh quality alone
    does not authorize deployment or resolve pending acquisition costs.
    """
    graph = (
        reference.graph_comparison_token is not None
        or candidate.graph_comparison_token is not None
    )
    reference_token = (
        reference.graph_comparison_token if graph else reference.comparison_token
    )
    candidate_token = (
        candidate.graph_comparison_token if graph else candidate.comparison_token
    )
    if not reference_token or not candidate_token:
        return ParallelComparison(
            status="abstained", reason="comparison_receipts_required"
        )
    try:
        from agentc import _native

        compare = (
            _native.optimize_compare_provider_graph
            if graph else _native.optimize_compare_multi_call
        )
        value = json.loads(compare(reference_token, candidate_token))
        if value.get("status") in ("recorded", "already_recorded"):
            return ParallelComparison(
                status=value["status"],
                pair_id=value["pair_id"],
                divergence=value["divergence"],
                acquisition_cost_usd=value["acquisition_cost_usd"],
                acquisition_latency_ms=value["acquisition_latency_ms"],
                graph_quality=value.get("quality") if graph else None,
            )
    except Exception:
        return ParallelComparison(
            status="abstained", reason="native_comparison_unavailable"
        )
    return ParallelComparison(status="abstained", reason="native_comparison_rejected")


_current: ContextVar[tuple["BatchObservation", int] | None] = ContextVar(
    "agentc_batch_observation", default=None
)


def graph_original_capture_requested() -> bool:
    """Capture an untouched original only for an explicit whole-graph run."""
    current = _current.get()
    if current is None or not current[0].graph_comparison_requested:
        return False
    from agentc._interception_context import interception_is_nested

    return not interception_is_nested()


def note_sdk_call() -> None:
    """Count one outer SDK invocation, including unsupported bypass paths."""
    current = _current.get()
    if current is not None:
        from agentc._interception_context import interception_is_nested

        if interception_is_nested():
            return
        from agentc._dispatch_context import current_provider_dispatch

        dispatch = current_provider_dispatch()
        if dispatch is not None and dispatch.kind in {
            "validation",
            "exploration",
            "shadow",
        }:
            return
        batch, item = current
        with batch.lock:
            if not batch.closed:
                batch.sdk_calls[item] += 1
                from agentc._provider_receipts import note_logical_call

                note_logical_call(batch.sdk_calls[item] - 1)


def note_plan(plan: Any) -> None:
    current = _current.get()
    if current is not None:
        batch, item = current
        with batch.lock:
            if not batch.closed:
                batch.plans[item].append((plan, None))
        from agentc._provider_receipts import bind_plan

        bind_plan(plan)


def note_outcome(plan: Any, outcome: dict[str, Any]) -> None:
    current = _current.get()
    if current is None:
        return
    batch, item = current
    with batch.lock:
        if batch.closed:
            return
        matches = [
            i for i, (candidate, _) in enumerate(batch.plans[item]) if candidate is plan
        ]
        if len(matches) != 1 or batch.plans[item][matches[0]][1] is not None:
            batch.reason = "duplicate_or_unbound_outcome"
            return
        # Freeze the measured outcome before the adapter changes its plan for
        # a later probe. JSON copying also rejects NaN/unsupported metrics.
        try:
            copied = json.loads(json.dumps(outcome, allow_nan=False))
        except (TypeError, ValueError, OverflowError):
            batch.reason = "invalid_outcome"
            return
        batch.plans[item][matches[0]] = (plan, copied)


class BatchObservation:
    def __init__(
        self,
        *,
        application_version: str | None,
        callback_version: str,
        item_count: int,
        max_workers: int,
        comparison_requested: bool = False,
        comparison_input_sha256: str | None = None,
        execution_contract: str | None = None,
        comparison_version: str | None = None,
        graph_comparison_requested: bool = False,
        provider_receipt_batch_id: str | None = None,
        graph_template_id: str | None = None,
        graph_pair_id: str | None = None,
        graph_admission: dict[str, Any] | None = None,
        graph_sampling_lease_id: str | None = None,
        graph_deployment_decision: dict[str, Any] | None = None,
    ) -> None:
        if graph_admission is not None and (
            not graph_comparison_requested or application_version is None
            or graph_template_id is None or graph_pair_id is not None
        ):
            raise ValueError("guarded graph admission requires an application version and one graph template")
        if graph_pair_id is not None and graph_template_id is None:
            raise ValueError('graph_pair_id requires graph_template_id')
        if graph_template_id is not None and (
            not graph_comparison_requested or application_version is None
        ):
            raise ValueError('graph_template_id requires a graph comparison and application_version')
        self.application_version = application_version
        self.callback_version = callback_version
        self.item_count = item_count
        self.max_workers = max_workers
        self.comparison_requested = comparison_requested
        self.comparison_input_sha256 = comparison_input_sha256
        self.execution_contract = execution_contract
        self.comparison_version = comparison_version
        self.graph_comparison_requested = graph_comparison_requested
        self.lock = threading.Lock()
        self.sdk_calls = [0] * item_count
        self.plans: list[list[tuple[Any, dict[str, Any] | None]]] = [
            [] for _ in range(item_count)
        ]
        self.reason: str | None = None
        self.closed = False
        from agentc._provider_receipts import ProviderReceiptBatch

        self.provider_receipts = ProviderReceiptBatch(
            application_version,
            callback_version,
            item_count,
            batch_id=provider_receipt_batch_id,
        )
        self.graph_preparation_id: str | None = None
        self.graph_decision: dict[str, Any] | None = None
        if application_version is not None and graph_sampling_lease_id is None and graph_admission is None:
            binding: dict[str, Any] = {}
            try:
                from agentc import _native

                binding = json.loads(_native.optimize_record_provider_receipt(json.dumps({
                    "operation": "verify_unbudgeted_graph",
                    "batch_id": self.provider_receipts.batch_id,
                })))
            except Exception:
                pass
            if binding.get("hard_stop") == "graph_campaign_binding":
                raise RuntimeError(binding["reason"])
        if graph_comparison_requested and application_version is not None:
            try:
                from agentc import _native

                prepared: dict[str, Any]
                if graph_sampling_lease_id is not None:
                    if graph_admission is not None or (
                        (graph_template_id is None) != (graph_pair_id is None)
                        and not (graph_deployment_decision is not None and graph_template_id is not None and graph_pair_id is None)
                    ):
                        raise ValueError("sampled graphs require their original discovery or paired preparation")
                    self.graph_decision = graph_deployment_decision
                    prepared = {"status": "recorded", "lease_id": graph_sampling_lease_id}
                elif graph_admission is not None:
                    if graph_template_id is None or graph_pair_id is not None:
                        raise ValueError("guarded graphs require one primary template")
                    self.graph_decision = self.provider_receipts.prepare_guarded_primary_graph(
                        graph_template_id, None, comparison_input_sha256 or "", graph_admission,
                        max_workers=max_workers,
                    )
                    self.graph_preparation_id = self.provider_receipts.graph_preparation_id
                    prepared = {"status": "recorded", "lease_id": self.graph_preparation_id}
                else:
                    prepared = json.loads(_native.optimize_begin_provider_graph(json.dumps({
                        "batch_id": self.provider_receipts.batch_id,
                        "max_workers": max_workers,
                        "input_sha256": comparison_input_sha256,
                        "template_execution_id": graph_template_id,
                        "comparison_pair_id": graph_pair_id,
                    })))
                if prepared.get("status") == "recorded":
                    self.graph_preparation_id = prepared["lease_id"]
                    self.provider_receipts.graph_preparation_id = self.graph_preparation_id
                    self.provider_receipts.graph_template_active = graph_template_id is not None
                    self.provider_receipts.graph_discovery_active = graph_sampling_lease_id is not None and graph_template_id is None
                elif graph_template_id is not None:
                    raise RuntimeError(prepared.get("reason", "graph preparation was rejected"))
                else:
                    self.reason = "graph_preparation_unavailable"
            except Exception:
                if graph_template_id is not None:
                    raise
                self.reason = "graph_preparation_unavailable"
        parent = _current.get()
        if parent is not None:
            with parent[0].lock:
                parent[0].reason = "nested_map"

    @contextmanager
    def item(self, index: int) -> Iterator[None]:
        token = _current.set((self, index))
        try:
            with self.provider_receipts.item(index):
                yield
        finally:
            _current.reset(token)

    def finish(
        self,
        *,
        wall_latency_ms: float,
        succeeded: bool,
        results: list[Any] | None = None,
    ) -> ParallelObservation:
        provider_receipts = self.provider_receipts.seal(self.sdk_calls)
        if self.graph_preparation_id is not None:
            self.provider_receipts.finish_economics(wall_latency_ms)
        graph_acquisition = self.provider_receipts.acquisition_report()
        with self.lock:
            self.closed = True
            sdk_calls = sum(self.sdk_calls)
            observed_calls = sum(
                outcome is not None for item in self.plans for _, outcome in item
            )
            reason = self.reason
            if not succeeded:
                reason = "callback_failed"
            elif self.application_version is None:
                reason = "application_version_required"
            elif reason is None and any(
                count == 0 or count != len(plans)
                for count, plans in zip(self.sdk_calls, self.plans)
            ):
                reason = "unprofiled_sdk_call"
            elif reason is None and observed_calls != sdk_calls:
                reason = "incomplete_outcomes"
            calls = []
            if reason is None:
                for item, plans in enumerate(self.plans):
                    for call, (plan, outcome) in enumerate(plans):
                        if getattr(plan, "counterfactual", None) is not None:
                            reason = "auxiliary_calls_require_receipts"
                            break
                        if not isinstance(getattr(plan, "source_call_json", None), str):
                            reason = "source_call_required"
                            break
                        assert outcome is not None
                        calls.append(
                            {
                                "position": {"item": item, "call": call},
                                "source_call_json": plan.source_call_json,
                                "plan_json": plan.raw_json,
                                "cost_source": outcome.get("cost_source", "unknown"),
                                "outcome": outcome,
                                "scalar_observation_token": getattr(
                                    plan, "observation_token", None
                                ),
                            }
                        )
            # A background probe can retain a copied context after the map
            # exits. Its closed collector must not keep every source request.
            self.plans.clear()

        fields: dict[str, Any] = {
            "sdk_calls": sdk_calls,
            "observed_calls": observed_calls,
            "wall_latency_ms": wall_latency_ms,
            "provider_receipt_batch_id": self.provider_receipts.batch_id,
            "provider_receipts": provider_receipts,
            "graph_acquisition": graph_acquisition,
            "graph_decision": self.graph_decision,
        }
        if reason is not None:
            return ParallelObservation(status="abstained", reason=reason, **fields)
        comparison = None
        comparison_reason = None
        if self.comparison_requested:
            version = GRAPH_COMPARISON_VERSION if self.graph_comparison_requested else COMPARISON_VERSION
            output_sha256 = comparison_fingerprint(results, version=version)
            if self.comparison_input_sha256 is None:
                comparison_reason = "comparison_inputs_unsupported"
            elif output_sha256 is None:
                comparison_reason = "comparison_results_unsupported"
            else:
                comparison = {
                    "version": version,
                    "input_sha256": self.comparison_input_sha256,
                    "output_sha256": output_sha256,
                }
        try:
            from agentc import _native

            receipt = json.loads(
                _native.optimize_observe_multi_call(
                    json.dumps(
                        {
                            "application_version": self.application_version,
                            "callback_version": self.callback_version,
                            "item_count": self.item_count,
                            "max_workers": self.max_workers,
                            "wall_latency_ms": wall_latency_ms,
                            "calls": calls,
                            "comparison": comparison,
                            "comparison_requested": self.comparison_requested,
                            "graph_comparison_requested": self.graph_comparison_requested,
                            "graph_preparation_id": self.graph_preparation_id,
                            "execution_contract": self.execution_contract,
                            "comparison_version": self.comparison_version,
                            "provider_receipt_batch_id": self.provider_receipts.batch_id,
                        },
                        allow_nan=False,
                    )
                )
            )
            if receipt["status"] == "recorded":
                totals, key = receipt["totals"], receipt["key"]
                return ParallelObservation(
                    status="recorded",
                    reason=receipt.get("comparison_reason") or comparison_reason,
                    comparison_token=receipt.get("comparison_token"),
                    cost_usd=totals["cost_usd"],
                    input_tokens=totals["input_tokens"],
                    output_tokens=totals["output_tokens"],
                    call_site_version=key["call_site_version"],
                    execution_plan_id=key["execution_plan_id"],
                    cost_source=receipt.get("cost_source"),
                    graph_execution_id=receipt.get("graph_execution_id"),
                    graph_comparison_token=receipt.get("graph_comparison_token"),
                    **fields,
                )
            if receipt["status"] == "pending_billing":
                key = receipt.get("key") or {}
                return ParallelObservation(
                    status="pending_billing",
                    reason="verified_provider_costs_required",
                    graph_execution_id=receipt.get("graph_execution_id"),
                    graph_comparison_token=receipt.get("graph_comparison_token"),
                    cost_source=receipt.get("cost_source"),
                    call_site_version=key.get("call_site_version"),
                    execution_plan_id=key.get("execution_plan_id"),
                    **fields,
                )
        except Exception:
            return ParallelObservation(
                status="abstained", reason="native_observation_unavailable", **fields
            )
        return ParallelObservation(
            status="abstained", reason=receipt.get("reason", "native_receipt_rejected"), **fields
        )
