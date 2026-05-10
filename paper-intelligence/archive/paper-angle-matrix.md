---
title: Paper Angle Matrix
status: active
last-updated: 2026-05-08
owner: paper-intelligence
---

# Paper Angle Matrix

This file compares possible AgentC paper narratives. It is a decision aid, not a commitment.

## Candidate Angles

| ID | Angle | Central claim | Strongest current evidence | Weakest current evidence | Best audience | Status |
|---|---|---|---|---|---|---|
| `ANG-001` | Runtime optimizer for compound AI systems | AgentC can transparently optimize repeated LLM-agent call sites with rule-based rewrites. | Interception path, planner, ContextCompress and ModelDowngrade savings. | Need broader end-to-end tasks and stronger safety framing. | systems, AI infrastructure, agent tooling | `promising` |
| `ANG-002` | JIT-style optimizer for LLM workloads | LLM calls can be profiled and optimized with hot-call thresholds, rule selection, and safety checks. | `planner.rs`, optimizer rules, ablation CSVs. | JIT analogy must not overclaim compiler equivalence. | systems, programming languages, ML systems | `promising` |
| `ANG-003` | Practical cost optimizer for AI agents | AgentC reduces cost with low application-code burden on suitable workloads. | 34-35% cost savings on ContextCompress and ModelDowngrade workloads. | Workloads may look purpose-built unless related work and evaluation are careful. | applied AI, agent frameworks, workshops | `strong` |
| `ANG-004` | Evaluation methodology for LLM-runtime optimizers | Optimizer papers need workload-specific ablations, activation boundaries, and quality-preserving checks. | 11-config ablation matrices and Hotpot activation-boundary result. | Needs statistical analysis and more explicit methodology framing. | ML systems, empirical software engineering | `promising` |
| `ANG-005` | Rule library for agent-call optimization | A small set of transparent rewrite rules covers common inefficiencies in agent workloads. | ContextCompress, ModelDowngrade, StateDrop implementations. | CacheHit and ParallelBranch need careful descoping or future-work treatment. | agent frameworks, developer tools | `needs evidence` |

## Current Recommendation

After ingesting `DRP-001` and `DRP-002`, lead with `ANG-001`: **runtime optimizer for compound AI systems**. Use `ANG-003` as the practical motivation and `ANG-004` as the evaluation-strengthening thread. Avoid making `ANG-005` the main claim until every rule has either a clean result or a clear status label.

## Venue Mapping

| Angle | Best Venue Lane | Must-Keep Claim | Must-Add Evidence | Closest Threat | Red-Flag Objection |
|---|---|---|---|---|---|
| `ANG-001` runtime optimizer for compound AI systems | MLSys, ATC | AgentC optimizes application-level agent traces under one runtime control plane. | end-to-end optimizer run, overhead, artifact readiness | Autellix, Halo, DSPy/SGLang | Is this just an engineering wrapper around existing tricks? |
| `ANG-003` practical cost optimizer for AI agents | ATC, agent workshops | AgentC saves cost on suitable agent call sites with low application-code burden. | operational lessons, failure modes, guardrails | FrugalGPT, RouteLLM, LLMLingua, GPTCache | Why is this not just routing/compression/caching glued together? |
| `ANG-004` evaluation methodology for LLM-runtime optimizers | COLM, MLSys workshops | Optimizer evaluation must report cost, quality, activation boundaries, and stochastic uncertainty. | repeated-run/paired uncertainty plan, stronger baselines | HELM, tau-bench, ReliableEval | Do current results prove behavior preservation? |
| `ANG-002` JIT-style optimizer for LLM workloads | MLSys, ASPLOS only if strengthened | Hot call sites can be profiled and rewritten under safety checks. | tighter compiler analogy and semantics | compiler/PL work, DSPy | Is JIT language too strong? |

## Decision Criteria

- Does the angle make ContextCompress and ModelDowngrade obvious headline results?
- Can StateDrop be presented as promising without weakening the paper?
- Does the angle make Hotpot's near-zero savings result useful rather than embarrassing?
- Does the related-work landscape have a clear nearest-neighbor comparison?
- Would a reviewer understand why these workloads are representative enough to study?
