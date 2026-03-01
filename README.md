# Agentc

## Motivation

Multi-agent systems cost 4–15× more tokens than single-agent baselines, with no principled way to control that cost. No existing framework optimizes the full execution plan before running it — token spend is simply incurred, step by step, blindly.

---

## How It Works

Agentc sits between a task specification and its execution. It has three components:

**Execution DAG (IR)** — Instead of running a task directly, Agentc represents it as a directed acyclic graph of LLM calls, tool invocations, and sub-agent dispatches connected by data dependencies. Each node carries multiple candidate execution strategies: full context vs. compressed summary, frontier model vs. cheaper model, parallel vs. sequential.

**Cost Model** — An empirical model fitted from real executions that estimates token cost, latency, and accuracy degradation for any node × strategy combination. Grounded in real inference characteristics — attention is quadratic in sequence length, context position affects recall, KV cache reuse has measurable latency implications.

**Optimizer** — Applies named rewrite rules over the DAG to find the cheapest execution plan within a user-specified accuracy budget.

---

## Architecture

```
Task Specification
       │
       ▼
  DAG Builder       →   execution graph with alternative strategies per node
       │
       ▼
  Cost Model        →   token cost, latency, accuracy estimates per node × strategy
       │
       ▼
  Optimizer         →   rewrites DAG using rewrite rules within accuracy budget
       │
       ▼
  Executor          →   runs optimized plan, instruments results, updates cost model
       │
       ▼
    Output
```

---

## Optimizer Rules

- `ContextCompress` — Replace full context passed to a sub-agent with a summary, at a measured accuracy cost
- `ParallelBranch` — Schedule dependency-free nodes for concurrent execution
- `ModelDowngrade` — Substitute a cheaper model where the cost model predicts sufficient accuracy
- `StateDrop` — Prune intermediate results not consumed by any downstream node
- `DeferredEvaluation` — Delay speculative branches until upstream results confirm they're needed

---

## Research Phases

1. **IR design** — DAG schema for agent execution
2. **Cost model** — Profiling real executions
3. **Optimizer** — Rule-based plan rewriting
4. **Evaluation** — SWE-bench, GAIA, tool-use benchmarks

---

## Implementation

Agentc is written in **Rust** for the core runtime — DAG representation, optimizer, and executor — with **Python** bindings via [PyO3](https://github.com/PyO3/pyo3) for the benchmarking harness, cost model fitting, and LLM API integrations.

```
agentc/
├── core/          # Rust — DAG IR, cost model, optimizer, executor
├── bindings/      # PyO3 — Python interface to core runtime
└── bench/         # Python — benchmarking harness, API clients, eval
```

Rust enforces the right constraints for a DAG runtime with shared state and concurrent execution. Python keeps the project in the LLM API ecosystem where benchmarking tooling lives.

---

## Related Work

Multi-agent research systems show large quality gains but accept 4–15× higher token costs without principled optimization. Context compression methods improve efficiency but operate on individual calls, not whole plans. Planner-executor patterns focus on correctness and decomposition, not cost-based plan selection. Agentc is the missing optimization phase that cuts across all three.

---

## Open Questions

- How concrete should node semantics be vs. remaining abstract?
- Learned vs. heuristic cost models — which generalizes better?
- Which rewrite rules give the biggest efficiency wins in practice?

---

## Name

**Agentc** — agent compiler. The `-c` suffix is a deliberate nod to the compiler toolchain tradition (`rustc`, `gcc`, `clangd`). Agentc occupies the same role in the agent stack that a compiler occupies in the software stack: it takes a high-level specification and produces an efficient execution plan.
