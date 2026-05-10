---
title: Statistical Analysis Plan
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Statistical Analysis Plan

This file tracks uncertainty and paired-test plans for Agentc results.

## Current Needs

| ID | Status | Need | Related Results | Next Action |
|---|---|---|---|---|
| `STAT-001` | `open` | Accuracy deltas need standard-error framing. | `RES-001`, `RES-002`, `RES-004`, `RES-005` | Compute or verify SE values from pass counts. |
| `STAT-002` | `open` | Paired binary tests may strengthen accuracy claims. | all shared-baseline ablations | Determine whether per-task paired outputs are available. |
| `STAT-003` | `open` | Cost savings and input-token savings need separate interpretation. | `RES-003`, `RES-004` | Explain output-token stochasticity and deterministic input-token signal. |
| `STAT-004` | `open` | Stochastic optimizer evaluation needs reliability framing, not only one sampled run. | headline results and any judge-based future eval | Decide whether repeated trials, pass^k-style reporting, or paired bootstrap are feasible. |
| `STAT-005` | `open` | Any LLM-as-judge quality metric needs bias controls. | future StateDrop/quality eval | Verify MT-Bench/AlpacaEval/length-control sources before using judge scores. |

## Reporting Rules

- Never report an accuracy delta without sample size.
- Treat small deltas as noise unless uncertainty analysis supports stronger wording.
- Separate price-ratio savings from token-count savings.
- Mark partial matrices as partial in all result interpretation.
- Avoid "behavior-preserving" unless the metric, tolerance, and uncertainty treatment are explicit.
- If repeated trials are unavailable, say the result is a controlled ablation result, not a stochastic reliability proof.
