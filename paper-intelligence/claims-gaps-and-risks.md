---
title: Claims, Gaps, and Risks
status: active
last-updated: 2026-05-09
owner: paper-intelligence
---

# Claims, Gaps, and Risks

This is the defensive paper-positioning document. It answers three questions:

1. What can AgentC safely claim today?
2. What should the paper avoid saying?
3. What evidence, citations, and experiments are still missing?

Supersedes:

- `claim-bank.md`
- `paper-gap-register.md`
- `reviewer-risk-register.md`
- `citation-gap-list.md`
- `question-backlog.md`
- `weak-point-resolution-plan.md`

## Current Verdict

AgentC has a plausible paper shape, but the claim must be narrow: **a runtime control plane for framework-emitted, multi-step LLM agent traces that can apply several rewrite classes under one policy.**

The current evidence supports targeted savings for `ContextCompress` and `ModelDowngrade`, promising but caveated `StateDrop`, and a useful activation-boundary story for real HotpotQA. It does not yet support a broad claim that all five rewrites are validated, that behavior is preserved in a semantics-level sense, or that AgentC is the first optimizer for LLM agents.

## Safe Claims

| ID | Status | Safe wording | Evidence | Caveat |
|---|---|---|---|---|
| `CLM-001` | supported | AgentC sits between agent code and LLM APIs, intercepting calls and failing open when optimization is unsafe or unavailable. | `ART-020`, `ART-024`, `ART-025` | Transparency depends on SDK patches/framework coverage. |
| `CLM-002` | supported | On the purpose-built `long_context_qa` workload, `ContextCompress` achieves about 34.5% cost savings with similar input-token savings. | `RES-001`, `ART-021` | Do not generalize to all real-world QA. |
| `CLM-003` | supported | On `gaia_router`, `ModelDowngrade` saves about 35.3% by routing from `gpt-4o` to `gpt-4o-mini`. | `RES-002`, `ART-022` | Savings are price-ratio driven; quality needs uncertainty framing. |
| `CLM-004` | promising | `StateDrop` reduces stale state in iterative refinement, with current evidence showing about 6-10% input-token savings depending on run. | `RES-003`, `RES-004`, `ART-023` | Matrix is partial and metric is lenient. |
| `CLM-005` | supported | On real HotpotQA, `ContextCompress` fires rarely and produces near-zero savings, supporting the activation-gate story. | `RES-005`, `ART-001` | Present as diagnostic/gating evidence, not headline savings. |
| `CLM-006` | needs-analysis | Gold-label compression suggests HotpotQA distractors can be removed profitably and may improve answers. | `RES-006`, `ART-001` | Current automated rule does not achieve oracle compression. |
| `CLM-007` | promising | AgentC optimizes multi-call traces emitted by agent frameworks, using multiple rewrite classes under one runtime control plane. | `DRP-001`, `LIT-002`, `LIT-024`, `LIT-025`, `LIT-040`, `LIT-043`, `LIT-044`, `ART-020`, `ART-024` | Novelty must be narrowed against close systems. |
| `CLM-008` | needs-analysis | AgentC evaluates cost savings alongside task-quality metrics and should report uncertainty or repeated-run reliability where possible. | `DRP-001`, `STAT-001`, `STAT-004`, `LIT-026`, `LIT-031`, `LIT-032`, `LIT-064`, `LIT-068`, `LIT-070` | Strong wording depends on paired/repeated evaluation. |

## Unsafe Claims

