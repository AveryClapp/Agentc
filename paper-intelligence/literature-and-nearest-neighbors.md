---
title: Literature and Nearest Neighbors
status: active
last-updated: 2026-05-09
owner: paper-intelligence
---

# Literature and Nearest Neighbors

This is the authoritative literature map for AgentC. It merges the verified source blurbs, the conceptual related-work map, and the nearest-neighbor comparison.

Routing is one subsection here. The paper-level frame is broader: **runtime optimization for compound AI / multi-step LLM agent traces**.

Supersedes:

- `literature-verified-blurbs.md`
- `literature-ledger.md`
- `literature-blurb-todo.md`
- `nearest-neighbor-comparison.md`
- `related-work-map.md`
- `literature-review-section-plan.md`
- `bibliography-ledger.md` until final BibTeX cleanup

## Verification Summary

- Verified source rows: 69 active literature entries.
- Rows needing correction from the candidate pass: `LIT-003`, `LIT-006`, `LIT-011`, `LIT-015`, `LIT-020`, `LIT-024`, `LIT-034`, `LIT-039`, `LIT-043`, `LIT-044`, `LIT-045`, `LIT-046`, `LIT-049`, `LIT-053`, `LIT-054`, `LIT-059`, `LIT-061`, `LIT-063`, `LIT-069`, `LIT-070`.
- Strongest novelty threats: `LIT-008`, `LIT-024`, `LIT-025`, `LIT-036`, `LIT-040`, `LIT-041`, `LIT-043`, `LIT-044`, `LIT-055`, `LIT-060`.
- Strongest runnable baselines to consider: `LIT-007`, `LIT-008`, `LIT-009`, `LIT-013`, `LIT-014`, `LIT-017`, `LIT-021`, `LIT-034`, `LIT-036`, `LIT-042`, `LIT-047`, `LIT-055`.
- Most important evaluation-caution sources: `LIT-031`, `LIT-032`, `LIT-064`, `LIT-065`, `LIT-068`, `LIT-070`.

## High-Level Blurb

The verified literature still supports the main framing: AgentC is best positioned as a runtime optimizer for compound AI / multi-step LLM agent traces, not as a paper about routing alone. Prior work already owns the individual tricks: routing, cascades, prompt compression, semantic caching, parallel tool execution, and serving optimization. AgentC's plausible contribution is the control layer: observing framework-emitted traces and applying multiple rewrite classes under one runtime policy.

The dangerous reviewer objection is now sharper: several systems already optimize compound AI or agent workflows, especially Agentix/Autellix, Halo, Murakkab, AIOS, Cognify, DSPy, LMQL, SGLang, LLMCompiler, LLM-Tool Compiler, and vCache. The paper should avoid broad "first runtime optimizer" claims and instead say exactly what AgentC optimizes, where it intercepts, which rewrites it applies, what guarantees it does not provide, and how behavior is measured under stochasticity.

## How To Use This File

- Use the **cluster map** to decide where a paper belongs in related work.
- Use the **nearest-neighbor matrix** before making novelty claims.
- Use the **runnable baseline table** before proposing experiments.
- Use the **source blurbs** as checked building blocks, not final manuscript prose.
- Do not cite a source from model-generated summaries alone; verify final metadata before camera-ready writing.

## Cluster Map

| Cluster | AgentC connection | Main IDs | What it proves | What it threatens |
|---|---|---|---|---|
| Compound AI systems and agent frameworks | Optimizer target is a multi-call workflow/trace. | `LIT-002`-`LIT-006`, `LIT-042`, `LIT-043` | Agent workloads are structured systems, not isolated prompts. | AgentC may look like a thinner framework/runtime layer. |
| Runtime optimization for LM workflows | AgentC belongs in systems/runtime optimization. | `LIT-006`, `LIT-008`, `LIT-023`-`LIT-025`, `LIT-040`-`LIT-045` | Cost/latency/quality optimization over LM programs is legitimate. | Broad "first optimizer" claims are unsafe. |
| Model routing and cascades | Direct home for `ModelDowngrade`. | `LIT-007`-`LIT-012`, `LIT-046` | Some calls can use cheaper models. | Routing itself is crowded prior art. |
| Prompt/context compression | Direct home for `ContextCompress`. | `LIT-013`-`LIT-016`, `LIT-047`-`LIT-050` | Long prompts contain removable redundancy. | Standalone compressors are strong baselines. |
| State/liveness/program analysis | Best support for `StateDrop`. | `LIT-037`, `LIT-038`, `LIT-051`-`LIT-054` | Pruning irrelevant state has a compiler lineage. | StateDrop is only principled if dependencies/read windows are defined. |
| Semantic caching and memoization | Direct home for `CacheHit`. | `LIT-017`-`LIT-020`, `LIT-055`-`LIT-059` | Reuse can save cost/latency. | False hits and context sensitivity are central reviewer risks. |
| Tool-call scheduling and parallelism | Direct home for `ParallelBranch`. | `LIT-021`-`LIT-023`, `LIT-060`, `LIT-061` | Independent tool/model calls can be parallelized. | Dependency and side-effect safety must be explicit. |
| Serving and inference systems | Orthogonal systems contrast. | `LIT-020`, `LIT-033`-`LIT-036`, `LIT-062` | Serving stacks optimize admitted requests. | Reviewers may ask if serving systems erase gains. |
| Stochastic evaluation | Evaluation protocol backbone. | `LIT-026`-`LIT-032`, `LIT-063`-`LIT-070` | Single-run agent evaluation is weak. | Quality-preservation claims need repeated paired tests. |

## Nearest-Neighbor Matrix

