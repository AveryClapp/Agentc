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
import sys
from typing import Any, Callable, Optional

from agentc._degradation import log_degraded

log = logging.getLogger(__name__)

# Cross-language marker mirrored by
# `agentc_optimizer::dag::NATIVE_MESSAGES_OPAQUE_KEY`. The Rust DAG stores
# message content as strings, so provider-native blocks and protocol metadata
# must remain on the Python side and may only flow through shape-preserving
# rules.
_NATIVE_MESSAGES_OPAQUE_KEY = "agentc_native_messages_opaque"

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
)


def derive_call_site_id() -> str:
    """Walk the stack and return the first user-level call site.

    Format: ``module:function:line``. Falls through to a sentinel if no
    user frame is found (shouldn't happen in practice).
    """
    frame = sys._getframe(1)
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
    # OpenAI — pricing as of 2026-05-11
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
    # claude-4.x series — pricing estimated from Anthropic pricing page
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-haiku-4-5": (0.80, 4.00),
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
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for a chat completion. Returns 0 for unknown models."""
    prices = _MODEL_PRICES.get(model)
    if prices is None:
        # Try matching by prefix — handle dated suffix variants.
        for known, p in _MODEL_PRICES.items():
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


def _openai_output_cap_field(kwargs: dict[str, Any], model: str) -> str:
    """Choose the caller-compatible OpenAI output-token parameter name."""
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

    if extra_obj:
        parameters["extra"] = extra_obj

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


def apply_call_mutations_openai(
    kwargs: dict[str, Any],
    mutated_call: dict[str, Any],
) -> dict[str, Any]:
    """Thread a Rewritten plan's mutated Call back into OpenAI kwargs."""
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

    if extra_obj:
        parameters["extra"] = extra_obj

    return {
        "call_site_id": call_site_id,
        "trace_id": trace_id_hex,
        "span_id": span_id_hex,
        "model": str(kwargs.get("model", "")),
        "messages": messages,
        "parameters": parameters,
        "tools": [],
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
    }


def _response_output_text(response: Any) -> Optional[str]:
    """Best-effort extraction of the assistant text from a ChatCompletion.

    Returns the text ("" for a well-formed but empty completion) on success,
    or ``None`` if extraction FAILED (unexpected shape / attribute error).
    Callers must treat None as "unknown output", never as an empty string:
    otherwise a swallowed error would be handed to the accuracy guard as
    maximal divergence and could auto-disable a working rule (bd-kq7).
    """
    try:
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


def _normalize_tokens(s: str) -> set:
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
    return 1.0 - dot / (norm_a * norm_b)


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


def maybe_shadow_record(
    plan: Any,
    call_site_id: Optional[str],
    optimized_response: Any,
    run_original: Callable[[], Any],
) -> None:
    """Shadow-mode accuracy sampling (sync path).

    On a fraction (``AGENTC_OPTIMIZE_SHADOW``, default 0.02) of rewritten /
    composed calls, run the *unrewritten* call, measure output divergence
    against the optimized result, and feed it to the Rust accuracy budget
    via :func:`record_divergence`. After ``BREACH_STREAK`` consecutive
    over-budget samples the budget auto-disables the rule, so subsequent
    calls to this site pass it through.

    Cost note: ``run_original()`` issues a second, real and billed LLM call.
    It runs synchronously here — after the optimized response is obtained but
    BEFORE the wrapper returns — so on the sampled ~2% of calls it adds that
    call's latency and cost to the user-visible request. Fail-open: it never
    raises, but it is not free and not off the request's critical path.
    """
    import os
    import random

    rules = _applied_rules(plan)
    if not rules or not call_site_id:
        return
    try:
        rate = float(os.environ.get("AGENTC_OPTIMIZE_SHADOW", "0.02"))
    except (TypeError, ValueError):
        rate = 0.02
    if rate <= 0.0 or random.random() >= rate:
        return
    try:
        original = run_original()
        opt_text = _response_output_text(optimized_response)
        orig_text = _response_output_text(original)
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

        for rule in rules:
            record_divergence(call_site_id, rule, divergence)
    except BaseException:
        log.debug("shadow sample failed; dropping", exc_info=True)


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
        return run_original()
    if plan.kind == "cached":
        try:
            decoded = decode(plan.value)
        except BaseException:
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
            log_degraded(
                "cache_decode_empty",
                "cached plan decoded to None; ran the original call",
            )
            return run_original()
        return decoded
    if plan.kind in ("rewritten", "composed"):
        if plan.call is None:
            return run_original()
        try:
            return run_mutated(plan.call)
        except BaseException:
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
        return run_original()
    return run_original()