| Claim to avoid | Why it is unsafe | Safer replacement |
|---|---|---|
| AgentC is the first runtime optimizer for all LLM agents. | Too broad given Agentix/Autellix, Halo, Murakkab, AIOS, Cognify, DSPy, LMQL, SGLang, LLMCompiler, LLM-Tool Compiler, and vCache. | AgentC explores transparent runtime trace rewriting for existing agent-framework calls. |
| Routing is the main novelty. | FrugalGPT, RouteLLM, RouterBench, cascades, and LLMSelector already cover model choice. | `ModelDowngrade` is one pass in a broader optimizer. |
| `ContextCompress` invents prompt compression. | LLMLingua, LongLLMLingua, Selective Context, LLMLingua-2, and tool-use compression are strong prior art. | AgentC integrates conservative compression with trace/runtime policy. |
| `StateDrop` is sound dead-code elimination for prompts. | Soundness needs a dependency/read-window model. | `StateDrop` is inspired by liveness/slicing and currently evaluated as conservative runtime pruning. |
| `CacheHit` preserves behavior. | False hits, stale context, and multi-turn state sensitivity are unresolved. | CacheHit is future/conditional until correctness metrics exist. |
| `ParallelBranch` is safe whenever siblings look independent. | Tools may have side effects or hidden dependencies. | ParallelBranch needs explicit idempotence/dependency policy. |
| Single-run results prove behavior preservation. | Stochastic agents need repeated/paired uncertainty treatment. | Current results are evidence, not final reliability proof. |

## Gap Register

| ID | Severity | Gap | Blocks | Fix path |
|---|---|---|---|---|
| `GAP-001` | high | Local reference artifacts need summaries and provenance notes. | `CLM-006`, manual writing | Summarize `ART-001`-`ART-004`. |
| `GAP-002` | high | `StateDrop` n=50 matrix is partial 10/11. | `CLM-004` | Finish missing row or frame as partial. |
| `GAP-003` | high | Real HotpotQA `ContextCompress` matrix is partial 7/11. | `CLM-005` | Finish remaining configs only if needed for activation-boundary story. |
| `GAP-004` | high | Accuracy deltas need uncertainty framing. | `CLM-002`, `CLM-003`, `CLM-008` | Complete paired/statistical analysis. |
| `GAP-005` | medium | `StateDrop` accuracy check is lenient. | `CLM-004` | Limit claims or add stronger evaluation. |
| `GAP-006` | medium | Oracle ceiling claim needs trace-query evidence, not just CSV. | `CLM-006` | Locate or reproduce trace queries. |
| `GAP-007` | high | Nearest-neighbor metadata/baseline cleanup was incomplete. | Novelty claims | Now mostly handled by `literature-and-nearest-neighbors.md`; final BibTeX cleanup remains open. |
| `GAP-008` | high | Venue lane affects required evidence. | Paper angle | Use `strategy-and-venues.md` to choose target lane. |
| `GAP-009` | high | Bibliography metadata is not final. | Related work | Final citation cleanup remains. |
| `GAP-010` | high | Novelty must be narrowed against close systems and single-rewrite baselines. | Title, abstract, intro | Use nearest-neighbor matrix and avoid broad firstness. |
| `GAP-011` | high | Main venues need end-to-end optimizer evidence, not only rule-isolation ablations. | MLSys/ATC/COLM readiness | Design one workload where multiple rules fire together. |
| `GAP-012` | high | Direct or conceptual baselines missing for routing, compression, caching, and parallelism. | Evaluation credibility | Decide runnable vs citation-only baselines. |
| `GAP-013` | medium | `StateDrop` needs a concrete dependency/read-window model. | `CLM-004` | Use compiler sources as analogy only unless semantics are defined. |
| `GAP-014` | high | Stochastic evaluation needs repeated-run or paired uncertainty treatment. | Quality preservation | Add repeated/paired analysis plan. |
| `GAP-015` | high | Runtime overhead, fallback behavior, and operational failure modes are not summarized. | Systems venues | Add overhead/guardrail evidence plan. |
| `GAP-016` | medium | Serving-system orthogonality needs crisp explanation. | Systems framing | Use serving sources to separate application-level rewrites from serving internals. |

## Reviewer Risk Register