| LIT ID | Work/System | Optimization Target | Intervention Point | Runtime vs Offline | AgentC Distinction |
|---|---|---|---|---|---|
| `LIT-024` | Agentix / Autellix | Agentic-program execution/scheduling | Serving/runtime scheduler with program context | Runtime | Closest serving-layer threat; AgentC must show semantic rewrite rules above API/server scheduling. |
| `LIT-025` | Halo | Batch agent-workflow DAG/query-plan optimization | Workflow/query optimizer | Runtime/batch | Close systems framing; AgentC distinction depends on online SDK/API interception and concrete rewrite passes. |
| `LIT-040` | Murakkab | Compound-AI workflow resource efficiency | Declarative workflow + adaptive runtime | Runtime/adaptive | Major threat to broad runtime-optimizer claim; AgentC must emphasize online trace rewrite and concrete multi-rule passes. |
| `LIT-043` | AIOS | Agent execution runtime | Agent OS scheduler/context/memory/tool layer | Runtime | Closest OS/runtime analogy; AgentC is narrower and focused on trace rewrite/control. |
| `LIT-044` | Cognify | Gen-AI workflow autotuning | Hierarchical autotuner over structure/operator/model/prompt choices | Mostly offline/autotuning | Close optimizer threat; AgentC is runtime interception and trace rewrites rather than evaluator-driven autotuning. |
| `LIT-006` | DSPy | LM pipelines | Declarative LM-program compiler | Mostly compile/optimize time | AgentC does not require applications to be authored in DSPy. |
| `LIT-041` | LMQL | LM programs | Query language and optimizing runtime | Runtime/compiler | AgentC does not require rewriting apps into a new language. |
| `LIT-036` | SGLang | Structured LM-program execution | LM-program language + optimized runtime | Runtime/compiler | AgentC should emphasize framework-emitted trace optimization without porting workloads into SGLang. |
| `LIT-008` | Optimizing Model Selection for Compound AI Systems | Per-component model choice | Compound-system model selector | Offline/online mixed | Direct model-selection comparison; AgentC spans more rewrite classes. |
| `LIT-007` | FrugalGPT | Model/API cost | Routing/cascade controller | Runtime/query-time | ModelDowngrade prior art; AgentC wraps routing as one pass inside trace optimizer. |
| `LIT-009` | RouteLLM | Model routing | Query router | Runtime | Direct ModelDowngrade baseline; not a full agent-runtime optimizer. |
| `LIT-013` | LLMLingua | Prompt token compression | Prompt compressor | Pre-call/runtime | Direct ContextCompress baseline; not message-trace/state-aware runtime rewrite. |
| `LIT-017` | GPTCache | Repeated prompt reuse | Semantic cache | Runtime | Direct CacheHit baseline; not a multi-rule optimizer. |
| `LIT-055` | vCache | Verified semantic prompt caching | Cache layer with error controls | Runtime | Raises correctness bar for CacheHit; AgentC needs false-hit and context-key story. |
| `LIT-021` | LLMCompiler | Parallel function/tool execution | Planner/compiler | Compile/planning time | ParallelBranch prior art; AgentC claims transparent trace-level pass. |
| `LIT-060` | LLM-Tool Compiler | Tool-call fusion and parallel function calling | Compiler/planner | Compile/planning time | Direct ParallelBranch threat; AgentC must distinguish runtime trace interception. |

## Runnable Baselines To Consider

| Rewrite or comparison | Strongest runnable candidates | Notes |
|---|---|---|
| ModelDowngrade | `LIT-007` FrugalGPT, `LIT-009` RouteLLM, `LIT-008` LLMSelector if artifact works | Use if routing claims are more than incidental. |
| ContextCompress | `LIT-013` LLMLingua, `LIT-014` LongLLMLingua, `LIT-047` LLMLingua-2 | Needed if ContextCompress is a headline result. |
| CacheHit | `LIT-017` GPTCache, `LIT-055` vCache, possibly `LIT-019` ContextCache | Only run if CacheHit is evaluated; otherwise cite as future-risk. |
| ParallelBranch | `LIT-021` LLMCompiler | Needed if ParallelBranch becomes a result, not just future work. |
| Framework/runtime substrate | `LIT-042` LangGraph, `LIT-043` AIOS, `LIT-036` SGLang | Mostly integration/orthogonality comparisons. |
| Evaluation methods | `LIT-066` JudgeBench, `LIT-068` Don't Pass@k tooling | Useful if judge or stochastic reliability claims become central. |

## Related-Work Section Shape

1. Start with compound AI systems and framework-emitted traces.
2. Position AgentC against runtime/workflow optimizers.
3. Cover rewrite families as prior art: routing, compression, caching, parallelism, and StateDrop's compiler analogy.
4. Separate orthogonal serving systems from application-level trace rewrites.
5. End with stochastic evaluation, because behavior-preserving optimization must be measured with repeated paired trials.

## Source Blurbs

The entries below are checked source blurbs. They preserve the `LIT` IDs, citation-use notes, differentiation notes, action decisions, and evidence/threat scores from the verification pass.

## Compound AI Systems And Frameworks

### `LIT-002` - The Shift from Models to Compound AI Systems

- Primary: https://bair.berkeley.edu/blog/2024/02/18/compound-ai-systems/
- Verdict: accurate, but source type is BAIR blog/essay, not archival paper.
- Citation blurb: Use as intro vocabulary for compound AI systems: models, tools, retrievers, and control logic form the real optimization target.
- AgentC difference: AgentC does not invent compound systems; it optimizes runtime traces inside that frame.
- Use: framing only. Baseline: `not-comparable`. Evidence: 4. Threat: 2.

### `LIT-003` - Are More LLM Calls All You Need?

