"""Shared helpers for routing SDK patches through the optimizer.

The OpenAI / Anthropic patches both need the same plumbing:
1. Build a Rust-shaped `Call` dict from vendor kwargs.
2. Ask the optimizer for a `Plan`.
3. Dispatch the plan (sync or async).
4. Build an `Outcome` (with `call_site_id`) and feed it back via
   `observe_outcome` so the cost model warms up.

This module owns the vendor-agnostic glue. Vendors translate
kwargs ↔ Call and response ↔ Outcome.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import threading
import time
from types import FrameType
from typing import Any, Awaitable, Callable, Optional

from agentc._degradation import log_degraded

log = logging.getLogger(__name__)

# Cross-language marker mirrored by
# `agentc_optimizer::dag::NATIVE_MESSAGES_OPAQUE_KEY`. The Rust DAG stores
# message content as strings, so provider-native blocks and protocol metadata
# must remain on the Python side and may only flow through shape-preserving
# rules.
_NATIVE_MESSAGES_OPAQUE_KEY = "agentc_native_messages_opaque"

# Provider-safe routing contract mirrored by
# ``agentc_optimizer::model_catalog``. Rust chooses targets; Python verifies
# that the selected target belongs to the adapter and credential namespace
# that owns the intercepted call before changing ``model``.
OPENAI_CHAT_COMPLETIONS_PROTOCOL = "openai.chat.completions.v1"
ANTHROPIC_MESSAGES_PROTOCOL = "anthropic.messages.v1"
LITELLM_COMPLETION_PROTOCOL = "litellm.completion.v1"
_ROUTE_CONTEXT_KEY = "agentc_route_context"
_ROUTED_TARGET_KEY = "agentc_routed_target"

# Counterfactual calls are off the request path, but they are not allowed to
# create an unbounded process-wide thread/task fan-out across many call sites.
# SQLite independently enforces the stricter one-live-call limit per site.
_EXPLORATION_WORKER_LIMIT = 4
_exploration_worker_slots = threading.BoundedSemaphore(_EXPLORATION_WORKER_LIMIT)
_exploration_workers_lock = threading.Lock()
_exploration_threads: dict[threading.Thread, Any] = {}
_exploration_tasks: dict[Any, Any] = {}


class UnsafeModelRouteError(ValueError):
    """A plan attempted a model change outside its declared dispatch contract."""

# Sticky ignored modules — frames inside these are infrastructure, not the
# call site we want to attribute optimization decisions to. The wrapper
# `bench.agents._runtime.call_llm` is *not* skipped: if a user routes all
# their calls through their own helper, that helper IS the call site for
# profiling purposes (cost distributions are per-helper, which is the
# right granularity).
_SKIP_MODULE_PREFIXES = (
    "agentc.",
    "agentc",
    "openai.",
    "openai",
    "wrapt.",
    "wrapt",
    "anthropic.",
    "anthropic",
    "litellm.",
    "litellm",
)


def derive_call_site_id() -> str:
    """Walk the stack and return the first user-level call site.

    Format: ``module:function:line``. Falls through to a sentinel if no
    user frame is found (shouldn't happen in practice).
    """
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        modname = frame.f_globals.get("__name__", "")
        if not modname.startswith(_SKIP_MODULE_PREFIXES):
            return f"{modname}:{frame.f_code.co_name}:{frame.f_lineno}"
        frame = frame.f_back
    return "unknown:unknown:0"


# Per-million-token pricing (USD). Subset matching the optimizer's
# default downgrade routes plus the common OpenAI / Anthropic models the
# bench agents touch. Unknown models fall back to (0, 0) — the optimizer
# can still rank rules but won't see meaningful baseline cost.
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    # OpenAI — official model pages observed 2026-09-03
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-2026-03-05": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-mini-2026-03-17": (0.75, 4.50),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o-2024-05-13": (5.00, 15.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4-turbo-2024-04-09": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    # Anthropic — pricing as of 2026-05-11 (api.anthropic.com/v1)
    # claude-3.x series (EOL models kept for historical cost accounting)
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (1.00, 5.00),
    "claude-3-opus-20240229": (15.00, 75.00),
    "claude-3-haiku-20240307": (0.25, 1.25),
    # claude-4.x series — official Anthropic pricing observed 2026-09-03
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-5-20250929": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-7": (15.00, 75.00),
    # Groq (api.groq.com/openai/v1) — pricing as of 2026-05-11
    "llama-3.1-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama3-70b-8192": (0.59, 0.79),
    "llama3-8b-8192": (0.05, 0.08),
    # Hugging Face Inference API (proxy pricing — serverless, no published $/tok;
    # use Groq rates as a conservative stand-in for Llama variants)
    # Correct HF names do NOT include "Meta-" prefix
    "meta-llama/Llama-3.3-70B-Instruct": (0.59, 0.79),
    "meta-llama/Llama-3.1-70B-Instruct": (0.59, 0.79),
    "meta-llama/Llama-3.1-8B-Instruct": (0.05, 0.08),
    # Legacy names kept for historical cost accounting
    "meta-llama/Meta-Llama-3.1-70B-Instruct": (0.59, 0.79),
    "meta-llama/Meta-Llama-3.1-8B-Instruct": (0.05, 0.08),
    # Together AI (api.together.xyz/v1) — pricing as of 2026-05-15
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": (0.88, 0.88),
    "meta-llama/Llama-3.1-70B-Instruct-Turbo": (0.88, 0.88),
    "meta-llama/Llama-3.1-8B-Instruct-Turbo": (0.18, 0.18),
    # Together serverless catalog observed 2026-09-03. LiteLLM preserves the
    # provider prefix on dispatch; provider responses may echo either form.
    "zai-org/GLM-5.3": (1.40, 4.40),
    "zai-org/GLM-5.3-Flash": (0.15, 0.50),
    "together_ai/zai-org/GLM-5.3": (1.40, 4.40),
    "together_ai/zai-org/GLM-5.3-Flash": (0.15, 0.50),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for a chat completion. Returns 0 for unknown models."""
    prices = _MODEL_PRICES.get(model)
    if prices is None:
        # Try matching by prefix — handle dated suffix variants.
        for known in sorted(_MODEL_PRICES, key=len, reverse=True):
            p = _MODEL_PRICES[known]
            if model.startswith(known):
                prices = p
                break
    if prices is None:
        return 0.0
    in_per_mtok, out_per_mtok = prices
    return (input_tokens * in_per_mtok + output_tokens * out_per_mtok) / 1_000_000.0


def _content_text_projection(content: Any) -> str:
    """Return a bounded textual projection for the string-only optimizer DAG.

    The projection is for profiling only. Non-text blocks are represented by
    their type rather than serialized, which avoids copying image payloads or
    pretending the DAG can reconstruct them.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if hasattr(content, "model_dump"):
        content = content.model_dump()
    if isinstance(content, dict):
        content = [content]
    elif isinstance(content, tuple):
        content = list(content)
    elif not isinstance(content, list):
        if isinstance(content, (bool, int, float)):
            return str(content)
        return f"[{type(content).__name__}]"

    parts: list[str] = []
    for block in content:
        if hasattr(block, "model_dump"):
            block = block.model_dump()
        if not isinstance(block, dict):
            parts.append(f"[{type(block).__name__}]")
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        nested = block.get("content")
        if isinstance(nested, (str, list)):
            projected = _content_text_projection(nested)
            if projected:
                parts.append(projected)
                continue
        block_type = str(block.get("type") or "content_block")
        parts.append(f"[{block_type}]")
    return " ".join(parts)


def _project_native_message(message: Any) -> tuple[str, Any, str, bool]:
    """Return ``(role, raw_content, text_projection, is_opaque)``.

    Only a literal mapping with exactly ``role`` and string ``content`` can be
    reconstructed losslessly by the current DAG. Everything else—including
    SDK models, images, tool metadata, names, and nullable content—is opaque.
    """
    if isinstance(message, dict):
        data = message
    elif hasattr(message, "model_dump"):
        dumped = message.model_dump()
        data = dumped if isinstance(dumped, dict) else {}
    else:
        data = {
            "role": getattr(message, "role", "user"),
            "content": getattr(message, "content", ""),
        }

    role = data.get("role", "user")
    raw_content = data.get("content", "")
    lossless_plain_text = (
        isinstance(message, dict)
        and set(data) == {"role", "content"}
        and isinstance(role, str)
        and isinstance(raw_content, str)
    )
    return (
        str(role),
        raw_content,
        _content_text_projection(raw_content),
        not lossless_plain_text,
    )


def _call_has_opaque_native_messages(call: dict[str, Any]) -> bool:
    parameters = call.get("parameters") or {}
    extra = parameters.get("extra") if isinstance(parameters, dict) else None
    return bool(
        isinstance(extra, dict) and extra.get(_NATIVE_MESSAGES_OPAQUE_KEY) is True
    )


def _provider_namespace(provider_protocol: str, model: str) -> str:
    if provider_protocol == OPENAI_CHAT_COMPLETIONS_PROTOCOL:
        return "openai"
    if provider_protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        return "anthropic"
    if provider_protocol == LITELLM_COMPLETION_PROTOCOL and "/" in model:
        return model.split("/", 1)[0]
    return "unresolved"


def _contains_image_input(value: Any) -> bool:
    """Conservatively detect provider-native image blocks without decoding them."""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        block_type = str(value.get("type") or "").lower()
        if block_type in {"image", "image_url", "input_image"}:
            return True
        if "image_url" in value:
            return True
        return any(_contains_image_input(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_image_input(item) for item in value)
    return False


def _input_tokens_upper_bound(*values: Any) -> int:
    """Return a safe byte-count upper bound for JSON-shaped request tokens."""
    try:
        encoded = json.dumps(values, default=str, ensure_ascii=False).encode("utf-8")
    except BaseException:
        # Unknown size is not zero size. Force catalog abstention when a
        # provider-native object cannot be projected safely.
        return 2**32 - 1
    # A text tokenizer cannot emit more tokens than source bytes. This is
    # deliberately conservative for base64 image blocks.
    return min(len(encoded), 2**32 - 1)


def _structured_output_requested(kwargs: dict[str, Any]) -> bool:
    if kwargs.get("response_format") or kwargs.get("output_format"):
        return True
    output_config = kwargs.get("output_config")
    model_dump = getattr(output_config, "model_dump", None)
    if callable(model_dump):
        output_config = model_dump()
    return bool(isinstance(output_config, dict) and output_config.get("format"))


def _structured_output_schema_version(kwargs: dict[str, Any]) -> str | None:
    """Return a stable digest of the declared output contract, never its text."""
    import hashlib

    value = kwargs.get("response_format") or kwargs.get("output_format")
    if value is None:
        output_config = kwargs.get("output_config")
        model_dump = getattr(output_config, "model_dump", None)
        if callable(model_dump):
            output_config = model_dump()
        if isinstance(output_config, dict):
            value = output_config.get("format")
    if value is None:
        return None

    model_json_schema = getattr(value, "model_json_schema", None)
    if callable(model_json_schema):
        value = model_json_schema()
    else:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            value = model_dump()
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        value_type = value if isinstance(value, type) else type(value)
        canonical = f"{value_type.__module__}.{value_type.__qualname__}"
    return "structured-output-v1:" + hashlib.sha256(canonical.encode()).hexdigest()


def _route_context(
    kwargs: dict[str, Any],
    *,
    provider_protocol: str,
    provider_namespace: str,
) -> dict[str, Any]:
    native_values = (
        kwargs.get("system"),
        kwargs.get("messages"),
        kwargs.get("tools"),
    )
    image_input = _contains_image_input(native_values)
    context = {
        "provider_protocol": provider_protocol,
        "provider_namespace": provider_namespace,
        # Provider image accounting cannot be bounded from a URL alone. Force
        # catalog abstention until the adapter has a provider-reported count.
        "input_tokens_upper_bound": (
            2**32 - 1 if image_input else _input_tokens_upper_bound(*native_values)
        ),
        "image_input": image_input,
        "tool_calling": bool(kwargs.get("tools")),
        "structured_outputs": _structured_output_requested(kwargs),
        "streaming": bool(kwargs.get("stream")),
    }
    structured_output_schema_version = _structured_output_schema_version(kwargs)
    if structured_output_schema_version is not None:
        context["structured_output_schema_version"] = structured_output_schema_version
    return context


def _routed_target_metadata(mutated_call: dict[str, Any]) -> dict[str, Any] | None:
    parameters = mutated_call.get("parameters")
    if not isinstance(parameters, dict):
        return None
    extra = parameters.get("extra")
    if not isinstance(extra, dict):
        return None
    metadata = extra.get(_ROUTED_TARGET_KEY)
    return metadata if isinstance(metadata, dict) else None


def _validate_model_route(
    kwargs: dict[str, Any],
    mutated_call: dict[str, Any],
    *,
    provider_protocol: str,
) -> dict[str, Any] | None:
    original_model = str(kwargs.get("model") or "")
    target_model = str(mutated_call.get("model") or original_model)
    if target_model == original_model:
        return None

    metadata = _routed_target_metadata(mutated_call)
    if metadata is None:
        raise UnsafeModelRouteError("model change has no catalog dispatch contract")

    required = {
        "catalog_version",
        "price_table_version",
        "provider_protocol",
        "provider_namespace",
        "requested_model_id",
        "resolved_requested_model_id",
        "target_model_id",
        "target_model_version",
        "target_revision_kind",
        "output_token_parameter",
    }
    if any(not str(metadata.get(field) or "").strip() for field in required):
        raise UnsafeModelRouteError("catalog dispatch contract is incomplete")
    if metadata["provider_protocol"] != provider_protocol:
        raise UnsafeModelRouteError("route targets a different provider protocol")
    if metadata["requested_model_id"] != original_model:
        raise UnsafeModelRouteError("route is bound to a different requested model")
    if metadata["target_model_id"] != target_model:
        raise UnsafeModelRouteError("route metadata and target model disagree")

    expected_namespace = _provider_namespace(provider_protocol, original_model)
    target_namespace = _provider_namespace(provider_protocol, target_model)
    if (
        expected_namespace == "unresolved"
        or target_namespace != expected_namespace
        or metadata["provider_namespace"] != expected_namespace
    ):
        raise UnsafeModelRouteError("route crosses a provider credential namespace")
    if metadata["output_token_parameter"] not in {
        "max_tokens",
        "max_completion_tokens",
    }:
        raise UnsafeModelRouteError("route declares an unknown output-token parameter")
    if metadata["target_revision_kind"] not in {
        "immutable_snapshot",
        "catalog_observation",
    }:
        raise UnsafeModelRouteError("route declares an unknown revision kind")
    return metadata


def _openai_output_cap_field(
    kwargs: dict[str, Any],
    model: str,
    routed_metadata: dict[str, Any] | None = None,
) -> str:
    """Choose the caller-compatible OpenAI output-token parameter name."""
    if routed_metadata is not None:
        return str(routed_metadata["output_token_parameter"])
    if "max_completion_tokens" in kwargs:
        return "max_completion_tokens"
    if "max_tokens" in kwargs:
        return "max_tokens"
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        return "max_completion_tokens"
    return "max_tokens"


def build_call_dict_openai(
    kwargs: dict[str, Any],
    *,
    call_site_id: str,
    trace_id_hex: str,
    span_id_hex: str,
    provider_protocol: str = OPENAI_CHAT_COMPLETIONS_PROTOCOL,
    provider_namespace: str | None = None,
) -> dict[str, Any]:
    """Translate OpenAI ``chat.completions.create`` kwargs into a Call dict."""
    from agentc._parallel import get_parallel_peer
    from agentc._provenance import as_json, consume_state_reads, tag_of

    messages: list[dict[str, str]] = []
    # Track the *original* content objects so we can look up their
    # provenance tags. Stringifying via ``str(...)`` would create a
    # fresh object whose ``id()`` no longer matches the tagged input.
    raw_contents: list[Any] = []
    native_messages_opaque = False
    for msg in kwargs.get("messages", []) or []:
        role, raw, projection, is_opaque = _project_native_message(msg)
        messages.append({"role": role, "content": projection})
        raw_contents.append(raw)
        native_messages_opaque = native_messages_opaque or is_opaque

    input_deps = [as_json(tag_of(content)) for content in raw_contents]

    parameters: dict[str, Any] = {}
    if "temperature" in kwargs and kwargs["temperature"] is not None:
        parameters["temperature"] = float(kwargs["temperature"])
    if "top_p" in kwargs and kwargs["top_p"] is not None:
        parameters["top_p"] = float(kwargs["top_p"])
    if "max_tokens" in kwargs and kwargs["max_tokens"] is not None:
        parameters["max_output_tokens"] = int(kwargs["max_tokens"])
    elif (
        "max_completion_tokens" in kwargs
        and kwargs["max_completion_tokens"] is not None
    ):
        parameters["max_output_tokens"] = int(kwargs["max_completion_tokens"])
    stop = kwargs.get("stop")
    if stop is not None:
        if isinstance(stop, str):
            parameters["stop"] = [stop]
        elif isinstance(stop, list):
            parameters["stop"] = [str(s) for s in stop]

    tools = []
    for tool in kwargs.get("tools", []) or []:
        if isinstance(tool, dict):
            fn = tool.get("function", {})
            tools.append(
                {
                    "name": str(fn.get("name", tool.get("name", "tool"))),
                    "schema": fn.get("parameters", {}),
                }
            )

    existing_extra = parameters.get("extra")
    extra_obj: dict[str, Any] = (
        dict(existing_extra) if isinstance(existing_extra, dict) else {}
    )

    peer = get_parallel_peer()
    if peer is not None:
        extra_obj["parallel_peer"] = peer

    # StateDrop / ContextCompress consume per-message provenance from
    # ``parameters.extra.message_deps`` (parallel to ``messages``). Mirror
    # ``input_deps`` here — the rules read this slot, not the top-level
    # ``input_deps`` (which feeds ParallelBranch's peer-dependency check).
    extra_obj["message_deps"] = input_deps

    # StateDrop also reads ``parameters.extra.window_state_reads``: the
    # set of state keys the agent has read on this thread *since the
    # previous LLM call*. Snapshot + clear so each call sees a fresh
    # window — matches the spec's "reads since the last call" semantic.
    explicit_reads = consume_state_reads()
    extra_obj["window_state_reads"] = explicit_reads

    if native_messages_opaque:
        extra_obj[_NATIVE_MESSAGES_OPAQUE_KEY] = True

    # Merge TraceOptimizer inferred state reads (StateReadWindowPropagation).
    # Keys inferred from prior LlmOutput tokens are added here so StateDrop
    # fires transparently on uninstrumented agents.
    try:
        from agentc._trace_optimizer import get_trace_optimizer

        trace_opt = get_trace_optimizer()
        if trace_opt is not None:
            recs = trace_opt.get_recommendations(trace_id_hex)
            if recs.inferred_state_reads:
                merged = list(set(explicit_reads) | set(recs.inferred_state_reads))
                extra_obj["window_state_reads"] = merged
            if recs.output_is_dead_branch:
                extra_obj["output_is_dead_branch"] = True
            if recs.shared_prefix_messages:
                extra_obj["shared_prefix_messages"] = recs.shared_prefix_messages
    except BaseException:
        # Degradation, not a decision: without these keys StateDrop,
        # DeadOutputTruncation and PrefixAlign silently stop firing.
        log_degraded(
            "trace_recommendations_failed",
            "StateDrop/DeadOutputTruncation/PrefixAlign will not fire for this call (openai)",
        )

    # ContextCompress reads ``parameters.extra.attention_scores`` (per
    # message) and ``parameters.extra.follow_on_tokens`` (must-keep).
    # Compute via the online token-overlap proxy: prior-trace tokens for
    # multi-turn agents, last user message for single-turn QA.
    from agentc._attention import compute_attention_scores

    try:
        attn_scores, follow_on = compute_attention_scores(messages, trace_id_hex)
    except BaseException:
        # Degradation, not a decision: an empty attention map means the Rust
        # ContextCompress rule reads no scores and silently no-ops.
        log_degraded(
            "attention_failed", "ContextCompress will not fire for this call (openai)"
        )
        attn_scores, follow_on = [], []
    if attn_scores:
        extra_obj["attention_scores"] = attn_scores
        extra_obj["follow_on_tokens"] = follow_on
        # The Rust rule's default DEAD_ATTENTION_EPSILON (1e-4) is
        # calibrated for true model attention; our token-overlap proxy
        # emits scores roughly in [0.05, 1.0]. Override so distractors
        # actually qualify as drop-eligible.
        extra_obj["dead_attention_epsilon"] = 0.10

    model = str(kwargs.get("model", ""))
    namespace = provider_namespace or _provider_namespace(provider_protocol, model)
    extra_obj[_ROUTE_CONTEXT_KEY] = _route_context(
        kwargs,
        provider_protocol=provider_protocol,
        provider_namespace=namespace,
    )

    if extra_obj:
        parameters["extra"] = extra_obj

    return {
        "call_site_id": call_site_id,
        "trace_id": trace_id_hex,
        "span_id": span_id_hex,
        "model": model,
        "messages": messages,
        "parameters": parameters,
        "tools": tools,
        "input_deps": input_deps,
        "occurrence_ix": 0,
    }


def apply_call_mutations_openai(
    kwargs: dict[str, Any],
    mutated_call: dict[str, Any],
    *,
    provider_protocol: str = OPENAI_CHAT_COMPLETIONS_PROTOCOL,
) -> dict[str, Any]:
    """Thread a Rewritten plan's mutated Call back into OpenAI kwargs."""
    routed_metadata = _validate_model_route(
        kwargs,
        mutated_call,
        provider_protocol=provider_protocol,
    )
    new_kwargs = dict(kwargs)
    if "model" in mutated_call:
        new_kwargs["model"] = mutated_call["model"]
    msgs = mutated_call.get("messages")
    if msgs is not None and not _call_has_opaque_native_messages(mutated_call):
        new_kwargs["messages"] = [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in msgs
        ]
    params = mutated_call.get("parameters") or {}
    if "temperature" in params:
        new_kwargs["temperature"] = params["temperature"]
    if "top_p" in params:
        new_kwargs["top_p"] = params["top_p"]
    if "max_output_tokens" in params:
        cap_field = _openai_output_cap_field(
            kwargs,
            str(mutated_call.get("model") or kwargs.get("model") or ""),
            routed_metadata,
        )
        other_field = (
            "max_tokens"
            if cap_field == "max_completion_tokens"
            else "max_completion_tokens"
        )
        new_kwargs.pop(other_field, None)
        new_kwargs[cap_field] = int(params["max_output_tokens"])
    return new_kwargs


def build_outcome_openai(
    response: Any,
    *,
    elapsed_s: float,
    model: str,
    call_site_id: str,
    plan: Any | None = None,
) -> dict[str, Any]:
    """Build an Outcome dict from a ChatCompletion response."""
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

    # Detect short / structured output by sampling the first choice.
    output_text = ""
    choices = getattr(response, "choices", None) or []
    if choices:
        msg = getattr(choices[0], "message", None)
        if msg is not None:
            output_text = str(getattr(msg, "content", "") or "")
    output_is_structured = False
    if output_text:
        try:
            json.loads(output_text)
            output_is_structured = True
        except (ValueError, TypeError):
            output_is_structured = False

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": elapsed_s * 1000.0,
        "cost_usd": estimate_cost_usd(model, input_tokens, output_tokens),
        "output_is_structured": output_is_structured,
        "output_is_short": output_tokens <= 128,
        "call_site_id": call_site_id,
        **_outcome_dispatch_fields(plan, model),
    }


