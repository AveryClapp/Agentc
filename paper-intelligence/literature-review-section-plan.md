---
title: Literature Review Section Plan
status: draft
last-updated: 2026-05-09
owner: paper-intelligence
---

# Literature Review Section Plan

This file plans the whole-paper literature review. Routing is one subsection, not the paper's main topic.

## Current Coverage

`DRP-001` surfaced many named sources and seeded the first 38 active candidate rows. `DRP-004` added 31 more candidate rows. `literature-ledger.md` currently tracks 69 active literature entries after ingestion.

`literature-verified-blurbs.md` now contains the primary-source verification pass for `LIT-002` through `LIT-070`, including corrected blurbs, differentiation notes, baseline decisions, evidence/threat scores, and rows that need metadata correction. `literature-blurb-todo.md` remains useful as the original checklist, but it is no longer the source of truth for citation-use wording.

## Intended Section Shape

The literature review should explain the whole AgentC contribution:

1. **Compound AI systems and agent frameworks**
   - Purpose: justify why the unit of optimization is a multi-call trace, not one prompt.
   - Current candidates: `LIT-002` through `LIT-006`.

2. **Runtime optimization for LLM applications**
   - Purpose: place AgentC in the systems/runtime family.
   - Current candidates: `LIT-007`, `LIT-008`, `LIT-023`, `LIT-024`, `LIT-025`.

3. **Rewrite families**
   - Model routing/cascades for `ModelDowngrade`: `LIT-007` through `LIT-012`.
   - Prompt/context compression for `ContextCompress`: `LIT-013` through `LIT-016`.
   - Semantic caching for `CacheHit`: `LIT-017` through `LIT-020`.
   - Tool-call scheduling and parallel execution for `ParallelBranch`: `LIT-021` through `LIT-023`.
   - State/liveness/program analysis for `StateDrop`: `LIT-015`, `LIT-037`, `LIT-038`, `LIT-039`.

4. **Serving and inference systems**
   - Purpose: explain orthogonality. Serving systems optimize how model calls run; AgentC optimizes which calls happen and how application-level traces are rewritten.
   - Current candidates: `LIT-020`, `LIT-033` through `LIT-036`.

5. **Evaluation methodology for stochastic LLM systems**
   - Purpose: justify paired uncertainty, repeated runs, reliability, and judge-bias controls.
   - Current candidates: `LIT-026` through `LIT-032`.

## Main Narrative

AgentC sits at the intersection of these literatures. Each rewrite family has prior art, but the paper should argue that AgentC's contribution is the **runtime control plane over multi-step agent traces**, not any single trick in isolation.

## Current Gaps

- Promote verified metadata corrections from `literature-verified-blurbs.md` back into `literature-ledger.md` and `bibliography-ledger.md`.
- More sources for StateDrop as liveness/data-flow/slicing.
- More sources for online/runtime optimization of agent traces.
- More papers that combine multiple rewrite classes under one system.
- Clear baseline plan for which related systems are runnable versus citation-only.
