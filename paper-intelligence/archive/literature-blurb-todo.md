---
title: Literature Blurb Todo
status: active
last-updated: 2026-05-09
owner: paper-intelligence
---

# Literature Blurb Todo

This file is the normalized candidate blurb pass over the current literature candidates. It uses the scoring protocol from `specs/paper-intelligence-consolidation.md`.

Important caveat: this file is now superseded by `literature-verified-blurbs.md` for citation-use summaries, differentiation notes, baseline decisions, and corrected source metadata. Keep this file as the original todo/checklist view; do not treat its candidate wording as the latest source of truth.

`LIT-001` is discarded and intentionally skipped. This pass covers `LIT-002` through `LIT-070`.

## Scoring Key

- `Evidence`: 1 weak/background, 3 useful support, 5 central anchor or direct baseline.
- `Threat`: 1 no novelty threat, 3 overlaps one rewrite family, 5 direct nearest-neighbor threat.
- `Baseline`: `run`, `cite-only`, `not-comparable`, or `unknown`.
- `Priority`: rough triage score from the consolidation spec; higher means verify sooner.

## High-Level Literature Blurb

The current literature supports framing AgentC as a runtime optimizer for compound AI systems rather than as a routing, compression, caching, or serving paper. Prior work already covers many individual mechanisms: model routing and cascades, prompt/context compression, semantic caching, parallel tool execution, serving-layer scheduling, and stochastic LLM evaluation. AgentC should not claim those tricks are new.

The plausible contribution is integration and control level: AgentC observes multi-step agent traces emitted by existing frameworks and applies several rewrite classes under one runtime control plane. The most dangerous novelty threats are systems and framework-level optimizers such as Murakkab, AIOS, Cognify, DSPy, LMQL, SGLang, Agentix/Autellix, Halo, LLMCompiler, LLM-Tool Compiler, and the routing/compression/cache baselines. The biggest paper gaps are StateDrop's liveness/program-analysis grounding, runnable baseline decisions, CacheHit/ParallelBranch correctness, and evaluation credibility under stochastic LLM behavior.

## Cluster Synthesis

| Cluster | What literature establishes | What it does not cover | Safe AgentC claim | Must avoid | Must-cite / verify first |
|---|---|---|---|---|---|
| Compound AI systems and frameworks | Modern LLM apps are multi-call systems with tools, control flow, and state. | They usually define frameworks or programs, not transparent runtime rewrites below them. | AgentC targets traces from compound AI systems. | AgentC invented agent traces. | `LIT-002`, `LIT-003`, `LIT-004`, `LIT-005`, `LIT-006` |
| Runtime optimization | Cost/latency/quality over LM programs is a legitimate optimization target. | Most work is narrower, offline, framework-specific, or serving-side. | AgentC is a runtime control plane over agent traces. | First universal optimizer for all agents. | `LIT-006`, `LIT-007`, `LIT-008`, `LIT-023`, `LIT-024`, `LIT-025` |
| Routing/model selection | Cheaper models can safely handle some calls under policies/budgets. | Routing alone does not cover compression, state dropping, caching, or branch parallelism. | ModelDowngrade is a routing-style pass inside a broader runtime. | ModelDowngrade is novel routing. | `LIT-007`, `LIT-008`, `LIT-009`, `LIT-011`, `LIT-012` |
| Compression/context pruning | Prompts often contain removable redundancy. | Most compression work is text/document/prompt-level, not trace/message-state-level. | ContextCompress is runtime message-trace compression. | AgentC invented prompt compression. | `LIT-013`, `LIT-014`, `LIT-015`, `LIT-016` |
| State/liveness | Compiler ideas justify removing unused data under a dependency model. | LLM literature weakly supports AgentC's exact StateDrop idea. | StateDrop is best framed with liveness/slicing analogies. | StateDrop is semantics-preserving for arbitrary traces. | `LIT-015`, `LIT-037`, `LIT-038`, `LIT-039` |
| Caching/memoization | Semantic reuse can save latency/cost but correctness is context-sensitive. | Most cache work is single-query, RAG, or serving-cache oriented. | CacheHit needs call-site/state-aware keys and conservative invalidation. | CacheHit is safe by semantic similarity alone. | `LIT-017`, `LIT-018`, `LIT-019`, `LIT-020`, `LIT-039` |
| Parallel execution | Agent/tool traces contain latent parallelism. | Prior work often assumes explicit plans or graphs. | ParallelBranch is adjacent to planner/compiler work and needs dependency proof. | Arbitrary sibling calls are safe to parallelize. | `LIT-021`, `LIT-022`, `LIT-023` |
| Serving/inference | Serving systems optimize model execution internals. | They generally do not decide which semantic calls should exist above the API boundary. | AgentC is orthogonal to serving systems. | Serving systems are competitors in exactly the same layer. | `LIT-020`, `LIT-033`, `LIT-034`, `LIT-035`, `LIT-036` |
| Stochastic evaluation | LLM evaluation needs repeated trials, uncertainty, judge-bias controls, and reliability metrics. | These works are not optimizer systems. | AgentC should report bounded quality and uncertainty, not semantic equivalence. | Single-run success proves behavior preservation. | `LIT-026`, `LIT-027`, `LIT-028`, `LIT-031`, `LIT-032` |

## Source Todo Items

### Compound AI Systems And Agent Frameworks

- [x] `LIT-002` - The Shift from Models to Compound AI Systems
  - Status: `candidate`
  - Cluster: compound systems
  - One-line takeaway: The paper can frame AgentC around optimizing systems of model calls, tools, and control logic rather than isolated prompts.
  - What it offers: High-level vocabulary for compound AI systems.
  - How AgentC compares: AgentC operates inside this compound-system frame as a runtime optimizer for emitted traces.
  - Reviewer risk: If the source is blog/essay-style, it may be weaker than archival anchors for foundational claims.
  - How we use it: Must-cite framing if primary source is acceptable.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 2; Priority: 6.
  - Action: verify exact source type and pull the safest wording.

