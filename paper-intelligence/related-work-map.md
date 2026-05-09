---
title: Related Work Map
status: draft
last-updated: 2026-05-09
owner: paper-intelligence
---

# Related Work Map

This map organizes the literature by conceptual neighborhood. Primary-source checked source blurbs now live in `literature-verified-blurbs.md`; this file is the conceptual map, not the citation ledger.

## Neighborhoods

- **Compound AI systems and agent frameworks:** AgentC should be framed around optimizing runtime traces from multi-call systems, not isolated prompts. Anchors: `LIT-002` through `LIT-006`.
- **Runtime optimization for LLM applications:** Most important high-level related-work cluster. Anchors: `LIT-007`, `LIT-008`, `LIT-023`, `LIT-024`, `LIT-025`, `LIT-040`, `LIT-043`, `LIT-044`.
- **Model routing and cascades:** Direct home for `ModelDowngrade`; routing itself is prior art. Anchors: `LIT-007` through `LIT-012`, plus `LIT-046`.
- **Prompt/context compression:** Direct home for `ContextCompress`; AgentC needs to distinguish message-trace compression from standalone text compression. Anchors: `LIT-013` through `LIT-016`, plus `LIT-047` through `LIT-050`.
- **Semantic caching and memoization:** Direct home for `CacheHit`; correctness depends on call site, state, and context. Anchors: `LIT-017` through `LIT-020`, plus `LIT-055` through `LIT-059`.
- **Tool-call scheduling and parallel execution:** Direct home for `ParallelBranch`; main question is dependency/side-effect safety. Anchors: `LIT-021` through `LIT-023`, plus `LIT-060` and `LIT-061`.
- **State/liveness/program analysis:** Best conceptual support for `StateDrop`; LLM context-pruning papers are not enough by themselves. Anchors: `LIT-037`, `LIT-038`, `LIT-051`, `LIT-052`, `LIT-053`, `LIT-054`.
- **Stochastic LLM evaluation methodology:** Supports repeated runs, paired uncertainty, judge-bias controls, and reliability reporting. Anchors: `LIT-026` through `LIT-032`, plus `LIT-063` through `LIT-070`.
- **Serving-layer optimization, KV cache, and prefix caching:** Orthogonal systems contrast. AgentC optimizes which calls are made and rewritten above the API/server layer. Anchors: `LIT-020`, `LIT-033` through `LIT-036`, plus `LIT-062`.

## Rule

Use `literature-verified-blurbs.md` before making any citation claim. Do not rely on the older candidate wording in `literature-blurb-todo.md`.

## Most Important Differentiation Sentence

AgentC is best positioned as a **transparent runtime optimizer for compound AI systems**: it intercepts multi-step agent traces emitted by existing frameworks and applies several rewrite classes under one control plane. That distinguishes it from papers that only route models, compress prompts, cache responses, parallelize tool calls, or optimize serving internals.

## DRP-004 Updates

The second full-literature pass added several sources that should be treated as first-priority checks before any novelty claim:

- **Runtime/compound-system threats:** `LIT-040` Murakkab, `LIT-043` AIOS, `LIT-044` Cognify, `LIT-041` LMQL, `LIT-036` SGLang, `LIT-006` DSPy.
- **Parallel execution threats:** `LIT-021` LLMCompiler and `LIT-060` LLM-Tool Compiler.
- **Compression baselines:** `LIT-047` LLMLingua-2 and `LIT-048` tool-using context compression, in addition to `LIT-013` and `LIT-014`.
- **Cache correctness baselines:** `LIT-055` vCache, `LIT-019` ContextCache, `LIT-018` MeanCache, and `LIT-017` GPTCache.
- **StateDrop compiler grounding:** `LIT-038` program slicing, `LIT-051` program dependence graphs, `LIT-052` SSA/control dependence, and `LIT-037` data-flow analysis.
- **Evaluation backbone:** `LIT-063` pass@k, `LIT-064` and `LIT-065` judge bias, `LIT-068` Bayesian evaluation, `LIT-070` agent-run variance, plus `LIT-031` and `LIT-032`. Use `LIT-069` only as nuance, not as a blanket anti-single-run citation.

Use `literature-verified-blurbs.md` as the current source-priority queue until the consolidation refactor creates `literature-review.md`.
