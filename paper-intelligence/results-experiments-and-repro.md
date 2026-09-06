---
title: Results, Experiments, and Repro
status: active
last-updated: 2026-09-06
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

The strongest recent quality evidence is a **fixed model × rewrite diagnostic**, not adaptive joint-policy success. Its complete paired matrix and provenance are summarized immediately below. The subsequent public-workload run stopped at a timeout and remains unscored; its partial archive and full uncertainty are retained. Historical warmup-corrected rule-level results retain their existing scope and caveats; they are not substitutes for the missing native admission and learned-policy comparison. The original USD50 campaign and all failed-attempt uncertainty remain authoritative.

### Verified typed fixed factorial: opportunity, not adaptive proof

The [acquisition archive](../bench/repro/openrouter-typed-factorial-2026-09-05/README.md) contains all 384 calls, 384 intents, 32 generated development tasks, and a complete financial reconstruction. Native rule materialization performs ContextCompress at filter and StateDrop at answer, without invoking optimizer selection/admission. Each configuration executes its own three-stage history. No output is selected by the evaluator or repaired from gold.

| Fixed configuration | Exact final answers | Paid cost for all 32 workflows |
|---|---:|---:|
| Sonnet original (S0) | 30/32 | USD0.847623 |
| Sonnet + CC/SD (SR) | 29/32 | USD0.329367 |
| Haiku original (H0) | 25/32 | USD0.282515 |
| Haiku + CC/SD (HR) | 31/32 | USD0.109783 |

All 128 workflows are application-valid; there are no failed provider calls, extra attempts, new uncertainty holds, or reported cached tokens. Actual materialization counts are 64 CC, 64 SD, and 256 stages without a rule. Total stage cost is USD1.569288. Application validity is not task correctness.

Within-model rewriting gives Sonnet 0 gains/1 loss/31 ties and Haiku 7 gains/1 loss/24 ties. The mean paired quality interaction `(HR−H0)−(SR−S0)` is +21.875 percentage points, with descriptive 95% whole-task bootstrap interval [3.125, 43.75]. There are **32 task clusters**, one three-call workflow realization per task/configuration in this primary run, not 128 independent tasks. No confirmatory test or damage certificate follows from this interval.

HR costs 87.048% less than S0. The mean log-cost-ratio interaction is about 0.0000368262, essentially zero at the observed scale. Thus the cost reduction does not establish extra joint cost synergy beyond input reduction and model prices. The best observed aggregate configuration is already fixed HR; the result does not show that learning or adaptive selection improves on it. Latency is observational.

Provenance: acquisition source `7f65733`, archive `acd3334`, manifest `4e30338f35205059a2b3d5b32fe41ee2b771b4f35eeea18bc194b5730b2ac148`, frozen native materializer `b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122`. Independent review `bd-323l.6.13.8.6.20.11.3` reconstructed every request, own-history path, native deletion, response, and bill. The completion analysis digest is `962b12a8e2f1f27ab9322c42e19f5758986875df777cb79ebf5b5d6d2f5adc74`; its whole-campaign snapshot may change on later analysis, while the manifest, fixture, calls, intents, and completion summary remain fixed.

The [verified v2 figures](../bench/repro/openrouter-typed-factorial-figures-v2-2026-09-05/README.md) retain all 32 task values and use the canonical bootstrap seed. The initial render is preserved as superseded provenance because it used a different seed; do not mix its interval with the canonical analysis.

The [post-hoc error trace](../bench/repro/openrouter-typed-factorial-error-trace-2026-09-05/README.md), source `f0a41c3`, derives stage answers from public records and preserves all cells. All 128 filters are correct. Both Haiku cells make the same incorrect join on T19; twelve other final errors occur with correct upstream facts. H0's T19 final answer happens to match gold while violating its own synthesis policy; HR follows that incorrect synthesis faithfully and is wrong at the final metric. Original final scores are unchanged. The trace localizes observed differences, but is not a randomized stage intervention or evidence about hidden model reasoning.

A single complete matched repeat was predeclared at primary call 195/384 before quality inspection (`bd-323l.6.13.8.6.20.13`). It uses the same tasks/order and fresh own histories in a later provider time block. Public-data generalization and independent CC/SD controls have a [separate prospective design](research-inbox/2026-09-05-public-hotpot-typed-followup.md); no result is implied by that plan.

### Verified matched repeat: same questions, two workflow realizations

