---
title: Weak Point Resolution Plan
status: active
last-updated: 2026-05-08
owner: paper-intelligence
---

# Weak Point Resolution Plan

This plan turns reviewer risks and paper gaps into ordered work. It should be worked top-down unless new deep research changes the priority.

## Work Types

- `decision`: William/Avery chooses framing or inclusion policy.
- `local audit`: inspect existing repo code, results, or docs.
- `analysis`: compute or validate statistics from current artifacts.
- `experiment`: spend API tokens or run benchmarks.
- `literature`: verify external papers or venues.
- `briefing`: summarize evidence for human writing.

## Ordered Work Items

| ID | Type | Weak point | Cheapest fix | Strongest fix | Output artifact | Status |
|---|---|---|---|---|---|---|
| `WP-001` | `decision` | Main paper angle is not locked. | Use runtime optimizer for compound AI systems as the default. | Choose explicit venue lane: ATC 2026 versus longer-run MLSys/COLM. | `decision-log.md`, `paper-angle-matrix.md`, `venue-positioning-matrix.md` | `in-progress` |
| `WP-002` | `analysis` | Accuracy preservation lacks paired analysis. | Add simple standard errors and paired binary tests for accuracy deltas. | Run paired bootstrap or McNemar-style tests where row-level artifacts allow it. | `statistical-analysis-plan.md`, `results-ledger.md` | `open` |
| `WP-003` | `experiment` | StateDrop n=50 matrix is partial. | Finish the missing config if cost is acceptable. | Rerun full StateDrop matrix with stronger metric at temperature 0. | `experiment-run-log.md`, `results-ledger.md` | `open` |
| `WP-004` | `experiment` | Real HotpotQA matrix is partial. | Finish remaining configs only if it answers an activation-boundary question. | Build a clearer real-task evaluation suite for ContextCompress. | `results-ledger.md`, `claim-bank.md` | `open` |
| `WP-005` | `literature` | Nearest-neighbor comparison is empty. | Candidate rows were ingested from `DRP-001`. | Verify primary papers and make a comparison table. | `literature-ledger.md`, `nearest-neighbor-comparison.md` | `in-progress` |
| `WP-006` | `briefing` | Rule mechanism explanation needs a compact writer-facing version. | Expand `section-briefs/system-and-rules.md`. | Add rule activation map figure sketch tied to code paths. | `figure-idea-bank.md`, section brief | `open` |
| `WP-007` | `local audit` | CacheHit/ParallelBranch status could confuse contribution count. | Label them implementation/future-work only. | Add evidence or experiments if they become paper claims. | `claim-bank.md`, `paper-gap-register.md` | `open` |
| `WP-008` | `briefing` | Imported source artifacts are not summarized. | Summarize `ART-001` and `ART-002` into evidence notes. | Extract every claim/table/figure idea into ledgers. | `manual-writing-brief.md`, `claim-bank.md` | `open` |
| `WP-009` | `literature` | Novelty claim is too broad unless narrowed against closest systems. | Mark "first runtime optimizer" as unsafe. | Verify Autellix, Halo, FrugalGPT, LLMCompiler, RouteLLM, LLMLingua, GPTCache and write comparison table. | `nearest-neighbor-comparison.md`, `claim-bank.md` | `open` |
| `WP-010` | `literature` | StateDrop lacks direct LLM-literature support. | Treat StateDrop as promising and cite prompt pruning only indirectly. | Add program-analysis/liveness/slicing anchors and align rule explanation to them. | `citation-gap-list.md`, `related-work-map.md`, section brief | `open` |
| `WP-011` | `experiment` | Main venues want end-to-end and overhead evidence. | Add a focused "needed for ATC/MLSys" experiment plan. | Run representative multi-rule workload plus overhead/latency-tail measurement. | `experiment-priority-board.md`, `experiment-run-log.md` | `open` |
| `WP-012` | `analysis` | Evaluation should handle stochasticity. | Add uncertainty/repeated-run requirements to analysis plan. | Run repeated trials or paired bootstrap where row-level artifacts allow it. | `statistical-analysis-plan.md`, `results-ledger.md` | `open` |

## Execution Rule

Every completed weak-point item must update at least one durable artifact. A chat answer alone does not close a weak point.
