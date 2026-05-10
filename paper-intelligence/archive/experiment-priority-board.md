---
title: Experiment Priority Board
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Experiment Priority Board

Rank experiments by paper value per token/dollar/hour. Candidate experiments must close a specific `GAP-###`.

| ID | Status | Gap Closed | Experiment | Expected Paper Value | Estimated Cost | Runtime/Risk | Command | Output Artifact | Stop Condition |
|---|---|---|---|---|---|---|---|---|---|
| `EXP-001` | `candidate` | `GAP-002` | Finish missing StateDrop n=50 config row | high if StateDrop table needs full matrix | low-to-medium | may require API calls and fixture availability | derive from `bench.optimizer_ablation bench.agents.iterative_refiner` | updated StateDrop CSV | stop if cost/rate limits exceed plan |
| `EXP-002` | `candidate` | `GAP-003` | Finish remaining real HotpotQA configs | medium-to-high for reviewer pushback | medium | API cost and Tier-1 rate limits | derive from `bench.optimizer_ablation bench.agents.hotpot_qa` | completed HotpotQA matrix | stop if partial matrix is sufficient for venue |
| `EXP-003` | `candidate` | `GAP-004` | Add paired accuracy/McNemar analysis | high for reviewer confidence | low if existing paired data available | may require stored per-task outputs | TBD | statistical-analysis note | stop if paired outputs unavailable |
| `EXP-004` | `candidate` | `GAP-005` | Stronger StateDrop quality metric | medium | medium | requires new evaluator or LLM judge | TBD | new diagnostic result | stop if venue does not need stronger StateDrop claim |
| `EXP-005` | `candidate` | `GAP-004` | Repeat ModelDowngrade robustness pass | medium | medium | API cost; stochasticity | TBD | robustness CSV | stop if current venue is workshop/positioning-focused |
| `EXP-006` | `candidate` | `GAP-011` | End-to-end multi-rule workload where several rewrites can fire together | very high for MLSys/ATC | medium-to-high | workload design risk plus API cost | TBD | end-to-end optimizer result ledger entry | stop if near-term lane is only literature/positioning |
| `EXP-007` | `candidate` | `GAP-015` | Measure interception/planner overhead and latency-tail effects | very high for systems venues | low-to-medium | needs stable harness; may be local-only | TBD | overhead/latency note | stop after overhead bounds are credible |
| `EXP-008` | `candidate` | `GAP-012` | Baseline comparison plan for RouteLLM/FrugalGPT/LLMLingua-style alternatives | high for related-work defense | variable | some baselines may be discussion-only or hard to run | TBD | baseline feasibility matrix | stop when runnable vs cite-only baselines are classified |
| `EXP-009` | `candidate` | `GAP-014` | Repeated-run or paired-bootstrap reliability pass for headline results | high for behavior-preservation claims | medium | API cost if reruns are needed; row-level data if analysis-only | TBD | uncertainty/reliability table | stop if existing artifacts support paired analysis without reruns |

## Priority Rule

Do not run experiments just because tokens are available. Run them when they close a named gap for a target venue or a human writing brief.
