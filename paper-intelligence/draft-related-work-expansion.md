---
title: Draft — expanded Related Work for the workshop paper
status: draft
last-updated: 2026-05-10
owner: paper-intelligence
---

# Drafted Related Work expansion

This is paper-ready prose drafted from the verified blurbs in
`literature-and-nearest-neighbors.md`. It addresses GAP-007, GAP-010,
RR-005, RR-009, RR-010, RR-013 — the largest single fixable gap in the
current draft. Every cited source has a `LIT-###` blurb upstream; the
required new BibTeX entries are listed at the end.

## Drop-in replacement for the current §7 Related Work

\section{Related Work}
\label{sec:related}

\textbf{Compound AI systems and agent runtimes.} Several recent
systems target runtime efficiency for compound AI workflows.
LMQL~\cite{beurer2023lmql} and DSPy~\cite{khattab2023dspy} expose
LM programs as first-class objects and optimize them through
language-level constructs and offline compilation against a labeled
metric. SGLang~\cite{zheng2024sglang} couples a structured LM-program
frontend with serving-runtime optimizations. AIOS~\cite{ge2024aios}
treats agents as the primary workload of an operating-system-like
scheduler with explicit context, memory, and tool services.
Agentix~\cite{luo2026agentix} (formerly Autellix) intercepts calls
from agentic programs at the serving layer and schedules them using
program-level context. Halo~\cite{abdulhaque2025halo} optimizes
batches of agent workflows as query-plan DAGs across shared
computation, cache reuse, and GPU placement.
Murakkab~\cite{mahesh2025murakkab} adapts resource allocation across
declarative compound-AI workflows. Cognify~\cite{li2025cognify}
hierarchically autotunes Gen-AI workflows offline against an
evaluator, optimizing structure, operator and model choice, and
prompts simultaneously. Agentc occupies a different position in this
design space: it operates above the model server by rewriting
already-emitted application traces, requires no language constructs
or workflow declaration, and applies several conservative rewrite
classes online without a labeled metric.

\textbf{Model routing and cascades.}
FrugalGPT~\cite{chen2023frugalgpt} pioneered cost-aware model
cascades for natural-language tasks.
RouteLLM~\cite{ong2024routellm} learns a preference-trained router
between cheaper and stronger LLMs at query time.
LLMSelector~\cite{chen2025llmselector} extends per-component model
selection to compound AI systems.
RouterBench~\cite{hu2024routerbench} standardizes multi-LLM routing
evaluation. A unified routing-and-cascading
formulation~\cite{lee2025routingcascade} models quality and cost
estimators jointly. Agentc's ModelDowngrade rule covers a strict
subset of this space: it routes hot internal call sites in a single
trace to a configured cheaper model under an accuracy budget, and is
one pass among several inside a unified runtime control plane.

\textbf{Prompt and context compression.}
LLMLingua~\cite{jiang2023llmlingua},
LongLLMLingua~\cite{jiang2024longllmlingua}, and
LLMLingua-2~\cite{pan2024llmlingua2} compress prompts via proxy LLMs
or distilled token classifiers. Selective
Context~\cite{li2023selective} prunes low-information segments at
generation time. RECOMP~\cite{xu2024recomp} compresses retrieved
contexts for retrieval-augmented generation. Tool-aware
compression~\cite{xu2024toolcompress} preserves tool documentation
fields under compression. Agentc's ContextCompress targets the same
goal but trades compression precision for zero auxiliary-LLM
overhead: it computes an IDF-weighted online proxy from the prompt
itself and applies extractive deletion under per-role retention
floors and provenance constraints. Where LLMLingua-style methods
operate on a single prompt, Agentc rewrites runtime message traces
with knowledge of the surrounding agent context and call-site
profile.

\textbf{Semantic caching and memoization.}
GPTCache~\cite{bang2023gptcache} popularized semantic LLM caching
via embedding-similarity lookup. ContextCache~\cite{yan2025contextcache}
showed that multi-turn caches need explicit context modeling to avoid
spurious hits on superficially similar queries.
MeanCache~\cite{gill2025meancache} reduces false hits via
user/context-aware keys. vCache~\cite{schnitzler2026vcache} sets the
modern correctness bar with user-specified error bounds and
adaptive thresholds, measuring false-hit rates explicitly. Agentc's
CacheHit rule occupies the same niche but, in this work, is
characterized mechanistically rather than benchmarked end-to-end:
its correctness story (call-site, state, and provenance-aware
keying) is described in §4 and left to future evaluation.

