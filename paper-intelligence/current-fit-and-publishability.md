---
title: Current Fit And Publishability
status: active
last-updated: 2026-09-04
owner: paper-intelligence
---

# Current Fit And Publishability

This is the short reality check for AgentC after the current experiment review and the verified literature-blurb pass.

## One-Screen Summary

AgentC has a plausible paper shape, but it is not ready for a confident main-conference systems submission yet.

The current alpha is strongest as a **guarded, application-side rewrite control plane for opaque LLM-agent calls**. The literature says the individual tricks and the broad agent-runtime/JIT story are already known: routing, prompt compression, semantic caching, parallel tool calls, workflow compilation, and serving optimization. AgentC's narrower opening is online, call-conditional, interaction-aware selection across model targets and semantic rewrites, with explicit abstention and persistent damage control at an existing provider boundary.

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
| Complete-call size/concurrency diagnostic (`RES-013`) | Agentix, Murakkab, Parrot, SGLang, serving/runtime scaling work | Sequential planner cost is small and the deadline fails open safely. | The synchronous audit connection is a real bottleneck: C=32 p99 reaches 46.1ms and throughput gains at most 1.98x. |

## Publishability Read

| Target | Current readiness | Why |
|---|---|---|
| Workshop / short paper | plausible | The trace-optimizer framing, verified literature map, and two strong rule-level results are enough for useful feedback. |
| ATC operational track | possible but rushed | Needs operational lessons, overhead, failure modes, and a tighter deployed-runtime story. |
| MLSys / EuroSys / strong systems venue | not yet | Needs a positive held-out joint-policy result, a non-blocking audit path with a post-fix scaling rerun, artifact polish, and stronger baselines. |
| COLM / LM-facing venue | possible later | Needs clearer cost-quality frontier, stochastic evaluation, and comparisons against routing/compression baselines. |
| Broad AI/ML main venue | weak right now | The contribution currently reads more like systems infrastructure than a new AI method. |

## What Is Real Alpha

- `RES-001`: ContextCompress has the cleanest token-savings story.
- `RES-002`: ModelDowngrade has the cleanest dollar-savings story.
- `RES-005`: HotpotQA near-zero savings can be used as an activation-boundary diagnostic.
- `RES-013`: complete-call timing is now auditable at fixed shape and across size/concurrency; the scaling artifact is an honest negative result that identifies synchronous persistence as the next systems fix.
- `literature-and-nearest-neighbors.md`: the related-work map is now strong enough to guide writing and experiment selection.

## What Is Not Ready

- A claim that AgentC is the first runtime optimizer for LLM agents.
- A claim that all five rewrite rules are equally validated.
- A CacheHit or ParallelBranch headline contribution.
- A strong behavior-preservation claim without paired/repeated uncertainty.
- A StateDrop soundness claim without a concrete dependency/read-window model.

## Best Next Contributions

1. Remove or batch the synchronous per-call audit write, preserve bounded-loss durability, and rerun the frozen 153,600-call matrix.
2. Run the held-out joint model-routing plus semantic-rewrite campaign against fixed, route-only, rewrite-only, both sequential orders, current greedy, and best-static controls.
3. Convert the strongest runnable baselines into a feasibility matrix: AgentOpt, RouteLLM, FrugalGPT, LLMSelector, LLMLingua/LongLLMLingua/LLMLingua-2, GPTCache/vCache, and LLMCompiler.
4. Validate the persistent risk controller at its advertised 2% sampling rate, charging counterfactual calls and damage.
5. Run the blind unengineered workload gate before investing further in the broad MLSys story.

## Avery Read Path

Read these in order:

1. `README.md`
2. `current-fit-and-publishability.md`
3. `literature-and-nearest-neighbors.md`
4. `claims-gaps-and-risks.md`
5. `results-experiments-and-repro.md`
6. `strategy-and-venues.md`

That path gives the paper state without requiring a linear read through every ledger.