- [x] `LIT-003` - Are More LLM Calls All You Need? Towards the Scaling Properties of Compound AI Systems
  - Status: `candidate`
  - Cluster: compound systems
  - One-line takeaway: More calls/modules can improve capability, which makes call-trace cost and latency worth optimizing.
  - What it offers: Evidence that multi-call systems are a real scaling pattern.
  - How AgentC compares: AgentC does not add calls for capability; it tries to make existing or repeated call traces cheaper.
  - Reviewer risk: Could pull the paper toward capability scaling rather than runtime optimization.
  - How we use it: Supporting citation for why multi-call traces matter.
  - Baseline: `not-comparable`; Evidence: 3; Threat: 2; Priority: 5.
  - Action: verify claims and decide whether it belongs in intro or related work.

- [x] `LIT-004` - ReAct
  - Status: `candidate`
  - Cluster: agent frameworks
  - One-line takeaway: ReAct anchors the idea of interleaved reasoning and acting traces.
  - What it offers: Canonical reasoning-plus-tool-action pattern.
  - How AgentC compares: ReAct creates the kind of multi-step traces AgentC could optimize, but is not itself an optimizer.
  - Reviewer risk: Reviewers may expect evidence on ReAct-style workloads if the paper claims agent-general behavior.
  - How we use it: Background anchor for agent traces.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 2; Priority: 6.
  - Action: verify citation metadata and use sparingly as background.

- [x] `LIT-005` - AutoGen
  - Status: `candidate`
  - Cluster: agent frameworks
  - One-line takeaway: AutoGen makes multi-agent orchestration a mainstream framework comparison.
  - What it offers: Framework evidence for multi-agent, multi-call workflows.
  - How AgentC compares: AgentC should be framed as sitting below or beside such frameworks, not replacing them.
  - Reviewer risk: "Why is this not a feature in AutoGen or a framework-level scheduler?"
  - How we use it: Background and integration-scope comparison.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 3; Priority: 7.
  - Action: verify whether AgentC can claim framework transparency relative to AutoGen.

- [x] `LIT-006` - DSPy
  - Status: `candidate`
  - Cluster: runtime optimization / LM programs
  - One-line takeaway: DSPy is a major comparison because it already treats LM applications as optimizable programs.
  - What it offers: Compiler/programming model and optimization framing for LM pipelines.
  - How AgentC compares: DSPy optimizes authored LM programs; AgentC aims at runtime interception and trace rewrites from existing agent code.
  - Reviewer risk: "Is AgentC just a weaker DSPy-style optimizer?"
  - How we use it: Must-cite nearest-neighbor framing.
  - Baseline: `unknown`; Evidence: 5; Threat: 4; Priority: 12.
  - Action: verify exact optimization scope and decide if a DSPy comparison is conceptual or runnable.

### Routing, Cascades, And Model Selection

- [x] `LIT-007` - FrugalGPT
  - Status: `candidate`
  - Cluster: routing
  - One-line takeaway: FrugalGPT is the obvious prior art for reducing API cost through cascades and cheaper models.
  - What it offers: Cost-quality tradeoff methods for model selection/cascading.
  - How AgentC compares: ModelDowngrade is similar in spirit, but AgentC applies it to internal call sites as one pass in a broader trace optimizer.
  - Reviewer risk: "ModelDowngrade is just FrugalGPT for agents."
  - How we use it: Must-cite for ModelDowngrade.
  - Baseline: `unknown`; Evidence: 5; Threat: 4; Priority: 12.
  - Action: verify whether a runnable cascade baseline is practical for the current workloads.

- [x] `LIT-008` - Optimizing Model Selection for Compound AI Systems
  - Status: `candidate`
  - Cluster: routing / compound systems
  - One-line takeaway: This is probably the closest model-selection source because it works at component level inside compound systems.
  - What it offers: Per-component model choice for compound AI applications.
  - How AgentC compares: AgentC includes model choice but also compresses context, drops state, caches, and parallelizes under one runtime control plane.
  - Reviewer risk: It may narrow the novelty of AgentC's model-selection story substantially.
  - How we use it: Must-cite nearest-neighbor for ModelDowngrade and compound-system optimization.
  - Baseline: `unknown`; Evidence: 5; Threat: 5; Priority: 14.
  - Action: verify details early and add to nearest-neighbor comparison.

- [x] `LIT-009` - RouteLLM
  - Status: `candidate`
  - Cluster: routing
  - One-line takeaway: RouteLLM is a modern routing baseline for deciding when to use cheaper versus stronger models.
  - What it offers: Learned or preference-based LLM routing.
  - How AgentC compares: AgentC should distinguish internal call-site routing from query-level routing.
  - Reviewer risk: "Why not just use RouteLLM?"
  - How we use it: Direct ModelDowngrade comparator.
  - Baseline: `run`; Evidence: 5; Threat: 4; Priority: 13.
  - Action: assess setup cost and whether current `gaia_router` can support a fair RouteLLM-style comparison.

- [x] `LIT-010` - RouterBench
  - Status: `candidate`
  - Cluster: routing / evaluation
  - One-line takeaway: RouterBench can anchor what routing benchmarks measure and what AgentC does not currently cover.
  - What it offers: Evaluation benchmark for router quality/cost tradeoffs.
  - How AgentC compares: AgentC's routing pass is trace-level and embedded in a multi-rule optimizer; RouterBench is likely query/router-centered.
  - Reviewer risk: Reviewers may ask why routing results are not benchmarked against router benchmarks.
  - How we use it: Optional benchmark/evaluation citation.
  - Baseline: `cite-only`; Evidence: 4; Threat: 3; Priority: 7.
  - Action: verify whether RouterBench is relevant enough to mention or run.

