---
title: Claim Bank
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Claim Bank

This file stores candidate claims. It is not manuscript prose.

| ID | Status | Claim | Allowed Wording | Forbidden Wording | Evidence | Minimum Evidence To Publish | Caveats | Related Gaps |
|---|---|---|---|---|---|---|---|---|
| `CLM-001` | `supported` | Agentc transparently intercepts LLM calls and routes them through an optimizer. | Agentc sits between agent code and LLM APIs, intercepting calls and failing open when optimization is unsafe or unavailable. | Agentc optimizes every agent workload automatically. | `ART-020`, `ART-024`, `ART-025` | code path plus README/spec explanation | Transparency depends on SDK patches/framework coverage. | `GAP-007`, `GAP-010` |
| `CLM-002` | `supported` | ContextCompress can produce large savings on long-context workloads. | On the purpose-built `long_context_qa` workload, ContextCompress achieves about 34.5% cost savings with similar input-token savings. | ContextCompress always saves 34.5% on real-world QA. | `RES-001`, `ART-021` | validated n=100 result and rule implementation | Purpose-built long prompts; not standard HotpotQA. | `GAP-004` |
| `CLM-003` | `supported` | ModelDowngrade can produce large savings on routing workloads by changing price per token. | On `gaia_router`, ModelDowngrade-only saves about 35.3% by routing from `gpt-4o` to `gpt-4o-mini`. | ModelDowngrade preserves quality universally. | `RES-002`, `ART-022` | validated n=127 result and route implementation | Savings are price-ratio driven; accuracy needs uncertainty framing. | `GAP-004` |
| `CLM-004` | `promising` | StateDrop produces smaller but real input-token savings on iterative refinement. | StateDrop reduces stale state in iterative refinement, with current evidence showing about 6-10% input-token savings depending on run. | StateDrop is fully headline-ready with no caveats. | `RES-003`, `RES-004`, `ART-023` | complete/clean n=50 treatment or explicit partial caveat | n=50 matrix is partial; accuracy metric is lenient. | `GAP-002`, `GAP-005` |
| `CLM-005` | `supported` | ContextCompress correctly declines near its activation boundary on standard HotpotQA. | On real HotpotQA, ContextCompress fires rarely and produces near-zero savings, supporting the activation-gate story. | ContextCompress fails on real HotpotQA. | `RES-005`, `ART-001` | partial matrix plus audit/fire-rate evidence | Matrix is partial; fire-rate query needs source artifact if used. | `GAP-003` |
| `CLM-006` | `needs-analysis` | Oracle compression shows a large ceiling on real HotpotQA. | Gold-label compression suggests distractors can be removed profitably and may improve answers. | Agentc's current automated rule achieves oracle-level compression. | `RES-006`, `ART-001` | trace queries or richer source beyond one-row CSV | CSV alone does not encode full oracle-ceiling story. | `GAP-006` |
| `CLM-007` | `promising` | AgentC is best framed as a runtime optimizer for compound AI systems. | AgentC optimizes multi-call traces emitted by agent frameworks, using multiple rewrite classes under one runtime control plane. | AgentC is the first runtime optimizer for all LLM agents. | `DRP-001`, `LIT-002`, `LIT-024`, `LIT-025`, `LIT-040`, `LIT-043`, `LIT-044`, `ART-020`, `ART-024` | verified literature blurbs plus nearest-neighbor comparison | Novelty must be narrowed against Agentix/Autellix, Halo, Murakkab, AIOS, Cognify, DSPy, LMQL, SGLang, LLMCompiler, LLM-Tool Compiler, vCache, and single-rewrite baselines. | `GAP-010`, `GAP-012`, `GAP-016` |
| `CLM-008` | `needs-analysis` | Behavior preservation should be stated as metric- and tolerance-bounded, not semantic equivalence. | AgentC evaluates cost savings alongside task-quality metrics and should report uncertainty or repeated-run reliability where possible. | AgentC rewrites are semantics-preserving for arbitrary traces. | `DRP-001`, `STAT-001`, `STAT-004`, `LIT-026`, `LIT-031`, `LIT-032` | statistical analysis and evaluation-source verification | Strong wording depends on paired/repeated evaluation. | `GAP-004`, `GAP-014` |
