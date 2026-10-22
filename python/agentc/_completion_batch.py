"""Prepared, stateless SDK completions with native scheduling admission.

This driver materializes the entire request list before invoking a provider.
It accepts SDK clients and data, never application dispatch callbacks. Original
responses retain their SDK types and input order. Explicit calibration modes
run only the requested batch; comparing receipts never replays it.
"""

from __future__ import annotations

import contextvars
import hashlib
import inspect
import json
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Literal, Mapping

from agentc._batch_observation import (
    BatchObservation,
    ParallelObservation,
    comparison_fingerprint,
    COMPARISON_VERSION,
)
from agentc._optimizer import Plan, _hydrate_dispatch_metadata, _plan_from_dict
from agentc._prepared_plan import prepared_plan, request_json

VERSION = "independent-provider-requests-v1"
DECLARATION_KEY = "agentc_prepared_provider_batch"
_PROTOCOLS = {
    "openai": "openai.chat.completions.v1",
    "anthropic": "anthropic.messages.v1",
    "litellm": "litellm.completion.v1",
}
_KEYS = frozenset(
    {
        "model",
        "messages",
        "system",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "stop",
        "n",
        "seed",
        "response_format",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "thinking",
        "reasoning_effort",
        "store",
        "stream",
        "num_retries",
        "allowed_openai_params",
        "extra_headers",
    }
)


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    provider: Literal["openai", "anthropic", "litellm"]
    kwargs: Mapping[str, Any]
    client: Any = None


@dataclass(frozen=True, slots=True)
class BatchPolicy:
    remaining_batches: int
    objective: Literal["latency", "cost"] = "latency"
    max_expected_cost_usd: float | None = None
    max_expected_latency_ms: float | None = None
    economic_quantity: Literal["billed_usd", "published_token_tariff_usd"] = "billed_usd"
    max_expected_tariff_nanodollars: int | None = None


@dataclass(frozen=True, slots=True)
class BatchSchedule:
    max_workers: int
    selected_reference: bool
    reason: str
    completed_acquisition_cost_usd: float | None = None
    completed_acquisition_latency_ms: float | None = None
    acquisition_cost_complete: bool | None = None
    unknown_acquisition_invocations: int | None = None
    incomplete_acquisition_batches: int | None = None
    economic_quantity: str = "billed_usd"
    expected_upper_tariff_nanodollars: int | None = None
    acquisition_tariff_complete: bool | None = None
    acquisition_upper_tariff_nanodollars: int | None = None


@dataclass
class _Request:
    provider: Literal["openai", "anthropic", "litellm"]
    kwargs: dict[str, Any]
    invoke: Callable[..., Any]
    transport_version: dict[str, Any]
    client: Any
    transport_identity: tuple[int, int]
    source: dict[str, Any] | None = None
    plan: Plan | None = None


