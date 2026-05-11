---
title: Results, Experiments, and Repro
status: active
last-updated: 2026-05-11
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

The evidence base is substantially stronger after the V2 experimental campaign (2026-05-10/11). The core headline pair (`RES-001`, `RES-002`) remains the cleanest per-rule savings story. V2 adds three validated contributions: the LLMLingua-2 comparison with exact paired statistics (`RES-007`), a natural-prose generalization that confirms ContextCompress correctly abstains when the structural precondition is absent (`RES-008`), and a planner ablation showing the CompositionPlanner avoids a concrete V1 greedy error (`RES-010`). Overhead is now measured across 1,818 plan decisions (`RES-013`).

`RES-004` (StateDrop n=50) has **contaminated cost columns** — use only the per-task accuracy data from that run; headline StateDrop savings come from `RES-003`. `RES-006` remains a diagnostic only.

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
| `RES-007` | headline-ready | `ContextCompress` vs LLMLingua-2 | HotpotQA distractor | 100 | `gpt-4o-mini` | `bench/paper_results/agentc_hotpot_n100.csv`, `llmlingua_accuracy_n100.csv` | CC: 68%→100% (BB=68 BF=0 FB=32 FF=0, McNemar exact p=4.7×10⁻¹⁰); LLMLingua-2: 68%→53% (BB=51 BF=17 FB=2 FF=30, p=0.0013); LLMLingua-2 53.1% token reduction, 11,400ms avg overhead | dual-regime LLMLingua comparison; CC favorable-fixture half | Fixture designed with injected distractors — favorable for IDF. Must be paired with RES-008. |
| `RES-008` | canonical | `ContextCompress` abstention | Wikipedia QA (natural prose) | 39 | `gpt-4o-mini` | `bench/paper_results/wikipedia_qa_comparison.csv` | CC: 94.9%→94.9% (BB=37 BF=0 FB=0 FF=2, p=1.0, abstained entirely); LLMLingua-2: 94.9%→97.4% (BB=37 BF=0 FB=1 FF=1, p=1.0, 53.5% reduction, 13,678ms overhead) | dual-regime natural-prose half; confirms CC abstains when structural precondition absent | n=39 (SE ≈ 3.5pp); model already at 94.9% baseline leaving minimal headroom. |
| `RES-009` | canonical | `ContextCompress` + `StateDrop` composition | `multirule_qa` (n=30) | 30 | `gpt-4o-mini` | `bench/paper_results/cc_sd_composition.csv` | baseline 43.3% acc, 379,880 input tokens; CC-only: +6.7pp, 33.1% token savings; SD-only: +3.3pp, 0.1% savings; CC+SD: +6.7pp, 21.7% savings (65.3% of additive ideal) | V2 orthogonality gate validation; multi-rule activation evidence | No accuracy delta is significant (SE ≈ 9pp). Sub-additivity is gate behavior, not a failure. |
| `RES-010` | headline-ready | CompositionPlanner ablation (V1 vs V2) | `composition_qa` | 50 | `gpt-4o-mini` | `bench/paper_results/planner_ablation.csv` | baseline 32%; V2-CC: +12pp (p=0.0412*); V1-CC+OB: −2pp (p=1.0, BF=1 — greedy wrong pick); V2-CC+OB: +0pp (p=1.0 — gate corrects) | V2 correctness claim: orthogonality gate avoids greedy composition error | V2-CC+PD dropped (model drift at temp=0 makes shared-baseline impossible). Borderline p=0.0412 at n=50. |
| `RES-011` | canonical | Agent diversity / rule activation rates | `rag_summarizer` + `autogen_bridge` | 63 + 83 optimizer calls | `gpt-4o-mini` | `bench/paper_results/agent_diversity.csv` | rag_summarizer: CC 54.0%, SD 9.5%, 1 composed (1.6%); autogen_bridge: CC 30.1%, SD 24.1% | Multi-rule activation on real-agent traces; GAP-011 closed | Activation rates, not accuracy; both agents require explicit state instrumentation for SD. |
| `RES-012` | canonical | `StateDrop` isolation, paired | `iterative_refiner` | 50 | `gpt-4o-mini` | `bench/paper_results/iterative_refiner-statedrop-n50-paired.per_task.csv` | baseline 100%; SD-only 98% (−2pp, p=1.0, BF=1 FB=0); all 11 configs fail to reject McNemar at α=0.05 | Paired accuracy evidence for StateDrop | **Cost columns in the aggregate CSV are contaminated** (cross-process DB writes); use RES-003 for savings numbers. Per-task accuracy is from stdout and is clean. |
| `RES-013` | headline-ready | Optimizer overhead | plan_audit (1,818 decisions) | 1,818 | n/a | `bench/paper_results/optimizer_overhead.txt`, `bench/paper_results/overhead_scaling.csv` | pass-through p50=76µs, p95=13ms, p99=21ms (bimodal: first-call load); rewrite p50=120µs, p99=1.2ms; overhead scales sub-linearly 4KB→64KB (p50: 0.33ms→0.71ms) | Overhead claim: three orders of magnitude below LLM round-trip latency | p99 tail from SQLite cold-start loads; steady-state is sub-millisecond. |
| `RES-014` | canonical | Cold-start curve | `ContextCompress` single call site | 20 obs | `gpt-4o-mini` | `bench/paper_results/coldstart_curve.csv` | PassThrough at obs 0–2; first fire at obs=3 (hot_threshold=3); savings stable by obs=5; max projected savings $0.0048 | Hot-threshold gate verification | Synthetic single-site measurement; not a real-trace result. |