def build_call_dict_anthropic(
    kwargs: dict[str, Any],
    *,
    call_site_id: str,
    trace_id_hex: str,
    span_id_hex: str,
) -> dict[str, Any]:
    """Translate Anthropic ``messages.create`` kwargs into a Call dict.

    Anthropic carries ``system`` as a top-level string alongside ``messages``.
    We lift it into the messages list as a leading ``{"role": "system", ...}``
    so the optimizer sees the same unified format as OpenAI calls.
    """
    from agentc._attention import compute_attention_scores
    from agentc._parallel import get_parallel_peer
    from agentc._provenance import as_json, consume_state_reads, tag_of

    messages: list[dict[str, str]] = []
    raw_contents: list[Any] = []
    native_messages_opaque = False

    # Anthropic system param → leading system message
    system_content = kwargs.get("system")
    if system_content:
        messages.append(
            {
                "role": "system",
                "content": _content_text_projection(system_content),
            }
        )
        raw_contents.append(system_content)
        native_messages_opaque = not isinstance(system_content, str)

    for msg in kwargs.get("messages", []) or []:
        role, raw, projection, is_opaque = _project_native_message(msg)
        messages.append({"role": role, "content": projection})
        raw_contents.append(raw)
        native_messages_opaque = native_messages_opaque or is_opaque

    input_deps = [as_json(tag_of(content)) for content in raw_contents]

    parameters: dict[str, Any] = {}
    if "temperature" in kwargs and kwargs["temperature"] is not None:
        parameters["temperature"] = float(kwargs["temperature"])
    if "top_p" in kwargs and kwargs["top_p"] is not None:
        parameters["top_p"] = float(kwargs["top_p"])
    if "max_tokens" in kwargs and kwargs["max_tokens"] is not None:
        parameters["max_output_tokens"] = int(kwargs["max_tokens"])

    extra_obj: dict[str, Any] = {}

    peer = get_parallel_peer()
    if peer is not None:
        extra_obj["parallel_peer"] = peer

    extra_obj["message_deps"] = input_deps
    explicit_reads = consume_state_reads()
    extra_obj["window_state_reads"] = explicit_reads

    if native_messages_opaque:
        extra_obj[_NATIVE_MESSAGES_OPAQUE_KEY] = True

    try:
        from agentc._trace_optimizer import get_trace_optimizer

        trace_opt = get_trace_optimizer()
        if trace_opt is not None:
            recs = trace_opt.get_recommendations(trace_id_hex)
            if recs.inferred_state_reads:
                merged = list(set(explicit_reads) | set(recs.inferred_state_reads))
                extra_obj["window_state_reads"] = merged
    except BaseException:
        log_degraded(
            "trace_recommendations_failed",
            "StateDrop/DeadOutputTruncation/PrefixAlign will not fire for this call (anthropic)",
        )

    try:
        attn_scores, follow_on = compute_attention_scores(messages, trace_id_hex)
    except BaseException:
        log_degraded(
            "attention_failed",
            "ContextCompress will not fire for this call (anthropic)",
        )
        attn_scores, follow_on = [], []
    if attn_scores:
        extra_obj["attention_scores"] = attn_scores
        extra_obj["follow_on_tokens"] = follow_on
        extra_obj["dead_attention_epsilon"] = 0.10

    extra_obj[_ROUTE_CONTEXT_KEY] = _route_context(
        kwargs,
        provider_protocol=ANTHROPIC_MESSAGES_PROTOCOL,
        provider_namespace="anthropic",
    )

    if extra_obj:
        parameters["extra"] = extra_obj

    tools = []
    for native_tool in kwargs.get("tools", []) or []:
        if hasattr(native_tool, "model_dump"):
            native_tool = native_tool.model_dump()
        if isinstance(native_tool, dict):
            tools.append(
                {
                    "name": str(native_tool.get("name", "tool")),
                    "schema": native_tool.get("input_schema", {}),
                }
            )

    return {
        "call_site_id": call_site_id,
        "trace_id": trace_id_hex,
        "span_id": span_id_hex,
        "model": str(kwargs.get("model", "")),
        "messages": messages,
        "parameters": parameters,
        "tools": tools,
        "input_deps": input_deps,
        "occurrence_ix": 0,
    }