- Primary: https://openreview.net/forum?id=m5106RRLgx and https://arxiv.org/abs/2403.02419
- Verdict: needs revision from candidate pass.
- Citation blurb: Multi-call compound inference can help up to a task-dependent optimum, but extra calls can hurt; this motivates cost/latency-aware runtime choices.
- AgentC difference: AgentC is not trying to add calls for capability; it tries to make existing traces cheaper and safer.
- Use: intro or related-work motivation, not a direct baseline. Baseline: `cite-only`. Evidence: 3. Threat: 2.

### `LIT-004` - ReAct

- Primary: https://openreview.net/forum?id=WE_vluYUL-X
- Verdict: accurate.
- Citation blurb: ReAct is the canonical reasoning-plus-acting pattern that produces interleaved model/tool traces.
- AgentC difference: ReAct is a prompting/agent pattern; AgentC is a runtime optimizer over traces that patterns like ReAct can emit.
- Use: background anchor. Baseline: `not-comparable`. Evidence: 4. Threat: 2.

### `LIT-005` - AutoGen

- Primary: https://openreview.net/forum?id=BAakY1hNKS and https://www.microsoft.com/en-us/research/publication/autogen-enabling-next-gen-llm-applications-via-multi-agent-conversation-framework/
- Verdict: accurate; fill in COLM 2024 metadata.
- Citation blurb: AutoGen establishes multi-agent conversation workflows as mainstream agent framework practice.
- AgentC difference: AgentC should sit below or beside frameworks like AutoGen, not claim to replace their orchestration model.
- Use: framework comparison and possible integration target. Baseline: `not-comparable`. Evidence: 4. Threat: 3.

### `LIT-006` - DSPy

- Primary: https://openreview.net/forum?id=sY5N0zY5Od
- Verdict: needs exact title revision: "DSPy: Compiling Declarative Language Model Calls into State-of-the-Art Pipelines."
- Citation blurb: DSPy is a major LM-program/compiler comparison because it optimizes declarative LM pipelines against metrics.
- AgentC difference: DSPy optimizes authored LM programs; AgentC should emphasize runtime interception and rewrite passes over existing framework traces.
- Use: must-cite nearest neighbor. Baseline: `run-if-compatible`. Evidence: 5. Threat: 4.

## Routing, Cascades, And Model Selection

### `LIT-007` - FrugalGPT

- Primary: https://openreview.net/forum?id=cSimKw5p6R
- Verdict: accurate.
- Citation blurb: FrugalGPT is the obvious cost-saving/cascade ancestor for ModelDowngrade.
- AgentC difference: ModelDowngrade is one trace-level pass inside a broader optimizer, not the paper's whole contribution.
- Use: must-cite and candidate runnable baseline. Baseline: `run-if-routing-workloads-exist`. Evidence: 5. Threat: 4.

### `LIT-008` - Optimizing Model Selection for Compound AI Systems

- Primary: https://arxiv.org/abs/2502.14815
- Verdict: accurate.
- Citation blurb: LLMSelector directly targets per-component model allocation in compound AI systems.
- AgentC difference: AgentC includes model selection but also context compression, state dropping, caching, and parallelism under one control plane.
- Use: must-cite nearest neighbor for ModelDowngrade. Baseline: `run`. Evidence: 5. Threat: 5.

### `LIT-009` - RouteLLM

- Primary: https://openreview.net/forum?id=8sSqNntaMr
- Verdict: accurate.
- Citation blurb: RouteLLM is the modern preference-trained router for choosing cheaper versus stronger LLMs.
- AgentC difference: AgentC routes internal call sites inside agent traces, not only external user queries.
- Use: direct ModelDowngrade comparator. Baseline: `run`. Evidence: 5. Threat: 4.

### `LIT-010` - RouterBench

- Primary: https://openreview.net/forum?id=IVXmV8Uxwh
- Verdict: accurate.
- Citation blurb: RouterBench standardizes multi-LLM routing evaluation over query-level outcomes.
- AgentC difference: AgentC's routing is embedded in a multi-rule trace optimizer, so RouterBench is useful but not sufficient.
- Use: optional routing-eval citation. Baseline: `cite-only`. Evidence: 4. Threat: 3.

### `LIT-011` - Language Model Cascades

- Primary: https://arxiv.org/abs/2207.10342
- Verdict: needs revision.
- Citation blurb: This is mostly about composing repeated LM calls with control flow as probabilistic programs, not primarily cheap-to-expensive routing.
- AgentC difference: Use it for composed LM trace vocabulary, not as a direct ModelDowngrade baseline.
- Use: background, demote from routing must-cite. Baseline: `not-comparable`. Evidence: 3. Threat: 2.

### `LIT-012` - A Unified Approach to Routing and Cascading for LLMs

- Primary: https://openreview.net/forum?id=AAl89VNNy1
- Verdict: accurate.
- Citation blurb: Routing and cascading can be jointly modeled through quality/cost estimators.
- AgentC difference: AgentC should treat downgrade/fallback as one runtime pass, not claim routing-plus-cascade novelty.
- Use: ModelDowngrade related work. Baseline: `run-if-routing-central`. Evidence: 4. Threat: 4.

### `LIT-046` - Large Language Model Routing with Benchmark Datasets

- Primary: https://openreview.net/forum?id=Zb0ajZ7vAt and https://arxiv.org/abs/2309.15789
- Verdict: needs revision from "benchmark only" to routing method.
- Citation blurb: Benchmark evaluation data can train routers for unseen tasks without new labeled examples.
- AgentC difference: AgentC routes internal call sites as one pass among several, not whole-task model choice only.
- Use: routing related work and possible routing-only baseline. Baseline: `run`. Evidence: 4. Threat: 3.

## Context Compression And Context Pruning

### `LIT-013` - LLMLingua

