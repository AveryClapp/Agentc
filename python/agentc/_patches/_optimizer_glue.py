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

log = logging.getLogger(__name__)

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
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o-2024-05-13": (5.00, 15.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4-turbo-2024-04-09": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (1.00, 5.00),
    "claude-3-opus-20240229": (15.00, 75.00),
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
    for msg in kwargs.get("messages", []) or []:
        if isinstance(msg, dict):
            raw = msg.get("content", "")
            messages.append({
                "role": str(msg.get("role", "user")),
                "content": str(raw),
            })
        elif hasattr(msg, "model_dump"):
            d = msg.model_dump()
            raw = d.get("content", "")
            messages.append({
                "role": str(d.get("role", "user")),
                "content": str(raw),
            })
        else:
            raw = getattr(msg, "content", "")
            messages.append({
                "role": str(getattr(msg, "role", "user")),
                "content": str(raw),
            })
        raw_contents.append(raw)

    input_deps = [as_json(tag_of(content)) for content in raw_contents]

    parameters: dict[str, Any] = {}
    if "temperature" in kwargs and kwargs["temperature"] is not None:
        parameters["temperature"] = float(kwargs["temperature"])
    if "top_p" in kwargs and kwargs["top_p"] is not None:
        parameters["top_p"] = float(kwargs["top_p"])
    if "max_tokens" in kwargs and kwargs["max_tokens"] is not None:
        parameters["max_output_tokens"] = int(kwargs["max_tokens"])
    elif "max_completion_tokens" in kwargs and kwargs["max_completion_tokens"] is not None:
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
            tools.append({
                "name": str(fn.get("name", tool.get("name", "tool"))),
                "schema": fn.get("parameters", {}),
            })

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
        log.debug("trace_optimizer recommendations failed; skipping", exc_info=True)

    # ContextCompress reads ``parameters.extra.attention_scores`` (per
    # message) and ``parameters.extra.follow_on_tokens`` (must-keep).
    # Compute via the online token-overlap proxy: prior-trace tokens for
    # multi-turn agents, last user message for single-turn QA.
    from agentc._attention import compute_attention_scores

    try:
        attn_scores, follow_on = compute_attention_scores(messages, trace_id_hex)
    except BaseException:
        log.debug("compute_attention_scores raised (suppressed)", exc_info=True)
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
    if msgs is not None:
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
        new_kwargs["max_tokens"] = int(params["max_output_tokens"])
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
            return decode(plan.value)
        except BaseException:
            log.debug("cached plan decode failed; falling back", exc_info=True)
            return run_original()
    if plan.kind in ("rewritten", "composed"):
        if plan.call is None:
            return run_original()
        try:
            return run_mutated(plan.call)
        except BaseException:
            log.debug("rewritten/composed plan failed; falling back", exc_info=True)
            return run_original()
    if plan.kind == "parallel":
        # Sync path can't gather; degrade to original.
        return run_original()
    return run_original()