def apply_call_mutations_anthropic(
    kwargs: dict[str, Any],
    mutated_call: dict[str, Any],
) -> dict[str, Any]:
    """Thread a Rewritten plan's mutated Call back into Anthropic kwargs.

    The optimizer's unified message list may include a leading system message.
    We split it back out to Anthropic's ``system`` + ``messages`` shape.
    """
    routed_metadata = _validate_model_route(
        kwargs,
        mutated_call,
        provider_protocol=ANTHROPIC_MESSAGES_PROTOCOL,
    )
    if (
        routed_metadata is not None
        and routed_metadata["output_token_parameter"] != "max_tokens"
    ):
        raise UnsafeModelRouteError(
            "Anthropic route must use the max_tokens output convention"
        )
    new_kwargs = dict(kwargs)
    if "model" in mutated_call:
        new_kwargs["model"] = mutated_call["model"]

    msgs = mutated_call.get("messages")
    if msgs is not None and not _call_has_opaque_native_messages(mutated_call):
        anthro_msgs = []
        system_text = None
        for m in msgs:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_text = content
            else:
                anthro_msgs.append({"role": role, "content": content})
        if system_text is not None:
            new_kwargs["system"] = system_text
        new_kwargs["messages"] = anthro_msgs

    params = mutated_call.get("parameters") or {}
    if "max_output_tokens" in params:
        new_kwargs["max_tokens"] = int(params["max_output_tokens"])
    return new_kwargs