- Primary: https://aclanthology.org/2023.emnlp-main.825/
- Verdict: accurate.
- Citation blurb: LLMLingua is the canonical prompt-compression comparator for reducing tokens while preserving task quality.
- AgentC difference: ContextCompress should claim runtime trace/message awareness, not invention of prompt compression.
- Use: must-cite and likely runnable baseline. Baseline: `run`. Evidence: 5. Threat: 4.

### `LIT-014` - LongLLMLingua

- Primary: https://aclanthology.org/2024.acl-long.91/
- Verdict: accurate.
- Citation blurb: LongLLMLingua is the key long-context compression baseline.
- AgentC difference: AgentC needs to show why conservative runtime message dropping is different from generic long-context compression.
- Use: must-cite compression baseline. Baseline: `run`. Evidence: 4. Threat: 3.

### `LIT-015` - Selective Context

- Primary: https://aclanthology.org/2023.emnlp-main.391/
- Verdict: needs revision.
- Citation blurb: Selective Context supports text-context pruning for efficiency, but only weakly supports StateDrop liveness claims.
- AgentC difference: AgentC should distinguish metadata/state-read windows from salience-based text pruning.
- Use: must-cite for context pruning; weak bridge for StateDrop. Baseline: `cite-only`. Evidence: 4 for ContextCompress, 2 for StateDrop. Threat: 3.

### `LIT-016` - RECOMP

- Primary: https://openreview.net/forum?id=mlJLVigNHp
- Verdict: accurate.
- Citation blurb: RECOMP trains compressors for retrieval-augmented contexts and can omit irrelevant retrieved material.
- AgentC difference: AgentC compresses runtime agent message traces, not primarily retrieved documents.
- Use: optional compression breadth citation. Baseline: `cite-only`. Evidence: 3. Threat: 2.

### `LIT-047` - LLMLingua-2

- Primary: https://aclanthology.org/2024.findings-acl.57/
- Verdict: accurate.
- Citation blurb: LLMLingua-2 uses distilled token classification for faster, faithful task-agnostic compression.
- AgentC difference: AgentC must justify trace/runtime awareness beyond generic prompt compression.
- Use: must-cite and likely runnable baseline. Baseline: `run`. Evidence: 5. Threat: 4.

### `LIT-048` - Concise and Precise Context Compression for Tool-Using Language Models

- Primary: https://aclanthology.org/2024.findings-acl.974/
- Verdict: accurate.
- Citation blurb: Tool documentation can be compressed while preserving tool and parameter names.
- AgentC difference: AgentC compression should preserve tool-critical fields or avoid compression in unsafe tool contexts.
- Use: must-cite for tool-use ContextCompress safety. Baseline: `cite-only` unless artifact appears. Evidence: 5. Threat: 4.

### `LIT-049` - TACO-RL

- Primary: https://aclanthology.org/2025.findings-acl.81/
- Verdict: needs final ACL metadata update.
- Citation blurb: Task-aware RL can improve prompt compression over task-agnostic methods at fixed compression rates.
- AgentC difference: AgentC can claim lower integration overhead and conservatism, not compression optimality.
- Use: optional compression citation unless ContextCompress becomes central. Baseline: `cite-only`. Evidence: 3. Threat: 3.

### `LIT-050` - Prompt Compression for Large Language Models: A Survey

- Primary: https://aclanthology.org/2025.naacl-long.368/
- Verdict: accurate.
- Citation blurb: Survey for prompt-compression taxonomy: hard versus soft methods, uses, limitations, and open problems.
- AgentC difference: Helps position ContextCompress but does not threaten the runtime-control-plane story.
- Use: background only. Baseline: `not-comparable`. Evidence: 3. Threat: 1.

## Caching, Memoization, And Reuse

### `LIT-017` - GPTCache

- Primary: https://aclanthology.org/2023.nlposs-1.24/
- Verdict: accurate, with metadata caution because the ACL page and PDF author metadata differ.
- Citation blurb: GPTCache is the canonical open-source semantic response cache for LLM applications.
- AgentC difference: AgentC needs richer call-site, state, and invalidation constraints beyond prompt similarity.
- Use: must-cite and candidate CacheHit baseline. Baseline: `run-if-cachehit-evaluated`. Evidence: 4. Threat: 3.

### `LIT-018` - MeanCache

- Primary: https://doi.org/10.1109/IPDPS64566.2025.00117
- Verdict: accurate.
- Citation blurb: MeanCache emphasizes user/context-aware semantic caching to reduce false hits.
- AgentC difference: AgentC cache keys should include runtime call-site, state, and context.
- Use: cache correctness and false-hit framing. Baseline: `cite-only`. Evidence: 4. Threat: 3.

### `LIT-019` - ContextCache

- Primary: https://www.vldb.org/pvldb/vol18/p5391-yan.pdf
- Verdict: accurate.
- Citation blurb: Multi-turn semantic caching needs context modeling to avoid wrong hits for superficially similar queries.
- AgentC difference: Agent traces are context-sensitive, so AgentC must avoid similarity-only CacheHit claims.
- Use: must-cite if CacheHit is more than future work. Baseline: `cite-only`. Evidence: 4. Threat: 3.

### `LIT-020` - Prompt Cache

- Primary: https://proceedings.mlsys.org/paper_files/paper/2024/hash/a66caa1703fe34705a4368c3014c1966-Abstract-Conference.html
- Verdict: needs wording revision.
- Citation blurb: Prompt Cache reuses precomputed attention states for declared prompt modules; it is serving-layer attention reuse, not semantic response caching.
- AgentC difference: AgentC rewrites or removes semantic calls above the model server/API boundary.
- Use: serving-cache orthogonality citation. Baseline: `not-comparable`. Evidence: 4. Threat: 3.

### `LIT-039` - Compile-Time Function Memoization

