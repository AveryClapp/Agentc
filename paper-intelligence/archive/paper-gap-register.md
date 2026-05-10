---
title: Paper Gap Register
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Paper Gap Register

This is the canonical table for paper gaps, risks, and missing evidence. Other files may provide filtered views, but this is the master register.

| ID | Status | Type | Severity | Description | Blocks | Fix Path | Owner | Next Action |
|---|---|---|---|---|---|---|---|---|
| `GAP-001` | `open` | `artifact` | high | Local reference artifacts need summaries and provenance notes before claims depend on them. | `CLM-006`, manual writing brief | source triage | paper-intelligence | Summarize `ART-001` through `ART-004`. |
| `GAP-002` | `open` | `result` | high | StateDrop n=50 matrix is partial 10/11. | `CLM-004`, StateDrop table | experiment or explicit partial framing | TBD | Decide whether to finish missing row or keep partial with caveat. |
| `GAP-003` | `open` | `result` | high | Real HotpotQA ContextCompress matrix is partial 7/11. | `CLM-005`, pushback table | experiment or explicit partial framing | TBD | Decide whether to finish remaining configs. |
| `GAP-004` | `open` | `method` | high | Accuracy deltas need uncertainty framing and possibly paired testing. | `CLM-002`, `CLM-003` | statistical plan | TBD | Create/complete `statistical-analysis-plan.md`. |
| `GAP-005` | `open` | `reviewer-risk` | medium | StateDrop accuracy check is lenient. | `CLM-004` | limitations/framing or stronger eval | TBD | Decide whether future ROUGE/LLM-judge eval is needed. |
| `GAP-006` | `open` | `artifact` | medium | Oracle ceiling claim needs trace-query evidence, not just the CSV. | `CLM-006` | source extraction | TBD | Locate or reproduce trace queries referenced in `ART-001`. |
| `GAP-007` | `in-progress` | `positioning` | high | Related work has primary-source checked blurbs, but the nearest-neighbor matrix still needs final metadata and baseline cleanup. | all novelty claims | literature verification | paper-intelligence | Promote corrections from `literature-verified-blurbs.md` into `nearest-neighbor-comparison.md` and final related-work notes. |
| `GAP-008` | `in-progress` | `positioning` | high | Venue fit is partially researched; top lanes are MLSys, ATC operational systems, and COLM. | paper angle, experiment priority | venue verification and target decision | paper-intelligence | Choose near-term lane and update experiment priorities. |
| `GAP-009` | `in-progress` | `citation` | high | Core background claims now have checked blurbs, but ledger metadata and final bibliography rows are not fully promoted. | introduction/related work | literature review | paper-intelligence | Copy verified metadata from `literature-verified-blurbs.md` into `literature-ledger.md` and `bibliography-ledger.md`. |
| `GAP-010` | `open` | `positioning` | high | Novelty claim must be narrowed against Agentix/Autellix, Halo, Murakkab, AIOS, Cognify, DSPy, LMQL, SGLang, LLMCompiler, LLM-Tool Compiler, vCache, and single-rewrite baselines. | title/abstract, introduction, related work | nearest-neighbor comparison | paper-intelligence | Tighten `nearest-neighbor-comparison.md` and keep "first runtime optimizer" wording out unless narrowly delimited. |
| `GAP-011` | `open` | `result` | high | Main venues need end-to-end optimizer evidence, not only rule-isolation ablations. | MLSys/ATC/COLM submission readiness | experiment | TBD | Design one representative workload where multiple rules can fire together. |
| `GAP-012` | `open` | `method` | high | Direct or conceptual baselines are missing for routing, compression, caching, and parallelism. | related work, evaluation credibility | baseline plan | TBD | Decide which baselines are runnable versus discussion-only. |
| `GAP-013` | `open` | `citation` | medium | StateDrop has verified program-analysis/liveness/slicing support, but still needs a concrete AgentC dependency/read-window model before strong claims. | `CLM-004`, StateDrop section | targeted literature review | paper-intelligence | Use `LIT-037`, `LIT-038`, `LIT-051`, and `LIT-052` as analogies; avoid soundness claims without an AgentC semantics. |
| `GAP-014` | `open` | `method` | high | Stochastic evaluation needs repeated-run or paired uncertainty treatment. | quality-preservation claims | statistical/evaluation plan | TBD | Update `statistical-analysis-plan.md` and experiment board. |
| `GAP-015` | `open` | `artifact` | high | Runtime overhead, fallback behavior, and operational failure modes are not yet summarized. | ATC/MLSys readiness | local audit + experiment | TBD | Add overhead and guardrail evidence plan. |
| `GAP-016` | `open` | `reviewer-risk` | medium | Serving-system orthogonality needs crisp explanation: AgentC works above the API/server layer. | related work, systems framing | related-work verification | paper-intelligence | Use verified serving sources in `literature-verified-blurbs.md` to write the orthogonality comparison. |