- [x] `LIT-011` - Language Model Cascades
  - Status: `candidate`
  - Cluster: routing / cascades
  - One-line takeaway: Cascades support risk-aware deferral from cheap to expensive models.
  - What it offers: Theoretical and empirical framing for uncertainty and fallback.
  - How AgentC compares: AgentC can use cascade logic to explain downgrade guardrails, but it is not only a cascade.
  - Reviewer risk: "Where is AgentC's calibration or fallback evidence?"
  - How we use it: Must-cite for quality-risk and fallback wording.
  - Baseline: `cite-only`; Evidence: 4; Threat: 3; Priority: 9.
  - Action: verify exact claims about uncertainty/deferral and add to evaluation gap.

- [x] `LIT-012` - A Unified Approach to Routing and Cascading for LLMs
  - Status: `candidate`
  - Cluster: routing / cascades
  - One-line takeaway: Routing and fallback can be treated together, which matters if AgentC claims a risk-budgeted downgrade policy.
  - What it offers: Unified framing for model choice and escalation.
  - How AgentC compares: AgentC's downgrade policy needs to be explained as a runtime pass with explicit safety budget, not generic routing.
  - Reviewer risk: Weakens any claim that combining routing/fallback is novel.
  - How we use it: Related-work support for ModelDowngrade limitations.
  - Baseline: `unknown`; Evidence: 4; Threat: 3; Priority: 10.
  - Action: verify primary source and decide if it belongs in nearest-neighbor table.

### Compression And Context Pruning

- [x] `LIT-013` - LLMLingua
  - Status: `candidate`
  - Cluster: compression
  - One-line takeaway: LLMLingua is the canonical prompt-compression comparator.
  - What it offers: Token-level prompt compression with quality retention claims.
  - How AgentC compares: ContextCompress rewrites runtime message traces using conservative conditions; LLMLingua compresses prompt text more directly.
  - Reviewer risk: "Why not just run LLMLingua?"
  - How we use it: Must-cite and likely runnable baseline.
  - Baseline: `run`; Evidence: 5; Threat: 4; Priority: 13.
  - Action: evaluate setup cost for a ContextCompress baseline comparison.

- [x] `LIT-014` - LongLLMLingua
  - Status: `candidate`
  - Cluster: compression
  - One-line takeaway: LongLLMLingua is especially relevant for long-context AgentC workloads.
  - What it offers: Long-context compression and information-density framing.
  - How AgentC compares: AgentC should show why runtime-aware message dropping is different from generic long-context compression.
  - Reviewer risk: "ContextCompress is just a weaker long-context compressor."
  - How we use it: Must-cite compression baseline.
  - Baseline: `run`; Evidence: 4; Threat: 3; Priority: 11.
  - Action: decide whether to run it on `long_context_qa` or use as cite-only if infeasible.

- [x] `LIT-015` - Compressing Context to Enhance Inference Efficiency of Large Language Models / Selective Context
  - Status: `candidate`
  - Cluster: compression / state pruning
  - One-line takeaway: Selective context supports the general idea that context can be pruned, and indirectly supports StateDrop.
  - What it offers: Context-pruning anchor before/alongside newer prompt compressors.
  - How AgentC compares: AgentC uses runtime metadata and state-read windows rather than only text salience.
  - Reviewer risk: StateDrop may look like context pruning unless the liveness story is clear.
  - How we use it: Must-cite for context pruning; bridge citation for StateDrop.
  - Baseline: `cite-only`; Evidence: 4; Threat: 3; Priority: 11.
  - Action: verify source and map which parts support ContextCompress versus StateDrop.

- [x] `LIT-016` - RECOMP
  - Status: `candidate`
  - Cluster: compression / retrieval
  - One-line takeaway: RECOMP is relevant if AgentC discusses document/RAG-style compression.
  - What it offers: Compression in retrieval-augmented settings.
  - How AgentC compares: AgentC currently targets agent prompt/message traces, not primarily retrieved-document compression.
  - Reviewer risk: Low unless the paper leans into RAG workloads.
  - How we use it: Optional background for compression breadth.
  - Baseline: `cite-only`; Evidence: 3; Threat: 2; Priority: 5.
  - Action: verify and keep optional unless new RAG experiments appear.

### Caching, Memoization, And Reuse

- [x] `LIT-017` - GPTCache
  - Status: `candidate`
  - Cluster: caching
  - One-line takeaway: GPTCache is the obvious semantic-cache comparator for CacheHit.
  - What it offers: Application-level caching of LLM responses using similarity.
  - How AgentC compares: AgentC's cache needs call-site and state-aware correctness, not just prompt similarity.
  - Reviewer risk: "CacheHit is just GPTCache."
  - How we use it: Must-cite for CacheHit, but not headline unless CacheHit results exist.
  - Baseline: `unknown`; Evidence: 4; Threat: 3; Priority: 8.
  - Action: verify and keep CacheHit scoped unless experiments are added.

- [x] `LIT-018` - MeanCache
  - Status: `candidate`
  - Cluster: caching
  - One-line takeaway: MeanCache may help explain context-aware semantic cache correctness.
  - What it offers: More nuanced semantic cache matching than simple embedding reuse.
  - How AgentC compares: AgentC should include runtime context and call-site state in any cache-key story.
  - Reviewer risk: Cache correctness is harder than the current headline paper can carry.
  - How we use it: Citation for cache correctness and false-hit risk.
  - Baseline: `cite-only`; Evidence: 4; Threat: 3; Priority: 8.
  - Action: verify source and extract correctness conditions.

- [x] `LIT-019` - ContextCache
  - Status: `candidate`
  - Cluster: caching
  - One-line takeaway: Multi-turn context changes whether a cached answer is valid.
  - What it offers: Cache correctness in context-sensitive settings.
  - How AgentC compares: Agent traces are context-sensitive by construction, so AgentC must avoid shallow cache-hit claims.
  - Reviewer risk: "How do you avoid stale or context-wrong cache hits?"
  - How we use it: Must-cite if CacheHit appears beyond future work.
  - Baseline: `cite-only`; Evidence: 4; Threat: 3; Priority: 8.
  - Action: verify and add to CacheHit guardrail notes.

