---
title: Results, Experiments, and Repro
status: active
last-updated: 2026-05-09
owner: paper-intelligence
---

# Results, Experiments, and Repro

This is the authoritative evidence ledger for current AgentC paper work. It answers:

1. What experiments do we already have?
2. What do they actually prove?
3. What artifacts support them?
4. What should be run next?
5. What reproducibility/statistical caveats apply?

Supersedes:

- `results-ledger.md`
- `experiment-priority-board.md`
- `experiment-run-log.md`
- `artifact-inventory.md`
- `artifact-evaluation-plan.md`
- `reproduction-commands.md`
- `result-validation-checklist.md`
- `statistical-analysis-plan.md`
- `negative-results-ledger.md`
- `experiments/README.md`

## Evidence Verdict

The current evidence is real but narrow. `RES-001` and `RES-002` are the cleanest headline savings results. `RES-004` supports `StateDrop` only with partial-matrix caveats. `RES-005` is best read as an activation-boundary diagnostic: real HotpotQA does not trigger much compression, and that is useful because it shows the rule declines near its gate. `RES-006` is a diagnostic oracle/compression-headroom result, not proof that the automated optimizer reaches oracle-level compression.

Do not present `CacheHit` or `ParallelBranch` as empirically validated paper contributions until new `RES` entries exist.

## Current Results

| ID | Status | Rule | Workload | n | Model | Source | Headline numbers | Paper use | Caveats |
|---|---|---|---|---:|---|---|---|---|---|
| `RES-001` | headline-ready | `ContextCompress` | `long_context_qa` | 100 | `gpt-4o-mini` | `ART-010` | all-on: 34.455% cost savings, 34.548% input-token savings, -2.000 pp accuracy; ContextCompress-only: 34.761% cost, 34.856% input-token, -2.000 pp | headline savings result | Purpose-built long prompts; needs uncertainty framing. |
| `RES-002` | headline-ready | `ModelDowngrade` | `gaia_router` | 127 | `gpt-4o -> gpt-4o-mini` | `ART-011` | all-on: 33.499% cost savings; ModelDowngrade-only: 35.290% cost savings; near-zero input-token change | headline cost result | Savings are price-ratio driven; GAIA pass rate is low. |
| `RES-003` | canonical | `StateDrop` | `iterative_refiner` | 30 | `gpt-4o-mini` | `ART-012` | all-on: 5.944% cost savings, 9.340% input-token savings, 0.000 pp accuracy; StateDrop-only: 4.011% cost, 7.795% input-token | supporting result | Older n=30 run; paper reference prefers n=50/temp=0 framing. |
| `RES-004` | partial | `StateDrop` | `iterative_refiner` | 50 | `gpt-4o-mini` | `ART-013` | all-on: 6.000% cost savings, 9.630% input-token savings, 0.000 pp accuracy; StateDrop-off drops to 1.823% cost / 1.569% input-token | promising StateDrop evidence | Partial 10/11 matrix; metric is lenient. |
| `RES-005` | partial | `ContextCompress` | real HotpotQA | 300 | `gpt-4o-mini` | `ART-014` | all-on: 0.170% cost savings, 0.195% input-token savings, +1.333 pp accuracy; ContextCompress-off: 0.005% cost, 0.000% input-token | activation-boundary diagnostic | Partial 7/11 matrix; not headline savings. |
| `RES-006` | diagnostic | oracle compression | `hotpot_oracle` | 300 | `gpt-4o-mini` | `ART-015` | baseline passed 193/300; oracle/optimized passed 196/300; CSV costs almost identical | headroom diagnostic only | CSV alone does not encode the larger oracle-ceiling story. |

## Interpretation Rules

- Use `RES-001` and `RES-002` as the cleanest current savings evidence.
- Use `RES-004` only with the word `partial`.
- Use `RES-005` as a positive gating/boundary result, not as a failed compression result.
- Use `RES-006` only if trace-query evidence is found or reproduced.
- Separate cost savings from input-token savings; pricing and provider cache behavior can make them diverge.
- Do not use "behavior-preserving" unless the metric, tolerance, and uncertainty treatment are explicit.