## Interpretation Rules

- Use `RES-001` and `RES-002` as the cleanest per-rule savings evidence.
- Use `RES-007` and `RES-008` together as the dual-regime LLMLingua comparison — never cite `RES-007` alone.
- Use `RES-009` to show multi-rule activation and sub-additive savings under the orthogonality gate; do not claim statistical significance (SE ≈ 9pp, no McNemar test rejects).
- Use `RES-010` for the V2 CompositionPlanner correctness claim (V1-CC+OB −2pp vs V2-CC+OB +0pp is the adversarial case).
- Use `RES-012` accuracy data only; **never cite its cost savings columns** (contaminated). Use `RES-003` for StateDrop cost/token savings numbers.
- Use `RES-004` only with the word `partial`; its cost columns predate the contamination bug and are usable, but the matrix is 10/11.
- Use `RES-005` as a positive gating/boundary result, not as a failed compression result.
- Use `RES-006` only if trace-query evidence is found or reproduced.
- Separate cost savings from input-token savings; pricing and provider cache behavior can make them diverge.
- Do not use "behavior-preserving" unless the metric, tolerance, and uncertainty treatment are explicit.
- McNemar exact p-values (statsmodels `exact=True`) are preferred over the continuity-corrected chi-squared approximation. `RES-007` CC p-value is 4.7×10⁻¹⁰ exact (not the earlier "p<0.0001" estimate).

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

| ID | Status | Gap closed | Experiment | Result | Remaining work |
|---|---|---|---|---|---|
| `EXP-001` | ✅ done | `GAP-002` | StateDrop n=50 full paired ablation. | `RES-012`: 11/11 configs, all McNemar fail to reject. Cost data contaminated; accuracy clean. | None. Use RES-003 for cost numbers. |
| `EXP-002` | ✅ done | `GAP-011` | End-to-end multi-rule workload (CC+SD). | `RES-009` + `RES-011`: CC fires 30–54%, SD 9–24%; CC+SD 21.7% token savings; both activate on same trace. | None. Report sub-additivity as gate behavior, not failure. |
| `EXP-003` | ✅ done | `GAP-004` + V2 planner | CompositionPlanner ablation (V1 vs V2). | `RES-010`: V1-CC+OB −2pp (wrong pick), V2-CC+OB +0pp (corrected). V2-CC+PD dropped (model drift). | None. Four valid rows sufficient. |
| `EXP-004` | ✅ done | design verification | Cold-start curve. | `RES-014`: first fire at obs=3, stable by obs=5. | None. |
| `EXP-005` | ✅ done | `GAP-002` | StateDrop isolation (paired). | `RES-012`: SD-only 98% vs baseline 100%, p=1.0. | None. |
| `EXP-006` | ✅ done | `GAP-014` | Paired McNemar / bootstrap CI across experiments. | Done for `RES-007`, `RES-008`, `RES-009`, `RES-010`, `RES-012`. Exact statsmodels tests used. | Apply exact p-values in paper draft (draft-paper-edits.md §11 has updated numbers). |
| `EXP-007` | ✅ done | `GAP-015` | Optimizer overhead measurement. | `RES-013`: p50=76µs pass-through, 120µs rewrite, p99=21ms tail. 1,818 plan decisions. | None. Paper-ready paragraph in `bench/paper_results/optimizer_overhead.txt`. |
| `EXP-008` | ✅ done | `GAP-012` (compression only) | LLMLingua-2 direct baseline. | `RES-007` + `RES-008`: dual-regime comparison complete. | Routing (RouteLLM/FrugalGPT) and caching (vCache) baselines remain cite-only for now. |
| `EXP-009` | open | `GAP-005` | Stronger StateDrop quality metric. | — | Low priority unless venue requires it. Current metric (substring match) is lenient but consistent. |
| `EXP-010` | open | ModelDowngrade composition | MD+CC composition at adequate n. | MD+CC n=20 too underpowered (SE ≈ 11pp). Needs n≥100 on gpt-4o base without rate-limit issues. | Blocked by Tier-1 30K TPM ceiling on gpt-4o. Future work. |

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