- [x] `LIT-020` - Prompt Cache
  - Status: `candidate`
  - Cluster: caching / serving
  - One-line takeaway: Prefix/KV-style caching is related reuse, but at a different layer from AgentC.
  - What it offers: Attention/prefix reuse below the application semantic layer.
  - How AgentC compares: AgentC can save or rewrite calls even when model-server caching is unavailable or orthogonal.
  - Reviewer risk: "Would serving/cache systems make AgentC unnecessary?"
  - How we use it: Orthogonality contrast for CacheHit and serving systems.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 3; Priority: 9.
  - Action: verify and use in serving-orthogonality section.

### Parallel Tool Execution And Workflow Scheduling

- [x] `LIT-021` - An LLM Compiler for Parallel Function Calling
  - Status: `candidate`
  - Cluster: parallelism
  - One-line takeaway: LLMCompiler is the direct comparison for parallel tool/function calls.
  - What it offers: Planning or compiling function calls into parallel execution structures.
  - How AgentC compares: AgentC wants transparent runtime detection over traces; LLMCompiler likely changes planning/program structure.
  - Reviewer risk: "ParallelBranch is just LLMCompiler."
  - How we use it: Must-cite for ParallelBranch, and nearest-neighbor if ParallelBranch is discussed.
  - Baseline: `cite-only`; Evidence: 5; Threat: 4; Priority: 11.
  - Action: verify exact mechanism and keep ParallelBranch non-headline unless results exist.

- [x] `LIT-022` - ReWOO
  - Status: `candidate`
  - Cluster: parallelism / execution pattern
  - One-line takeaway: ReWOO shows that reorganizing reasoning and observation can save tokens.
  - What it offers: An execution-pattern rewrite for tool-using reasoning.
  - How AgentC compares: ReWOO changes prompting/program structure; AgentC aims to optimize already-emitted traces.
  - Reviewer risk: Adjacent work may make AgentC look less novel if the trace-level distinction is blurry.
  - How we use it: Supporting comparison for execution rewrite ideas.
  - Baseline: `cite-only`; Evidence: 4; Threat: 3; Priority: 7.
  - Action: verify and decide if it belongs in nearest-neighbor table.

- [x] `LIT-023` - ALTO
  - Status: `candidate`
  - Cluster: runtime optimization / parallelism / serving
  - One-line takeaway: ALTO is a systems comparison for compound pipeline orchestration.
  - What it offers: Runtime/orchestration optimization in compound pipelines.
  - How AgentC compares: AgentC focuses on application-level semantic rewrites over agent call traces.
  - Reviewer risk: "Is AgentC subsumed by pipeline/orchestration systems?"
  - How we use it: Systems-side comparison and possible nearest neighbor.
  - Baseline: `cite-only`; Evidence: 4; Threat: 4; Priority: 10.
  - Action: verify details and decide whether it is a core novelty threat.

### Closest Systems Threats

- [x] `LIT-024` - Agentix / Autellix
  - Status: `candidate`
  - Cluster: runtime optimization / systems threat
  - One-line takeaway: Agentix, formerly Autellix, is one of the most dangerous serving-layer novelty threats.
  - What it offers: Interception and scheduling for agentic programs with program-level context.
  - How AgentC compares: AgentC must distinguish semantic rewrite classes above the API/server layer, not only scheduling.
  - Reviewer risk: "Agentix already does runtime optimization for agentic programs."
  - How we use it: Must-cite nearest neighbor; final source of truth is `literature-verified-blurbs.md`.
  - Baseline: `cite-only`; Evidence: 5; Threat: 5; Priority: 12.
  - Action: use verified `LIT-024` notes to narrow novelty wording around application-level semantic rewrites.

- [x] `LIT-025` - Halo
  - Status: `candidate`
  - Cluster: runtime optimization / systems threat
  - One-line takeaway: Halo is a close workflow/DAG/query-plan optimizer threat.
  - What it offers: Workflow-level optimization for agent or compound-AI execution.
  - How AgentC compares: AgentC should emphasize online framework interception and multiple rewrite classes on traces.
  - Reviewer risk: "Halo already frames agent workflows as query plans."
  - How we use it: Must-cite nearest neighbor; final source of truth is `literature-verified-blurbs.md`.
  - Baseline: `cite-only`; Evidence: 5; Threat: 5; Priority: 12.
  - Action: read primary source and add a crisp comparison table row.

### Evaluation Methodology

- [x] `LIT-026` - HELM
  - Status: `candidate`
  - Cluster: evaluation
  - One-line takeaway: HELM supports broad multi-metric evaluation rather than one-number reporting.
  - What it offers: Evaluation methodology and metric discipline.
  - How AgentC compares: AgentC is not a benchmark suite, but needs HELM-style multi-metric reporting: cost, latency, quality, and uncertainty.
  - Reviewer risk: Makes thin evaluation look weak.
  - How we use it: Evaluation-method citation.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 1; Priority: 7.
  - Action: verify exact language and use to justify evaluation dimensions.

- [x] `LIT-027` - Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena
  - Status: `candidate`
  - Cluster: evaluation
  - One-line takeaway: If AgentC uses LLM judges, judge bias must be acknowledged.
  - What it offers: Evidence on LLM-as-judge strengths and biases.
  - How AgentC compares: AgentC's quality-preservation claims need care if judged outputs are involved.
  - Reviewer risk: "Your judge-based evaluation is biased or undercontrolled."
  - How we use it: Evaluation caveat citation.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 1; Priority: 7.
  - Action: verify and connect to any future judge-based metrics.

- [x] `LIT-028` - Length-Controlled AlpacaEval
  - Status: `candidate`
  - Cluster: evaluation
  - One-line takeaway: Verbosity can bias automated preference evaluation.
  - What it offers: Control for length/verbosity bias in LLM evaluation.
  - How AgentC compares: AgentC changes prompts and may change output length, so judge/preference metrics need length controls.
  - Reviewer risk: "Savings or quality deltas are output-length artifacts."
  - How we use it: Evaluation caution, especially for output-token stochasticity.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 1; Priority: 7.
  - Action: verify and cross-link to statistical plan.

