---
title: Optimizer
status: draft
last-updated: 2026-03-17
---

# Optimizer

The full JIT runtime optimizer. Represents agent execution as a DAG of typed nodes, fits an empirical cost model from profiler data, and applies named rewrite rules at call boundaries to minimize token spend within an accuracy budget.

This is the long-term vision for Agentc. It depends on the profiler (for execution data) and may incorporate the memoization cache as one of its optimization strategies.

---

## Overview

The optimizer intercepts LLM calls as they fire and applies locally-scoped optimizations without requiring global plan visibility. It operates on a rolling window of the execution graph — closer to a JIT compiler than an AOT compiler. For structured pipelines where the DAG is known upfront, it applies static optimizations. For reactive workflows, it optimizes each call individually at the call boundary.

Three sub-components:
- **DAG IR** — Typed node + edge schema, incremental construction
- **Cost Model** — Empirical model fitted from profiler traces; estimates token cost, latency, accuracy per node x strategy
- **Rewrite Rules** — ContextCompress, ParallelBranch, ModelDowngrade, StateDrop, DeferredEvaluation

---

## Interface

<!-- TODO -->

---

## Architecture

<!-- TODO -->
<!-- - DAG IR: node types, edge semantics, typed-but-not-fully-specified nodes -->
<!-- - Cost model: learned vs. heuristic, distribution over costs conditioned on node type -->
<!-- - Rewrite rules: application order, composability, conflict resolution -->
<!-- - Executor: runs optimized calls, instruments results, feeds back into cost model -->

---

## Dependencies

- Profiler: execution traces are the training data for the cost model
- Memoization: may be integrated as a cache-hit optimization rule

---

## Evaluation

<!-- TODO -->
<!-- - Token cost reduction vs. baseline (same agent, no optimizer) -->
<!-- - Accuracy retention across SWE-bench / GAIA tasks -->
<!-- - Optimizer overhead: does the cost of running the optimizer exceed the savings? -->
<!-- - Pareto curves: cost vs. accuracy at different optimization aggressiveness levels -->