\textbf{Parallel tool-call scheduling.}
LLMCompiler~\cite{kim2024llmcompiler} plans function-call DAGs and
executes independent calls in parallel. The LLM-Tool
Compiler~\cite{singh2024llmtoolcompiler} fuses similar tool
operations to increase parallelism. ReWOO~\cite{xu2023rewoo}
separates planning, tool execution, and solving to reduce repeated
reasoning loops. LLMOrch~\cite{wang2025llmorch} schedules parallel
function calls under explicit def-use dependencies. Agentc's
ParallelBranch is closer to a runtime trace-rewrite pass than to a
planner or compiler: it identifies disjoint sibling calls in
already-emitted traces using DepSource annotations. The current
synchronous executor degrades this plan to pass-through; an
asynchronous executor backend is left to future work (§\ref{sec:futurework}).

\textbf{State and program analysis.}
StateDrop is grounded in classical liveness and slicing analysis:
data-flow analysis~\cite{allen1976dataflow}, program
slicing~\cite{weiser1981slicing}, the program dependence
graph~\cite{ferrante1987pdg}, SSA~\cite{cytron1991ssa}, and Tip's
slicing survey~\cite{tip1995survey}. We do not claim sound program
slicing: Agentc operates on runtime agent traces with limited
metadata, not on whole programs with full IR. The connection is
analogical and bounded: a state-tagged message with key $k$ is
removable iff $k$ is absent from the per-call \texttt{window\_state\_reads}
set populated by \texttt{agentc.state\_read}. This is conservative
runtime pruning informed by liveness, not dependency-graph slicing.
MemGPT~\cite{packer2023memgpt} addresses a related concern from a
different angle, providing OS-style memory management to LLM agents
as an architectural pattern rather than a transparent rewrite pass.

\textbf{Serving and inference systems.}
Orca~\cite{yu2022orca} introduced iteration-level scheduling and
selective batching at the serving layer. vLLM with
PagedAttention~\cite{kwon2023vllm} optimized KV-cache memory
efficiency. DistServe~\cite{zhong2024distserve} and
Sarathi-Serve~\cite{agrawal2024sarathiserve} optimize serving
goodput through prefill/decode disaggregation and chunked
scheduling. Prompt Cache~\cite{gim2024promptcache} reuses
precomputed attention states for declared prompt modules. These
serving systems optimize \emph{how admitted requests execute}.
Agentc operates at the orthogonal layer above the model server,
optimizing \emph{which calls exist and how they are constructed}.
The two compose: Agentc reduces or rewrites application-level
calls, then the serving layer makes the surviving calls cheaper.

\textbf{Stochastic evaluation methodology.}
Pass@k~\cite{chen2021humaneval} and the surrounding
sampling-based evaluation literature established that LM behavior
must be evaluated across multiple trials. tau-bench~\cite{yao2025taubench}
uses pass\textsuperscript{k} to evaluate tool-using agent
reliability. ReliableEval~\cite{rotem2025reliableeval} formalizes
stochastic evaluation under meaning-preserving perturbations and
estimates resampling needs. ``Don't Pass\textsuperscript{k}''~\cite{hariri2025passk}
shows that pass\textsuperscript{k} can produce unstable rankings
and offers Bayesian alternatives. SWE-rebench~\cite{badertdinov2025swerebench}
documents high single-run variance on coding agents. Agentc's
shared-baseline ablation (§\ref{sec:methodology}) and input-token
attribution signal partially address this evaluation challenge by
localizing comparison to deterministic prompt-driven quantities. We
report binomial standard errors on accuracy and discuss paired
analysis as a natural extension in §\ref{sec:limitations}.

## New BibTeX entries needed (annotated)

These must be added to `references.bib`. Sourced from the verified
blurbs in `literature-and-nearest-neighbors.md`; double-check final
metadata against primary sources before submission.