| ID | Level | Likely objection | Current answer | Mitigation |
|---|---|---|---|---|
| `RR-001` | high | Workloads may look purpose-built. | They are targeted stress tests for common agent inefficiencies. | Add workload taxonomy and rationale. |
| `RR-002` | high | Accuracy preservation is under-tested. | Current CSVs include accuracy, but uncertainty is incomplete. | Execute paired/uncertainty analysis. |
| `RR-003` | medium | `StateDrop` savings are smaller and accuracy metric is lenient. | Treat as promising, not equal headline evidence. | Finish matrix or improve metric. |
| `RR-004` | medium | Real HotpotQA near-zero savings weakens `ContextCompress`. | It supports activation-boundary behavior. | Present as diagnostic/gating evidence. |
| `RR-005` | high | Related work already has close analogs. | Verified blurbs identify the threats. | Keep novelty narrow. |
| `RR-006` | medium | Prompt caching/pricing confuse savings. | Report both cost and token savings. | Add pricing/accounting note. |
| `RR-007` | medium | Rule activation policy is heuristic. | It is conservative by design. | Add rule activation map and code references. |
| `RR-008` | medium | CacheHit/ParallelBranch distract if unbenchmarked. | They should not be headline claims yet. | Label as future or implementation inventory. |
| `RR-009` | high | `ModelDowngrade` looks like ordinary routing. | AgentC routes internal call sites as one pass in a trace optimizer. | Compare to routing/cascade literature. |
| `RR-010` | high | `ContextCompress` looks like LLMLingua. | AgentC rewrites runtime message traces. | Run compression baseline or make sharp conceptual distinction. |
| `RR-011` | medium | CacheHit unsafe in multi-turn/stateful contexts. | Needs call-site/state-aware keys. | Keep caveated until correctness story exists. |
| `RR-012` | medium | ParallelBranch independence is unsound. | Needs dependency/side-effect policy. | Treat as future unless evaluated. |
| `RR-013` | high | Agentix/Halo/Murakkab/Cognify/serving systems subsume the story. | AgentC works above server/API layer and rewrites application semantics. | Make application-level trace rewriting central. |
| `RR-014` | high | Single-run evaluation is underpowered. | Current results need paired/repeated framing. | Elevate `GAP-014`; avoid proof language. |

## Citation Gaps

| ID | Status | Claim needing support | Current source set | Next action |
|---|---|---|---|---|
| `CIT-001` | checked-blurb | Agent traces expose systems-level optimization opportunities. | `LIT-002`-`LIT-006` | Use in intro/related work. |
| `CIT-002` | checked-blurb | Routing is related but incomplete. | `LIT-007`-`LIT-012` | Decide runnable routing baselines. |
| `CIT-003` | checked-blurb | Context compression is established. | `LIT-013`-`LIT-016`, `LIT-047`, `LIT-048` | Decide compression baseline plan. |
| `CIT-004` | checked-blurb | Semantic caching correctness depends on context/invalidation. | `LIT-017`-`LIT-020`, `LIT-039`, `LIT-055` | Keep CacheHit caveated. |
| `CIT-005` | checked-blurb | Parallel execution needs dependency/side-effect framing. | `LIT-021`-`LIT-023`, `LIT-060`, `LIT-061` | Keep ParallelBranch caveated. |
| `CIT-006` | checked-blurb | StateDrop is better supported by liveness/slicing than prompt compression alone. | `LIT-015`, `LIT-037`, `LIT-038`, `LIT-051`, `LIT-052` | Define dependency/read-window model. |
| `CIT-007` | checked-blurb | Stochastic agents need repeated/paired uncertainty and judge-bias controls. | `LIT-026`-`LIT-032`, `LIT-063`-`LIT-070` | Decide metrics for target venue. |
| `CIT-008` | checked-blurb | Serving systems are orthogonal. | `LIT-020`, `LIT-033`-`LIT-036`, `LIT-062` | Write orthogonality paragraph. |
| `CIT-009` | checked-blurb | Broad runtime novelty must be narrowed. | `LIT-040`, `LIT-041`, `LIT-043`, `LIT-044`, `LIT-006`, `LIT-036` | Use in novelty caveats. |
| `CIT-010` | checked-blurb | ParallelBranch must compare to compiler-style function calling. | `LIT-021`, `LIT-060`, `LIT-061`, `LIT-042`, `LIT-043` | Decide future-work vs result. |
| `CIT-011` | checked-blurb | CacheHit needs correctness-aware evaluation. | `LIT-055`, `LIT-019`, `LIT-018`, `LIT-056`-`LIT-058` | Define false-hit metrics first. |
| `CIT-012` | checked-blurb | StateDrop needs dependency/liveness grounding. | `LIT-037`, `LIT-038`, `LIT-051`-`LIT-054` | Avoid soundness claims. |
| `CIT-013` | checked-blurb | Judge/stochastic evaluation needs repeated trials and uncertainty. | `LIT-063`-`LIT-070` | Update stats plan. |