- Primary: https://doi.org/10.1145/3033019.3033024
- Verdict: needs revision from "classic" to compiler paper.
- Citation blurb: Compiler-inserted memoization frames recomputation avoidance, but LLM equivalence is harder due to stochasticity and context.
- AgentC difference: AgentC caches API-level LLM calls with semantic and state-aware invalidation risk.
- Use: optional background, not nearest neighbor. Baseline: `not-comparable`. Evidence: 3. Threat: 1.

### `LIT-055` - vCache

- Primary: https://openreview.net/forum?id=zF0A0xw3HZ and https://github.com/vcache-project/vCache
- Verdict: accurate and stronger threat than the candidate pass implied.
- Citation blurb: vCache sets the modern bar for semantic caching with user-specified error bounds, adaptive thresholds, and measured false-hit control.
- AgentC difference: AgentC can use richer call-site/state keys, but still needs false-hit metrics or bounded-risk language.
- Use: must-cite for CacheHit correctness. Baseline: `run-if-cachehit-evaluated`. Evidence: 5. Threat: 5.

### `LIT-056` - Semantic Caching for Low-Cost LLM Serving

- Primary: https://arxiv.org/abs/2508.07675
- Verdict: accurate.
- Citation blurb: This studies semantic-cache eviction and online adaptation under changing query/cost distributions.
- AgentC difference: CacheHit's first burden is correctness/keying; eviction policy is second-order unless caching becomes central.
- Use: optional cache-policy citation. Baseline: `cite-only`. Evidence: 4. Threat: 3.

### `LIT-057` - Semantic Caching and Query Processing

- Primary: https://doi.org/10.1109/TKDE.2003.1161590
- Verdict: accurate.
- Citation blurb: Classical semantic caching gives query/segment/query-trimming vocabulary for semantic reuse.
- AgentC difference: AgentC caches stochastic natural-language calls, where equivalence is approximate and context-sensitive.
- Use: historical systems anchor. Baseline: `not-comparable`. Evidence: 3. Threat: 1.

### `LIT-058` - Semantic Caching via Query Matching for Web Sources

- Primary: https://ir.webis.de/anthology/1999.cikm_conference-99.11/
- Verdict: accurate.
- Citation blurb: Early query-matching semantic cache work supports the cache-correctness lineage.
- AgentC difference: Does not prove correctness for stochastic LLM calls with trace state and tools.
- Use: background only. Baseline: `not-comparable`. Evidence: 3. Threat: 1.

### `LIT-059` - A Consistent Semantics of Self-Adjusting Computation

- Primary: https://www.cambridge.org/core/journals/journal-of-functional-programming/article/consistent-semantics-of-selfadjusting-computation/441A28C813BDA23B57F1ED2BB1A7E36E and https://arxiv.org/abs/1106.0478
- Verdict: needs venue/year revision.
- Citation blurb: Self-adjusting computation gives rigorous memoization/change-propagation semantics under changing state.
- AgentC difference: AgentC uses approximate stochastic LLM calls, not deterministic program evaluation.
- Use: optional conceptual support. Baseline: `not-comparable`. Evidence: 3. Threat: 1.

## Parallel Execution And Tool-Call Scheduling

### `LIT-021` - An LLM Compiler for Parallel Function Calling

- Primary: https://proceedings.mlr.press/v235/kim24y.html
- Verdict: accurate.
- Citation blurb: LLMCompiler plans function-call DAGs and executes independent calls in parallel, reducing latency/cost versus ReAct.
- AgentC difference: AgentC should emphasize transparent runtime trace optimization rather than planner/compiler orchestration.
- Use: must-cite and direct ParallelBranch baseline. Baseline: `run-if-parallelbranch-evaluated`. Evidence: 5. Threat: 4.

### `LIT-022` - ReWOO

- Primary: https://arxiv.org/abs/2305.18323
- Verdict: accurate.
- Citation blurb: ReWOO separates planning, tool execution, and solving to reduce repeated reasoning/observation loops.
- AgentC difference: ReWOO changes prompting/program structure; AgentC aims to optimize already-emitted traces.
- Use: supporting comparison for execution rewrites. Baseline: `cite-only`. Evidence: 4. Threat: 3.

### `LIT-023` - ALTO

- Primary: https://sing.stanford.edu/site/assets/publications/alto-euromlsys24.pdf
- Verdict: accurate, but metadata should use full title and note newer arXiv version.
- Citation blurb: ALTO optimizes distributed compound-AI pipelines through partial-output streaming, aggregation-aware routing, and prompt-aware scheduling.
- AgentC difference: AgentC rewrites application-level LLM call traces for token/cost savings rather than orchestrating distributed streaming pipelines.
- Use: systems nearest neighbor and orchestration contrast. Baseline: `cite-only`. Evidence: 4. Threat: 4.

### `LIT-060` - An LLM-Tool Compiler for Fused Parallel Function Calling

- Primary: https://arxiv.org/abs/2405.17438
- Verdict: accurate.
- Citation blurb: LLM-Tool Compiler fuses similar tool operations into grouped calls, increasing parallelism and reducing token/latency costs.
- AgentC difference: AgentC must distinguish framework-emitted trace optimization from a tool-fusion compiler.
- Use: must-cite direct ParallelBranch threat. Baseline: `cite-only` unless artifact appears. Evidence: 5. Threat: 4.

### `LIT-061` - Efficient Function Orchestration for Large Language Models / LLMOrch

- Primary: https://ink.library.smu.edu.sg/sis_research/11019/ and https://arxiv.org/abs/2504.14872
- Verdict: needs revision because it has IEEE TSE publication metadata.
- Citation blurb: LLMOrch schedules parallel function calls with def-use dependencies, mutual exclusion, and processor load.
- AgentC difference: AgentC's broader claim requires multiple rewrite types and framework-trace interception.
- Use: important if ParallelBranch is central. Baseline: `cite-only`. Evidence: 4. Threat: 3.

