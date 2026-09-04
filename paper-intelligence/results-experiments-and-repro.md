---
title: Results, Experiments, and Repro
status: active
last-updated: 2026-09-04
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

The evidence base is substantially stronger after the warmup-corrected campaign. The core headline pair (`RES-001`, `RES-002`) remains the cleanest per-rule savings story. The campaign also adds the LLMLingua-2 comparison with exact paired statistics (`RES-007`), a natural-prose generalization that confirms ContextCompress correctly abstains when the structural precondition is absent (`RES-008`), additive CC+SD composition (`RES-009`), and a controlled planner ablation in which first-match and composition modes have identical accuracy (`RES-010`). The historical 1,818-decision overhead result is now correctly scoped as an internal pre-audit clock. The complete-call Stage E0 diagnostics under `RES-013` supersede it for systems debugging and now include a clean progression from synchronous audit, to off-path persistence, to thread-CPU attribution, to non-blocking planner admission. The mechanism has exact post-flush row and fallback conservation, while C=8/C=32 p99 remains above the frozen target on the measured host.

The warmup-corrected `RES-004` supersedes the retracted partial StateDrop matrix. `RES-012` still supplies the separate paired accuracy check; do not use its contaminated cost columns. `RES-006` remains a diagnostic only.

Do not present `CacheHit` or `ParallelBranch` as empirically validated paper contributions until canonical evidence is added to the manifest and this ledger. The `RES` namespace is frozen.

### Joint-planner engineering checkpoint (not paper evidence)

As of 2026-09-04, the runtime path required by the narrowed literature thesis
exists end to end: supported provider adapters return the immutable reference,
run one durably leased model-and-rewrite candidate off-path, persist its exact
paired cost/latency/divergence profile, reload that evidence after restart, and
admit the plan only after the 20-pair floor. The deterministic
`bench/live_exploration_preflight.py` run records 23 reference-visible
calibration calls, 20 background candidates, and one post-restart admitted
candidate over 44 fake-provider calls. The artifact is
`bench/repro/live-exploration-preflight-2026-09-04.json` and is explicitly
`paper_evidence=false`.

This closes a mechanism gap, not the contribution-strength gap. The next
evidence step is the frozen joint-planning baseline/ablation campaign: held-out
natural workloads, route-only and rewrite-only controls, both sequential
orders, best static joint, current greedy, and AgentOpt/other feasible
baselines, with exploration and shadow cost charged. Until that result exists,
the MLSys main-track assessment remains conditional.

### Request-path engineering checkpoint (not paper evidence)

The synchronous and off-path artifacts under `RES-013` first establish that
moving SQLite persistence behind a bounded non-blocking queue reduces matched
C=32 p50 by 89--93% and raises absolute throughput by 2.6--4.0x. The subsequent
307,200-call thread-CPU matrix keeps pooled planner CPU p99 at or below 0.717 ms
in every cell even while C=32 wall p99 reaches 11.1--20.6 ms and mean off-CPU
share reaches 76--81%. A separate 92,160-call boundary control likewise records
only 40.7--68.6 us of C=32 thread CPU when wall p99 reaches 0.817--1.258 ms.
Together these runs attribute the residual primarily to host scheduling and GIL
reacquisition rather than SQLite or planner instruction count.

The runtime now applies a configurable, non-blocking planner-admission limit
before releasing the GIL. Saturated calls return the immutable reference request
with a versioned, persisted `optimizer_saturated` reason. In the clean 153,600-
call default-four-permit run, matched C=32 p50 falls another 85--98%, p99 falls
34--82%, and absolute throughput rises 1.86--3.83x relative to the attribution
run. C=32 p50 is 4--25 us and p99 is 2.33--10.19 ms; 54.6--81.4% of C=32 calls
safely abstain from optimization. All 170,700 setup-plus-measured audit attempts
are written with zero reported loss, and admission counters exactly match the
returned and persisted reasons.

This is still not a paper-level scalability result. The host was loaded at
7.13/7.15/8.17 before and 5.64/6.75/7.97 after on eight logical CPUs, every C=32
cell still misses 1.2 ms p99, and the abstention rate is a utility cost rather
than a free speedup. A quiet second-host run and a latency-versus-optimization-
coverage policy decision remain necessary.

## Current Results

