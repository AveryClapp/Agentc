---
title: Current Fit And Publishability
status: active
last-updated: 2026-05-09
owner: paper-intelligence
---

# Current Fit And Publishability

This is the short reality check for AgentC after the current experiment review and the verified literature-blurb pass.

## One-Screen Summary

AgentC has a plausible paper shape, but it is not ready for a confident main-conference systems submission yet.

The current alpha is strongest as a **runtime trace optimizer for multi-step LLM agents**. The literature says the individual tricks are already known: routing, prompt compression, semantic caching, parallel tool calls, and serving optimization. AgentC's opening is the control plane: apply several of those ideas at the framework-call boundary over observed agent traces.

## How Current Results Fit The Literature

| Current result or idea | Literature neighborhood | What it proves now | What it does not prove yet |
|---|---|---|---|
| `ContextCompress` on `long_context_qa` | LLMLingua, LongLLMLingua, Selective Context, tool-use compression | Runtime context rewriting can save substantial input tokens on long-context stress workloads. | It does not beat specialist compressors yet, and it does not prove broad real-task savings. |
| `ModelDowngrade` on `gaia_router` | FrugalGPT, RouteLLM, LLMSelector, routing/cascades | Internal call-site model substitution can cut cost sharply. | Routing itself is not novel, and quality preservation needs stronger uncertainty treatment. |
| `StateDrop` on iterative refinement | Program slicing, data-flow, liveness, memory/context systems | Stale state can be pruned for modest input-token savings. | It is not sound compiler slicing unless AgentC defines dependencies/read windows precisely. |
| Real HotpotQA near-zero savings | Compression gating and conservative runtime policy | The rule can decline near the activation boundary, which is a useful systems behavior. | It is not a headline savings result. |
| Hotpot oracle compression | Compression headroom / idea generation | There is likely useful headroom if AgentC can identify irrelevant context better. | The current automated rule does not achieve oracle-level compression. |
| `CacheHit` | GPTCache, ContextCache, MeanCache, vCache | Important future direction and likely useful runtime pass. | Needs false-hit, invalidation, and context-key evidence before becoming a paper claim. |
| `ParallelBranch` | LLMCompiler, LLM-Tool Compiler, LangGraph, LLMOrch | Important future direction for latency. | Needs dependency, side-effect, and idempotence policy before strong claims. |

## Publishability Read

| Target | Current readiness | Why |
|---|---|---|
| Workshop / short paper | plausible | The trace-optimizer framing, verified literature map, and two strong rule-level results are enough for useful feedback. |
| ATC operational track | possible but rushed | Needs operational lessons, overhead, failure modes, and a tighter deployed-runtime story. |
| MLSys / EuroSys / strong systems venue | not yet | Needs end-to-end multi-rule evidence, overhead/tail-latency numbers, artifact polish, and stronger baselines. |
| COLM / LM-facing venue | possible later | Needs clearer cost-quality frontier, stochastic evaluation, and comparisons against routing/compression baselines. |
| Broad AI/ML main venue | weak right now | The contribution currently reads more like systems infrastructure than a new AI method. |

## What Is Real Alpha

- `RES-001`: ContextCompress has the cleanest token-savings story.
- `RES-002`: ModelDowngrade has the cleanest dollar-savings story.
- `RES-005`: HotpotQA near-zero savings can be used as an activation-boundary diagnostic.
- `literature-and-nearest-neighbors.md`: the related-work map is now strong enough to guide writing and experiment selection.

## What Is Not Ready

- A claim that AgentC is the first runtime optimizer for LLM agents.
- A claim that all five rewrite rules are equally validated.
- A CacheHit or ParallelBranch headline contribution.
- A strong behavior-preservation claim without paired/repeated uncertainty.
- A StateDrop soundness claim without a concrete dependency/read-window model.

## Best Next Contributions

1. Run or design one end-to-end workload where multiple rules can fire together.
2. Measure interception/planner overhead and latency-tail impact.
3. Convert the strongest runnable baselines into a feasibility matrix: RouteLLM, FrugalGPT, LLMSelector, LLMLingua/LongLLMLingua/LLMLingua-2, GPTCache/vCache, and LLMCompiler.
4. Add paired or repeated-run uncertainty for headline accuracy deltas.
5. Write the StateDrop dependency model in plain language before trying to sell it as principled.

## Avery Read Path

Read these in order:

1. `README.md`
2. `current-fit-and-publishability.md`
3. `literature-and-nearest-neighbors.md`
4. `claims-gaps-and-risks.md`
5. `results-experiments-and-repro.md`
6. `strategy-and-venues.md`

That path gives the paper state without requiring a linear read through every ledger.