## Open Questions

| ID | Type | Question | Current disposition |
|---|---|---|---|
| `QST-001` | experiment | Should we finish the partial `StateDrop` n=50 matrix before writing? | Yes for stronger systems paper; optional for short/positioning paper if caveated. |
| `QST-002` | venue | Is the first target ATC, longer-run MLSys, or LM-native COLM? | Needs venue choice in `strategy-and-venues.md`. |
| `QST-003` | artifact | Where is the trace evidence for the Hotpot oracle ceiling? | Still open; needed before strong `CLM-006`. |
| `QST-004` | positioning | How narrow should novelty be against close systems? | Very narrow: framework-call interception plus multi-rule trace rewriting. |
| `QST-005` | method | What does behavior-preserving mean? | Use metric/tolerance-bounded wording, not semantic equivalence. |
| `QST-006` | experiment | Can we produce one workload where multiple rewrite rules fire together? | Highest-value next experiment for MLSys/ATC. |
| `QST-007` | systems | What are interception overhead and latency-tail effects? | Required for systems venues. |

## Ordered Weak-Point Plan

| ID | Type | Weak point | Cheapest fix | Strongest fix |
|---|---|---|---|---|
| `WP-001` | decision | Main paper angle not locked. | Use runtime optimizer for compound AI systems. | Choose explicit venue lane. |
| `WP-002` | analysis | Accuracy preservation lacks paired analysis. | Add simple standard errors/paired tests. | Run paired bootstrap or McNemar-style tests. |
| `WP-003` | experiment | `StateDrop` n=50 matrix partial. | Finish missing config. | Rerun with stronger metric. |
| `WP-004` | experiment | Real HotpotQA matrix partial. | Finish only if it answers activation-boundary question. | Build clearer real-task suite. |
| `WP-005` | literature | Nearest-neighbor comparison needs cleanup. | Use verified blurbs. | Final metadata and baseline cleanup. |
| `WP-006` | briefing | Rule mechanism explanation needs compact version. | Expand system/rules brief. | Add rule activation figure. |
| `WP-007` | local audit | CacheHit/ParallelBranch status can confuse contribution count. | Label future/implementation only. | Add evidence if they become claims. |
| `WP-008` | briefing | Imported source artifacts not summarized. | Summarize `ART-001` and `ART-002`. | Extract every claim/table/figure idea. |
| `WP-009` | literature | Novelty too broad. | Mark firstness unsafe. | Write exact distinction against closest systems. |
| `WP-010` | literature | StateDrop lacks soundness story. | Cite program analysis carefully. | Define dependency/read-window model. |
| `WP-011` | experiment | Main venues want end-to-end and overhead evidence. | Add focused experiment plan. | Run multi-rule workload plus overhead/tail measurement. |
| `WP-012` | analysis | Evaluation should handle stochasticity. | Add uncertainty requirements. | Run repeated trials/paired bootstrap where possible. |

## Highest-Priority Next Fixes

1. Build one end-to-end workload where multiple rules can fire together: `GAP-011`, `QST-006`, `WP-011`.
2. Add paired/repeated uncertainty treatment for headline results: `GAP-004`, `GAP-014`, `RR-002`, `RR-014`, `WP-002`, `WP-012`.
3. Keep CacheHit and ParallelBranch out of headline claims unless new evidence lands: `RR-008`, `RR-011`, `RR-012`, `WP-007`.
4. Define StateDrop's dependency/read-window model before using compiler-soundness language: `GAP-013`, `CIT-006`, `CIT-012`, `WP-010`.
5. Write the exact novelty sentence against close systems: `GAP-010`, `RR-005`, `RR-013`, `WP-009`.