def build_outcome_anthropic(
    response: Any,
    *,
    elapsed_s: float,
    model: str,
    call_site_id: str,
    plan: Any | None = None,
) -> dict[str, Any]:
    """Build an Outcome dict from an Anthropic Message response."""
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

    output_text = ""
    content = getattr(response, "content", None) or []
    for block in content:
        if hasattr(block, "text"):
            output_text = block.text
            break
        if isinstance(block, dict) and block.get("type") == "text":
            output_text = block.get("text", "")
            break

    output_is_structured = False
    if output_text:
        try:
            json.loads(output_text)
            output_is_structured = True
        except (ValueError, TypeError):
            pass

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": elapsed_s * 1000.0,
        "cost_usd": estimate_cost_usd(model, input_tokens, output_tokens),
        "output_is_structured": output_is_structured,
        "output_is_short": output_tokens <= 128,
        "call_site_id": call_site_id,
        **_outcome_dispatch_fields(plan, model),
    }


def _outcome_dispatch_fields(plan: Any | None, executed_model: str) -> dict[str, Any]:
    if plan is None:
        return {
            "dispatch_fallback": False,
            "executed_model_id": executed_model,
        }
    return {
        "dispatch_fallback": bool(getattr(plan, "dispatch_fallback", False)),
        "dispatch_fallback_reason": getattr(plan, "dispatch_fallback_reason", None),
        "provider_protocol": getattr(plan, "provider_protocol", None),
        "provider_namespace": getattr(plan, "provider_namespace", None),
        "target_model_id": getattr(plan, "target_model_id", None),
        "target_model_version": getattr(plan, "target_model_version", None),
        "price_table_version": getattr(plan, "price_table_version", None),
        "catalog_version": getattr(plan, "catalog_version", None),
        "executed_model_id": executed_model,
    }