The [repeat archive](../bench/repro/openrouter-typed-factorial-repeat-2026-09-05/README.md) completed another 384 calls and independently passed exact raw, native, own-history, financial, and paired-cluster reconstruction (`bd-323l.6.13.8.6.20.13.5`). S0/SR/H0/HR score **30/30/25/31 out of 32**, at exactly the same four costs as the primary. All 384 provider payloads match their primary counterparts; 383 raw output strings match. Only SR's answer on T13 changes from wrong M70 to correct M50. There are 768 distinct generation IDs, no reported cached input tokens, and no extra or failed attempts.

The repeat quality interaction is +18.75 points. The two-realization mean is +20.3125 points, with descriptive 95% task-cluster interval [3.125, 37.5]; repeat-minus-primary is −3.125 points, interval [−9.375, 0]. Resampling retains both full realizations and all four configurations inside each of the **32 distinct task clusters**, not 64 independent questions. The identical-payload observation allows a concrete reproducibility comparison here, but does not separate provider/time effects or certify immutable model weights.

Stage cost is USD1.569288, both matrices USD3.138576, and both plus the feasibility pilot counted once USD3.508295. All charges and historical uncertainty remain in the original campaign. At repeat completion, USD21.61855947 known plus USD6.259659 retained uncertainty equals USD27.87821847 committed; pending is zero. The corresponding USD22.12178153 remaining is a dated snapshot, not a standing authorization to ignore later spending.

Repeat acquisition freeze `351cd64`, archive `0df301b`, manifest `ea2778d363317a6d8b4cbad377cbc86d6c1fa8a38390cd6f82c55226113ffe2e`, protocol `0a931035f6a6d7f5f3858b28f7ffbc2e67712a4b463843a2709c0b9f1b99eb1c`, canonical paired report `2b80e2c2dfc599bc6d8850adad2da0fffd9f74d6d3253cccbd336f725034b607`. All one-shot claims are preserved; no third repetition is allowed. The result supports repeatability of the fixed opportunity, not adaptive-controller superiority, independent confirmation, native admission, or a damage guarantee.

### Public model × StateDrop follow-up: partial, no quality result

The [public archive](../bench/repro/openrouter-public-model-sd-2026-09-06/README.md), source `51a47a9`, freeze `b31d1f3`, terminal archive `b938741`, uses 32 primary and six disjoint pilot questions selected by ID before selected text or labels were read. [Dataset provenance](../bench/repro/hotpot-train-typed-2026-09-05/README.md) pins the HotpotQA training revision, full original paragraphs, exclusions, official evaluator, and separate label artifact. This is public training-set development data, not a new held-out confirmation.

Native preflight found ContextCompress applicable on zero of 38 inputs, without padding or changing thresholds. The four-cell scope was frozen before any public call: S0/SR/H0/HR, operationally model × answer-stage StateDrop, with CC attempts retained as recorded no-ops. It cannot identify a CC effect or CC×SD interaction. The separately frozen 36-call format/resource pilot passed without reading labels, costing USD0.095678.

The primary completed **229/384 scheduled calls and 230 intents**, all completed calls structurally valid, before attempt 230 timed out after 120 seconds at 00:28:35 UTC on September 6. It was Sonnet SR/synthesize for `hotpot_5ac2d102554299657fa29080`, with no response or charge receipt. Known primary spend is USD0.561710, plus the full unresolved-attempt allowance USD6.015360. Family known spend, including the pilot once, is USD0.657388. Completed input/output tokens are 208,054/14,058; no cached input tokens were reported. Partial-prefix materialization counts are 39 CC abstentions, 38 SD applications, and 153 no-rule intents; these are not balanced-cell efficacy contrasts.

Main and independent audit `bd-323l.6.13.8.6.20.14.2.5` reconstructed every saved request, native deletion, own-history state, provider receipt, and original-ledger charge, with no missing/extra results or retry. The [diagnostic analysis](../bench/repro/openrouter-public-model-sd-2026-09-06/analysis.json) has `schedule_complete=false`, `labels_read=false`, zero scored paired tasks, no intervals, and no efficacy claim. It was generated with label/credential access forbidden. No public efficacy figures were rendered. The timeout is an operational failure, not evidence for or against the generated-workload quality interaction.