| Cite key | Closest LIT id | Use |
|---|---|---|
| `beurer2023lmql` | LIT-041 | Compound AI / LM languages |
| `khattab2023dspy` | LIT-006 | LM compilation |
| `zheng2024sglang` | LIT-036 | Structured LM-program serving runtime |
| `ge2024aios` | LIT-043 | Agent OS |
| `luo2026agentix` | LIT-024 | Closest serving threat |
| `abdulhaque2025halo` | LIT-025 | Batch agent-workflow query optimizer |
| `mahesh2025murakkab` | LIT-040 | Compound-AI runtime resource efficiency |
| `li2025cognify` | LIT-044 | Hierarchical workflow autotuner |
| `chen2023frugalgpt` | LIT-007 | Cost-saving cascades |
| `ong2024routellm` | LIT-009 | Modern preference-trained router |
| `chen2025llmselector` | LIT-008 | Compound-AI model selection |
| `hu2024routerbench` | LIT-010 | Routing benchmark |
| `lee2025routingcascade` | LIT-012 | Unified routing/cascading |
| `jiang2023llmlingua` | LIT-013 | Already cited |
| `jiang2024longllmlingua` | LIT-014 | Long-context compression |
| `pan2024llmlingua2` | LIT-047 | Distilled compression |
| `li2023selective` | LIT-015 | Selective Context |
| `xu2024recomp` | LIT-016 | RAG compression |
| `xu2024toolcompress` | LIT-048 | Tool-use compression |
| `bang2023gptcache` | LIT-017 | Already cited |
| `yan2025contextcache` | LIT-019 | Multi-turn semantic cache |
| `gill2025meancache` | LIT-018 | Context-aware semantic cache |
| `schnitzler2026vcache` | LIT-055 | Verified semantic caching |
| `kim2024llmcompiler` | LIT-021 | Parallel function calling |
| `singh2024llmtoolcompiler` | LIT-060 | Tool-fusion parallel calling |
| `xu2023rewoo` | LIT-022 | Plan/exec/solve separation |
| `wang2025llmorch` | LIT-061 | Function orchestration with deps |
| `allen1976dataflow` | LIT-037 | Data-flow analysis classic |
| `weiser1981slicing` | LIT-038 | Program slicing classic |
| `ferrante1987pdg` | LIT-051 | PDG |
| `cytron1991ssa` | LIT-052 | SSA |
| `tip1995survey` | LIT-053 | Slicing survey |
| `packer2023memgpt` | LIT-054 | Already cited |
| `yu2022orca` | LIT-033 | Iteration-level scheduling |
| `kwon2023vllm` | LIT-034 | Already cited |
| `zhong2024distserve` | LIT-035 | Disaggregated serving |
| `agrawal2024sarathiserve` | LIT-062 | Chunked-prefill scheduling |
| `gim2024promptcache` | LIT-020 | Attention reuse for prompt modules |
| `chen2021humaneval` | LIT-063 | Pass@k origin |
| `yao2025taubench` | LIT-031 | Pass^k for tool agents |
| `rotem2025reliableeval` | LIT-032 | Stochastic-eval formalism |
| `hariri2025passk` | LIT-068 | Pass@k critique |
| `badertdinov2025swerebench` | LIT-070 | High-variance coding agents |

## Other small paper edits this draft assumes

These are mentioned in the prose above; apply alongside the section
swap.

1. **Add to §3.2 (Two-Gate Rule Pipeline)**: after the safety-check
   paragraph, append: ``This separation between proposal and
   safety check is intentional: rules can claim arbitrary projected
   savings, but provenance invariants (UserInput preservation,
   per-role retention floors) are enforced by the planner, not by
   the rule.''

2. **Add to §4 (StateDrop block)**: end with ``StateDrop's correctness
   rests on a runtime read-window model, not sound program slicing.
   A State-tagged message with key $k$ is removable iff $k$ is absent
   from \texttt{window\_state\_reads} for the current call. This is
   conservative runtime pruning informed by liveness analysis~\cite{allen1976dataflow,
   weiser1981slicing,tip1995survey}, not dependency-graph slicing
   over a static program.''

3. **Drop the word ``novel'' twice**: in the §1 contribution bullet
   list and earlier mentions of the two-gate design — replace with
   ``two-stage''.

4. **Soften title** (optional): consider ``Toward Just-in-Time
   Optimization for Multi-Step LLM Agent Workloads''.

5. **Add overhead paragraph to §6 (Evaluation)**: see
   `bench/paper_results/optimizer_overhead.txt` for the verified
   median 76 µs / p99 21 ms numbers and a paper-ready sentence.