def dispatch_span_attributes(
    plan: Any | None,
    executed_model_id: str | None = None,
) -> dict[str, Any]:
    """Stable span attributes for routed execution and exact fallback."""
    if plan is None:
        return {}
    if executed_model_id:
        plan.executed_model_id = executed_model_id
    attrs: dict[str, Any] = {
        "agentc.dispatch.fallback": bool(
            getattr(plan, "dispatch_fallback", False)
        )
    }
    for attribute, field in [
        ("agentc.dispatch.fallback_reason", "dispatch_fallback_reason"),
        ("agentc.dispatch.provider_protocol", "provider_protocol"),
        ("agentc.dispatch.provider_namespace", "provider_namespace"),
        ("agentc.dispatch.target_model", "target_model_id"),
        ("agentc.dispatch.target_model_version", "target_model_version"),
        ("agentc.dispatch.catalog_version", "catalog_version"),
        ("agentc.dispatch.price_table_version", "price_table_version"),
        ("agentc.dispatch.executed_model", "executed_model_id"),
    ]:
        value = getattr(plan, field, None)
        if value is not None:
            attrs[attribute] = value
    return attrs


def resolve_executed_model_id(
    plan: Any | None,
    response: Any,
    requested_model_id: Any,
) -> str:
    """Prefer provider evidence, then the dispatcher's selected target."""
    response_model = getattr(response, "model", None)
    if response_model:
        return str(response_model)
    planned_model = getattr(plan, "executed_model_id", None)
    if planned_model:
        return str(planned_model)
    return str(requested_model_id or "")