| ID | Status | Rule | Workload | n | Model | Source | Headline numbers | Paper use | Caveats |
|---|---|---|---|---:|---|---|---|---|---|
| `RES-001` | headline-ready, warmup-corrected | `ContextCompress` | `long_context_qa` | 300 | `gpt-4o-mini` | `bench/paper_results/long_context_qa-contextcompress-n300-warmup.csv` | all-on: 33.9% cost and 34.0% input-token savings; ContextCompress-only: 36.1% cost and 36.3% input-token savings, +1.7pp accuracy (McNemar p=0.4244), 280/300 fires | headline savings result | Purpose-built long prompts; all paired p-values are non-significant and this does not imply broad real-task savings. |
| `RES-002` | headline-ready, warmup-corrected | `ModelDowngrade` | `gaia_router` | 127 | `gpt-4o -> gpt-4o-mini` | `bench/paper_results/gaia_router-modeldowngrade-n127-warmup.csv` | ModelDowngrade-only: 11.4% cost savings, approximately zero input-token change, 65/254 calls routed; −3.9pp accuracy, BF=7/FB=2, McNemar p=0.1797 | headline cost result | Savings are price-ratio driven; low baseline pass rate and non-significant quality uncertainty require careful framing. The earlier 35.3% cold-start result is retracted. |
| `RES-003` | canonical | `StateDrop` | `iterative_refiner` | 30 | `gpt-4o-mini` | `ART-012` | all-on: 5.944% cost savings, 9.340% input-token savings, 0.000 pp accuracy; StateDrop-only: 4.011% cost, 7.795% input-token | supporting result | Older n=30 run; paper reference prefers n=50/temp=0 framing. |
| `RES-004` | canonical, warmup-corrected | `StateDrop` | `iterative_refiner` | 50 | `gpt-4o-mini` | `bench/paper_results/iterative_refiner-statedrop-n50-warmup.csv` | StateDrop-only: 6.1% cost and 10.8% input-token savings with 0.0pp accuracy delta; StateDrop-off: 2.1% input-token savings and zero StateDrop fires | supporting isolation result | Use within-run comparisons; the metric remains lenient and the all-on accuracy delta is noisy. |
| `RES-005` | canonical activation-boundary result | `ContextCompress` | real HotpotQA | 300 | `gpt-4o-mini` | `bench/paper_results/hotpot_real-contextcompress-n300-warmup.csv` | ContextCompress fires 0–1/300 calls per configuration; savings are 0.00–0.19%, and ContextCompress-only is −2.0pp with McNemar p=0.1796 | activation-boundary diagnostic | Full 11-config warmup-corrected matrix; not a headline savings result. |
| `RES-006` | diagnostic | oracle compression | `hotpot_oracle` | 300 | `gpt-4o-mini` | `ART-015` | baseline passed 193/300; oracle/optimized passed 196/300; CSV costs almost identical | headroom diagnostic only | CSV alone does not encode the larger oracle-ceiling story. |
| `RES-007` | headline-ready | `ContextCompress` vs LLMLingua-2 | HotpotQA distractor | 100 | `gpt-4o-mini` | `bench/paper_results/agentc_hotpot_n100.csv`, `llmlingua_accuracy_n100.csv` | CC: 68%→100% (BB=68 BF=0 FB=32 FF=0, McNemar exact p=4.7×10⁻¹⁰); LLMLingua-2: 68%→53% (BB=51 BF=17 FB=2 FF=30, p=0.0013); LLMLingua-2 53.1% token reduction, 11,400ms avg overhead | dual-regime LLMLingua comparison; CC favorable-fixture half | Fixture designed with injected distractors — favorable for IDF. Must be paired with RES-008. |
| `RES-008` | canonical | `ContextCompress` abstention | Wikipedia QA (natural prose) | 39 | `gpt-4o-mini` | `bench/paper_results/wikipedia_qa_comparison.csv` | CC: 94.9%→94.9% (BB=37 BF=0 FB=0 FF=2, p=1.0, abstained entirely); LLMLingua-2: 94.9%→97.4% (BB=37 BF=0 FB=1 FF=1, p=1.0, 53.5% reduction, 13,678ms overhead) | dual-regime natural-prose half; confirms CC abstains when structural precondition absent | n=39 (SE ≈ 3.5pp); model already at 94.9% baseline leaving minimal headroom. |
| `RES-009` | canonical, warmup-corrected | `ContextCompress` + `StateDrop` composition | `multirule_qa` | 30 | `gpt-4o-mini` | `bench/paper_results/multirule_qa-ccsd-n30-warmup.csv` | CC-only: 32.54% input-token savings; SD-only: 0.06%; CC+SD: 32.78%, or 100.5% of the 32.60% additive ideal; CC-only and CC+SD are both +3.3pp (p=1.0) | additive composition and multi-rule activation evidence | Small purpose-built fixture; the slight super-additivity is rounding/workload-specific, not a general interaction claim. |
| `RES-010` | canonical control, not a planner-win claim | First-match vs composition planner | `composition_qa` | 50 | `gpt-4o-mini` | `bench/paper_results/composition_qa-planner-ablation-n50-warmup.csv` | baseline 32%; CC-only +18pp (p=0.0039); first-match CC+OB and composed CC+OB are identical at +14pp (p=0.0156) with 47 plans fired | controlled evidence that planner mode does not change accuracy on this fixture | Does not show the joint planner beating greedy. The CC+PlannerDispatch row is unrecoverable and omitted. |
| `RES-011` | canonical | Agent diversity / rule activation rates | `rag_summarizer` + `autogen_bridge` | 63 + 83 optimizer calls | `gpt-4o-mini` | `bench/paper_results/agent_diversity.csv` | rag_summarizer: CC 54.0%, SD 9.5%, 1 composed (1.6%); autogen_bridge: CC 30.1%, SD 24.1% | Multi-rule activation on real-agent traces; GAP-011 closed | Activation rates, not accuracy; both agents require explicit state instrumentation for SD. |
| `RES-012` | canonical | `StateDrop` isolation, paired | `iterative_refiner` | 50 | `gpt-4o-mini` | `bench/paper_results/iterative_refiner-statedrop-n50-paired.per_task.csv` | baseline 100%; SD-only 98% (−2pp, p=1.0, BF=1 FB=0); all 11 configs fail to reject McNemar at α=0.05 | Paired accuracy evidence for StateDrop | **Cost columns in the aggregate CSV are contaminated** (cross-process DB writes); use RES-003 for savings numbers. Per-task accuracy is from stdout and is clean. |
| `RES-013` | diagnostic, request-path progression complete; ship gate open | Optimizer complete-call overhead, tail attribution, and overload admission | fixed-shape calls, exact 4/8/16/32/64 KiB scaling at C=1/2/4/8/16/32, and three boundary controls | 828,000 complete calls + 92,160 boundary calls | n/a | `bench/repro/optimizer-e2e-overhead[-offpath-audit]-2026-09-04.{json,csv}`, `bench/repro/optimizer-e2e-scaling[-offpath-audit|-threadcpu|-admission-control]-2026-09-04.{json,csv.gz}`, `bench/repro/optimizer-boundary-scheduler-attribution-2026-09-04.{json,csv.gz}` | Off-path audit cuts matched C=32 p50 89–93% and raises throughput 2.6–4.0×. Thread-CPU p99 stays ≤0.717ms across all attribution cells while wall tails are mostly off-CPU. Default four-permit admission then cuts matched C=32 p50 another 85–98%, p99 34–82%, and raises throughput 1.86–3.83×; C=32 p50/p99 is 4–25µs/2.33–10.19ms with 54.6–81.4% safe saturation fallback. All 170,700 admission-run audit attempts persist with exact counter/reason conservation and zero reported loss. | Audit redesign, scheduler attribution, and safe-overload validation | One loaded Apple M2 host, synthetic calls, no provider/network; `paper_evidence=false`. Every C=32 cell still misses 1.2ms, overload abstention reduces optimization coverage, and zero observed loss is bounded by the documented crash-loss contract. |
| `RES-014` | canonical | Cold-start curve | `ContextCompress` single call site | 20 obs | `gpt-4o-mini` | `bench/paper_results/coldstart_curve.csv` | PassThrough at obs 0–2; first fire at obs=3 (hot_threshold=3); savings stable by obs=5; max projected savings $0.0048 | Hot-threshold gate verification | Synthetic single-site measurement; not a real-trace result. |

