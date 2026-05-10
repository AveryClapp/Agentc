---
title: Positioning Taxonomy
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Positioning Taxonomy

This file defines the conceptual neighborhoods around Agentc.

| Neighborhood | What It Optimizes | Where It Acts | Relationship To Agentc |
|---|---|---|---|
| Agent frameworks | Task decomposition and tool orchestration | application/framework layer | Agentc sits below these frameworks and optimizes LLM calls they emit. |
| Model routing | Model choice per request | application/router/runtime layer | Agentc includes ModelDowngrade but is broader than routing. |
| Prompt/context compression | Prompt length and salience | prompt construction/runtime layer | Agentc includes ContextCompress as one runtime rewrite. |
| Semantic caching | Duplicate or similar requests | cache/runtime layer | Agentc's CacheHit bridges memoization with optimizer planning. |
| LLM serving optimization | Batching, KV cache, scheduling | provider/server layer | Agentc is application-side/runtime-side, not provider serving infrastructure. |
| Tool-call parallelism | Latency and scheduling | workflow/runtime layer | Agentc's ParallelBranch is observability/future dispatcher hook. |
| Compiler/runtime systems | Program optimization | runtime/compiler layer | Agentc borrows optimizer framing for LLM call graphs. |
| Evaluation methodology | Measurement under stochasticity | benchmark layer | Agentc's shared-baseline ablation is part of its contribution story. |

## Do-Not-Compare-As-Equivalent Notes

- Do not treat provider-side KV cache optimization as the same intervention as Agentc.
- Do not treat model routing alone as the same as a multi-rule runtime optimizer.
- Do not treat prompt compression papers as covering state pruning or semantic caching.