## Artifact Inventory

### Source Artifacts

| ID | Path | Type | Importance | Linked IDs | Notes |
|---|---|---|---|---|---|
| `ART-001` | `paper-intelligence/references/source/agentc-paper-reference-v2.md` | md | high | `RES-001`, `RES-002`, `RES-003`, `GAP-001` | Master paper reference and experiment interpretation. |
| `ART-002` | `paper-intelligence/references/source/agentc-feedback.md` | md | medium | `GAP-001` | Feedback/research critique context. |
| `ART-003` | `paper-intelligence/references/source/agentc-response.pdf` | pdf | medium | `GAP-001` | Needs summary before claims depend on it. |
| `ART-004` | `paper-intelligence/references/source/readme-local-before-upstream.md` | md | low | none | Older local framing; triage before use. |

### Canonical Result Artifacts

| ID | Path | Importance | Linked IDs | Notes |
|---|---|---|---|---|
| `ART-010` | `bench/paper_results/long_context_qa-contextcompress-n100.csv` | high | `RES-001`, `CLM-002` | ContextCompress matrix, 11 data rows. |
| `ART-011` | `bench/paper_results/gaia_router-modeldowngrade-n127.csv` | high | `RES-002`, `CLM-003` | ModelDowngrade matrix, 11 data rows. |
| `ART-012` | `bench/paper_results/iterative_refiner-statedrop-n30.csv` | medium | `RES-003` | StateDrop n=30 matrix. |
| `ART-013` | `bench/paper_results/iterative_refiner-statedrop-n50-partial10of11.csv` | high | `RES-004`, `GAP-002` | StateDrop n=50 partial matrix. |
| `ART-014` | `bench/paper_results/hotpot_real-contextcompress-n300-partial7of11.csv` | high | `RES-005`, `GAP-003` | Real HotpotQA ContextCompress partial matrix. |
| `ART-015` | `bench/paper_results/hotpot_oracle-n300.csv` | high | `RES-006` | Oracle/manual-compression baseline. |

### Core Implementation Artifacts

| ID | Path | Importance | Linked IDs | Notes |
|---|---|---|---|---|
| `ART-020` | `crates/agentc-optimizer/src/planner.rs` | high | `CLM-001` | Planner hot threshold, rule ranking, pass-through behavior. |
| `ART-021` | `crates/agentc-optimizer/src/rules/context_compress.rs` | high | `CLM-002` | ContextCompress implementation. |
| `ART-022` | `crates/agentc-optimizer/src/rules/model_downgrade.rs` | high | `CLM-003` | ModelDowngrade implementation. |
| `ART-023` | `crates/agentc-optimizer/src/rules/state_drop.rs` | high | `CLM-004` | StateDrop implementation. |
| `ART-024` | `python/agentc/_intercept.py` | high | `CLM-001` | Python interception flow. |
| `ART-025` | `python/agentc/_optimizer.py` | high | `CLM-001` | Python optimizer FFI shim. |

### Paper-Intelligence Artifacts