def _freeze(request: CompletionRequest) -> _Request:
    invoke: Callable[..., Any]
    if request.provider not in _PROTOCOLS or any(
        key not in _KEYS for key in request.kwargs
    ):
        raise ValueError(
            "prepared batches support explicit stateless completion controls only"
        )
    if comparison_fingerprint(dict(request.kwargs)) is None:
        raise ValueError("prepared requests must be complete finite JSON values")
    kwargs = json.loads(request_json(dict(request.kwargs)))
    if kwargs.get("stream") not in (None, False) or kwargs.get("store") not in (
        None,
        False,
    ):
        raise ValueError("prepared batches require non-streaming stateless requests")
    headers = kwargs.get("extra_headers")
    if headers and (
        not isinstance(headers, dict) or set(headers) != {"agentc-optimize"}
    ):
        raise ValueError(
            "prepared batches do not accept application transport hooks or headers"
        )
    if (
        not isinstance(kwargs.get("model"), str)
        or not kwargs["model"]
        or not isinstance(kwargs.get("messages"), list)
    ):
        raise ValueError("prepared requests need a model and materialized messages")
    # Images, attachments and opaque native blocks need a separate accounting
    # and independence contract. Tool declarations remain data, never execution.
    if any(
        type(message) is not dict or type(message.get("content")) is not str
        for message in kwargs["messages"]
    ):
        raise ValueError("prepared batches require materialized text messages")
    if request.provider == "litellm":
        import importlib

        litellm = importlib.import_module("litellm")

        if (
            type(kwargs.get("num_retries", 0)) is not int
            or kwargs.get("num_retries", 0) != 0
            or kwargs.get("allowed_openai_params", ["store"]) != ["store"]
            or any(
                getattr(litellm, name, None)
                for name in (
                    "callbacks",
                    "input_callback",
                    "success_callback",
                    "failure_callback",
                    "service_callback",
                    "_async_input_callback",
                    "_async_success_callback",
                    "_async_failure_callback",
                    "pre_call_rules",
                    "post_call_rules",
                    "model_alias_map",
                    "model_fallbacks",
                    "fallbacks",
                    "default_fallbacks",
                    "context_window_fallbacks",
                    "content_policy_fallbacks",
                    "cache",
                    "caching_with_models",
                    "headers",
                    "organization",
                    "project",
                    "proxy_auth",
                    "config_path",
                    "drop_params",
                    "modify_params",
                    "add_function_to_prompt",
                    "return_response_headers",
                )
            )
        ):
            raise ValueError(
                "prepared LiteLLM calls require no hooks, fallbacks, cache or implicit request changes"
            )
        if not kwargs["model"].startswith("openai/"):
            raise ValueError(
                "prepared LiteLLM calls require the OpenAI backend and an explicit standard OpenAI client"
            )
        direct_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key not in {"num_retries", "allowed_openai_params"}
        }
        direct = _freeze(CompletionRequest("openai", direct_kwargs, request.client))
        kwargs["num_retries"] = 0
        kwargs["store"] = False
        # LiteLLM's provider allowlist can lag the standard SDK. Explicitly
        # forward this one verified control so storage stays disabled on wire.
        kwargs["allowed_openai_params"] = ["store"]
        if not str(getattr(litellm.completion, "__module__", "")).startswith(
            "litellm."
        ):
            raise ValueError(
                "prepared LiteLLM calls cannot use application dispatch callbacks"
            )
        invoke = partial(litellm.completion, client=request.client)
        transport_version = {
            "sdk": version("litellm"),
            "provider": request.provider,
            "backend": direct.transport_version,
        }
    else:
        if request.provider == "openai":
            import openai

            if type(request.client) is not openai.OpenAI:
                raise ValueError(
                    "prepared OpenAI calls require a standard synchronous OpenAI client"
                )
            invoke = request.client.chat.completions.create
            kwargs["store"] = False
        else:
            import anthropic

            if type(request.client) is not anthropic.Anthropic:
                raise ValueError(
                    "prepared Anthropic calls require a standard synchronous Anthropic client"
                )
            invoke = request.client.messages.create
        client = request.client
        if (
            client.max_retries != 0
            or any(client._client.event_hooks.values())
            or getattr(client, "_custom_headers", None)
            or getattr(client, "_custom_query", None)
        ):
            raise ValueError(
                "prepared SDK clients require zero retries and no application hooks, headers or query controls"
            )
        if not str(getattr(invoke, "__module__", "")).startswith(
            request.provider + "."
        ):
            raise ValueError(
                "prepared requests cannot use application dispatch callbacks"
            )
        transport_version = {
            "sdk": version(request.provider),
            "provider": request.provider,
            "base_url": str(client.base_url),
        }
        parameters = inspect.signature(invoke).parameters
        if any(key not in parameters for key in kwargs) or any(
            name not in kwargs
            and parameter.default is inspect.Parameter.empty
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            for name, parameter in parameters.items()
        ):
            raise ValueError(
                "prepared request does not match the SDK method's complete argument contract"
            )
    http_client = None if request.client is None else request.client._client
    return _Request(
        request.provider,
        kwargs,
        invoke,
        transport_version,
        request.client,
        (id(http_client), id(getattr(http_client, "_transport", None))),
    )


def _assert_unchanged(request: _Request) -> None:
    checked = _freeze(
        CompletionRequest(request.provider, request.kwargs, request.client)
    )
    if (
        checked.transport_version != request.transport_version
        or checked.transport_identity != request.transport_identity
    ):
        raise RuntimeError("prepared SDK client changed after request preparation")


def _output(response: Any, provider: str) -> Any | None:
    """Versioned semantic projection; operational IDs and usage are excluded."""
    try:
        if provider == "anthropic":
            value = response.model_dump(mode="json")
            content = value.get("content")
            if not isinstance(content, list):
                return None
            for index, block in enumerate(content):
                if block.get("type") == "tool_use":
                    block["id"] = str(index)
                if block.get("type") not in {"text", "tool_use"}:
                    return None
            return {
                "role": value.get("role"),
                "content": content,
                "stop_reason": value.get("stop_reason"),
                "stop_sequence": value.get("stop_sequence"),
            }
        from agentc._patches._optimizer_glue import (
            _InvalidTextAction,
            _response_output_text,
        )

        choices = response.choices
        if not isinstance(choices, list) or not choices:
            return None
        projected = []
        for choice in choices:
            semantic = _response_output_text(SimpleNamespace(choices=[choice]))
            if semantic is None or isinstance(semantic, _InvalidTextAction):
                return None
            message = choice.message.model_dump(mode="json")
            for index, call in enumerate(message.get("tool_calls") or []):
                call["id"] = str(index)
            logprobs = getattr(choice, "logprobs", None)
            projected.append(
                {
                    "index": choice.index,
                    "finish_reason": choice.finish_reason,
                    "message": message,
                    "logprobs": None
                    if logprobs is None
                    else logprobs.model_dump(mode="json"),
                }
            )
        return projected
    except (AttributeError, TypeError, ValueError):
        return None


