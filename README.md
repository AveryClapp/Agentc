# Agentc

> **Status: Early-stage research prototype**

**A JIT optimization runtime for multi-step LLM agent workloads.**

Multi-agent systems cost 4-15x more tokens than single-agent baselines. Today, every framework just fires LLM calls and eats the cost. No one optimizes execution *before* it runs. Agentc sits between agent frameworks and the LLM APIs, intercepts calls as they happen, and rewrites them to be cheaper — without touching application code.

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

Think of it like a compiler for agent workloads. Frameworks describe *what* to do. Agentc decides *how* to do it cheaply.

---

## The Three Core Components

Agentc is built around three pieces that work together in a loop.

### 1. Execution DAG (the IR)

Every agent workload is a graph of operations: LLM calls, tool invocations, sub-agent dispatches. Agentc represents this as a **directed acyclic graph** where each node is a typed struct carrying:

- **Execution parameters** — the model, prompt, temperature, tools available
- **Candidate strategies** — full context vs. compressed summary, frontier model vs. cheaper model, parallel vs. sequential
- **Data dependencies** — edges encoding which nodes need output from which other nodes

The DAG is built **incrementally during execution**, not pre-compiled upfront. As each new call comes in, Agentc adds it to the graph, resolves its dependencies, and decides the cheapest way to run it before it actually fires.

### 2. Cost Model

An empirical model fitted from real executions. For any **node x strategy** combination, it estimates three things:

- **Token cost** — how many tokens this strategy will consume
- **Latency** — wall-clock time including network, queuing, and inference
- **Accuracy degradation** — how much quality drops compared to the baseline (full context, best model)

The cost model is grounded in real inference characteristics:
- Attention is quadratic in sequence length — longer contexts cost disproportionately more
- Context position affects recall — information at the start and end of a prompt is retrieved more reliably than the middle
- KV cache reuse has measurable latency implications — cache hits are significantly faster

The model improves with use. Every execution feeds real data back in.

### 3. Optimizer

The optimizer applies **named rewrite rules** at the call boundary to find the cheapest execution strategy that stays within a user-specified accuracy budget. It works locally on the known subgraph — no global plan visibility required.

The five rewrite rules:

| Rule | What it does | Example |
|---|---|---|
| `ContextCompress` | Replace full context with a summary | 50K-token conversation history compressed to 3K-token summary before passing to a sub-agent |
| `ParallelBranch` | Run dependency-free nodes concurrently | Three independent tool calls dispatched simultaneously instead of sequentially |
| `ModelDowngrade` | Swap in a cheaper model when accuracy permits | Use Haiku instead of Opus for a classification step the cost model scores at 98%+ accuracy either way |
| `StateDrop` | Prune intermediate results no downstream node needs | Drop verbose tool output that was only used to decide a branch, not passed forward |
| `DeferredEvaluation` | Delay speculative branches until upstream results confirm they're needed | Don't run the fallback search until the primary search actually fails |

These are optimizations every agent engineer already does by hand. Agentc formalizes them as composable, measurable rules that can be applied systematically.

---

## Why Models Can't Replace This

Models are getting better at managing their own context and tool usage through RL training. But Agentc operates on things the model literally cannot see or control:

| Rule | Level | Can RL training replicate this? |
|---|---|---|
| `ContextCompress` | Input shaping | No — the model doesn't control what gets sent to it |
| `ParallelBranch` | Runtime scheduling | No — the model has no awareness of other concurrent calls |
| `ModelDowngrade` | Routing | Partially — overlaps with learned routing, but the model can't swap itself out |
| `StateDrop` | Memory management | No — the model cannot prune its own input before receiving it |
| `DeferredEvaluation` | Control flow | No — the model cannot defer calls it hasn't been asked to make yet |

KV cache scheduling, parallel dispatch, semantic memoization, and checkpointing are infrastructure problems. The model has no visibility into them. That's the moat.

---

## Architecture

```
Incoming LLM Call
       │
       ▼
  DAG Builder       →   adds typed node + edges to the execution graph
       │
       ▼
  Cost Model        →   scores token cost · latency · accuracy for each strategy
       │
       ▼
  Optimizer         →   picks cheapest strategy within accuracy budget
       │
       ▼
  Executor          →   runs the optimized call, instruments the result
       │
       ▼
  Cost Model        ←   feeds real execution data back (the loop closes here)
       │
       ▼
    Output
```

The feedback loop is the key. The cost model starts with heuristics and gets empirically calibrated with every execution. Agentc gets cheaper the more you use it.

---

## Implementation

**Rust** for the core runtime (DAG IR, optimizer, executor) — ownership model enforces the right constraints for a DAG with shared state and concurrent execution.

**Python** bindings via [PyO3](https://github.com/PyO3/pyo3) for benchmarking, cost model fitting, and LLM API integrations — keeps the project in the ecosystem where all the LLM tooling lives.

```
agentc/
├── core/          # Rust — DAG IR, cost model, optimizer, executor
├── bindings/      # PyO3 — Python interface to core runtime
└── bench/         # Python — benchmarking harness, API clients, eval
```

---

## Research Phases

1. **IR design** — Typed DAG schema for agent execution: node structs, edge semantics, incremental construction
2. **Profiler** — Standalone instrumentation layer that characterizes where tokens go across real agent executions
3. **Optimizer** — Rule-based plan rewriting over the cost model
4. **Evaluation** — SWE-bench, GAIA, tool-use benchmarks; token cost vs. accuracy Pareto curves

The profiler ships first as a standalone open source tool. A pipeline that answers "where did my tokens go?" has immediate utility on its own and produces the execution data the cost model needs to get calibrated.

---

## Related Work

Kim et al. (2024) parallelizes independent tool calls within a single agent turn — optimizing call scheduling. Agentc goes one level deeper: it optimizes the execution engine the plan runs on, treating model selection, context compression, semantic caching, and inference scheduling as first-class runtime concerns.

Model routing work (MasRouter et al.) handles model selection in isolation. Compound AI optimization literature focuses on differentiable learned approaches. Agentc's rewrite-rule approach — closer to traditional compiler passes — is a less-explored angle that unifies these concerns under a single runtime.

---

## Open Questions

- How concrete should node semantics be vs. remaining abstract?
- Learned vs. heuristic cost models — which generalizes better across task domains?
- Which rewrite rules give the biggest efficiency wins in practice?
- Where is the break-even point between optimizer overhead and token savings?

---

## Name

**Agentc** — agent compiler. The `-c` suffix nods to the compiler toolchain tradition (`rustc`, `gcc`, `clangd`). Agentc occupies the same role in the agent stack that a compiler occupies in the software stack: takes a high-level specification, produces an efficient execution plan.
