---
title: Title And Abstract Idea Bank
status: active
last-updated: 2026-05-08
owner: paper-intelligence
---

# Title And Abstract Idea Bank

This is an idea bank, not final paper prose.

## Title Ideas

| ID | Title idea | Angle | Risk | Evidence needed | Status |
|---|---|---|---|---|---|
| `TTL-001` | AgentC: A Runtime Optimizer for Compound AI Systems | runtime optimizer | "compound AI" term needs literature support | `CIT-001`, systems related work | `promising` |
| `TTL-002` | Profiling and Rewriting LLM Agent Calls for Lower Cost | practical cost optimizer | sounds narrow if planner contribution is underplayed | result ledgers, rule map | `promising` |
| `TTL-003` | Toward JIT Optimization for LLM Agent Workloads | JIT analogy | "JIT" may overclaim | planner details, hot-call evidence | `needs care` |
| `TTL-004` | Transparent Cost Optimization for Multi-Step LLM Applications | applied systems | "transparent" needs precise definition | interception path, Python/Rust docs | `promising` |
| `TTL-005` | Runtime Rewrite Rules for Cost-Aware LLM Agents | rule library | may overemphasize rule count | all rule statuses and related work | `candidate` |

## Abstract Direction Notes

Good abstract directions should include:

- the problem: repeated multi-call LLM workloads waste tokens or expensive model calls;
- the method: runtime profiling plus conservative rewrite-rule planning;
- the evidence: ContextCompress, ModelDowngrade, and StateDrop result posture;
- the safety story: rule preconditions, hot-call thresholds, and pass-through defaults;
- the caveat: results are workload-specific and quality-preserving evaluation matters.

Do not include:

- unverified venue claims;
- final uniqueness claims;
- CacheHit/ParallelBranch as empirical headline contributions unless results exist;
- exact claims from imported source artifacts until they are in the ledgers.