## Interpretation Rules

- Use `RES-001` and `RES-002` as the cleanest per-rule savings evidence.
- Use `RES-007` and `RES-008` together as the dual-regime LLMLingua comparison — never cite `RES-007` alone.
- Use `RES-009` to show additive CC+SD composition on the controlled fixture: 32.78% savings versus a 32.60% additive ideal. Do not generalize the 100.5% ratio beyond this workload.
- Use `RES-010` as a controlled non-difference: first-match and composed CC+OB have identical accuracy and activation. It is not evidence that the joint planner beats greedy.
- Use warmup-corrected `RES-004` for StateDrop within-run savings and `RES-012` only for its separate paired accuracy check; never use `RES-012` cost columns.
- Use the complete warmup-corrected `RES-005` as a positive gating/boundary result, not as a failed compression result.
- Use `RES-006` only if trace-query evidence is found or reproduced.
- Use the full `RES-013` artifact sequence as Stage E0 timing diagnostics. The fixed-shape and scaling pairs isolate the synchronous audit bottleneck; the boundary and thread-CPU controls attribute the residual to off-CPU scheduling/GIL delay; the admission run validates bounded fail-open behavior and exposes its lost-coverage cost. High-concurrency p99 still fails the frozen target. Do not promote the result to a confirmatory latency, multi-host, optimal-policy, or universal-losslessness claim.
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
| `ART-010` | `bench/paper_results/long_context_qa-contextcompress-n300-warmup.csv` | high | `RES-001`, `CLM-002` | Canonical warmup-corrected ContextCompress matrix, 11 data rows. |
| `ART-011` | `bench/paper_results/gaia_router-modeldowngrade-n127-warmup.csv` | high | `RES-002`, `CLM-003` | Canonical warmup-corrected ModelDowngrade matrix, 11 data rows. |
| `ART-012` | `bench/paper_results/iterative_refiner-statedrop-n30.csv` | medium | `RES-003` | StateDrop n=30 matrix. |
| `ART-013` | `bench/paper_results/iterative_refiner-statedrop-n50-warmup.csv` | high | `RES-004`, `GAP-002` | Complete warmup-corrected StateDrop n=50 matrix; supersedes the retracted partial artifact. |
| `ART-014` | `bench/paper_results/hotpot_real-contextcompress-n300-warmup.csv` | high | `RES-005`, `GAP-003` | Complete warmup-corrected real-HotpotQA ContextCompress matrix. |
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
| `EXP-002` | ✅ done | `GAP-011` | End-to-end multi-rule workload (CC+SD). | `RES-009`: CC-only saves 32.54% input tokens, SD-only 0.06%, and CC+SD 32.78% versus a 32.60% additive ideal; both rules activate in the composed condition. | None. Report this as additive behavior on one controlled fixture, not a universal interaction law. |
| `EXP-003` | ✅ done | `GAP-004` + planner control | First-match versus composition ablation. | `RES-010`: CC-only is +18pp; first-match CC+OB and composed CC+OB are identical at +14pp with the same 47 activations. | This is a non-difference/control result, not proof that composition beats greedy. The CC+PlannerDispatch row remains unrecoverable. |
| `EXP-004` | ✅ done | design verification | Cold-start curve. | `RES-014`: first fire at obs=3, stable by obs=5. | None. |
| `EXP-005` | ✅ done | `GAP-002` | StateDrop isolation (paired). | `RES-012`: SD-only 98% vs baseline 100%, p=1.0. | None. |
| `EXP-006` | ✅ done | `GAP-014` | Paired McNemar / bootstrap CI across experiments. | Done for `RES-007`, `RES-008`, `RES-009`, `RES-010`, `RES-012`. Exact statsmodels tests used. | Apply exact p-values in paper draft (draft-paper-edits.md §11 has updated numbers). |
| `EXP-007` | diagnostic mechanism complete; confirmation open | `GAP-015` (attribution/admission portions) | Optimizer overhead progression from synchronous audit through off-path persistence, thread-CPU attribution, and non-blocking planner admission. | `RES-013`: 828,000 complete calls plus 92,160 boundary controls. CPU p99 stays ≤0.717ms; default admission reduces matched C=32 p99 34–82% and throughput improves 1.86–3.83×, but C=32 wall p99 is still 2.33–10.19ms and saturation fallback is 54.6–81.4%. | Repeat the frozen admission sweep on a quiet second host, select/report the latency-versus-coverage policy, then execute the provider-backed confirmatory campaign. Historical 76/120µs values remain pre-audit internal timings. |
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
