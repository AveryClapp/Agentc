# Agentc

> **Status: Early-stage research prototype**

**A JIT optimization runtime for multi-step LLM agent workloads.**

Multi-agent systems cost 4–15× more tokens than single-agent baselines. No existing framework optimizes execution before running it — token spend is simply incurred, step by step, blindly. Agentc is the missing runtime layer: it intercepts LLM calls as they fire and applies principled optimizations the model cannot perform for itself.

---

## The Key Insight

Agent frameworks like Claude Code and LangGraph sit *above* the LLM API. They describe what to do. Nobody has built the layer *between* them and the API that decides how to do it cheaply. Agentc is that layer — the runtime they all sit on, making every agent workload cheaper without changing application code.

```
  Agent Frameworks       Claude Code · LangGraph · CrewAI
         │               describe what to do
         ▼
      Agentc             intercepts calls, optimizes execution
         │               decides how to do it cheaply
         ▼
      LLM APIs           Anthropic · OpenAI · Gemini
                         raw inference
```

---

## How It Works

Agentc has three components:

**Execution DAG (IR)** — Agent execution is represented as a directed acyclic graph of typed nodes — LLM calls, tool invocations, sub-agent dispatches — connected by data dependencies. Each node is a typed struct carrying its execution parameters and a set of candidate strategies: full context vs. compressed summary, frontier model vs. cheaper model, parallel vs. sequential. The DAG is built incrementally during execution, not pre-compiled upfront.

**Cost Model** — An empirical model fitted from real executions that estimates token cost, latency, and accuracy degradation for any node × strategy combination. Grounded in real inference characteristics: attention is quadratic in sequence length, context position affects recall, KV cache reuse has measurable latency implications.

**Optimizer** — Applies named rewrite rules at the call boundary to find the cheapest execution strategy within a user-specified accuracy budget. Operates locally on the known subgraph — no global plan visibility required.

---

## Optimizer Rules

- `ContextCompress` — Replace full context passed to a sub-agent with a summary, at a measured accuracy cost
- `ParallelBranch` — Schedule dependency-free nodes for concurrent execution
- `ModelDowngrade` — Substitute a cheaper model where the cost model predicts sufficient accuracy
- `StateDrop` — Prune intermediate results not consumed by any downstream node
- `DeferredEvaluation` — Delay speculative branches until upstream results confirm they're needed

These are optimizations every agent engineer already does by hand. Agentc formalizes them as named, composable rewrite rules so they can be measured, studied, and applied systematically.

---

## What Agentc Can and Cannot Be Replaced By

Models are increasingly RL-trained to manage their own context and tool usage efficiently — learning decision-level optimizations from the inside. Agentc's defensible territory is the runtime level: optimizations that happen *outside the model's context window* and that no amount of RL training can touch.

| Rule | Level | Safe from RL encroachment? |
|---|---|---|
| `ContextCompress` | Decision | Yes — model doesn't control its own input |
| `ParallelBranch` | Runtime | Yes — model has no cross-call awareness |
| `ModelDowngrade` | Decision | Partial — overlaps with learned routing |
| `StateDrop` | Runtime | Yes — model cannot prune its own input |
| `DeferredEvaluation` | Decision | Yes — model cannot defer its own calls |

KV cache scheduling, parallel dispatch, semantic memoization, and checkpointing are infrastructure problems the model has no visibility into. That is Agentc's moat.

---

## Architecture

```
Incoming LLM Call
       │
       ▼
  DAG Builder       →   typed node + edge schema, incremental construction
       │
       ▼
  Cost Model        →   token cost · latency · accuracy per node × strategy
       │
       ▼
  Optimizer         →   rewrite rules applied at call boundary
       │
       ▼
  Executor          →   runs optimized call, instruments result, updates cost model
       │
       ▼
    Output
```

The executor feeds real data back into the cost model after every run. Agentc improves with use.

---

## Implementation

Agentc is written in **Rust** for the core runtime — DAG IR, optimizer, and executor — with **Python** bindings via [PyO3](https://github.com/PyO3/pyo3) for the benchmarking harness, cost model fitting, and LLM API integrations.

```
agentc/
├── core/          # Rust — DAG IR, cost model, optimizer, executor
├── bindings/      # PyO3 — Python interface to core runtime
└── bench/         # Python — benchmarking harness, API clients, eval
```

Rust's ownership model enforces the right constraints for a DAG runtime with shared state and concurrent execution. Python keeps the project in the LLM API ecosystem where tooling lives.

---

## Research Phases

1. **IR design** — Typed DAG schema for agent execution; node structs, edge semantics, incremental construction
2. **Profiler** — Standalone instrumentation layer; characterize where tokens go across real agent executions
3. **Optimizer** — Rule-based plan rewriting over the cost model
4. **Evaluation** — SWE-bench, GAIA, tool-use benchmarks; token cost vs. accuracy Pareto curves

The profiler ships first as a standalone open source tool. A pipeline that answers "where did my tokens go?" has immediate utility and produces the execution data the cost model requires.

---

## Related Work

Kim et al. (2024) parallelizes independent tool calls within a single agent turn — optimizing call scheduling. Agentc operates one level deeper: it optimizes the execution engine the plan runs on, adding model selection, context compression, semantic caching, and inference scheduling as first-class runtime concerns. Model routing work (MasRouter et al.) handles model selection in isolation; compound AI optimization literature focuses on differentiable learned approaches. Agentc's rewrite-rule approach — closer to traditional compiler passes — is a less-explored angle that unifies these concerns beneath a single runtime.

---

## Open Questions

- How concrete should node semantics be vs. remaining abstract?
- Learned vs. heuristic cost models — which generalizes better across task domains?
- Which rewrite rules give the biggest efficiency wins in practice?
- Where is the break-even point between optimizer overhead and token savings?

---

## Name

**Agentc** — agent compiler. The `-c` suffix is a deliberate nod to the compiler toolchain tradition (`rustc`, `gcc`, `clangd`). Agentc occupies the same role in the agent stack that a compiler occupies in the software stack: it takes a high-level specification and produces an efficient execution plan.