## Serving And Inference Systems

### `LIT-033` - Orca

- Primary: https://www.usenix.org/conference/osdi22/presentation/yu
- Verdict: accurate.
- Citation blurb: Orca anchors serving-layer optimization through iteration-level scheduling and selective batching.
- AgentC difference: AgentC operates above or around the model server by changing semantic application traces.
- Use: serving orthogonality. Baseline: `not-comparable`. Evidence: 4. Threat: 2.

### `LIT-034` - vLLM / PagedAttention

- Primary: https://arxiv.org/abs/2309.06180 and https://docs.vllm.ai/en/v0.9.2/features/automatic_prefix_caching.html
- Verdict: needs revision to split PagedAttention paper from current prefix-cache docs.
- Citation blurb: vLLM/PagedAttention is the central serving baseline for KV-cache memory efficiency; prefix caching is a later documented feature.
- AgentC difference: AgentC reduces or rewrites calls before serving; vLLM makes admitted calls cheaper.
- Use: must-cite orthogonality/composability source. Baseline: `cite-only` unless self-hosted inference is evaluated. Evidence: 4. Threat: 3.

### `LIT-035` - DistServe

- Primary: https://www.usenix.org/conference/osdi24/presentation/zhong-yinmin
- Verdict: accurate.
- Citation blurb: DistServe optimizes TTFT/TPOT goodput by disaggregating prefill and decode.
- AgentC difference: AgentC rewrites application traces, not prefill/decode placement.
- Use: serving related work. Baseline: `not-comparable`. Evidence: 4. Threat: 2.

### `LIT-036` - SGLang

- Primary: https://proceedings.neurips.cc/paper_files/paper/2024/hash/724be4472168f31ba1c9ac630f15dec8-Abstract-Conference.html
- Verdict: accurate.
- Citation blurb: SGLang is a serious nearest neighbor: structured LM-program frontend plus runtime optimizations.
- AgentC difference: AgentC should emphasize transparent optimization of framework-emitted traces instead of requiring SGLang programs.
- Use: must-cite nearest-neighbor systems comparison. Baseline: `run-if-feasible`. Evidence: 5. Threat: 4.

### `LIT-062` - Sarathi-Serve

- Primary: https://www.usenix.org/conference/osdi24/presentation/agrawal
- Verdict: accurate.
- Citation blurb: Sarathi-Serve optimizes serving throughput-latency tradeoffs through chunked prefill and scheduling.
- AgentC difference: Serving systems optimize how admitted requests execute; AgentC optimizes which calls exist and how they are rewritten/reused/reordered.
- Use: serving contrast. Baseline: `not-comparable`. Evidence: 4. Threat: 2.

## Runtime Optimization For LM Workflows

### `LIT-024` - Agentix / Autellix

- Primary: https://www.usenix.org/conference/nsdi26/presentation/luo
- Verdict: needs revision: final title is "Agentix: An Efficient Serving Engine for LLM Agents as General Programs"; formerly Autellix.
- Citation blurb: Agentix is a direct serving-layer threat: it intercepts calls from agentic programs and schedules them using program-level context.
- AgentC difference: AgentC must emphasize application-level semantic/cost rewrites above vendor APIs, not only serving scheduling.
- Use: must-cite nearest neighbor. Baseline: `cite-only`. Evidence: 5. Threat: 5.

### `LIT-025` - Halo

- Primary: https://arxiv.org/abs/2509.02121
- Verdict: accurate, with current v2 details needed.
- Citation blurb: Halo treats batches of agentic workflows as query-plan DAGs and optimizes shared computation, cache reuse, and GPU placement.
- AgentC difference: AgentC is transparent SDK/API interception with token-cost rewrite rules, not local multi-GPU batch query processing.
- Use: must-cite closest systems table. Baseline: `cite-only` unless code appears. Evidence: 5. Threat: 5.

### `LIT-040` - Towards Resource-Efficient Compound AI Systems / Murakkab

- Primary: https://doi.org/10.1145/3713082.3730377 and https://arxiv.org/abs/2501.16634
- Verdict: accurate, but Murakkab is the prototype name, not the official title.
- Citation blurb: Murakkab is a close systems threat for broad resource-efficient compound-AI runtime claims.
- AgentC difference: AgentC should emphasize online trace-time rewrites and concrete optimization passes over existing framework calls.
- Use: must-cite nearest neighbor. Baseline: `cite-only`. Evidence: 5. Threat: 5.

### `LIT-041` - LMQL

- Primary: https://doi.org/10.1145/3591300
- Verdict: accurate.
- Citation blurb: LMQL already frames prompting as programming with an optimizing runtime.
- AgentC difference: AgentC avoids requiring applications to be rewritten in a new LM language.
- Use: must-cite for compiler/runtime framing. Baseline: `cite-only`. Evidence: 5. Threat: 4.

### `LIT-042` - LangGraph Graph API

- Primary: https://docs.langchain.com/oss/python/langgraph/graph-api
- Verdict: accurate.
- Citation blurb: LangGraph exposes graph, state, edge, super-step, and parallel-node semantics for agent workflows.
- AgentC difference: AgentC should optimize traces emitted by graph frameworks, not claim graph orchestration novelty.
- Use: framework semantics and integration target. Baseline: `run`. Evidence: 4. Threat: 3.

### `LIT-043` - AIOS

- Primary: https://openreview.net/forum?id=L4HHkCDz2x and https://arxiv.org/abs/2403.16971
- Verdict: needs metadata and baseline-decision revision.
- Citation blurb: AIOS is a close agent-runtime/OS threat with scheduling, context, memory, storage, tool, and access-control services.
- AgentC difference: AgentC is narrower: a trace rewrite/control plane, not a full agent operating system.
- Use: must-cite nearest neighbor for agent-runtime framing. Baseline: `cite-only`. Evidence: 5. Threat: 5.