def _response_output_text(response: Any) -> Optional[str]:
    """Best-effort extraction of the assistant text from a ChatCompletion.

    Returns the text ("" for a well-formed but empty completion) on success,
    or ``None`` if extraction FAILED (unexpected shape / attribute error).
    Callers must treat None as "unknown output", never as an empty string:
    otherwise a swallowed error would be handed to the accuracy guard as
    maximal divergence and could auto-disable a working rule (bd-kq7).
    """
    try:
        if isinstance(response, str):
            return response
        # OpenAI ChatCompletion shape: choices[0].message.content.
        choices = getattr(response, "choices", None) or []
        if choices:
            msg = getattr(choices[0], "message", None)
            if msg is not None:
                return str(getattr(msg, "content", "") or "")
        # Anthropic Message shape: `content` is a list of blocks, each with a
        # `.text` (object) or `"text"` (dict). Needed so the shadow guard works
        # on the native Anthropic path, not just OpenAI (bd-1cb).
        content = getattr(response, "content", None)
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                text = getattr(block, "text", None)
                if text is None and isinstance(block, dict):
                    text = block.get("text")
                if text:
                    parts.append(str(text))
            return " ".join(parts)
        # Well-formed response with no recognizable content → genuinely empty.
        return ""
    except BaseException:
        return None


_ARTICLES = {"a", "an", "the"}
_PUNCT = str.maketrans({c: " " for c in ",.;:!?\"'`()[]{}<>-_/\\|"})


def _normalize_tokens(s: str) -> set[str]:
    """Lowercase, strip punctuation, drop articles. Makes the divergence
    signal invariant to formatting/article/case noise so benign rewrites
    (e.g. 'Kansas Song' vs 'Kansas Song (We're From Kansas)') register as
    agreement while genuinely different answers still diverge."""
    toks = s.lower().translate(_PUNCT).split()
    return {t for t in toks if t and t not in _ARTICLES}


def _cosine_distance_from_bytes(ba: bytes, bb: bytes) -> float:
    """Cosine distance in [0, 1] from two 256×f32 little-endian byte strings.

    Returns 1.0 (maximally distant) if either vector is zero-norm.
    """
    import struct

    n = len(ba) // 4
    va = struct.unpack_from(f"{n}f", ba)
    vb = struct.unpack_from(f"{n}f", bb)
    dot = sum(x * y for x, y in zip(va, vb))
    norm_a = sum(x * x for x in va) ** 0.5
    norm_b = sum(x * x for x in vb) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return float(1.0 - dot / (norm_a * norm_b))


def _text_divergence(a: str, b: str) -> float:
    """1 - Jaccard over output tokens. ``AGENTC_SHADOW_DIVERGENCE_MODE``:
    'lexical' (default, raw whitespace tokens, mirrors the Rust meter),
    'normalized' (article/punctuation/case-invariant) for a selective guard,
    or 'embedding' (cosine distance on 256-dim model2vec embeddings).

    The 'embedding' mode falls back to 'normalized' if the embedder is
    unavailable at runtime, keeping the guard fail-open."""
    import os

    mode = os.environ.get("AGENTC_SHADOW_DIVERGENCE_MODE", "lexical").strip().lower()
    if mode == "embedding":
        try:
            from agentc._native import embed_text_bytes

            ba = embed_text_bytes(a)
            bb = embed_text_bytes(b)
            if ba is not None and bb is not None:
                return _cosine_distance_from_bytes(bytes(ba), bytes(bb))
        except BaseException:
            log.debug(
                "embedding divergence failed; falling back to normalized", exc_info=True
            )
        # Fallback: treat as normalized mode (fail-open)
        mode = "normalized"
    if mode == "normalized":
        sa, sb = _normalize_tokens(a), _normalize_tokens(b)
        if not sa and not sb:
            return 0.0
        if not sa or not sb:
            return 1.0
        # Containment (overlap coefficient): agreement when one answer
        # subsumes the other (e.g. a span vs the same span plus a gloss),
        # so benign elaboration reads as 0 while a different answer diverges.
        return 1.0 - (len(sa & sb) / min(len(sa), len(sb)))
    sa, sb = set(a.split()), set(b.split())
    if not sa and not sb:
        return 0.0
    union = sa | sb
    if not union:
        return 0.0
    return 1.0 - (len(sa & sb) / len(union))


def _applied_rules(plan: Any) -> list[str]:
    if plan.kind == "rewritten" and plan.rule:
        return [plan.rule]
    if plan.kind == "composed":
        return [r for r in (plan.rules or []) if r]
    return []


def _shadow_sample_rules(plan: Any, call_site_id: Optional[str]) -> list[str]:
    """Return the rules to sample, or an empty list when sampling abstains."""
    import os
    import random

    # A failed optimized dispatch already executed the exact reference request
    # once as its user-visible fallback. Running another shadow reference would
    # duplicate side effects and violate the exactly-once fallback contract.
    if bool(getattr(plan, "dispatch_fallback", False)):
        return []

    rules = _applied_rules(plan)
    if (
        not rules
        or not call_site_id
        or not getattr(plan, "observation_token", None)
    ):
        return []
    try:
        rate = float(os.environ.get("AGENTC_OPTIMIZE_SHADOW", "0.02"))
    except (TypeError, ValueError):
        rate = 0.02
    if not math.isfinite(rate):
        rate = 0.02
    rate = min(max(rate, 0.0), 1.0)
    if rate == 0.0 or random.random() >= rate:
        return []
    return rules


def _record_shadow_comparison(
    plan: Any,
    call_site_id: str,
    optimized_response: Any,
    original_response: Any,
) -> None:
    opt_text = _response_output_text(optimized_response)
    orig_text = _response_output_text(original_response)
    if opt_text is None or orig_text is None:
        # Extraction failed on at least one side — we do NOT know the
        # outputs, so feeding a divergence here would be a fabricated
        # sample ("" vs real text scores as maximal divergence) that could
        # auto-disable a working rule. Skip the sample (bd-kq7).
        log_degraded(
            "shadow_output_extraction_failed",
            f"skipped shadow divergence sample for call_site={call_site_id}",
        )
        return
    divergence = _text_divergence(opt_text, orig_text)
    from agentc._optimizer import record_divergence

    observation_token = getattr(plan, "observation_token", None)
    if observation_token:
        record_divergence(observation_token, divergence)


