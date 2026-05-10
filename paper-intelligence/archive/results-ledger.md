---
title: Results Ledger
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Results Ledger

This ledger tracks paper-relevant experiment results. Numbers are source-grounded, but entries can still be `partial` or `diagnostic`.

## Results

| ID | Status | Rule | Workload | n | Model | Source Artifact | Headline Numbers | Caveats | Supports Claims | Related Gaps |
|---|---|---|---|---:|---|---|---|---|---|---|
| `RES-001` | `headline-ready` | `ContextCompress` | `long_context_qa` | 100 | `gpt-4o-mini` | `ART-010`, `bench/paper_results/long_context_qa-contextcompress-n100.csv` | all-on: 34.455% cost savings, 34.548% input-token savings, -2.000 pp accuracy delta; ContextCompress-only: 34.761% cost, 34.856% input-token, -2.000 pp | Purpose-built workload with HotpotQA-derived long prompts; accuracy delta needs uncertainty framing. | `CLM-002` | `GAP-004` |
| `RES-002` | `headline-ready` | `ModelDowngrade` | `gaia_router` | 127 | `gpt-4o -> gpt-4o-mini` | `ART-011`, `bench/paper_results/gaia_router-modeldowngrade-n127.csv` | all-on: 33.499% cost savings; ModelDowngrade-only: 35.290% cost savings; near-zero input-token change | GAIA pass rate is low; savings come from price ratio, not token reduction; accuracy delta needs uncertainty framing. | `CLM-003` | `GAP-004` |
| `RES-003` | `canonical` | `StateDrop` | `iterative_refiner` | 30 | `gpt-4o-mini` | `ART-012`, `bench/paper_results/iterative_refiner-statedrop-n30.csv` | all-on: 5.944% cost savings, 9.340% input-token savings, 0.000 pp accuracy delta; StateDrop-only: 4.011% cost, 7.795% input-token | n=30 older run; paper reference prefers n=50 plus temp=0 framing. | `CLM-004` | `GAP-002`, `GAP-005` |
| `RES-004` | `partial` | `StateDrop` | `iterative_refiner` | 50 | `gpt-4o-mini` | `ART-013`, `bench/paper_results/iterative_refiner-statedrop-n50-partial10of11.csv` | all-on: 6.000% cost savings, 9.630% input-token savings, 0.000 pp accuracy delta; StateDrop-off drops to 1.823% cost / 1.569% input-token | Partial 10/11 matrix; paper reference cites separate temp=0 StateDrop-only number. Cannot be a fully complete matrix until missing row is handled. | `CLM-004` | `GAP-002`, `GAP-005` |
| `RES-005` | `partial` | `ContextCompress` | `hotpot_qa` real HotpotQA | 300 | `gpt-4o-mini` | `ART-014`, `bench/paper_results/hotpot_real-contextcompress-n300-partial7of11.csv` | all-on: 0.170% cost savings, 0.195% input-token savings, +1.333 pp accuracy delta; ContextCompress-off: 0.005% cost, 0.000% input-token | Partial 7/11 matrix; useful as activation-boundary diagnostic, not headline savings. | `CLM-005` | `GAP-003` |
| `RES-006` | `diagnostic` | oracle compression | `hotpot_oracle` | 300 | `gpt-4o-mini` | `ART-015`, `bench/paper_results/hotpot_oracle-n300.csv` | baseline passed 193/300; optimized/oracle passed 196/300; baseline cost $0.014495, optimized cost $0.014492 in CSV | Paper reference says oracle ceiling and traces.db analysis show larger manual-compression story; CSV alone does not contain the full 82% claim. | `CLM-006` | `GAP-006` |

## Result Interpretation Rules

- Use `RES-001` and `RES-002` as the cleanest headline savings results.
- Use `RES-004` for StateDrop contribution, but label the matrix partial.
- Use `RES-005` as a boundary/gating result, not as a failure.
- Use `RES-006` only with clear explanation of what the CSV contains versus what trace queries establish.
- Do not claim end-to-end benchmark parity for `CacheHit` or `ParallelBranch` without new entries.