### `LIT-044` - Cognify

- Primary: https://arxiv.org/abs/2502.08056 and https://github.com/GenseeAI/cognify
- Verdict: needs revision: final title is "Cognify: Supercharging Gen-AI Workflows With Hierarchical Autotuning."
- Citation blurb: Cognify autotunes Gen-AI workflows across structure, operator/model choice, and prompts for quality/cost/latency tradeoffs.
- AgentC difference: AgentC should distinguish runtime interception and trace rewrites from training-data/evaluator-driven autotuning.
- Use: must-cite nearest neighbor; possible runnable comparison. Baseline: `run-if-compatible`. Evidence: 5. Threat: 5.

### `LIT-045` - TextGrad

- Primary: https://www.nature.com/articles/s41586-025-08661-4 and https://github.com/zou-group/textgrad
- Verdict: needs published-title revision; Nature title is "Optimizing generative AI by backpropagating language model feedback."
- Citation blurb: TextGrad optimizes generative/compound AI systems by backpropagating natural-language feedback.
- AgentC difference: AgentC is rule/runtime rewriting, not iterative textual-gradient optimization.
- Use: related-work context, not direct baseline. Baseline: `cite-only`. Evidence: 4. Threat: 3.

## State, Memory, And Compiler Analogies

### `LIT-037` - A Program Data Flow Analysis Procedure

- Primary: https://doi.org/10.1145/360018.360025 and https://amturing.acm.org/p137-allen.pdf
- Verdict: accurate.
- Citation blurb: Data-flow analysis gives StateDrop a defensible liveness/def-use analogy.
- AgentC difference: AgentC operates on runtime agent traces, not statically compiled programs.
- Use: StateDrop theory support with caveat. Baseline: `not-comparable`. Evidence: 4. Threat: 1.

### `LIT-038` - Program Slicing

- Primary: https://dl.acm.org/doi/10.5555/800078.802557
- Verdict: accurate.
- Citation blurb: Program slicing grounds StateDrop as dependency-aware removal relative to a slicing criterion.
- AgentC difference: AgentC performs trace-state pruning with limited metadata, not whole-program static slicing.
- Use: must-cite if StateDrop is framed as principled dependency removal. Baseline: `not-comparable`. Evidence: 4. Threat: 2.

### `LIT-051` - Program Dependence Graph

- Primary: https://doi.org/10.1145/24039.24041
- Verdict: accurate.
- Citation blurb: PDGs justify data/control dependency language for StateDrop.
- AgentC difference: AgentC needs a trace dependency model or should keep the analogy limited.
- Use: must-cite if using dependency/slicing language. Baseline: `not-comparable`. Evidence: 5. Threat: 1.

### `LIT-052` - SSA And Control Dependence Graph

- Primary: https://doi.org/10.1145/115372.115320
- Verdict: accurate.
- Citation blurb: SSA/control dependence supports precise def-use framing.
- AgentC difference: No SSA form exists for agent traces unless AgentC builds an IR with read/write/use edges.
- Use: optional after PDG/slicing. Baseline: `not-comparable`. Evidence: 4. Threat: 1.

### `LIT-053` - A Survey of Program Slicing Techniques

- Primary: https://dblp.org/rec/journals/jpl/Tip95
- Verdict: needs revision to prefer the 1995 journal article over the 1994 tech report.
- Citation blurb: Slicing is a family of relevance analyses, not one algorithm.
- AgentC difference: StateDrop is closer to conservative runtime trace pruning than full program slicing.
- Use: background only. Baseline: `not-comparable`. Evidence: 3. Threat: 1.

### `LIT-054` - MemGPT

- Primary: https://arxiv.org/abs/2310.08560
- Verdict: needs revision: arXiv/system artifact, not clearly archival.
- Citation blurb: MemGPT makes OS-style memory/context management a real LLM-agent prior.
- AgentC difference: AgentC applies rewrite passes over calls/traces; MemGPT is a memory-management agent architecture.
- Use: adjacent memory/runtime framing. Baseline: `cite-only`. Evidence: 4. Threat: 3.

## Evaluation Methodology

### `LIT-026` - HELM

- Primary: https://openreview.net/forum?id=iO4LZibEqW
- Verdict: accurate.
- Citation blurb: HELM anchors standardized multi-scenario, multi-metric evaluation instead of one-number reporting.
- AgentC difference: AgentC should analogously report cost, latency, quality, and uncertainty.
- Use: evaluation-method framing. Baseline: `not-comparable`. Evidence: 4. Threat: 1.

### `LIT-027` - MT-Bench And Chatbot Arena

- Primary: https://arxiv.org/abs/2306.05685
- Verdict: accurate.
- Citation blurb: LLM-as-judge can scale preference evaluation, but position, verbosity, self-enhancement, and reasoning biases must be controlled.
- AgentC difference: Use this to qualify any judge-based quality metric; it is not an optimizer source.
- Use: evaluation caveat. Baseline: `not-comparable`. Evidence: 4. Threat: 1.

### `LIT-028` - Length-Controlled AlpacaEval

- Primary: https://arxiv.org/abs/2404.04475
- Verdict: accurate.
- Citation blurb: Length-controlled AlpacaEval is the right citation for reducing verbosity/length bias in automatic preference evaluation.
- AgentC difference: AgentC may change output length, so preference scores need length controls.
- Use: evaluation caveat. Baseline: `not-comparable`. Evidence: 4. Threat: 1.

### `LIT-029` - AgentBench

