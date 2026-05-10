---
title: Citation Gap List
status: draft
last-updated: 2026-05-09
owner: paper-intelligence
---

# Citation Gap List

Claims needing citation support live here until resolved through `literature-ingestion-workflow.md`.

| ID | Status | Claim Needing Support | Likely Literature Area | Candidate Sources | Linked Claim/Gap | Next Search |
|---|---|---|---|---|---|---|
| `CIT-001` | `checked-blurb` | Multi-step LLM agents are compound systems whose traces expose systems-level optimization opportunities. | agent frameworks, compound AI systems | `LIT-002`, `LIT-003`, `LIT-004`, `LIT-005`, `LIT-006` | `CLM-001`, `GAP-009` | promote exact wording into related-work brief |
| `CIT-002` | `checked-blurb` | Model routing/cascading is related but does not fully cover transparent runtime optimization. | model routing | `LIT-007`, `LIT-008`, `LIT-009`, `LIT-010`, `LIT-011`, `LIT-012` | `CLM-003`, `GAP-009` | decide runnable versus cite-only baselines |
| `CIT-003` | `checked-blurb` | Context compression and prompt pruning are established but differ from AgentC's runtime message-trace rule. | prompt/context compression | `LIT-013`, `LIT-014`, `LIT-015`, `LIT-016`, `LIT-047`, `LIT-048` | `CLM-002`, `GAP-009` | decide whether direct compression baseline runs are needed |
| `CIT-004` | `checked-blurb` | Semantic caching/memoization is adjacent to CacheHit, but correctness depends on context and invalidation. | semantic cache, LLM cache, classical semantic caching | `LIT-017`, `LIT-018`, `LIT-019`, `LIT-020`, `LIT-039`, `LIT-055` | future CacheHit claim | keep CacheHit caveated until false-hit metrics exist |
| `CIT-005` | `checked-blurb` | Parallel execution of tool/function calls is adjacent to ParallelBranch, but safe discovery needs dependency/side-effect framing. | tool scheduling, function-call parallelism, agent graph execution | `LIT-021`, `LIT-022`, `LIT-023`, `LIT-060`, `LIT-061` | future ParallelBranch claim | keep ParallelBranch caveated until dependency policy exists |
| `CIT-006` | `checked-blurb` | StateDrop is better supported by liveness/data-flow/slicing analogies than by prompt-compression literature alone. | compiler/program analysis, state pruning | `LIT-015`, `LIT-037`, `LIT-038`, `LIT-051`, `LIT-052` | `CLM-004`, `GAP-013` | define AgentC dependency/read-window model |
| `CIT-007` | `checked-blurb` | Behavior-preserving optimization for stochastic LLM agents needs repeated-run reliability, paired uncertainty, and judge-bias controls. | stochastic LLM evaluation, agent benchmarks | `LIT-026`, `LIT-027`, `LIT-028`, `LIT-029`, `LIT-030`, `LIT-031`, `LIT-032`, `LIT-063`, `LIT-064`, `LIT-065`, `LIT-068`, `LIT-070` | `GAP-004`, `GAP-014` | decide required metrics for the target venue |
| `CIT-008` | `checked-blurb` | Serving/inference systems are orthogonal: they optimize how individual calls execute, while AgentC optimizes application-level call semantics. | serving systems, KV/prefix cache, inference scheduling | `LIT-020`, `LIT-033`, `LIT-034`, `LIT-035`, `LIT-036`, `LIT-062` | `GAP-010`, `RR-013` | write orthogonality paragraph |
| `CIT-009` | `checked-blurb` | AgentC's broad runtime novelty must be narrowed against compound-AI workflow runtimes and agent OS systems. | compound systems, LM programs, agent OS/runtime | `LIT-040`, `LIT-041`, `LIT-043`, `LIT-044`, `LIT-006`, `LIT-036` | `GAP-010`, `RR-013` | use verified blurbs to narrow novelty language |
| `CIT-010` | `checked-blurb` | ParallelBranch must be compared against compiler-style parallel function calling and tool-call fusion. | tool-call compiler, function orchestration | `LIT-021`, `LIT-060`, `LIT-061`, `LIT-042`, `LIT-043` | `RR-012`, `GAP-012` | decide whether ParallelBranch remains future work |
| `CIT-011` | `checked-blurb` | CacheHit requires correctness-aware evaluation, not just hit rate. | verified semantic caching, context cache, cache adaptation | `LIT-055`, `LIT-019`, `LIT-018`, `LIT-056`, `LIT-057`, `LIT-058` | `RR-011`, `GAP-012` | decide whether CacheHit remains future work |
| `CIT-012` | `checked-blurb` | StateDrop needs dependency/liveness grounding beyond generic context compression. | program dependence graph, SSA, slicing, memory systems | `LIT-037`, `LIT-038`, `LIT-051`, `LIT-052`, `LIT-053`, `LIT-054` | `CLM-004`, `GAP-013` | avoid overclaiming soundness |
| `CIT-013` | `checked-blurb` | Stochastic and judge-based LLM evaluation needs repeated trials, bias controls, and uncertainty estimates. | pass@k, judge bias, reproducibility, Bayesian evaluation | `LIT-063`, `LIT-064`, `LIT-065`, `LIT-066`, `LIT-067`, `LIT-068`, `LIT-070` | `GAP-014` | update statistical plan; use `LIT-069` only as nuance |
