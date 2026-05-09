---
title: Deep Research Drop - Literature Map
status: active
last-updated: 2026-05-09
owner: paper-intelligence
---

# Deep Research Drop - Literature Map

## Source

User pasted a deep-research response in chat on 2026-05-09. The response used model-internal citation handles, so all papers below are treated as `candidate` until checked against primary sources.

## Extracted Takeaway

The strongest framing is **runtime optimization for compound AI systems**. AgentC should not be sold as a single prompt-compression, routing, caching, or parallel-tool system. The distinct claim is that AgentC sits below existing agent frameworks, observes live multi-call traces, and applies several rewrite classes under one control plane.

## Closest Literature Clusters

| Cluster | AgentC relevance | Key candidate sources |
|---|---|---|
| Compound AI systems and agent frameworks | Justifies optimizing traces instead of single prompts. | Compound AI systems essays/papers, ReAct, AutoGen, DSPy, MRKL, Toolformer, TaskWeaver, LangGraph docs. |
| Runtime optimization for LLM applications | Best high-level related-work neighborhood. | FrugalGPT, DSPy/MIPRO, LLMSelector, Optimas, ALTO, Autellix, Halo. |
| Model routing/cascades | Direct home for `ModelDowngrade`; routing itself is not new. | FrugalGPT, RouteLLM, RouterBench, Language Model Cascades, unified routing/cascading, confidence-token routing. |
| Prompt/context compression | Direct home for `ContextCompress`; must distinguish message-trace compression from token-level compression. | LLMLingua, LongLLMLingua, Selective Context, RECOMP, In-Context Former. |
| Semantic caching/memoization | Direct home for `CacheHit`; correctness must account for call site and context. | GPTCache, MeanCache, ContextCache, Prompt Cache, classical semantic caching work. |
| Tool-call scheduling/parallel execution | Direct home for `ParallelBranch`; safety depends on side effects and dependency detection. | LLMCompiler, ReWOO, ALTO, W&D parallel tool calling, Tree/Graph of Thoughts. |
| State pruning and program analysis | Weakest direct LLM-literature match for `StateDrop`. | Selective Context and LLMLingua are indirect; program data-flow analysis, program slicing, liveness, and memoization are likely better anchors. |
| Stochastic LLM evaluation | Forces better evaluation discipline. | HELM, MT-Bench, AlpacaEval/length-controlled AlpacaEval, AgentBench, SWE-bench, tau-bench, ReliableEval. |
| Serving/inference systems | Orthogonal systems contrast. | Orca, vLLM, DistServe, Sarathi-Serve, SGLang, speculative decoding, Medusa, Prompt Cache, Autellix, Halo. |

## Main Reviewer Risks Extracted

- AgentC could look like RouteLLM + LLMLingua + GPTCache glued together.
- “First runtime optimizer” is unsafe unless narrowly delimited against Autellix, Halo, FrugalGPT, and LLMCompiler.
- “Behavior-preserving” is too strong unless supported by semantics, guardrails, and stochastic evaluation.
- `StateDrop` needs compiler/program-analysis citations and perhaps stronger experiments.
- `CacheHit` needs correctness conditions around context sensitivity, stale results, and false hits.
- `ParallelBranch` needs a side-effect and dependency story.
- Single-run evaluation will be attacked; repeated trials and paired uncertainty matter.

## Promotions Made

- Seeded `literature-ledger.md` with candidate rows.
- Updated `related-work-map.md`.
- Updated `citation-gap-list.md`.
- Added gap and reviewer-risk items for novelty, semantics, baselines, and stochastic evaluation.