| ID | Path | Importance | Linked IDs | Consolidation status |
|---|---|---|---|---|
| `ART-040` | `paper-intelligence/archive/agentc-paper-intelligence-workplan.md` | high | none | archived after consolidation. |
| `ART-041` | `paper-intelligence/archive/deep-research-prompt-templates.md` | high | none | merged into `research-prompts.md`. |
| `ART-042` | `paper-intelligence/README.md` | high | none | rewrite as entry point. |
| `ART-043` | `paper-intelligence/AGENTS.md` | high | none | rewrite as maintenance guide. |
| `ART-044` | `paper-intelligence/archive/pizza-import-plan.md` | medium | `DEC-003` | archived as process history. |
| `ART-045` | `paper-intelligence/archive/reviewer-risk-register.md` | high | `RR-001` | merged here. |
| `ART-046` | `paper-intelligence/archive/weak-point-resolution-plan.md` | high | `WP-001` | merged here. |
| `ART-047` | `paper-intelligence/archive/red-team-review-prompts.md` | medium | `RR-001` | merged into `research-prompts.md`. |
| `ART-048` | `paper-intelligence/archive/paper-angle-matrix.md` | high | `ANG-001` | merged into `strategy-and-venues.md`. |
| `ART-049` | `paper-intelligence/archive/section-briefs/` | high | `CLM-001`, `RES-001` | merged into `strategy-and-venues.md`. |
| `ART-050` | `paper-intelligence/archive/citation-style-and-hygiene.md` | medium | `CIT-001` | merged into `evidence-and-sources.md`. |
| `ART-051` | `paper-intelligence/research-inbox/2026-05-09-literature-map.md` | high | `DRP-001`, `LIT-002`, `GAP-010` | raw/provenance, keep in inbox. |
| `ART-052` | `paper-intelligence/research-inbox/2026-05-09-venue-research.md` | high | `DRP-002`, `VEN-001`, `VEN-009` | raw/provenance, keep in inbox. |
| `ART-053` | `paper-intelligence/archive/style-guide.md` | medium | `CIT-002`, `RR-009` | merged relevant style rules into AGENTS/README. |
| `ART-054` | `paper-intelligence/archive/literature-review-section-plan.md` | high | `LIT-002`, `GAP-009` | merged into `literature-and-nearest-neighbors.md`. |
| `ART-055` | `paper-intelligence/research-inbox/2026-05-09-post-june-venue-plan.md` | high | `DRP-003`, `VEN-001`, `VEN-009`, `VEN-010` | raw/provenance, keep in inbox. |
| `ART-056` | `paper-intelligence/archive/literature-blurb-todo.md` | high | `LIT-002`, `LIT-040`, `GAP-009`, `GAP-010`, `GAP-012`, `GAP-013`, `GAP-014`, `GAP-016` | archived; superseded by verified/consolidated blurbs. |
| `ART-057` | `paper-intelligence/research-inbox/2026-05-09-full-literature-review-map-v2.md` | high | `DRP-004`, `LIT-040`, `GAP-010`, `GAP-012`, `GAP-013`, `GAP-014` | raw/provenance, keep in inbox. |
| `ART-058` | `paper-intelligence/archive/literature-verified-blurbs.md` | high | `LIT-002`, `LIT-070`, `GAP-009`, `GAP-010`, `GAP-012`, `GAP-013`, `GAP-014` | merged into `literature-and-nearest-neighbors.md`; archived as provenance. |
| `ART-059` | `paper-intelligence/current-fit-and-publishability.md` | high | `RES-001`, `RES-002`, `RES-005`, `LIT-008`, `LIT-024`, `LIT-040`, `GAP-010`, `GAP-011`, `GAP-014` | keep active. |

## Experiment Queue

| ID | Gap closed | Experiment | Paper value | Current command status | Stop condition |
|---|---|---|---|---|---|
| `EXP-001` | `GAP-002` | Finish missing `StateDrop` n=50 config row. | high if StateDrop table needs full matrix | derive from `bench.optimizer_ablation bench.agents.iterative_refiner` | Stop if StateDrop is not a headline claim. |
| `EXP-002` | `GAP-003` | Finish remaining real HotpotQA configs. | medium-to-high for pushback | derive from `bench.optimizer_ablation bench.agents.hotpot_qa` | Stop if partial matrix is enough for activation-boundary story. |
| `EXP-003` | `GAP-004` | Add paired accuracy/McNemar analysis. | high | needs per-task outputs | Stop if paired outputs unavailable. |
| `EXP-004` | `GAP-005` | Stronger `StateDrop` quality metric. | medium | needs evaluator or judge | Stop if venue does not need stronger StateDrop. |
| `EXP-005` | `GAP-004` | Repeat ModelDowngrade robustness pass. | medium | API rerun likely | Stop if target is workshop/positioning. |
| `EXP-006` | `GAP-011` | End-to-end multi-rule workload where several rewrites can fire. | very high | needs workload design | Stop only if near-term lane is not systems/MLSys/ATC. |
| `EXP-007` | `GAP-015` | Measure interception/planner overhead and latency tails. | very high for systems venues | needs local harness | Stop after credible overhead bounds. |
| `EXP-008` | `GAP-012` | Baseline feasibility matrix for RouteLLM/FrugalGPT/LLMLingua-style alternatives. | high | analysis first, experiments second | Stop when runnable vs cite-only is classified. |
| `EXP-009` | `GAP-014` | Repeated-run or paired-bootstrap reliability pass. | high | analysis if row data exists; API rerun otherwise | Stop when headline uncertainty is reportable. |