Manifest canonical SHA is `b5b1769028a2b81d509907a1097838802b65e1efb214c3bffda0238a5a8750f3`; analysis raw-file SHA is `b96ee8e56ddeda88ee3acc16a15f29a72b5fd6c070e2a09fd318ff6b3ac58a3b`. [Terminal metadata](../bench/repro/openrouter-public-model-sd-2026-09-06/terminal-snapshot.json) pins the 19,340-event original-ledger prefix and raw artifacts. [Permanent retirement](../bench/repro/openrouter-public-model-sd-2026-09-06/retirement.json), appended at 00:33:56 UTC after review, preserves the full allowance and forbids retry/resume. The pre-retirement summary/report remain unchanged; historical replay needs that exact prefix, with later full-ledger accounting reported separately (`bd-8qlq`).

At retirement, the original campaign contained **USD22.27594747 known charges plus USD12.275019 retained uncertainty**, zero pending or unresolved calls: USD34.55096647 committed, USD15.44903353 remaining. Retirement changes dispatch eligibility, not the financial commitment. These are dated snapshots, never a fresh USD50 allowance.

### Earlier admission failure and accounting scope

The closed native QA study at archive `48e6358` (`exp/joint-input-rules`, manifest `dcb703739e460810825f0966c06371d2848a4d17346afbfea4e77c52a0548ace`) completed 1,372 paid calls at USD7.048348 but did not satisfy its frozen admission gate: no actually selected joint plan had 20 exact source-paired observations. None of its 48 protected QA questions was dispatched. This is an admission/learning-feasibility limitation, not a held-out quality failure or success, and the fixed typed factorial does not supersede it.

Earlier formatting/provider-metadata pilots remain failed or partial under their original contracts. Their charges and uncertainty are not erased when a prospective adapter changes. Immediately after the first factorial, the original campaign contained USD20.04927147 known charges plus USD6.259659 retained historical uncertainty, with zero pending: USD26.30893047 conservatively committed. This is a dated accounting snapshot, **not the current available balance**. Subsequent runs must reconcile the original ledger, never start a new USD50 allowance.

### Historical rule-level evidence

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

### Protected native profiling checkpoint (not paper evidence)

The separate September 6 protected-UNKNOWN prototype (`ac436255`, independent
review `bd-323l.6.13.8.6.20.5.1.3.2.1`) passes 300 unit and 31 integration tests
plus all-targets Clippy. It adds explicit exploration-only capability facts and
policy-bound plan identities without supplying a numeric input-token bound.
Warm/restarted UNKNOWN profiles never enable primary selection: the exact
reference remains primary. The reviewed source-first budget groups (`193caf8`)
are a separate no-network accounting prerequisite. Neither result establishes
provider-backed learning, a context-fit certificate, a damage guarantee, or
superiority to fixed HR. Default per-site exploration budgets and per-plan
evidence floors must both be satisfied; probe allowance cannot be confused with
the number of observations for a particular joint plan.

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

The following table is historical provenance using retired IDs. Current tasks, dependencies, and follow-ups are authoritative in Beads; do not mint new `EXP` IDs or maintain a parallel task queue here.

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

Current negative/limiting evidence includes the native admission failure above, the public factorial timeout and unscored partial matrix, earlier schema/format acquisition failures with retained cost, the essentially zero relative cost interaction in the fixed factorial, and the historical greedy-versus-composed non-difference. They must accompany positive results rather than be treated as unrecorded or discarded experiments.

| ID | Status | Type | Item | Decision |
|---|---|---|---|---|
| `NEG-001` | retired placeholder | historical | No substantive result was assigned to this legacy identifier. | Preserve the identifier; new failure evidence uses artifacts and Beads, not new `NEG` IDs. |

## Reproduction Commands

These are historical workload commands, not permission to resume a closed campaign or rebuild a protected fixture. They may spend API money when real keys are configured. Before running any command, identify its Bead, frozen protocol/source hashes, expected output path, git SHA, dirty-tree state, model/provider, original campaign ledger, pricing assumptions, and stop condition. Paid one-shot studies may only be analyzed after completion or failure; follow their own reviewed reproduction instructions.

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

- immutable source artifacts, canonical hashes, and a Bead are identified; do not mint a retired `RES` identifier;
- git SHA and dirty state are recorded;
- command, env vars, model, temperature/seed, dataset/fixture, and expected row count are recorded;
- CSV headers and row counts pass validation;
- partial matrices are explicitly labeled `partial`;
- accuracy deltas include sample size and caveat;
- cost savings and token savings are reported separately;
- interpretation is linked to the evidence and current Beads; historical `CLM`, `GAP`, and `STAT` references may be preserved but not newly minted.

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
