"""Durable SDK dispatch receipts; prompt bodies and credentials stay local.

Receipts measure SDK provider invocations. Only standard clients with zero
retries can attest the corresponding HTTP attempt count. Unknown failures
remain unknown, and a late probe can complete after its foreground is sealed.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
import time
import uuid
from functools import lru_cache
from importlib.metadata import version
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

VERSION = "provider-dispatch-receipts-v1"
_current: ContextVar[tuple["ProviderReceiptBatch", int, int] | None] = ContextVar(
    "agentc_provider_receipt_batch", default=None
)
_active_http: ContextVar[Any] = ContextVar("agentc_provider_receipt_http", default=None)
_http_originals: list[tuple[Any, Any, Any]] = []


def _note_http(invocation: Any, args: Any, kwargs: Any) -> None:
    invocation.http_attempts += 1
    if getattr(invocation, "tariff_active", False):
        request = args[0] if args else kwargs.get("request")
        url = str(getattr(request, "url", ""))
        suffix = (
            "/chat/completions"
            if invocation.protocol == "openai.chat.completions.v1"
            else "/v1/messages"
        )
        invocation.billing_scope_sha256 = (
            hashlib.sha256(
                ("billing-scope-v1\0" + url[: -len(suffix)].rstrip("/")).encode()
            ).hexdigest()
            if invocation.http_attempts == 1
            and getattr(request, "method", None) == "POST"
            and url.endswith(suffix)
            else None
        )


def patch_http_dispatch() -> None:
    """Count HTTPX sends only while an SDK dispatch receipt owns the context."""
    if _http_originals:
        return
    try:
        import httpx
        import wrapt
    except ImportError:
        return

    def send(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        invocation = _active_http.get()
        if invocation is not None:
            _note_http(invocation, args, kwargs)
        return wrapped(*args, **kwargs)

    async def send_async(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        invocation = _active_http.get()
        if invocation is not None:
            _note_http(invocation, args, kwargs)
        return await wrapped(*args, **kwargs)

    try:
        for cls, hook in ((httpx.Client, send), (httpx.AsyncClient, send_async)):
            original = cls._send_single_request
            wrapped = wrapt.FunctionWrapper(original, hook)
            setattr(cls, "_send_single_request", wrapped)
            _http_originals.append((cls, original, wrapped))
    except BaseException:
        unpatch_http_dispatch()


def unpatch_http_dispatch() -> None:
    for cls, original, wrapped in _http_originals:
        if cls.__dict__.get("_send_single_request") is wrapped:
            setattr(cls, "_send_single_request", original)
    _http_originals.clear()


def reject_unjournaled_graph_dispatch() -> None:
    """Hold SDK paths that cannot reserve a frozen graph's physical receipt."""
    current = _current.get()
    if current is not None and not current[0].dispatch_contract_active:
        try:
            _verify_unbudgeted_campaign(current[0])
        except Exception:
            pass
    if current is not None and current[0].dispatch_contract_active:
        with current[0].lock:
            current[0].failed = True
        raise RuntimeError("prepared graphs require a journaled, non-streaming SDK dispatch")


class GraphCampaignBindingStop(BaseException):
    """A campaign rejection must survive instrumentation and optimizer fallback."""


def _verify_unbudgeted_campaign(batch: ProviderReceiptBatch) -> None:
    if batch.application_version is None:
        return
    from agentc import _native

    binding = json.loads(_native.optimize_record_provider_receipt(json.dumps({
        "operation": "verify_unbudgeted_graph", "batch_id": batch.batch_id,
    })))
    if binding.get("hard_stop") == "graph_campaign_binding":
        raise GraphCampaignBindingStop(binding["reason"])


