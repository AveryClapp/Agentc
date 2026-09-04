"""Provider-agnostic interception of LLM calls.

The interceptor is intentionally thin: vendor-specific patching
(OpenAI, Anthropic, Cohere) lives in ``python/agentc/_patches/``;
framework provenance adapters live in
``python/agentc/_provenance_frameworks/``. This module owns
the control flow that every vendor shares:

1. Build a ``Call`` dict from the provider-native request.
2. Ask the optimizer for a ``Plan``.
3. Dispatch the plan via :mod:`agentc._executor`.
4. Observe the outcome (tokens, latency, cost) back into the cost
   model for future decisions.

Per-call opt-out is honoured by reading
``extra_headers['agentc-optimize']``: when the value is the literal
string ``"false"`` (case-insensitive), the plan is ignored and the
original call runs — profiling still proceeds because this module
doesn't touch the profiler.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

from agentc._executor import dispatch
from agentc._optimizer import Plan, observe_outcome, plan_call
from agentc._optimization_scope import (
    OPT_OUT_HEADER,
    OPT_OUT_VALUE,
    decide_optimization,
    is_opted_out,
)

log = logging.getLogger(__name__)


async def intercept(
    *,
    build_call: Callable[[], dict[str, Any]],
    run_original: Callable[[], Awaitable[Any]],
    run_mutated: Callable[[dict[str, Any]], Awaitable[Any]],
    extract_outcome: Callable[[Any, float], dict[str, Any]],
    extra_headers: Optional[dict[str, Any]] = None,
    decode_cached: Optional[Callable[[Any], Any]] = None,
) -> Any:
    """Run one intercepted LLM call end-to-end.

    ``build_call``   — constructs the Call dict the optimizer reads.
    ``run_original`` — awaits the user's unmutated provider call.
    ``run_mutated``  — awaits a provider call with a possibly-mutated
                       ``call`` dict (for ``Rewritten`` / ``Parallel``).
    ``extract_outcome`` — given the provider's response and the wall
                       time in seconds, returns an ``Outcome`` dict
                       with ``input_tokens`` / ``output_tokens`` /
                       ``latency_ms`` / ``cost_usd`` / ``output_is_*``.
    ``extra_headers`` — optional vendor-style header map; if it carries
                       ``agentc-optimize: false`` we skip optimization.
    ``decode_cached`` — converts the cached payload into the shape the
                       caller expects from the provider (most vendors
                       return a rich response object; cache stores the
                       output payload only).
    """
    decision = decide_optimization(extra_headers)
    if not decision.eligible:
        return await run_original()

    try:
        call = build_call()
    except BaseException:
        log.debug("build_call raised; passing through", exc_info=True)
        return await run_original()

    plan = plan_call(call)

    t0 = time.perf_counter()
    try:
        result = await dispatch(
            plan,
            run_original=run_original,
            run_mutated=run_mutated,
            decode_cached=decode_cached,
        )
    except BaseException:
        # dispatch() already falls back internally on controlled
        # failures; anything that escapes is a caller bug. Don't
        # observe — we have no reliable outcome.
        raise

    elapsed_s = time.perf_counter() - t0
    try:
        outcome = extract_outcome(result, elapsed_s)
        # Inject call_site_id so the FFI can warm the cost model on
        # PassThrough plans (the plan itself doesn't carry the site).
        site = call.get("call_site_id")
        if site and "call_site_id" not in outcome:
            outcome["call_site_id"] = site
        observe_outcome(plan, outcome)
        from agentc._patches._optimizer_glue import maybe_explore_record_async

        def _extract_counterfactual_outcome(
            _candidate_plan: Plan,
            candidate_result: Any,
            candidate_elapsed_s: float,
        ) -> dict[str, Any]:
            candidate_outcome = extract_outcome(
                candidate_result,
                candidate_elapsed_s,
            )
            if site and "call_site_id" not in candidate_outcome:
                candidate_outcome["call_site_id"] = site
            return candidate_outcome

        maybe_explore_record_async(
            plan,
            result,
            run_mutated,
            _extract_counterfactual_outcome,
        )
    except BaseException:
        log.debug("extract_outcome / observe failed; skipping", exc_info=True)

    return result


__all__ = ["intercept", "is_opted_out", "OPT_OUT_HEADER", "OPT_OUT_VALUE", "Plan"]