def _exploration_parts(plan: Any) -> tuple[Any, str] | None:
    candidate = getattr(plan, "counterfactual", None)
    lease_token = getattr(plan, "exploration_lease_token", None)
    if (
        getattr(plan, "kind", None) != "pass_through"
        or not isinstance(lease_token, str)
        or not lease_token
        or candidate is None
        or getattr(candidate, "kind", None) not in ("rewritten", "composed")
        or not isinstance(getattr(candidate, "call", None), dict)
    ):
        return None
    return candidate, lease_token


def cancel_exploration(plan: Any) -> None:
    """Best-effort release when the reference call never produced a result."""
    if _exploration_parts(plan) is None:
        return
    from agentc._optimizer import fail_exploration

    fail_exploration(plan)


def _record_exploration_result(
    plan: Any,
    candidate: Any,
    reference_text: str,
    candidate_response: Any,
    elapsed_s: float,
    extract_outcome: Callable[[Any, Any, float], dict[str, Any]],
) -> None:
    from agentc._optimizer import complete_exploration, fail_exploration

    candidate_text = _response_output_text(candidate_response)
    if candidate_text is None:
        fail_exploration(plan)
        return
    outcome = extract_outcome(candidate, candidate_response, elapsed_s)
    if not isinstance(outcome, dict):
        fail_exploration(plan)
        return
    divergence = _text_divergence(candidate_text, reference_text)
    if not complete_exploration(plan, outcome, divergence):
        fail_exploration(plan)


def _maybe_explore_record(
    plan: Any,
    reference_response: Any,
    run_mutated: Callable[[dict[str, Any]], Any],
    extract_outcome: Callable[[Any, Any, float], dict[str, Any]],
) -> threading.Thread | None:
    """Schedule one leased sync counterfactual after returning the reference.

    The daemon worker calls only the provider's mutated-call adapter. It never
    retries the original request and never changes the response held by the
    caller. The returned thread exists for deterministic tests; production
    wrappers deliberately do not join it.
    """
    parts = _exploration_parts(plan)
    if parts is None:
        return None
    candidate, _lease_token = parts
    reference_text = _response_output_text(reference_response)
    if reference_text is None:
        cancel_exploration(plan)
        return None
    if not _exploration_worker_slots.acquire(blocking=False):
        cancel_exploration(plan)
        return None

    def worker() -> None:
        try:
            started = time.perf_counter()
            candidate_response = run_mutated(candidate.call)
            candidate.executed_model_id = str(candidate.call.get("model") or "") or None
            _record_exploration_result(
                plan,
                candidate,
                reference_text,
                candidate_response,
                time.perf_counter() - started,
                extract_outcome,
            )
        except BaseException:
            cancel_exploration(plan)
            log.debug("counterfactual exploration failed; lease closed", exc_info=True)
        finally:
            with _exploration_workers_lock:
                _exploration_threads.pop(threading.current_thread(), None)
            _exploration_worker_slots.release()

    thread: threading.Thread | None = None
    try:
        thread = threading.Thread(
            target=worker,
            name="agentc-counterfactual",
            daemon=True,
        )
        with _exploration_workers_lock:
            _exploration_threads[thread] = plan
        thread.start()
    except BaseException:
        if thread is not None:
            with _exploration_workers_lock:
                _exploration_threads.pop(thread, None)
        _exploration_worker_slots.release()
        cancel_exploration(plan)
        log.debug("counterfactual worker start failed; lease closed", exc_info=True)
        return None
    return thread


def maybe_explore_record(
    plan: Any,
    reference_response: Any,
    run_mutated: Callable[[dict[str, Any]], Any],
    extract_outcome: Callable[[Any, Any, float], dict[str, Any]],
) -> threading.Thread | None:
    """Fail-open boundary for sync counterfactual worker setup."""
    try:
        return _maybe_explore_record(
            plan,
            reference_response,
            run_mutated,
            extract_outcome,
        )
    except BaseException:
        try:
            cancel_exploration(plan)
        except BaseException:
            log.debug("counterfactual setup lease close failed", exc_info=True)
        log.debug("counterfactual worker setup failed; reference preserved", exc_info=True)
        return None


def _maybe_explore_record_async(
    plan: Any,
    reference_response: Any,
    run_mutated: Callable[[dict[str, Any]], Awaitable[Any]],
    extract_outcome: Callable[[Any, Any, float], dict[str, Any]],
) -> Any | None:
    """Schedule the async counterpart without extending request latency."""
    import asyncio

    parts = _exploration_parts(plan)
    if parts is None:
        return None
    candidate, _lease_token = parts
    reference_text = _response_output_text(reference_response)
    if reference_text is None:
        cancel_exploration(plan)
        return None
    if not _exploration_worker_slots.acquire(blocking=False):
        cancel_exploration(plan)
        return None

    async def worker() -> None:
        try:
            started = time.perf_counter()
            candidate_response = await run_mutated(candidate.call)
            candidate.executed_model_id = str(candidate.call.get("model") or "") or None
            _record_exploration_result(
                plan,
                candidate,
                reference_text,
                candidate_response,
                time.perf_counter() - started,
                extract_outcome,
            )
        except asyncio.CancelledError:
            cancel_exploration(plan)
            raise
        except BaseException:
            cancel_exploration(plan)
            log.debug("async counterfactual exploration failed; lease closed", exc_info=True)
        finally:
            _exploration_worker_slots.release()

    worker_coro = worker()
    try:
        task = asyncio.create_task(worker_coro, name="agentc-counterfactual")
    except BaseException:
        worker_coro.close()
        _exploration_worker_slots.release()
        cancel_exploration(plan)
        log.debug("async counterfactual task start failed; lease closed", exc_info=True)
        return None
    with _exploration_workers_lock:
        _exploration_tasks[task] = plan

    def forget(done: Any) -> None:
        with _exploration_workers_lock:
            _exploration_tasks.pop(done, None)

    task.add_done_callback(forget)
    return task