- Primary: https://arxiv.org/abs/2308.03688
- Verdict: accurate.
- Citation blurb: AgentBench is a standard multi-environment benchmark for evaluating LLMs as agents.
- AgentC difference: AgentBench is workload context; AgentC would need optimizer-on/off instrumentation around an agent run.
- Use: benchmark context/future workload. Baseline: `unknown-as-workload`. Evidence: 4. Threat: 2.

### `LIT-030` - SWE-bench

- Primary: https://proceedings.iclr.cc/paper_files/paper/2024/hash/edac78c3e300629acfe6cbe9ca88fb84-Abstract-Conference.html
- Verdict: accurate.
- Citation blurb: SWE-bench evaluates systems on real GitHub issue resolution with test-based scoring.
- AgentC difference: It is a possible coding-agent workload, not a targeted rewrite benchmark.
- Use: real-world benchmark/future-work anchor. Baseline: `cite-only`. Evidence: 4. Threat: 2.

### `LIT-031` - tau-bench

- Primary: https://proceedings.iclr.cc/paper_files/paper/2025/hash/1b126cc38b8638e07bef37e7b2bb72bf-Abstract-Conference.html
- Verdict: accurate.
- Citation blurb: tau-bench directly supports repeated-run reliability evaluation for tool-using agents via pass^k.
- AgentC difference: AgentC changes stochastic execution traces, so one trial per task is weak.
- Use: must-cite repeated-run reliability source. Baseline: `not-comparable`. Evidence: 5. Threat: 2.

### `LIT-032` - ReliableEval

- Primary: https://aclanthology.org/2025.findings-emnlp.594/
- Verdict: accurate.
- Citation blurb: ReliableEval formalizes stochastic evaluation under meaning-preserving perturbations and estimates resampling needs.
- AgentC difference: It informs AgentC's uncertainty claims; it is not an optimizer.
- Use: must-cite statistical/evaluation caution. Baseline: `not-comparable`. Evidence: 5. Threat: 1.

### `LIT-063` - Evaluating Large Language Models Trained on Code

- Primary: https://arxiv.org/abs/2107.03374
- Verdict: accurate, but source type is arXiv/technical report.
- Citation blurb: This is the canonical HumanEval/pass@k source for stochastic generation.
- AgentC difference: AgentC needs paired before/after trials, cost, latency, and quality preservation, not just sample success.
- Use: evaluation-method background. Baseline: `not-comparable`. Evidence: 5. Threat: 1.

### `LIT-064` - Large Language Models are not Fair Evaluators

- Primary: https://aclanthology.org/2024.acl-long.511/
- Verdict: accurate.
- Citation blurb: LLM judges have positional bias; response order can flip judgments.
- AgentC difference: This tells us how not to overtrust judge scores.
- Use: must-cite if using LLM-as-judge. Baseline: `not-comparable`. Evidence: 5. Threat: 1.

### `LIT-065` - Humans or LLMs as the Judge?

- Primary: https://aclanthology.org/2024.emnlp-main.474/
- Verdict: accurate.
- Citation blurb: Human and LLM judges both show bias under perturbations.
- AgentC difference: AgentC should not lean on judge scores without bias controls.
- Use: must-cite if using LLM judges. Baseline: `not-comparable`. Evidence: 4. Threat: 1.

### `LIT-066` - JudgeBench

- Primary: https://openreview.net/forum?id=G0dksFayVq and https://github.com/ScalerLab/JudgeBench
- Verdict: accurate; fill in ICLR 2025.
- Citation blurb: JudgeBench tests whether LLM judges identify objectively correct answers on hard response pairs.
- AgentC difference: It validates judge reliability, not optimizer quality directly.
- Use: optional judge-validation tool. Baseline: `run-if-judge-validation-needed`. Evidence: 4. Threat: 1.

### `LIT-067` - The Leaderboard Illusion

- Primary: https://openreview.net/forum?id=4Ae8edNqm0
- Verdict: accurate.
- Citation blurb: Leaderboards can be distorted by selective disclosure, private testing, and uneven data access.
- AgentC difference: Supports transparent reporting rather than cherry-picked benchmark claims.
- Use: optional evaluation caveat. Baseline: `not-comparable`. Evidence: 3. Threat: 1.

### `LIT-068` - Don't Pass@k

- Primary: https://openreview.net/forum?id=PTXi3Ef4sT and https://github.com/mohsenhariri/scorio
- Verdict: accurate.
- Citation blurb: Pass@k can produce unstable rankings; Bayesian posterior estimates and intervals make stochastic evaluation more reliable.
- AgentC difference: Useful for repeated-run quality/cost comparison statistics.
- Use: strong if using pass@k or uncertainty intervals. Baseline: `run-as-analysis-tool`. Evidence: 5. Threat: 1.

### `LIT-069` - Is One Run Enough?

- Primary: https://academic.oup.com/jamia/advance-article/doi/10.1093/jamia/ocag039/8559659
- Verdict: needs revision.
- Citation blurb: For one constrained binary biomedical classification task, one run may be adequate for highly reproducible models; replication remains useful as a stability check.
- AgentC difference: Multi-step agent traces are likely more stochastic than constrained binary classification.
- Use: nuance, not anti-single-run backbone. Baseline: `not-comparable`. Evidence: 3. Threat: 1.

### `LIT-070` - SWE-rebench

- Primary: https://arxiv.org/abs/2505.20411 and https://swe-rebench.com/about
- Verdict: needs revision to cite the paper plus official page, not only the about page.
- Citation blurb: SWE-rebench is a fresh/decontaminated software-agent benchmark whose docs warn about high single-run variance.
- AgentC difference: It is a coding-agent benchmark/future workload, not a general AgentC runtime baseline.
- Use: evaluation caveat and possible future workload. Baseline: `cite-only`. Evidence: 4. Threat: 1.