class ProviderReceiptBatch:
    def __init__(
        self,
        application: str | None,
        callback: str,
        count: int,
        *,
        batch_id: str | None = None,
    ) -> None:
        self.application_version = application
        self.batch_id = batch_id or uuid.uuid4().hex
        self.graph_preparation_id: str | None = None
        self.graph_template_active = False
        self.graph_discovery_active = False
        self.graph_role_program_active = False
        self.graph_acquisition: dict[str, Any] | None = None
        self.lock = threading.Lock()
        self.positions: dict[int, tuple[int, int]] = {}
        self.bindings: dict[tuple[int, int], tuple[Any, Any]] = {}
        self.request_bindings: dict[tuple[int, int], str | None] = {}
        self.next_ordinal: dict[tuple[int, int], int] = {}
        self.failed = False
        self._record(
            "begin_batch",
            batch={
                "version": VERSION,
                "batch_id": self.batch_id,
                "application_version": application,
                "callback_version": callback,
                "item_count": count,
            },
        )

    def _record(self, operation: str, **data: Any) -> dict[str, Any] | None:
        hard_stop: str | None = None
        try:
            from agentc import _native

            value = json.loads(
                _native.optimize_record_provider_receipt(
                    json.dumps({"operation": operation, **data}, allow_nan=False)
                )
            )
            if value.get("hard_stop") == "graph_campaign_binding":
                hard_stop = str(value["reason"])
            if value.get("status") == "recorded":
                acquisition = value.get("graph_acquisition")
                if isinstance(acquisition, dict):
                    self.graph_acquisition = acquisition
                summary = value.get("summary", {})
                return summary if isinstance(summary, dict) else {}
        except BaseException:
            if self.dispatch_contract_active and operation == "begin_call":
                raise RuntimeError("prepared graph physical dispatch could not be verified")
            pass
        with self.lock:
            self.failed = True
        if hard_stop is not None:
            raise GraphCampaignBindingStop(hard_stop)
        if self.dispatch_contract_active and operation == "begin_call":
            raise RuntimeError("prepared graph physical dispatch was rejected")
        return None

    @property
    def dispatch_contract_active(self) -> bool:
        return self.graph_template_active or self.graph_role_program_active or self.graph_discovery_active

    def prepare_role_graph(
        self, program: dict[str, Any], input_sha256: str,
        *, source_pair_id: str | None = None,
    ) -> str:
        """Freeze explicitly prepared physical work before any SDK invocation.

        The native contract validates the full driver and every role binding.
        This private acquisition seam supplies no automatic scheduling or
        quality, financial or resource admission.
        """
        from agentc import _native

        result = json.loads(_native.optimize_begin_provider_graph(json.dumps({
            "batch_id": self.batch_id,
            "max_workers": program["program"]["graph"]["calls"]["max_workers"],
            "input_sha256": input_sha256,
            "template_execution_id": None,
            "comparison_pair_id": source_pair_id,
            "role_program": program,
        }, allow_nan=False)))
        if result.get("status") != "recorded":
            raise RuntimeError(result.get("reason", "physical graph preparation was rejected"))
        self.graph_preparation_id = result["lease_id"]
        self.graph_role_program_active = True
        return self.graph_preparation_id

    def prepare_guarded_role_graph(
        self, program: dict[str, Any], input_sha256: str,
        admission: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically admit and freeze a graph before any physical SDK call.

        The native clock and current evidence decide admission. Every later
        physical reservation rechecks quality, economics and resource policy.
        """
        return self._prepare_guarded_graph({
            "batch_id": self.batch_id,
            "max_workers": program["program"]["graph"]["calls"]["max_workers"],
            "input_sha256": input_sha256,
            "template_execution_id": None,
            "comparison_pair_id": None,
            "role_program": program,
        }, admission)

    def prepare_guarded_primary_graph(
        self, template_execution_id: str, bindings: list[dict[str, Any]] | None,
        input_sha256: str, admission: dict[str, Any], *, max_workers: int,
    ) -> dict[str, Any]:
        """Admit a primary template; None binds requests at their SDK dispatch."""
        return self._prepare_guarded_graph({
            "batch_id": self.batch_id, "max_workers": max_workers,
            "input_sha256": input_sha256,
            "template_execution_id": template_execution_id,
            "comparison_pair_id": None, "role_program": None,
        }, admission, bindings, dynamic_primary_nodes=bindings is None)

    def _prepare_guarded_graph(
        self, preparation: dict[str, Any], admission: dict[str, Any],
        bindings: list[dict[str, Any]] | None = None,
        *, dynamic_primary_nodes: bool = False,
    ) -> dict[str, Any]:
        from agentc import _native

        admission = dict(admission)
        campaign = admission.pop("campaign", None)
        result = json.loads(_native.optimize_record_provider_receipt(json.dumps({
            "operation": "begin_graph_deployment",
            "request": {"preparation": preparation, "admission": admission,
                        "primary_bindings": bindings or [],
                        "dynamic_primary_nodes": dynamic_primary_nodes,
                        **({"campaign": campaign} if campaign is not None else {})},
        }, allow_nan=False)))
        outcome = result.get("outcome", {})
        lease = outcome.get("lease")
        if result.get("status") != "recorded" or not isinstance(lease, dict):
            raise RuntimeError(result.get("reason", "graph deployment is not admitted"))
        self.graph_preparation_id = lease["lease_id"]
        self.graph_role_program_active = preparation["role_program"] is not None
        self.graph_template_active = preparation["template_execution_id"] is not None
        decision: dict[str, Any] = outcome["decision"]
        return decision

    @staticmethod
    def graph_campaign_report(campaign_id: str) -> dict[str, Any]:
        """Read shared commitments without running callbacks or SDK calls."""
        from agentc import _native

        value = json.loads(_native.optimize_record_provider_receipt(json.dumps({
            "operation": "graph_campaign", "campaign_id": campaign_id,
        })))
        if value.get("status") != "recorded":
            raise RuntimeError(value.get("reason", "shared graph campaign custody was rejected"))
        report: dict[str, Any] = value["campaign"]
        return report

    def finish_guarded_primary_graph(self, comparison: dict[str, Any]) -> dict[str, Any]:
        """Publish the callback result with its original completed SDK receipts."""
        from agentc import _native

        result = json.loads(_native.optimize_record_provider_receipt(json.dumps({
            "operation": "finish_primary_graph_deployment",
            "lease_id": self.graph_preparation_id, "comparison": comparison,
        }, allow_nan=False)))
        if result.get("status") != "recorded":
            raise RuntimeError(result.get("reason", "primary graph completion was rejected"))
        execution: dict[str, Any] = result["execution"]
        return execution

    @contextmanager
    def item(self, index: int) -> Iterator[None]:
        token = _current.set((self, index, -1))
        try:
            yield
        finally:
            _current.reset(token)

    def seal(self, counts: list[int]) -> dict[str, Any] | None:
        summary = self._record(
            "seal_batch",
            batch_id=self.batch_id,
            persistence_failed=self.failed,
            logical_calls=[
                {"item": item, "call": call}
                for item, count in enumerate(counts)
                for call in range(count)
            ],
        )
        if summary is not None and self.failed:
            summary["cost_complete"] = False
            summary["persistence_failed"] = True
        return summary

    def finish_economics(self, wall_latency_ms: float) -> None:
        self._record(
            "finish_batch_economics",
            batch_id=self.batch_id,
            wall_latency_ms=wall_latency_ms,
        )

    def acquisition_report(self) -> dict[str, Any] | None:
        if self.graph_preparation_id is None:
            return None
        self._record("report", batch_id=self.batch_id)
        return self.graph_acquisition


def note_logical_call(index: int) -> None:
    current = _current.get()
    if current is not None:
        batch, item, _ = current
        _current.set((batch, item, index))


def graph_template_position() -> tuple[str, int, int] | None:
    current = _current.get()
    if current is None:
        return None
    batch, item, call = current
    if not batch.graph_template_active or call < 0:
        return None
    return batch.graph_preparation_id or "", item, call


def graph_discovery_position() -> tuple[str, int, int] | None:
    current = _current.get()
    if current is None:
        return None
    batch, item, call = current
    if not batch.graph_discovery_active or call < 0:
        return None
    return batch.graph_preparation_id or "", item, call


def bind_plan(plan: Any) -> None:
    current = _current.get()
    if current is not None:
        batch, item, call = current
        with batch.lock:
            batch.positions[id(plan)] = (item, call)
            batch.bindings[(item, call)] = _plan_identity(
                getattr(plan, "raw_json", None)
            )
            batch.request_bindings[(item, call)] = _logical_fingerprint(
                getattr(plan, "source_call_json", None)
            )


def _logical_fingerprint(raw: Any) -> str | None:
    try:
        call = json.loads(raw or "null")
        context = call["parameters"]["extra"]["agentc_route_context"]
        value = {
            "version": VERSION,
            "protocol": context["provider_protocol"],
            "model": call["model"],
            "controls": context["provider_configuration"],
            "payload": context["sdk_response_cache"],
        }
        if not value["controls"]["supported"] or not value["payload"]["supported"]:
            return None
        return hashlib.sha256(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    except (TypeError, ValueError, KeyError):
        return None


def _plan_identity(raw: Any) -> tuple[Any, Any]:
    try:
        observed = json.loads(raw or "{}").get("agentc_observation_context", {})
        return observed.get("key"), observed.get("runtime_version")
    except (TypeError, ValueError):
        return None, None


@lru_cache(maxsize=8)
def _sdk_version(name: str) -> str:
    return version(name)


def _transport_fingerprint(instance: Any, protocol: str) -> str | None:
    try:
        sdk = "openai" if protocol == "openai.chat.completions.v1" else "anthropic"
        value = {
            "version": VERSION,
            "sdk": _sdk_version(sdk),
            "httpx": _sdk_version("httpx"),
            "base_url": str(instance._client.base_url),
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    except (AttributeError, TypeError, ValueError):
        return None


def _billing_scope_fingerprint(instance: Any) -> str | None:
    try:
        return hashlib.sha256(
            ("billing-scope-v1\0" + str(instance._client.base_url).rstrip("/")).encode()
        ).hexdigest()
    except (AttributeError, TypeError, ValueError):
        return None


def _provider_identity_fingerprint(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not all(c.isascii() and (c.isalnum() or c in "_-.") for c in value)
    ):
        return None
    return hashlib.sha256(("provider-identity-v1\0" + value).encode()).hexdigest()


def _generation_id(response: Any, error: BaseException | None) -> Any:
    if error is None:
        return getattr(response, "id", None)
    try:
        body = error.response.json()  # type: ignore[attr-defined]
        return body.get("id") if isinstance(body, dict) else None
    except (AttributeError, TypeError, ValueError):
        return None


def _transport_verified(instance: Any, protocol: str) -> bool:
    try:
        client = instance._client
    except AttributeError:
        return False
    return _client_transport_verified(client, protocol)


def _client_transport_verified(client: Any, protocol: str) -> bool:
    try:
        if not _http_originals:
            return False
        import httpx

        transports = [client._client._transport, *client._client._mounts.values()]
        if any(
            transport is not None
            and type(transport)
            not in {httpx.HTTPTransport, httpx.AsyncHTTPTransport, httpx.MockTransport}
            for transport in transports
        ):
            return False
        if protocol == "openai.chat.completions.v1":
            import openai

            standard = type(client) in {openai.OpenAI, openai.AsyncOpenAI}
            standard_http = {
                httpx.Client,
                httpx.AsyncClient,
                openai.DefaultHttpxClient,
                openai.DefaultAsyncHttpxClient,
            }
        else:
            import anthropic

            standard = type(client) in {anthropic.Anthropic, anthropic.AsyncAnthropic}
            standard_http = {
                httpx.Client,
                httpx.AsyncClient,
                anthropic.DefaultHttpxClient,
                anthropic.DefaultAsyncHttpxClient,
            }
        return bool(
            standard
            and type(client._client) in standard_http
            and client._client.auth is None
            and all(
                transport is None
                or type(transport) is httpx.MockTransport
                or getattr(transport._pool, "_retries", None) == 0
                for transport in transports
            )
            and type(client.max_retries) is int
            and client.max_retries == 0
            and not any(client._client.event_hooks.values())
            and not getattr(client, "_custom_headers", None)
            and not getattr(client, "_custom_query", None)
        )
    except (AttributeError, TypeError):
        return False


def _request_fingerprint(kwargs: dict[str, Any], protocol: str) -> str | None:
    from agentc._provider_config import declaration
    from agentc._response_cache import request_declaration

    try:
        kwargs = dict(kwargs)
        headers = kwargs.get("extra_headers")
        if isinstance(headers, dict):
            kwargs["extra_headers"] = {
                key: value
                for key, value in headers.items()
                if key.lower()
                not in {"x-stainless-raw-response", "x-stainless-override-cast-to"}
            }
        controls = declaration(kwargs, provider_protocol=protocol)
        payload = request_declaration(kwargs)
        if not controls["supported"] or not payload["supported"]:
            return None
        value = {
            "version": VERSION,
            "protocol": protocol,
            "model": kwargs.get("model"),
            "controls": controls,
            "payload": payload,
        }
        return hashlib.sha256(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    except (TypeError, ValueError, OverflowError):
        return None


class _Invocation:
    def __init__(self, instance: Any, kwargs: dict[str, Any], protocol: str) -> None:
        current = _current.get()
        self.batch: ProviderReceiptBatch | None = None
        self.standalone = False
        self.started = time.perf_counter()
        from agentc._provider_tariff import active_scope, request_scope_supported

        tariff = active_scope()
        self.tariff_active = tariff is not None
        if current is None and tariff is not None:
            current = (
                ProviderReceiptBatch(None, "standalone-token-tariff-v1", 1),
                0,
                0,
            )
            self.standalone = True
        if current is None:
            return
        from agentc._dispatch_context import (
            current_provider_dispatch,
            current_provider_plan,
        )

        batch, item, call = current
        owner = current_provider_plan()
        with batch.lock:
            position = batch.positions.get(id(owner), (item, call))
            ordinal = batch.next_ordinal.get(position, 0)
            batch.next_ordinal[position] = ordinal + 1
            key, runtime = batch.bindings.get(position, (None, None))
            logical_request = batch.request_bindings.get(position)
        if self.standalone:
            key, runtime = _plan_identity(getattr(owner, "raw_json", None))
            logical_request = _request_fingerprint(kwargs, protocol)
        self.batch = batch
        if batch.graph_template_active:
            from agentc import _native

            value = json.loads(_native.optimize_approve_provider_graph_node(
                batch.graph_preparation_id or "",
                json.dumps({"item": position[0], "call": position[1]}),
                getattr(owner, "source_call_json", None) or "null",
                getattr(owner, "raw_json", None) or "null",
                _request_fingerprint(kwargs, protocol),
                _transport_fingerprint(instance, protocol),
            ))
            if value.get("status") != "recorded":
                with batch.lock:
                    batch.failed = True
                raise RuntimeError(value.get("reason", "prepared graph node was rejected"))
        self.protocol = protocol
        self.model = str(kwargs.get("model") or "")
        self.invocation_id = uuid.uuid4().hex
        context = current_provider_dispatch()
        self.role = context.kind if context is not None else "primary"
        carrier = (
            getattr(owner, "counterfactual", None)
            if context is not None and context.kind == "exploration"
            else owner
            if context is None or context.kind != "shadow"
            else None
        )
        if carrier is not None:
            identities = getattr(carrier, "physical_invocations", None)
            if isinstance(identities, list):
                if context is not None and context.kind == "primary" and ordinal == 0:
                    identities.clear()
                identities.append(
                    {"batch_id": batch.batch_id, "invocation_id": self.invocation_id}
                )
        self.billing_scope_sha256 = _billing_scope_fingerprint(instance)
        if tariff is not None:
            tariff.note_batch(batch.batch_id)
        self.transport_verified = _transport_verified(instance, protocol)
        self.http_attempts = 0
        dispatched_key, dispatched_runtime = _plan_identity(
            context.native_plan_json if context is not None else None
        )
        parent_key, _ = _plan_identity(
            context.parent_native_plan_json if context is not None else None
        )
        batch._record(
            "begin_call",
            batch_id=batch.batch_id,
            invocation={
                "invocation_id": self.invocation_id,
                "position": {"item": position[0], "call": position[1]},
                "ordinal": ordinal,
                "role": context.kind if context is not None else "primary",
                "provider_protocol": protocol,
                "requested_model_id": self.model,
                "request_sha256": _request_fingerprint(kwargs, protocol),
                "logical_request_sha256": logical_request,
                "transport_sha256": _transport_fingerprint(instance, protocol),
                "logical_plan_key": key,
                "logical_runtime": runtime,
                "dispatch_plan_key": dispatched_key,
                "dispatch_runtime": dispatched_runtime,
                "parent_plan_key": parent_key,
                "transport_verified": self.transport_verified,
                "tariff_declaration": {
                    "version": "provider-token-tariff-v1",
                    "snapshot_sha256": tariff.snapshot_sha256,
                    "endpoint_sha256": self.billing_scope_sha256,
                    "request_scope_supported": request_scope_supported(
                        kwargs, protocol
                    ),
                }
                if tariff is not None
                else None,
            },
        )
        self.started = time.perf_counter()

    def finish(self, response: Any = None, error: BaseException | None = None) -> None:
        if self.batch is None:
            return
        from agentc._patches._optimizer_glue import (
            _reported_inference_cost,
            estimate_cost_usd,
        )
        from agentc._billing_usage import projection

        usage = getattr(response, "usage", None)
        if usage is None and type(response).__module__.startswith(
            ("openai.", "anthropic.")
        ):
            parse = getattr(response, "parse", None)
            if callable(parse) and not inspect.iscoroutinefunction(parse):
                try:
                    response = parse()
                    usage = getattr(response, "usage", None)
                except Exception:
                    pass
        fields = (
            ("input_tokens", "output_tokens")
            if self.protocol == "anthropic.messages.v1"
            else ("prompt_tokens", "completion_tokens")
        )
        counts = [getattr(usage, field, None) for field in fields]
        counts = [
            value if type(value) is int and 0 <= value < 2**32 else None
            for value in counts
        ]
        cost = _reported_inference_cost(usage, fields)
        source = "provider_reported" if cost is not None else "unknown"
        if cost is None and counts[0] is not None and counts[1] is not None:
            cost = estimate_cost_usd(self.model, int(counts[0]), int(counts[1]))
            source = "estimated"
        status = (
            "completed"
            if error is None
            else "cancelled"
            if isinstance(error, asyncio.CancelledError)
            else "failed"
        )
        attempts = self.http_attempts if self.transport_verified else None
        model_version = getattr(response, "model", None)
        self.batch._record(
            "finish_call",
            batch_id=self.batch.batch_id,
            invocation_id=self.invocation_id,
            completion={
                "status": status,
                "model_version": model_version
                if isinstance(model_version, str) and model_version
                else None,
                "input_tokens": counts[0],
                "output_tokens": counts[1],
                "cost_usd": cost,
                "cost_source": source,
                "latency_ms": (time.perf_counter() - self.started) * 1000,
                "http_attempts": attempts,
                "exception_type": None if error is None else type(error).__name__,
                "billing_scope_sha256": self.billing_scope_sha256,
                "provider_generation_sha256": _provider_identity_fingerprint(
                    _generation_id(response, error)
                ),
                "provider_request_id_sha256": _provider_identity_fingerprint(
                    getattr(response, "_request_id", None)
                    if error is None
                    else getattr(error, "request_id", None)
                ),
                "billing_usage": projection(response, self.protocol)
                if error is None
                else None,
            },
        )
        if error is None and self.role == "validation" and self.batch.graph_role_program_active:
            self.batch._record(
                "record_graph_validation", lease_id=self.batch.graph_preparation_id,
                invocation_id=self.invocation_id,
                decision=_graph_validation_decision(response),
            )
        if self.standalone:
            self.batch.seal([1])


def _graph_validation_decision(response: Any) -> str:
    """Interpret one strict boolean object; ambiguous outputs remain unknown."""
    from agentc._patches._optimizer_glue import _response_output_text

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate validation key")
            value[key] = item
        return value

    try:
        choices = getattr(response, "choices", None)
        if choices is not None and (not isinstance(choices, (list, tuple)) or len(choices) != 1):
            return "unknown"
        text = _response_output_text(response)
        if text is None:
            return "unknown"
        value = json.loads(text, object_pairs_hook=unique_object)
        if type(value) is not dict or set(value) != {"accepted"} or type(value["accepted"]) is not bool:
            return "unknown"
        return "accepted" if value["accepted"] else "rejected"
    except (TypeError, ValueError):
        return "unknown"


def instrument_provider(
    wrapped: Any, instance: Any, protocol: str, *, asynchronous: bool = False
) -> Any:
    if _current.get() is None:
        from agentc._provider_tariff import active_scope

        if active_scope() is None:
            return wrapped

    def begin(kwargs: dict[str, Any]) -> _Invocation | None:
        try:
            current = _current.get()
            if current is not None and not current[0].dispatch_contract_active:
                _verify_unbudgeted_campaign(current[0])
            return _Invocation(instance, kwargs, protocol)
        except GraphCampaignBindingStop:
            raise
        except BaseException:
            current = _current.get()
            if current is not None:
                with current[0].lock:
                    current[0].failed = True
                if current[0].dispatch_contract_active:
                    raise
            return None

    def finish(
        invocation: _Invocation | None,
        response: Any = None,
        error: BaseException | None = None,
    ) -> None:
        if invocation is not None:
            try:
                invocation.finish(response, error)
            except BaseException:
                if invocation.batch is not None:
                    with invocation.batch.lock:
                        invocation.batch.failed = True

    if asynchronous:

        async def invoke_async(*args: Any, **kwargs: Any) -> Any:
            invocation = begin(kwargs)
            token = _active_http.set(invocation)
            try:
                response = await wrapped(*args, **kwargs)
            except BaseException as error:
                finish(invocation, error=error)
                raise
            finally:
                _active_http.reset(token)
            finish(invocation, response=response)
            return response

        return invoke_async

    def invoke(*args: Any, **kwargs: Any) -> Any:
        invocation = begin(kwargs)
        token = _active_http.set(invocation)
        try:
            response = wrapped(*args, **kwargs)
        except BaseException as error:
            finish(invocation, error=error)
            raise
        finally:
            _active_http.reset(token)
        finish(invocation, response=response)
        return response

    return invoke