def completion_batch(
    requests: Iterable[CompletionRequest],
    *,
    site: str,
    policy: BatchPolicy,
    max_workers: int = 32,
    mode: Literal["guarded", "reference", "candidate"] = "guarded",
    on_observation: Callable[[ParallelObservation], None] | None = None,
    on_schedule: Callable[[BatchSchedule], None] | None = None,
) -> list[Any]:
    """Run prepared original completions once, preserving SDK response types.

    Guarded mode uses a qualified native schedule or a serial reference. A
    declared application version and explicit remaining deployment volume bind
    evidence and acquisition economics. Reference/candidate modes execute only
    the requested calibration batch; compare their completed receipts separately.
    """
    from agentc import _native, is_initialized
    from agentc._application_version import declaration
    from agentc._optimization_scope import decide_optimization
    from agentc._patches._optimizer_glue import (
        build_call_dict_anthropic,
        build_call_dict_openai,
    )

    if not is_initialized():
        raise RuntimeError("completion_batch requires agentc.init()")
    app = declaration()
    if app is None:
        raise ValueError("completion_batch requires an application_version")
    from agentc._application_version import _IDENTIFIER

    if not isinstance(site, str) or _IDENTIFIER.fullmatch(site) is None:
        raise ValueError(
            "prepared batch site must be a stable 1-128 character identifier"
        )
    if (
        type(max_workers) is not int
        or not 1 <= max_workers <= 32
        or type(policy.remaining_batches) is not int
        or policy.remaining_batches <= 0
        or policy.objective not in {"cost", "latency"}
        or policy.economic_quantity not in {"billed_usd", "published_token_tariff_usd"}
        or mode not in {"guarded", "reference", "candidate"}
    ):
        raise ValueError("prepared batch scheduling policy is invalid")
    from agentc._provider_tariff import active_scope
    tariff_policy = None
    if policy.economic_quantity == "published_token_tariff_usd":
        scope = active_scope()
        if scope is None or policy.max_expected_cost_usd is not None:
            raise ValueError("tariff decisions require a declared tariff scope and tariff cost limits")
        if policy.max_expected_tariff_nanodollars is not None and (
            type(policy.max_expected_tariff_nanodollars) is not int
            or not 0 <= policy.max_expected_tariff_nanodollars <= (1 << 64) - 1
        ):
            raise ValueError("tariff cost limit must be an unsigned integer in nanodollars")
        tariff_policy = {"quantity": policy.economic_quantity, "snapshot_sha256": scope.snapshot_sha256,
                         "max_expected_nanodollars": policy.max_expected_tariff_nanodollars}
    elif policy.max_expected_tariff_nanodollars is not None:
        raise ValueError("a tariff cost limit requires the tariff economic quantity")
    for value in (policy.max_expected_cost_usd, policy.max_expected_latency_ms):
        if value is not None and (
            isinstance(value, bool) or not math.isfinite(value) or value < 0
        ):
            raise ValueError(
                "prepared batch amortized limits must be finite and nonnegative"
            )
    started = time.perf_counter()
    frozen = [_freeze(request) for request in requests]
    if not frozen:
        return []
    site = "agentc.prepared." + site
    callback_version = hashlib.sha256(
        request_json(
            {
                "version": VERSION,
                "projection": "sdk-semantic-results-v1",
                "accounting": "physical-acquisition-economics-v1",
                "transports": [request.transport_version for request in frozen],
            }
        ).encode()
    ).hexdigest()
    eligible = all(
        decide_optimization(request.kwargs.get("extra_headers"), record=False).eligible
        for request in frozen
    )
    for request in frozen:
        protocol = _PROTOCOLS[request.provider]
        if request.provider == "anthropic":
            source = build_call_dict_anthropic(
                request.kwargs,
                call_site_id=site,
                trace_id_hex="0" * 32,
                span_id_hex="0" * 16,
                client=request.client,
            )
        else:
            source = build_call_dict_openai(
                request.kwargs,
                call_site_id=site,
                trace_id_hex="0" * 32,
                span_id_hex="0" * 16,
                provider_protocol=protocol,
                client=request.client,
            )
        source["parameters"]["extra"][DECLARATION_KEY] = {
            "version": VERSION,
            "provider_protocol": protocol,
            "retry_limit": 0,
            "application_callbacks": False,
            "remote_store": False,
        }
        request.source = source
        request.plan = Plan(kind="pass_through", source_call_json=json.dumps(source))
        _hydrate_dispatch_metadata(request.plan, source)
    provider_batch_id = uuid.uuid4().hex
    economic_owner = False
    schedule = BatchSchedule(
        max_workers=1, selected_reference=True, reason="native_preparation_unavailable"
    )
    try:
        value = json.loads(
            _native.optimize_prepare_provider_batch(
                json.dumps(
                    {
                        "application_version": app["sha256"],
                        "callback_version": callback_version,
                        "calls": [request.source for request in frozen],
                    }
                ),
                json.dumps(
                    {
                        "provider_receipt_batch_id": provider_batch_id,
                        "max_workers": max_workers,
                        "remaining_batches": policy.remaining_batches,
                        "objective": policy.objective,
                        "mode": mode,
                        "scope_eligible": eligible,
                        "max_expected_cost_usd": policy.max_expected_cost_usd,
                        "max_expected_latency_ms": policy.max_expected_latency_ms,
                        "tariff_policy": tariff_policy,
                    },
                    allow_nan=False,
                ),
            )
        )
        if value.get("status") == "prepared" and len(value["plans"]) == len(frozen):
            workers = value["max_workers"]
            if type(workers) is not int or not 1 <= workers <= min(
                max_workers, len(frozen)
            ):
                raise ValueError("native prepared schedule is invalid")
            for request, data in zip(frozen, value["plans"]):
                assert request.source is not None
                plan = _plan_from_dict(data, json.dumps(data))
                if plan.kind != "pass_through":
                    raise ValueError("prepared batches require original provider plans")
                plan.source_call_json = json.dumps(request.source)
                _hydrate_dispatch_metadata(plan, request.source)
                request.plan = plan
            if value.get("provider_receipt_batch_id") != provider_batch_id:
                raise ValueError("native provider economic owner is unavailable")
            economic_owner = True
            chosen = value["schedule"]
            accounting = chosen.get("acquisition_accounting", {})
            schedule = BatchSchedule(
                workers,
                workers == 1,
                str(chosen.get("reason")),
                chosen.get("completed_acquisition_cost_usd"),
                chosen.get("completed_acquisition_latency_ms"),
                accounting.get("cost_complete"),
                accounting.get("unknown_cost_invocations"),
                accounting.get("incomplete_batches"),
                str(chosen.get("economic_quantity", policy.economic_quantity)),
                next((row["estimate"]["expected_upper_nanodollars"]
                      for row in chosen.get("alternatives", [])
                      if row["max_workers"] == workers and row.get("estimate") is not None), None),
                accounting.get("coverage_complete") if policy.economic_quantity == "published_token_tariff_usd" else None,
                accounting.get("upper_nanodollars") if policy.economic_quantity == "published_token_tariff_usd" else None,
            )
    except Exception:
        pass
    if not economic_owner:
        raise RuntimeError(
            "completion_batch requires durable provider accounting before dispatch"
        )
    if not eligible:
        schedule = BatchSchedule(1, True, "optimization_excluded")
    batch = BatchObservation(
        application_version=app["sha256"],
        callback_version=callback_version,
        item_count=len(frozen),
        max_workers=schedule.max_workers,
        comparison_requested=mode != "guarded",
        provider_receipt_batch_id=provider_batch_id,
        comparison_input_sha256=comparison_fingerprint(list(range(len(frozen)))),
        execution_contract=VERSION,
        comparison_version=COMPARISON_VERSION,
    )
    if on_schedule is not None:
        try:
            on_schedule(schedule)
        except BaseException:
            pass
    succeeded = False
    results = None

    def run(index: int) -> Any:
        request = frozen[index]
        assert request.plan is not None
        _assert_unchanged(request)
        with (
            batch.item(index),
            prepared_plan(
                request.plan,
                _PROTOCOLS[request.provider],
                request.kwargs,
                site,
                dispatch_client=request.client
                if request.provider == "litellm"
                else None,
            ),
        ):
            return request.invoke(**request.kwargs)

    try:
        if schedule.max_workers == 1:
            results = [run(index) for index in range(len(frozen))]
        else:
            with ThreadPoolExecutor(max_workers=schedule.max_workers) as pool:
                futures = [
                    pool.submit(contextvars.copy_context().run, run, index)
                    for index in range(len(frozen))
                ]
                results = [future.result() for future in futures]
        succeeded = True
        return results
    finally:
        projected = (
            None
            if results is None
            else [
                _output(response, request.provider)
                for response, request in zip(results, frozen)
            ]
        )
        if projected is not None and any(value is None for value in projected):
            projected = [object()]
        try:
            observation = batch.finish(
                wall_latency_ms=(time.perf_counter() - started) * 1000.0,
                succeeded=succeeded,
                results=projected,
            )
            if economic_owner:
                batch.provider_receipts.finish_economics(
                    (time.perf_counter() - started) * 1000.0
                )
            if on_observation is not None:
                on_observation(observation)
        except BaseException:
            pass
