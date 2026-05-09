---
title: Related Work Brief
status: draft
last-updated: 2026-05-09
owner: paper-intelligence
---

# Related Work Brief

## Literature Neighborhoods

- LLM application and agent frameworks.
- Compound AI systems and runtime orchestration.
- Prompt and context compression.
- Cost-aware model routing, cascades, and model selection.
- Agent memory and state management.
- Semantic caching and prefix/KV caching.
- Tool-call and branch parallelism.
- ML systems and artifact evaluation.

## Current Gap

Deep research is ingested through `DRP-004`, and `LIT-002` through `LIT-070` now have primary-source checked blurbs in `literature-verified-blurbs.md`. The next step is metadata promotion and final bibliography cleanup.

## Current Related-Work Shape

- Lead with compound AI systems and agent frameworks to justify AgentC's abstraction level.
- Then organize related work by rewrite class: model routing, context compression, semantic caching, parallel tool execution, and StateDrop/program analysis.
- Keep serving systems as an orthogonal contrast: they optimize model-server internals; AgentC optimizes application-level call traces.
- End with stochastic evaluation methodology because the paper's quality-preservation claims depend on evaluation discipline.
- Treat Agentix/Autellix, Halo, Murakkab, AIOS, Cognify, DSPy, LMQL, and SGLang as the closest systems/runtime threats.
- Treat FrugalGPT/RouteLLM/LLMSelector, LLMLingua/LongLLMLingua/LLMLingua-2, GPTCache/ContextCache/vCache, and LLMCompiler/LLM-Tool Compiler as rule-specific nearest neighbors.
- For routing, use `style-guide.md` to compare FrugalGPT, RouteLLM, Optimizing Model Selection for Compound AI Systems, and Language Model Cascades without implying routing itself is novel.
## Current Count

The combined deep-research passes produced 69 active literature rows in `literature-ledger.md`. The source blurbs are now checked, but final related work still needs row-level metadata cleanup, BibTeX, and source-priority decisions.

## Evidence Pointers

- `literature-ledger.md`
- `literature-verified-blurbs.md`
- `related-work-map.md`
- `nearest-neighbor-comparison.md`
- `citation-gap-list.md`
- `citation-style-and-hygiene.md`
- `style-guide.md`
- `literature-review-section-plan.md`