def maybe_explore_record_async(
    plan: Any,
    reference_response: Any,
    run_mutated: Callable[[dict[str, Any]], Awaitable[Any]],
    extract_outcome: Callable[[Any, Any, float], dict[str, Any]],
) -> Any | None:
    """Fail-open boundary for async counterfactual task setup."""
    try:
        return _maybe_explore_record_async(
            plan,
            reference_response,
            run_mutated,
            extract_outcome,
        )
    except BaseException:
        try:
            cancel_exploration(plan)
        except BaseException:
            log.debug("async counterfactual setup lease close failed", exc_info=True)
        log.debug("async counterfactual task setup failed; reference preserved", exc_info=True)
        return None


def drain_exploration(timeout_ms: int) -> None:
    """Bound shutdown wait and close leases for work that cannot finish."""
    timeout_s = max(timeout_ms, 0) / 1000.0
    deadline = time.monotonic() + timeout_s
    with _exploration_workers_lock:
        threads = list(_exploration_threads.items())
        tasks = list(_exploration_tasks.items())

    current = threading.current_thread()
    for thread, _plan in threads:
        if thread is current:
            continue
        remaining = max(deadline - time.monotonic(), 0.0)
        thread.join(timeout=remaining)

    with _exploration_workers_lock:
        unfinished_threads = list(_exploration_threads.items())
        unfinished_tasks = list(_exploration_tasks.items())
    for _thread, plan in unfinished_threads:
        cancel_exploration(plan)
    for task, plan in unfinished_tasks:
        cancel_exploration(plan)
        _cancel_async_exploration_task(task)


def _cancel_async_exploration_task(task: Any) -> None:
    """Cancel on the task's owning loop when shutdown runs elsewhere."""
    try:
        loop = task.get_loop()
        if loop.is_closed():
            return
        if loop.is_running():
            loop.call_soon_threadsafe(task.cancel)
        else:
            task.cancel()
    except BaseException:
        log.debug("async counterfactual task cancellation failed", exc_info=True)


def maybe_shadow_record(
    plan: Any,
    call_site_id: Optional[str],
    optimized_response: Any,
    run_original: Callable[[], Any],
) -> None:
    """Shadow-mode accuracy sampling (sync path).

    On a fraction (``AGENTC_OPTIMIZE_SHADOW``, default 0.02) of rewritten /
    composed calls, run the *unrewritten* call, measure output divergence
    against the optimized result, and feed it to the Rust complete-plan
    exposure guard via :func:`record_divergence`. Crossing the rolling exposure
    budget disables that exact model-and-rewrite plan; identifiable solo plans
    also feed the legacy per-rule compatibility guard.

    Cost note: ``run_original()`` issues a second, real and billed LLM call.
    It runs synchronously here — after the optimized response is obtained but
    BEFORE the wrapper returns — so on the sampled ~2% of calls it adds that
    call's latency and cost to the user-visible request. Fail-open: it never
    raises, but it is not free and not off the request's critical path.
    """
    rules = _shadow_sample_rules(plan, call_site_id)
    if not rules or call_site_id is None:
        return
    try:
        original = run_original()
        _record_shadow_comparison(
            plan,
            call_site_id,
            optimized_response,
            original,
        )
    except BaseException:
        log.debug("shadow sample failed; dropping", exc_info=True)


async def maybe_shadow_record_async(
    plan: Any,
    call_site_id: Optional[str],
    optimized_response: Any,
    run_original: Callable[[], Awaitable[Any]],
) -> None:
    """Async counterpart to :func:`maybe_shadow_record` with identical gates."""
    import asyncio

    rules = _shadow_sample_rules(plan, call_site_id)
    if not rules or call_site_id is None:
        return
    try:
        original = await run_original()
        _record_shadow_comparison(
            plan,
            call_site_id,
            optimized_response,
            original,
        )
    except asyncio.CancelledError:
        # Shadow work remains on the request's critical path. Preserve normal
        # task cancellation rather than turning it into a successful response.
        raise
    except BaseException:
        log.debug("async shadow sample failed; dropping", exc_info=True)


def _mark_dispatch_fallback(plan: Any, reason: str) -> None:
    plan.dispatch_fallback = True
    plan.dispatch_fallback_reason = reason
    plan.executed_model_id = None


def dispatch_sync(
    plan: Any,  # agentc._optimizer.Plan
    *,
    run_original: Callable[[], Any],
    run_mutated: Callable[[dict[str, Any]], Any],
    decode_cached: Optional[Callable[[Any], Any]] = None,
) -> Any:
    """Sync mirror of ``agentc._executor.dispatch``.

    ``Parallel`` plans require ``asyncio.gather`` so we fall back to the
    original call when one shows up on the sync path.
    """
    decode = decode_cached or (lambda v: v)

    if plan.kind == "pass_through":
        try:
            return run_original()
        except BaseException:
            cancel_exploration(plan)
            raise
    if plan.kind == "cached":
        try:
            decoded = decode(plan.value)
        except BaseException:
            _mark_dispatch_fallback(plan, "cache_decode_failed")
            log_degraded(
                "cache_decode_failed",
                "cached plan decode raised; ran the original call",
            )
            return run_original()
        if decoded is None:
            # A cache "hit" that cannot be materialized (missing content, or a
            # decoder that returns None instead of raising — e.g.
            # _decode_cached_openai) must NOT be served to the app as a None
            # response. Fall back to the real call so the caller always gets a
            # completion (bd-8ln: over-reporting / None corruption).
            _mark_dispatch_fallback(plan, "cache_decode_empty")
            log_degraded(
                "cache_decode_empty",
                "cached plan decoded to None; ran the original call",
            )
            return run_original()
        return decoded
    if plan.kind in ("rewritten", "composed"):
        if plan.call is None:
            _mark_dispatch_fallback(plan, "missing_mutated_call")
            return run_original()
        try:
            result = run_mutated(plan.call)
            plan.executed_model_id = str(plan.call.get("model") or "") or None
            return result
        except BaseException:
            _mark_dispatch_fallback(plan, "mutated_dispatch_failed")
            # Degradation: a systematically broken mutation (bad downgrade
            # model, malformed rewritten Call) reverts to the original on
            # EVERY call and reports 0% savings. The async path already warns
            # here (agentc._executor.dispatch) — the sync path (which the
            # benchmarks use) must too.
            log_degraded(
                "rewrite_dispatch_failed",
                f"{plan.kind} plan reverted to the original call",
            )
            return run_original()
    if plan.kind == "parallel":
        # Sync path can't gather; degrade to original.
        _mark_dispatch_fallback(plan, "parallel_sync_unsupported")
        return run_original()
    _mark_dispatch_fallback(plan, "unknown_plan_kind")
    return run_original()