- [x] `LIT-029` - AgentBench
  - Status: `candidate`
  - Cluster: evaluation / benchmarks
  - One-line takeaway: AgentBench is a possible anchor for agentic benchmark expectations.
  - What it offers: Multi-turn agent benchmark framing.
  - How AgentC compares: AgentC optimizes agent runtime traces; AgentBench could provide task realism but may not match rewrite triggers.
  - Reviewer risk: "Why only purpose-built workloads instead of standard agent benchmarks?"
  - How we use it: Optional benchmark context.
  - Baseline: `unknown`; Evidence: 4; Threat: 2; Priority: 7.
  - Action: inspect feasibility and whether AgentBench traces expose AgentC rules.

- [x] `LIT-030` - SWE-bench
  - Status: `candidate`
  - Cluster: evaluation / benchmarks
  - One-line takeaway: SWE-bench is a serious real-world task benchmark for coding agents.
  - What it offers: Realistic end-to-end software task evaluation.
  - How AgentC compares: AgentC could use coding-agent traces to show broader applicability, but SWE-bench is expensive and not targeted to all rewrite rules.
  - Reviewer risk: "Where is a real-world agent benchmark?"
  - How we use it: Aspirational benchmark or future-work anchor.
  - Baseline: `unknown`; Evidence: 4; Threat: 2; Priority: 7.
  - Action: classify as future benchmark unless tokens/time support it.

- [x] `LIT-031` - tau-bench
  - Status: `candidate`
  - Cluster: evaluation / reliability
  - One-line takeaway: tau-bench supports repeated-run reliability metrics such as pass^k.
  - What it offers: Reliability framing for stochastic agents.
  - How AgentC compares: AgentC changes stochastic execution, so one run per task is not enough.
  - Reviewer risk: "Single-run evaluation is underpowered."
  - How we use it: Must-cite for repeated-run or reliability framing.
  - Baseline: `not-comparable`; Evidence: 5; Threat: 2; Priority: 9.
  - Action: verify pass^k language and connect to `GAP-014`.

- [x] `LIT-032` - ReliableEval
  - Status: `candidate`
  - Cluster: evaluation / reliability
  - One-line takeaway: ReliableEval supports explicit uncertainty and prompt-sensitivity reporting.
  - What it offers: Stochastic evaluation cautions and methods.
  - How AgentC compares: AgentC's preservation claims should be bounded by metrics, uncertainty, and repeated runs.
  - Reviewer risk: "Your results are prompt/noise artifacts."
  - How we use it: Must-cite for behavior-preservation caution.
  - Baseline: `not-comparable`; Evidence: 5; Threat: 2; Priority: 9.
  - Action: verify and use to shape final statistical-analysis plan.

### Serving And Inference Systems

- [x] `LIT-033` - Orca
  - Status: `candidate`
  - Cluster: serving
  - One-line takeaway: Orca anchors efficient LLM serving as a separate systems layer.
  - What it offers: Serving-side scheduling/throughput optimization.
  - How AgentC compares: AgentC operates above the server/API layer on application traces.
  - Reviewer risk: "Why is this not just serving optimization?"
  - How we use it: Orthogonality citation.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 2; Priority: 6.
  - Action: verify and use in serving contrast.

- [x] `LIT-034` - vLLM
  - Status: `candidate`
  - Cluster: serving
  - One-line takeaway: vLLM is the practical serving baseline everyone knows.
  - What it offers: Paged attention, prefix caching, and efficient serving/runtime infrastructure.
  - How AgentC compares: AgentC can reduce/rewrite calls regardless of whether serving is optimized underneath.
  - Reviewer risk: "Would vLLM/prefix caching erase AgentC's gains?"
  - How we use it: Must-cite practical orthogonality source.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 3; Priority: 7.
  - Action: verify docs/paper and explain composability.

- [x] `LIT-035` - DistServe
  - Status: `candidate`
  - Cluster: serving
  - One-line takeaway: DistServe strengthens the serving-system contrast around phase-aware scheduling.
  - What it offers: Latency/throughput optimization inside serving infrastructure.
  - How AgentC compares: AgentC rewrites application-level traces rather than model-server scheduling phases.
  - Reviewer risk: Low-to-medium; mostly reinforces the orthogonality story.
  - How we use it: Serving related work.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 2; Priority: 6.
  - Action: verify and decide whether to cite or omit for space.

- [x] `LIT-036` - SGLang
  - Status: `candidate`
  - Cluster: serving / LM programs
  - One-line takeaway: SGLang is a serious systems comparison because it optimizes structured language programs.
  - What it offers: Runtime and language support for structured LLM programs with execution optimizations.
  - How AgentC compares: AgentC targets framework-emitted agent traces and semantic rewrite classes above/around the API layer.
  - Reviewer risk: "SGLang already optimizes LLM programs at runtime."
  - How we use it: Must-cite systems comparison.
  - Baseline: `unknown`; Evidence: 5; Threat: 4; Priority: 11.
  - Action: verify exact layer and add to nearest-neighbor comparison.

### Program Analysis And Compiler Analogies

- [x] `LIT-037` - A Program Data Flow Analysis Procedure
  - Status: `candidate`
  - Cluster: state/liveness
  - One-line takeaway: Data-flow analysis gives StateDrop a principled analogy.
  - What it offers: Classic foundation for reasoning about what program values are needed.
  - How AgentC compares: AgentC is not compiling normal programs, but StateDrop can borrow the liveness/dependency vocabulary.
  - Reviewer risk: Overclaiming formal compiler equivalence would be weak.
  - How we use it: Background/theory support for StateDrop.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 1; Priority: 7.
  - Action: verify classic citation and use carefully as analogy.

