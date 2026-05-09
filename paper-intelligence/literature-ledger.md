---
title: Literature Ledger
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Literature Ledger

Literature candidates below were seeded from `DRP-001` and `DRP-004`. Primary-source blurbs and correction notes for `LIT-002` through `LIT-070` now live in `literature-verified-blurbs.md`; this ledger still needs a row-by-row metadata cleanup pass before final BibTeX work.

| ID | Status | Citation Key | Title | Authors/Year | Venue/Source | Link | Source Type | Relevance | Supports/Challenges | Verification |
|---|---|---|---|---|---|---|---|---|---|---|
| `LIT-001` | `discarded` | pending | placeholder removed after first ingestion | pending | pending | pending | pending | no longer used | none | not applicable |
| `LIT-002` | `candidate` | pending | The Shift from Models to Compound AI Systems | pending | pending | TBD | paper/blog | High-level framing: AgentC optimizes compound systems, not isolated prompts. | `CLM-001`, `CIT-001` | verified in `literature-verified-blurbs.md` |
| `LIT-003` | `candidate` | pending | Are More LLM Calls All You Need? Towards the Scaling Properties of Compound AI Systems | pending | pending | TBD | paper | Supports multi-call systems as capability/cost scaling target. | `CLM-001`, `CIT-001` | verified in `literature-verified-blurbs.md` |
| `LIT-004` | `candidate` | pending | ReAct | pending | pending | TBD | paper | Canonical reasoning-plus-acting traces. | `CIT-001` | verified in `literature-verified-blurbs.md` |
| `LIT-005` | `candidate` | pending | AutoGen | pending | pending | TBD | paper/tool | Multi-agent orchestration framework comparison. | `CIT-001`, `RR-001` | verified in `literature-verified-blurbs.md` |
| `LIT-006` | `candidate` | pending | DSPy | pending | pending | TBD | paper/tool | Strong comparison for LM programs and compiler-style optimization. | `CIT-001`, `RR-001` | verified in `literature-verified-blurbs.md` |
| `LIT-007` | `candidate` | pending | FrugalGPT | pending | pending | TBD | paper | Must-cite for API cost reduction and cascades. | `CLM-003`, `CIT-002`, `RR-009` | verified in `literature-verified-blurbs.md` |
| `LIT-008` | `candidate` | pending | Optimizing Model Selection for Compound AI Systems | pending | pending | TBD | paper | Direct prior art for per-component model choice. | `CLM-003`, `CIT-002`, `RR-009` | verified in `literature-verified-blurbs.md` |
| `LIT-009` | `candidate` | pending | RouteLLM | pending | pending | TBD | paper/tool | Modern routing baseline for `ModelDowngrade`. | `CLM-003`, `CIT-002`, `RR-009` | verified in `literature-verified-blurbs.md` |
| `LIT-010` | `candidate` | pending | RouterBench | pending | pending | TBD | benchmark | Benchmark anchor for router evaluation. | `CIT-002`, `GAP-012` | verified in `literature-verified-blurbs.md` |
| `LIT-011` | `candidate` | pending | Language Model Cascades | pending | pending | TBD | paper | Supports uncertainty/deferral framing for downgrade policies. | `CLM-003`, `GAP-014` | verified in `literature-verified-blurbs.md` |
| `LIT-012` | `candidate` | pending | A Unified Approach to Routing and Cascading for LLMs | pending | pending | TBD | paper | Relevant if AgentC claims routing plus fallback. | `CIT-002`, `GAP-012` | verified in `literature-verified-blurbs.md` |
| `LIT-013` | `candidate` | pending | LLMLingua | pending | pending | TBD | paper/tool | Must-cite prompt-compression baseline for `ContextCompress`. | `CLM-002`, `CIT-003`, `RR-010` | verified in `literature-verified-blurbs.md` |
| `LIT-014` | `candidate` | pending | LongLLMLingua | pending | pending | TBD | paper/tool | Long-context compression baseline. | `CLM-002`, `CIT-003`, `RR-010` | verified in `literature-verified-blurbs.md` |
| `LIT-015` | `candidate` | pending | Compressing Context to Enhance Inference Efficiency of Large Language Models / Selective Context | pending | pending | TBD | paper | Context-pruning anchor; also indirect support for `StateDrop`. | `CLM-002`, `CLM-004`, `CIT-003`, `CIT-006` | verified in `literature-verified-blurbs.md` |
| `LIT-016` | `candidate` | pending | RECOMP | pending | pending | TBD | paper | Retrieval/document compression comparison. | `CIT-003` | verified in `literature-verified-blurbs.md` |
| `LIT-017` | `candidate` | pending | GPTCache | pending | pending | TBD | paper/tool | Must-cite semantic-cache baseline for `CacheHit`. | `CIT-004`, `RR-011` | verified in `literature-verified-blurbs.md` |
| `LIT-018` | `candidate` | pending | MeanCache | pending | pending | TBD | paper | Context-aware semantic-cache source. | `CIT-004`, `RR-011` | verified in `literature-verified-blurbs.md` |
| `LIT-019` | `candidate` | pending | ContextCache | pending | pending | TBD | paper | Strong source for multi-turn/context-sensitive cache correctness risk. | `CIT-004`, `RR-011` | verified in `literature-verified-blurbs.md` |
| `LIT-020` | `candidate` | pending | Prompt Cache | pending | pending | TBD | paper/system | Contrast for prefix/KV cache reuse below the application layer. | `CIT-004`, `CIT-008` | verified in `literature-verified-blurbs.md` |
| `LIT-021` | `candidate` | pending | An LLM Compiler for Parallel Function Calling | pending | pending | TBD | paper/system | Closest baseline for `ParallelBranch`. | `CIT-005`, `RR-012` | verified in `literature-verified-blurbs.md` |
| `LIT-022` | `candidate` | pending | ReWOO | pending | pending | TBD | paper | Execution-pattern rewrite and token-efficiency comparison. | `CIT-005` | verified in `literature-verified-blurbs.md` |
| `LIT-023` | `candidate` | pending | ALTO | pending | pending | TBD | paper/system | Compound-pipeline orchestration and streaming comparison. | `CIT-005`, `CIT-008` | verified in `literature-verified-blurbs.md` |
| `LIT-024` | `candidate` | pending | Autellix | pending | pending | TBD | paper/system | Major closest-work threat: intercepts calls from agentic programs and schedules execution. | `GAP-010`, `RR-013` | verified in `literature-verified-blurbs.md` |
| `LIT-025` | `candidate` | pending | Halo | pending | pending | TBD | paper/system | Major closest-work threat: workflow/DAG/query-plan optimization for agents. | `GAP-010`, `RR-013` | verified in `literature-verified-blurbs.md` |
| `LIT-026` | `candidate` | pending | HELM | pending | pending | TBD | benchmark/paper | Multi-metric evaluation anchor. | `GAP-014`, `CIT-007` | verified in `literature-verified-blurbs.md` |
| `LIT-027` | `candidate` | pending | Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena | pending | pending | TBD | paper/benchmark | Judge-bias and evaluation methodology anchor. | `GAP-014`, `CIT-007` | verified in `literature-verified-blurbs.md` |
| `LIT-028` | `candidate` | pending | Length-Controlled AlpacaEval | pending | pending | TBD | paper/benchmark | Verbosity-bias control if any judge scoring is used. | `GAP-014`, `CIT-007` | verified in `literature-verified-blurbs.md` |
| `LIT-029` | `candidate` | pending | AgentBench | pending | pending | TBD | benchmark | Agent benchmark anchor. | `CIT-007` | verified in `literature-verified-blurbs.md` |
| `LIT-030` | `candidate` | pending | SWE-bench | pending | pending | TBD | benchmark | End-to-end real-world agent task benchmark anchor. | `CIT-007` | verified in `literature-verified-blurbs.md` |
| `LIT-031` | `candidate` | pending | tau-bench | pending | pending | TBD | benchmark/paper | Key source for repeated-run reliability/pass^k. | `GAP-014`, `CIT-007` | verified in `literature-verified-blurbs.md` |
| `LIT-032` | `candidate` | pending | ReliableEval | pending | pending | TBD | paper | Prompt sensitivity and uncertainty reporting anchor. | `GAP-014`, `CIT-007` | verified in `literature-verified-blurbs.md` |
| `LIT-033` | `candidate` | pending | Orca | pending | pending | TBD | paper/system | Serving-system contrast. | `CIT-008` | verified in `literature-verified-blurbs.md` |
| `LIT-034` | `candidate` | pending | vLLM | pending | pending | TBD | paper/tool | Serving-layer contrast: paged attention/prefix caching. | `CIT-008` | verified in `literature-verified-blurbs.md` |
| `LIT-035` | `candidate` | pending | DistServe | pending | pending | TBD | paper/system | Serving-layer contrast: phase-aware scheduling. | `CIT-008` | verified in `literature-verified-blurbs.md` |
| `LIT-036` | `candidate` | pending | SGLang | pending | pending | TBD | paper/system | Structured language-program runtime with execution optimizations. | `CIT-008`, `RR-013` | verified in `literature-verified-blurbs.md` |
| `LIT-037` | `candidate` | pending | A Program Data Flow Analysis Procedure | pending | pending | TBD | paper/classic | Compiler anchor for `StateDrop` and liveness framing. | `CIT-006`, `GAP-013` | verified in `literature-verified-blurbs.md` |
| `LIT-038` | `candidate` | pending | Program Slicing | pending | pending | TBD | paper/classic | Compiler anchor for state/dependency reasoning. | `CIT-006`, `GAP-013` | verified in `literature-verified-blurbs.md` |
| `LIT-039` | `candidate` | pending | Compile-time Function Memoization | pending | pending | TBD | paper/classic | Compiler anchor for memoization/reuse framing. | `CIT-004`, `GAP-013` | verified in `literature-verified-blurbs.md` |
| `LIT-040` | `candidate` | pending | Towards Resource-Efficient Compound AI Systems / Murakkab | pending | HotOS / 2025 per `DRP-004` | TBD | paper/system | Major novelty threat for declarative compound workflows plus adaptive runtime scheduling. | `GAP-010`, `RR-013` | verified in `literature-verified-blurbs.md` |
| `LIT-041` | `candidate` | pending | LMQL: Prompting Is Programming: A Query Language for Large Language Models | pending | pending | TBD | paper/system | LM programs plus optimizing runtime; important compiler/runtime analogy. | `CIT-001`, `GAP-010`, `RR-013` | verified in `literature-verified-blurbs.md` |
| `LIT-042` | `candidate` | pending | LangGraph Graph API | pending | official docs | TBD | official-doc | Production graph execution and parallel node semantics for agent frameworks. | `CIT-001`, `CIT-005` | verified in `literature-verified-blurbs.md` |
| `LIT-043` | `candidate` | pending | AIOS: LLM Agent Operating System | pending | pending | TBD | paper/system | Agent OS framing with scheduling, context, memory, and access-control concerns. | `GAP-010`, `RR-013` | verified in `literature-verified-blurbs.md` |
| `LIT-044` | `candidate` | pending | Cognify: Supercharging Gen-AI Workflows With Multi-Objective Optimization | pending | pending | TBD | paper/system | Workflow optimizer over quality, latency, and cost; close novelty threat. | `GAP-010`, `GAP-012`, `RR-013` | verified in `literature-verified-blurbs.md` |
| `LIT-045` | `candidate` | pending | TextGrad: Automatic Differentiation via Text | pending | pending | TBD | paper | Optimizes compound AI systems via natural-language feedback over computation graphs. | `CIT-001`, `GAP-010` | verified in `literature-verified-blurbs.md` |
| `LIT-046` | `candidate` | pending | Large Language Model Routing with Benchmark Datasets | pending | pending | TBD | paper/benchmark | Additional router benchmark/data source. | `CIT-002`, `GAP-012` | verified in `literature-verified-blurbs.md` |
| `LIT-047` | `candidate` | pending | LLMLingua-2: Data Distillation for Efficient and Faithful Task-Agnostic Prompt Compression | pending | Findings of ACL / 2024 per `DRP-004` | TBD | paper/tool | Stronger prompt-compression baseline with faithfulness/latency framing. | `CIT-003`, `RR-010`, `GAP-012` | verified in `literature-verified-blurbs.md` |
| `LIT-048` | `candidate` | pending | Concise and Precise Context Compression for Tool-Using Language Models | pending | pending | TBD | paper | Tool-use context compression; highly relevant to agent/tool traces. | `CIT-003`, `RR-010`, `GAP-012` | verified in `literature-verified-blurbs.md` |
| `LIT-049` | `candidate` | pending | TACO-RL: Task Aware Prompt Compression Optimization with Reinforcement Learning | pending | pending | TBD | paper | Task-aware compression source that raises bar for simple heuristics. | `CIT-003`, `RR-010` | verified in `literature-verified-blurbs.md` |
| `LIT-050` | `candidate` | pending | Prompt Compression for Large Language Models: A Survey | pending | pending | TBD | survey | Compression taxonomy and background. | `CIT-003` | verified in `literature-verified-blurbs.md` |
| `LIT-051` | `candidate` | pending | The Program Dependence Graph and Its Use in Optimization | pending | TOPLAS / 1987 per `DRP-004` | TBD | paper/classic | Data/control dependency foundation for StateDrop framing. | `CIT-006`, `GAP-013` | verified in `literature-verified-blurbs.md` |
| `LIT-052` | `candidate` | pending | Efficiently Computing Static Single Assignment Form and the Control Dependence Graph | pending | TOPLAS / 1991 per `DRP-004` | TBD | paper/classic | Def-use/control-dependence support for liveness framing. | `CIT-006`, `GAP-013` | verified in `literature-verified-blurbs.md` |
| `LIT-053` | `candidate` | pending | A Survey of Program Slicing Techniques | pending | pending | TBD | survey/classic | Background for slicing as a family of dependency analyses. | `CIT-006`, `GAP-013` | verified in `literature-verified-blurbs.md` |
| `LIT-054` | `candidate` | pending | MemGPT: Towards LLMs as Operating Systems | pending | pending | TBD | paper/system | LLM memory/context management as OS-style systems problem. | `CIT-006`, `GAP-013`, `RR-013` | verified in `literature-verified-blurbs.md` |
| `LIT-055` | `candidate` | pending | vCache: Verified Semantic Prompt Caching | pending | ICLR / 2026 per `DRP-004` | TBD | paper/system | Correctness-aware semantic caching with verification guarantees. | `CIT-004`, `RR-011`, `GAP-012` | verified in `literature-verified-blurbs.md` |
| `LIT-056` | `candidate` | pending | Semantic Caching for Low-Cost LLM Serving: From Offline Learning to Online Adaptation | pending | INFOCOM / 2026 per `DRP-004` | TBD | paper/system | Cache eviction/adaptation under unknown costs and distributions. | `CIT-004`, `RR-011` | verified in `literature-verified-blurbs.md` |
| `LIT-057` | `candidate` | pending | Semantic Caching and Query Processing | pending | TKDE / 2003 per `DRP-004` | TBD | paper/classic | Classical semantic-cache systems foundation. | `CIT-004` | verified in `literature-verified-blurbs.md` |
| `LIT-058` | `candidate` | pending | Semantic Caching via Query Matching for Web Sources | pending | CIKM / 1999 per `DRP-004` | TBD | paper/classic | Early semantic-cache/query-matching foundation. | `CIT-004` | verified in `literature-verified-blurbs.md` |
| `LIT-059` | `candidate` | pending | A Consistent Semantics of Self-Adjusting Computation | pending | pending | TBD | paper/classic | Change-propagation/memoization analogy for CacheHit. | `CIT-004`, `GAP-013` | verified in `literature-verified-blurbs.md` |
| `LIT-060` | `candidate` | pending | An LLM-Tool Compiler for Fused Parallel Function Calling | pending | pending | TBD | paper/system | Tool-call fusion and parallelization; direct ParallelBranch comparison. | `CIT-005`, `RR-012`, `GAP-012` | verified in `literature-verified-blurbs.md` |
| `LIT-061` | `candidate` | pending | Efficient Function Orchestration for Large Language Models / LLMOrch | pending | pending | TBD | paper/system | Automated parallel function orchestration for LLMs. | `CIT-005`, `RR-012` | verified in `literature-verified-blurbs.md` |
| `LIT-062` | `candidate` | pending | Sarathi-Serve | pending | OSDI / 2024 per `DRP-004` | TBD | paper/system | Serving-system contrast for throughput-latency scheduling. | `CIT-008`, `RR-013` | verified in `literature-verified-blurbs.md` |
| `LIT-063` | `candidate` | pending | Evaluating Large Language Models Trained on Code | pending | arXiv / 2021 per `DRP-004` | TBD | paper/benchmark | Canonical pass@k source for stochastic generation. | `CIT-007`, `GAP-014` | verified in `literature-verified-blurbs.md` |
| `LIT-064` | `candidate` | pending | Large Language Models are not Fair Evaluators | pending | ACL / 2024 per `DRP-004` | TBD | paper/evaluation | LLM judge bias source. | `CIT-007`, `GAP-014` | verified in `literature-verified-blurbs.md` |
| `LIT-065` | `candidate` | pending | Humans or LLMs as the Judge? A Study on Judgement Bias | pending | EMNLP / 2024 per `DRP-004` | TBD | paper/evaluation | Judge-bias source for LLM-as-judge evaluation. | `CIT-007`, `GAP-014` | verified in `literature-verified-blurbs.md` |
| `LIT-066` | `candidate` | pending | JudgeBench: A Benchmark for Evaluating LLM-based Judges | pending | pending | TBD | paper/benchmark | Benchmark for judge model robustness. | `CIT-007`, `GAP-014` | verified in `literature-verified-blurbs.md` |
| `LIT-067` | `candidate` | pending | The Leaderboard Illusion | pending | NeurIPS Datasets and Benchmarks / 2025 per `DRP-004` | TBD | paper/evaluation | Leaderboard/selective disclosure risk source. | `CIT-007`, `GAP-014` | verified in `literature-verified-blurbs.md` |
| `LIT-068` | `candidate` | pending | Don't Pass@k: A Bayesian Framework for Large Language Model Evaluation | pending | ICLR / 2026 per `DRP-004` | TBD | paper/evaluation | Principled uncertainty framework for stochastic evaluation. | `CIT-007`, `GAP-014` | verified in `literature-verified-blurbs.md` |
| `LIT-069` | `candidate` | pending | Is one run enough? Reproducibility of flagship large language models across temperature and reasoning settings in biomedical text processing | pending | JAMIA / 2026 per `DRP-004` | TBD | paper/evaluation | Direct source against single-run evaluation. | `CIT-007`, `GAP-014` | verified in `literature-verified-blurbs.md` |
| `LIT-070` | `candidate` | pending | SWE-rebench About page | pending | official benchmark page / 2026 per `DRP-004` | TBD | official-doc/benchmark | Agent benchmark signal about high run-to-run variance. | `CIT-007`, `GAP-014` | verified in `literature-verified-blurbs.md` |

## Topic Buckets To Fill

- Agent frameworks and compound AI systems
- Runtime optimization for LLM applications
- Model routing / cascades / model selection
- Prompt and context compression
- Semantic caching and memoization
- KV/prefix caching and serving-layer optimization
- Tool-call scheduling and parallel execution
- Evaluation under stochastic LLM outputs
- ML systems and LLM inference systems
