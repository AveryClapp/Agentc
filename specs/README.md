# Agentc Specs

Technical specifications for each component of the Agentc runtime. Start here to understand what we're building and why each piece exists.

---

## What Agentc Is

Agentc is a runtime that sits between agent frameworks (Claude Code, LangGraph, CrewAI) and the LLM APIs they call. It intercepts inference calls and makes them cheaper — compressing context, parallelizing independent calls, swapping in cheaper models where quality holds, and caching semantically equivalent prompts.

Three components make this work. They build on each other in order.

---

## 1. Profiler — [profiler.md](profiler.md)

> **Status: Active** | Hardened after three-agent review

Instruments any Python agent pipeline, captures every LLM call (tokens, latency, model, cost, full prompt/response), and produces structured execution traces. Answers: *"where did my tokens go?"*

Ships first because it has standalone value and generates the execution data everything downstream needs.

**Key choices:** Hybrid instrumentation (zero-config monkey-patching + explicit `@trace`/`span()` API), OTel `gen_ai.*` semantic conventions, Rust core via PyO3, model2vec embeddings at capture time, five waste detectors with dollar estimates.

---

## 2. Semantic Memoization — [memoization.md](memoization.md)

> **Status: Active** | Implementation-ready

Opt-in caching that deduplicates LLM inference. Exact-prompt hash lookup on the hot path; LSH over 256-dim embeddings as a secondary tier for semantically-similar prompts. Cache state piggybacks on the profiler's canonical `traces.db` and reuses its flock-merged cross-process infrastructure.

Users annotate high-repetition call sites with `@agentc.memoize(...)`. The profiler's `redundant_call` detector surfaces candidates; users promote them with full knowledge of what's being cached.

---

## 3. Optimizer — [optimizer.md](optimizer.md)

> **Status: Active** | Implementation-ready

JIT runtime that intercepts LLM calls on hot call sites and applies cost-ranked rewrite rules subject to a per-rule accuracy budget. Five rules ship in v1: `CacheHit`, `ContextCompress`, `ParallelBranch`, `ModelDowngrade`, `StateDrop`.

Cold calls pass through; optimization engages after `hot_threshold` observations, when the empirical cost model has real data to rank proposals. Shadow-mode sampling at 2% provides ground-truth divergence for the accuracy budget.

---

## Build Order

```
profiler  ──→  memoization  (parallel track, fast to results)
    │
    └──────→  optimizer     (requires profiling data for cost model)
```

---

## Directory Layout

```
specs/
├── README.md              # this file — project overview for humans
├── CLAUDE.md              # style guide + constraints for agents writing specs
├── profiler.md            # Profiler spec (active, implementation-ready)
├── memoization.md         # Semantic Memoization spec (active)
├── optimizer.md           # Optimizer spec (active)
├── future-work.md         # out-of-scope items, organized by component
└── working/               # research, analysis, handoffs
    ├── profiler-gap-analysis.md
    └── HANDOFF.md
```

**Top-level `.md` files** are canonical — specs, the future-work backlog, and this readme. **`working/`** holds reference material (research, gap analyses, handoff notes) that informed the specs but aren't specs themselves.

See [CLAUDE.md](CLAUDE.md) for the spec style guide and writing constraints.