- [x] `LIT-038` - Program Slicing
  - Status: `candidate`
  - Cluster: state/liveness
  - One-line takeaway: Program slicing is a strong analogy for dropping state irrelevant to the current computation.
  - What it offers: Conceptual support for dependency-aware removal.
  - How AgentC compares: StateDrop can be framed as trace-state slicing under limited runtime metadata, not full semantic slicing.
  - Reviewer risk: "Do you actually prove slice equivalence?"
  - How we use it: Must-cite if StateDrop is positioned as principled.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 2; Priority: 8.
  - Action: verify and add caveat that AgentC uses conservative runtime heuristics, not full program slicing.

- [x] `LIT-039` - Compile-time Function Memoization
  - Status: `candidate`
  - Cluster: caching / compiler analogy
  - One-line takeaway: Memoization gives a classic systems analogy for CacheHit and repeated-call reuse.
  - What it offers: Prior framing for avoiding recomputation when inputs are equivalent.
  - How AgentC compares: AgentC's equivalence problem is harder because LLM calls are stochastic and context-sensitive.
  - Reviewer risk: Low as a novelty threat, useful as grounding.
  - How we use it: Background for caching/memoization language.
  - Baseline: `not-comparable`; Evidence: 3; Threat: 1; Priority: 6.
  - Action: verify and decide if classical memoization belongs in main text or appendix.

## DRP-004 Addendum Source Todo Items

These rows were added from `DRP-004`, the second full-paper literature map. They are superseded by `literature-verified-blurbs.md`; keep this section as the original checklist only.

### Compound Runtime And LM Program Threats

- [x] `LIT-040` - Towards Resource-Efficient Compound AI Systems / Murakkab
  - Takeaway: Major threat to broad runtime-optimizer novelty claims.
  - Offers: Declarative compound workflows plus adaptive runtime/resource scheduling.
  - AgentC comparison: AgentC should emphasize online trace-time rewrites and concrete multi-rule passes rather than general workflow/runtime co-design.
  - Risk/use: Must-cite nearest neighbor; can force novelty narrowing.
  - Baseline: `cite-only`; Evidence: 5; Threat: 5; Priority: 14.
  - Action: verify first batch and add to nearest-neighbor table.

- [x] `LIT-041` - LMQL
  - Takeaway: LLM applications have already been framed as programs with optimizing runtimes.
  - Offers: Query/programming language for LLM calls and runtime optimization.
  - AgentC comparison: AgentC is not a new LM language; it intercepts calls from existing frameworks.
  - Risk/use: Must-cite for compiler/runtime framing.
  - Baseline: `cite-only`; Evidence: 5; Threat: 4; Priority: 11.
  - Action: verify venue/source metadata and use to constrain compiler-language claims.

- [x] `LIT-042` - LangGraph Graph API
  - Takeaway: Production agent frameworks already expose graph execution and parallel-node semantics.
  - Offers: Official framework semantics for graph/super-step execution.
  - AgentC comparison: AgentC can optimize traces emitted by graph frameworks rather than replacing their orchestration model.
  - Risk/use: Important if claiming framework integration or ParallelBranch relevance.
  - Baseline: `run`; Evidence: 4; Threat: 3; Priority: 10.
  - Action: verify docs and decide whether a LangGraph integration/eval is realistic.

- [x] `LIT-043` - AIOS
  - Takeaway: Agent runtimes have already been described as OS-like systems with scheduling, memory, and context management.
  - Offers: Agent operating-system abstraction.
  - AgentC comparison: AgentC is narrower: trace rewrite/control plane, not a full agent OS.
  - Risk/use: Must-cite nearest neighbor.
  - Baseline: `unknown`; Evidence: 5; Threat: 5; Priority: 13.
  - Action: verify and add to closest-work table.

- [x] `LIT-044` - Cognify
  - Takeaway: Multi-objective workflow optimization across quality/latency/cost is a close threat.
  - Offers: Workflow-level optimization for generative-AI applications.
  - AgentC comparison: AgentC must distinguish online call interception and concrete rewrite classes.
  - Risk/use: Must-cite if claims mention workflow optimization.
  - Baseline: `unknown`; Evidence: 5; Threat: 5; Priority: 14.
  - Action: verify artifact availability and compare against AgentC's control-plane claim.

- [x] `LIT-045` - TextGrad
  - Takeaway: Compound AI systems can be optimized through graph-like feedback procedures.
  - Offers: Textual gradient/feedback optimization for AI systems.
  - AgentC comparison: AgentC is rule/runtime based rather than learned textual optimization.
  - Risk/use: Optional but strategically useful for optimization framing.
  - Baseline: `cite-only`; Evidence: 4; Threat: 3; Priority: 7.
  - Action: verify and keep as optional runtime-optimization context.

### Additional Routing And Compression Sources

- [x] `LIT-046` - Large Language Model Routing with Benchmark Datasets
  - Takeaway: Router evaluation has more benchmark support than our current ModelDowngrade section reflects.
  - Offers: Routing datasets/evaluation setup.
  - AgentC comparison: AgentC routes internal call sites, but reviewers may expect router-benchmark awareness.
  - Risk/use: Useful for baseline/evaluation expectations.
  - Baseline: `unknown`; Evidence: 4; Threat: 3; Priority: 10.
  - Action: verify and classify runnable-vs-cite-only.

- [x] `LIT-047` - LLMLingua-2
  - Takeaway: Compression baselines are stronger than just LLMLingua/LongLLMLingua.
  - Offers: Faster, faithful task-agnostic compression via distillation/classification.
  - AgentC comparison: ContextCompress must justify runtime trace awareness, not generic compression novelty.
  - Risk/use: Must-cite or runnable compression baseline.
  - Baseline: `run`; Evidence: 5; Threat: 4; Priority: 13.
  - Action: assess setup cost for `long_context_qa`.