## Statistical Needs

| ID | Need | Related results | Next action |
|---|---|---|---|
| `STAT-001` | Accuracy deltas need standard-error framing. | `RES-001`, `RES-002`, `RES-004`, `RES-005` | Compute/verify SE values from pass counts. |
| `STAT-002` | Paired binary tests may strengthen accuracy claims. | shared-baseline ablations | Determine whether per-task paired outputs exist. |
| `STAT-003` | Cost savings and input-token savings need separate interpretation. | `RES-003`, `RES-004` | Explain output-token stochasticity and deterministic input-token signal. |
| `STAT-004` | Stochastic optimizer evaluation needs reliability framing. | headline results and future judge eval | Decide repeated trials, pass^k, or bootstrap feasibility. |
| `STAT-005` | LLM-as-judge metrics need bias controls. | future quality eval | Verify judge-bias/length-control sources first. |

## Artifact Evaluation Status

| ID | Artifact | Verifies | Release/rerun notes |
|---|---|---|---|
| `AE-001` | `bench/paper_results/*.csv` | canonical current results | committed, but validation metadata should be added before submission. |
| `AE-002` | benchmark scripts | reproduction path | API keys and fixtures required for full reruns. |
| `AE-003` | paper-intelligence references | paper context | source docs can stay tracked unless sensitivity/licensing concerns appear. |

## Negative Results

| ID | Status | Type | Item | Decision |
|---|---|---|---|---|
| `NEG-001` | not populated yet | none | No negative results recorded yet. | Future failed searches, rejected angles, and undercutting results should land here. |

## Reproduction Commands

These commands may spend API money when real keys are configured. Before running any command, identify the `EXP-###`, expected output path, git SHA, dirty-tree state, model/provider, pricing assumptions, and stop condition.

```bash
python -m bench.build_hotpot_fixture
python -m bench.build_gaia_fixture
python -m bench.build_long_context_fixture
python -m bench.optimizer_bench bench.agents.long_context_qa
BENCH_MAX_TASKS=100 python -m bench.optimizer_ablation bench.agents.long_context_qa
bash bench/scripts/run_paper_ablation.sh
bash bench/scripts/run_pushback_ablation.sh
bash bench/scripts/run_targeted_ablation.sh
bash bench/scripts/run_ablation.sh
BENCH_MAX_TASKS=300 python -m bench.run_oracle_baseline bench.agents.hotpot_oracle
```

## Promotion Checklist

Every new result starts as `quarantined`. Promote only after:

- source artifact and `RES-###` are assigned;
- git SHA and dirty state are recorded;
- command, env vars, model, temperature/seed, dataset/fixture, and expected row count are recorded;
- CSV headers and row counts pass validation;
- partial matrices are explicitly labeled `partial`;
- accuracy deltas include sample size and caveat;
- cost savings and token savings are reported separately;
- linked `CLM`, `GAP`, and `STAT` entries are updated.

Allowed result statuses:

| Status | Paper use |
|---|---|
| `quarantined` | none |
| `partial` | diagnostic only |
| `diagnostic` | methods/limitations/pushback |
| `canonical` | results table or appendix |
| `headline-ready` | headline result if caveats also pass |
| `needs-rerun` | gap register only |
| `do-not-use-yet` | do not cite |