- [x] `LIT-048` - Concise and Precise Context Compression for Tool-Using Language Models
  - Takeaway: Tool-use-specific compression is directly relevant to agent traces.
  - Offers: Compression that preserves tool names, parameters, and API details.
  - AgentC comparison: AgentC should show it preserves tool-use-critical context or avoids unsafe compression.
  - Risk/use: Must-cite for ContextCompress in agents.
  - Baseline: `unknown`; Evidence: 5; Threat: 4; Priority: 13.
  - Action: verify and consider as a more relevant baseline than generic prompt compressors.

- [x] `LIT-049` - TACO-RL
  - Takeaway: Task-aware compression raises the bar for simple heuristic pruning.
  - Offers: RL-based task-aware prompt compression.
  - AgentC comparison: AgentC is simpler/conservative; may win on runtime integration and low overhead, not compression optimality.
  - Risk/use: Optional but useful for "compression is established" point.
  - Baseline: `cite-only`; Evidence: 3; Threat: 3; Priority: 6.
  - Action: verify and probably keep optional.

- [x] `LIT-050` - Prompt Compression for Large Language Models: A Survey
  - Takeaway: Good taxonomy source; weak as novelty evidence.
  - Offers: Compression literature overview.
  - AgentC comparison: Helps place ContextCompress among known compressor types.
  - Risk/use: Background only.
  - Baseline: `not-comparable`; Evidence: 3; Threat: 1; Priority: 4.
  - Action: verify if a survey is useful for final related-work efficiency.

### StateDrop And Compiler/Memory Analogies

- [x] `LIT-051` - Program Dependence Graph
  - Takeaway: Stronger StateDrop analogy than generic context pruning.
  - Offers: Data/control dependency representation for optimization.
  - AgentC comparison: StateDrop can be framed as limited trace-state dependency pruning.
  - Risk/use: Must-cite if using dependency language.
  - Baseline: `not-comparable`; Evidence: 5; Threat: 1; Priority: 8.
  - Action: verify classic citation and add to StateDrop grounding.

- [x] `LIT-052` - SSA and Control Dependence Graph
  - Takeaway: Supports precise def-use/control-dependence language.
  - Offers: Compiler foundation for dependency computation.
  - AgentC comparison: Useful analogy, but AgentC does not prove full SSA-style semantics.
  - Risk/use: Optional support for StateDrop formality.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 1; Priority: 7.
  - Action: verify and cite only if needed.

- [x] `LIT-053` - Survey of Program Slicing Techniques
  - Takeaway: Shows slicing/liveness is a broad family, not one fragile citation.
  - Offers: Survey background.
  - AgentC comparison: Helps avoid overfitting StateDrop to one compiler paper.
  - Risk/use: Background.
  - Baseline: `not-comparable`; Evidence: 3; Threat: 1; Priority: 6.
  - Action: verify and use if StateDrop section needs broader grounding.

- [x] `LIT-054` - MemGPT
  - Takeaway: LLM context/memory management already has an OS-style framing.
  - Offers: Memory hierarchy / OS metaphor for LLM agents.
  - AgentC comparison: StateDrop is a specific rewrite over stale state, not a whole memory system.
  - Risk/use: Optional but useful adjacent systems framing.
  - Baseline: `cite-only`; Evidence: 4; Threat: 3; Priority: 8.
  - Action: verify and decide if it belongs in StateDrop or runtime framing.

### Additional Cache Correctness Sources

- [x] `LIT-055` - vCache
  - Takeaway: Correctness-aware caching is now a direct bar for CacheHit.
  - Offers: Verified semantic prompt caching and error guarantees.
  - AgentC comparison: AgentC must explain cache keys, thresholds, and false-hit policy.
  - Risk/use: Must-cite if CacheHit is in the paper.
  - Baseline: `run`; Evidence: 5; Threat: 4; Priority: 13.
  - Action: verify and decide whether CacheHit stays future work or needs vCache comparison.

- [x] `LIT-056` - Semantic Caching for Low-Cost LLM Serving
  - Takeaway: Cache adaptation/eviction is part of modern semantic-cache evaluation.
  - Offers: Offline/online cache adaptation under cost/query uncertainty.
  - AgentC comparison: AgentC cache policy would need similar operational details if claimed.
  - Risk/use: Optional but useful for cache policy caveats.
  - Baseline: `cite-only`; Evidence: 4; Threat: 3; Priority: 8.
  - Action: verify and add to CacheHit correctness notes.

- [x] `LIT-057` - Semantic Caching and Query Processing
  - Takeaway: Classical semantic caching predates LLMs and gives a systems foundation.
  - Offers: Query-processing view of semantic reuse.
  - AgentC comparison: Useful for principled reuse language, not direct LLM baseline.
  - Risk/use: Background.
  - Baseline: `not-comparable`; Evidence: 3; Threat: 1; Priority: 4.
  - Action: verify if classical cache lineage is included.

- [x] `LIT-058` - Semantic Caching via Query Matching for Web Sources
  - Takeaway: Another classical query-matching anchor for cache correctness.
  - Offers: Semantic cache matching/containment concepts.
  - AgentC comparison: AgentC's trace-state matching is harder than web-query matching.
  - Risk/use: Background.
  - Baseline: `not-comparable`; Evidence: 3; Threat: 1; Priority: 4.
  - Action: verify and likely cite only in extended related work.

- [x] `LIT-059` - Self-Adjusting Computation
  - Takeaway: Change propagation/memoization gives a more principled reuse analogy.
  - Offers: Semantics for recomputation avoidance under changes.
  - AgentC comparison: CacheHit faces stochastic and semantic equivalence issues beyond deterministic self-adjusting computation.
  - Risk/use: Optional conceptual support.
  - Baseline: `not-comparable`; Evidence: 3; Threat: 1; Priority: 6.
  - Action: verify and use only if writing a deeper systems analogy.

### Parallel Execution Additions

- [x] `LIT-060` - LLM-Tool Compiler
  - Takeaway: Direct threat for fused parallel function calling.
  - Offers: Tool-call fusion/parallelization using compiler-inspired methods.
  - AgentC comparison: AgentC must distinguish framework-agnostic runtime interception.
  - Risk/use: Must-cite for ParallelBranch.
  - Baseline: `unknown`; Evidence: 5; Threat: 4; Priority: 13.
  - Action: verify and add to nearest-neighbor table.

- [x] `LIT-061` - LLMOrch / Efficient Function Orchestration
  - Takeaway: Function orchestration is an active related-work line.
  - Offers: Automated orchestration/parallelism for LLM functions.
  - AgentC comparison: Adjacent to ParallelBranch, likely narrower than full AgentC.
  - Risk/use: Optional/direct if ParallelBranch becomes central.
  - Baseline: `cite-only`; Evidence: 4; Threat: 3; Priority: 7.
  - Action: verify and decide if it is close enough for nearest-neighbor table.

### Serving And Evaluation Additions

- [x] `LIT-062` - Sarathi-Serve
  - Takeaway: Strengthens serving-layer orthogonality discussion.
  - Offers: Throughput-latency scheduling for LLM inference.
  - AgentC comparison: Serving optimization is below AgentC's application-trace rewrite layer.
  - Risk/use: Serving contrast citation.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 2; Priority: 6.
  - Action: verify and cite only if serving section has room.

- [x] `LIT-063` - Evaluating LLMs Trained on Code
  - Takeaway: Canonical source for pass@k under stochastic generation.
  - Offers: Evaluation metric foundation.
  - AgentC comparison: AgentC can borrow repeated-sampling logic for stochastic optimizer evaluation.
  - Risk/use: Must-cite if using pass@k/pass^k language.
  - Baseline: `not-comparable`; Evidence: 5; Threat: 1; Priority: 8.
  - Action: verify and add to statistical plan.

- [x] `LIT-064` - LLMs are not Fair Evaluators
  - Takeaway: LLM judges have bias problems.
  - Offers: Evidence for evaluation caveats.
  - AgentC comparison: Any judge-based quality check must mitigate or acknowledge bias.
  - Risk/use: Must-cite if using LLM judge.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 1; Priority: 7.
  - Action: verify and link to `GAP-014`.

- [x] `LIT-065` - Humans or LLMs as the Judge?
  - Takeaway: Adds another judge-bias source.
  - Offers: Bias study for human/LLM judgement comparison.
  - AgentC comparison: Reinforces need for robust quality evaluation.
  - Risk/use: Evaluation caveat.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 1; Priority: 7.
  - Action: verify and keep if judge section needs more support.

- [x] `LIT-066` - JudgeBench
  - Takeaway: Judge models themselves need evaluation.
  - Offers: Benchmark for LLM-based judges.
  - AgentC comparison: Relevant if AgentC uses judge models for subtle quality drift.
  - Risk/use: Optional but useful if judge evaluation is added.
  - Baseline: `unknown`; Evidence: 4; Threat: 1; Priority: 8.
  - Action: verify and decide if runnable.

- [x] `LIT-067` - The Leaderboard Illusion
  - Takeaway: Leaderboard/aggregate claims can be misleading.
  - Offers: Evaluation credibility warning.
  - AgentC comparison: Supports avoiding overbroad aggregate claims from small benchmark sets.
  - Risk/use: Optional evaluation caveat.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 1; Priority: 7.
  - Action: verify and use if paper discusses benchmark limitations.

- [x] `LIT-068` - Don't Pass@k
  - Takeaway: Even pass@k needs careful statistical treatment.
  - Offers: Bayesian/uncertainty framing for LLM evaluation.
  - AgentC comparison: AgentC should report uncertainty around cost-quality changes, not only point estimates.
  - Risk/use: Strong optional evaluation-method source.
  - Baseline: `not-comparable`; Evidence: 4; Threat: 1; Priority: 7.
  - Action: verify and consider for statistical-analysis plan.

- [x] `LIT-069` - Is one run enough?
  - Takeaway: Nuance source, not a blanket anti-single-run citation; one constrained binary biomedical task was highly reproducible.
  - Offers: A warning to avoid overclaiming repeated-run requirements across all task types.
  - AgentC comparison: Multi-step agent traces are likely more stochastic than constrained binary classification, so AgentC still needs repeated/paired evaluation.
  - Risk/use: Optional nuance source, not the backbone for repeated-run motivation.
  - Baseline: `not-comparable`; Evidence: 3; Threat: 1; Priority: 5.
  - Action: use `LIT-031`, `LIT-032`, `LIT-064`, `LIT-065`, `LIT-068`, and `LIT-070` as stronger evaluation anchors.

- [x] `LIT-070` - SWE-rebench About page
  - Takeaway: Agent benchmarks can have high run-to-run variance.
  - Offers: Practical benchmark-ecosystem signal.
  - AgentC comparison: Supports repeated-run evaluation for agentic workloads.
  - Risk/use: Optional but high-value official-doc support.
  - Baseline: `not-comparable`; Evidence: 3; Threat: 1; Priority: 6.
  - Action: verify official page and decide if docs citation is acceptable.

## Immediate Verification Queue

1. `LIT-040`, `LIT-043`, `LIT-044`, `LIT-006`, `LIT-041`, `LIT-036` - closest runtime/LM-program novelty threats.
2. `LIT-008`, `LIT-024`, `LIT-025`, `LIT-021`, `LIT-060` - strongest system/rule-specific nearest neighbors.
3. `LIT-007`, `LIT-009`, `LIT-013`, `LIT-014`, `LIT-047`, `LIT-048` - most likely runnable or expected routing/compression baselines.
4. `LIT-055`, `LIT-019`, `LIT-018`, `LIT-017`, `LIT-020` - CacheHit correctness and serving/cache distinction.
5. `LIT-031`, `LIT-032`, `LIT-063`, `LIT-064`, `LIT-065`, `LIT-069` - stochastic evaluation and judge-bias backbone.
6. `LIT-037`, `LIT-038`, `LIT-051`, `LIT-052`, `LIT-015` - StateDrop grounding.
