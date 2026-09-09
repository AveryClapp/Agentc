---
title: Current Fit And Publishability
status: active
last-updated: 2026-09-09
owner: paper-intelligence
---

# Current Fit And Publishability

This is the short reality check for AgentC after the current experiment review and the verified literature-blurb pass.

## One-Screen Summary

**Joint-system efficacy remains UNPROVEN.** AgentC has a promising model-dependent rewrite result and completed protected profiling, but no demonstrated adaptive joint-system contribution. The decisive next comparison is a learned joint policy versus the strongest calibration-frozen fixed model-plus-rules configuration, charging warmup, probes, failures, and fallbacks. It is not ready for a confident MLSys main-track submission yet.

The working thesis is a **guarded, application-side rewrite control plane for opaque LLM-agent calls**. The broad combination of model routing and prompt/workflow optimization is already occupied. Even online quality learning, charged exploration, distribution-shift adaptation, and checking/recovery have prior art. AgentC's candidate distinction is evidence bound to exact call-site/complete-plan identities, persistent invalidation, and useful control under an executable failure/damage contract. That is a hypothesis requiring comparative evidence, not a novelty certificate.

## What Changed On September 5

The [completed fixed factorial](../bench/repro/openrouter-typed-factorial-2026-09-05/README.md) evaluates 32 generated development tasks, four configurations, and three own-history stages: 384 paid calls, with all 128 workflows application-valid. Sonnet original / Sonnet with CC+SD / Haiku original / Haiku with CC+SD score **30 / 29 / 25 / 31 out of 32**, respectively. Haiku with both rules costs 87.048% less than Sonnet original in this realization.

The primary paired quality interaction is +21.875 percentage points, with descriptive whole-task bootstrap interval [3.125, 43.75]. The cost interaction on a relative log scale is approximately 0.0000368262: token reduction multiplied by model price explains the cost benefit. Neither number demonstrates learned cost synergy.

The [predeclared full repeat](../bench/repro/openrouter-typed-factorial-repeat-2026-09-05/README.md) completed and independently passed reconstruction. Its four exact scores are 30 / 30 / 25 / 31; all costs are unchanged. All 384 transmitted payloads match between runs, and 383 raw outputs match. The sole change corrects one SR final answer. Averaging the two realizations within each of the same 32 task clusters gives a +20.3125-point quality interaction, descriptive interval [3.125, 37.5]. This strengthens reproducibility on these tasks, not public-data generalization or independent confirmation; there are still 32 distinct questions.

The [post-hoc error trace](../bench/repro/openrouter-typed-factorial-error-trace-2026-09-05/README.md) localizes most observed failures to final policy application despite correct upstream facts. The original and rewritten cells have equal parsed upstream values within each model, including an identical wrong join on one Haiku task. This motivates a stage-specific test, but does not causally isolate StateDrop: CC and SD were assigned together, and provider/time effects were uncontrolled.

This is materially better evidence of an opportunity than routing and rule activation alone. It also raises the strongest counterargument: **the fixed Haiku-plus-rules configuration already has the best aggregate quality and cost here.** An adaptive controller must beat that baseline after paying for learning. The actual native rules were materialized directly; optimizer selection, native admission, a damage guarantee, and public-workload generalization were not tested.

Separately, the earlier provider-backed native study failed its frozen admission gate after 1,372 calls: no actually selected joint plan had 20 exact source-paired observations. All 48 protected QA questions remained unused. This is a learning/admission limitation, not a held-out treatment-effect result; the successful fixed factorial does not erase it. The closed evidence is recorded in Beads `bd-323l.6.13.8.5` and archive commit `48e6358` on `exp/joint-input-rules`.

## September 6: Public Follow-Up Stopped; Native Mechanism Improved

The [public HotpotQA development follow-up](../bench/repro/openrouter-public-model-sd-2026-09-06/README.md) stopped after 229 of 384 calls when the next Sonnet request timed out. All 229 completions were structurally valid, but the frozen protocol forbids scoring a partial matrix. Labels remain unopened: there is **no public-data accuracy result**, favorable or unfavorable, and no efficacy figure. Known primary spend is USD0.561710; the unknown attempt retains its full USD6.015360 allowance. The separate 36-call format/resource pilot passed for USD0.095678.

All selected questions retained their original ten paragraphs. ContextCompress applied to zero of the 38 pilot/primary inputs, so the four-cell scope was fixed before payment as model × answer-stage StateDrop, not a CC/SD composition test. The timeout does not justify resampling these questions, resuming the closed run, or manufacturing longer contexts. The [long-public-workload research](research-inbox/2026-09-05-naturally-long-public-qa.md) identifies QASPER as a possible later natural-length workload, with distinct contract and evaluation requirements; it is not an acquired or evaluated dataset here.

The reviewed native prototype `ac436255` separates explicit protected exploration from proven context capacity. It passes 331 Rust tests and independent review, but **UNKNOWN-capacity requests still return the exact source plan as primary**, even with favorable profiles and after restart. This is a mechanism prerequisite, not native adaptive superiority. A further scheduling constraint is explicit: the default 20 probes per call site cannot supply a 20-observation floor to each of several candidate plans in one window. Any larger prospective exploration allowance must be declared and fully charged; the evidence floor and semantic guards cannot be quietly lowered to obtain admission.

## September 7: Protected Profiling Completed; Fixed Quality Interval Crosses Zero

The [protected study and post-completion analysis](../bench/repro/openrouter-protected-native-learning-2026-09-06/README.md) completed 63 generated **development** source workflows, 130 native probes, and 63 later fixed Haiku-plus-rules (HR) workflows. A separate six-task format/resource pilot contributed 36 calls. The prospective experiment declared 60 probes per site and a common 256-token output cap; the product defaults and all other numeric guards were unchanged. There was no seeded warmup or candidate output in source history, and UNKNOWN requests always returned the source as primary.

Source scores **58/63** and fixed HR **61/63**: four gains, one loss, 58 ties. The +4.7619-point paired difference has a descriptive 95% whole-task bootstrap interval **[−1.5873, 11.1111]**, including zero. Source workflow cost is USD1.666728 versus HR USD0.215898; probes cost USD0.306344 and the pilot USD0.211568, for **USD2.400538 all-in**. HR's lower fixed-workflow bill is not a saving produced by a learned policy. Source and HR ran in separate time blocks; this is not a new four-cell interaction test.

Five exact native profiles reached 20 pairs with zero observed disagreements. Two answer profiles stopped at 16/2 and 14/2 pairs/disagreements, with persisted acquisition-time divergence-exposure disables. This explains 130 rather than 140 probes; it is not a transport omission or a current guard/admission assessment. Reference agreement is not gold correctness; five 20/0 profiles are not a 2% damage certificate, and never promoted an UNKNOWN request.

The [exposure disclosure](../bench/repro/openrouter-protected-generated-2026-09-06/exposure-disclosure.json) preserves all IDs: inherited offline fake tests had generated the six pilot and first two study contexts before the checkpoint, including internal expected-value generation but no gold-driven feedback or selection. These are first prospective paid responses on a generated development draw, **not unseen-context holdout evidence**. [Current figures-v2](../bench/repro/openrouter-protected-native-learning-2026-09-06/figures-v2/rendering.json) use the verified analysis; original figures remain provenance.

## Has The Field Passed The Idea?

The broad idea is crowded, but the narrower operational question remains relevant. The [verified nearest-neighbor refresh](research-inbox/2026-09-05-joint-routing-novelty-refresh.md) distinguishes what each source actually establishes:

- AgentOpt already has opaque HTTP interception; Cognify and OPTIMAS jointly optimize heterogeneous workflow choices.
- ApproxMLIR includes runtime-state-dependent approximation and checking/recovery; it is not a static-only strawman.
- MESS+ learns routing online, explicitly charges exploration, and evaluates distribution shifts.
- CORAL explicitly couples routing and prompt optimization. The [September 7 full-paper retrieval follow-up](research-inbox/2026-09-07-coral-full-paper-followup.md) still obtained only the publisher abstract, not full methods or a runnable artifact. This is dated retrieval evidence, not proof that those materials cannot exist.

Therefore, “routing on top of JIT” is not the contribution by itself. The useful question is whether exact-plan evidence and invalidation deliver a better charged cost/quality/availability tradeoff than strong static, sequential, and joint-optimization alternatives. No matched competitive run establishes that yet.

## How Current Results Fit The Literature

| Current result or idea | Literature neighborhood | What it proves now | What it does not prove yet |
|---|---|---|---|
| Typed 32-task model × CC/SD factorial | Joint workflow optimization, model/prompt interactions | A positive descriptive model-dependent quality interaction and large fixed-configuration cost reduction, with all requests and charges reconstructed. | No adaptive controller, native admission, independent CC-versus-SD attribution, public-workload generalization, or damage bound. |
| Protected 63-task development study | Exact-plan online evidence and charged exploration | Real source-first profiling, retained guard disables, complete accounting, and a fixed source/HR comparison with a quality interval crossing zero. | No UNKNOWN primary promotion, adaptive-policy win, unseen holdout, causal rule attribution, or public-data quality evidence. |
| `ContextCompress` on `long_context_qa` | LLMLingua, LongLLMLingua, Selective Context, tool-use compression | Runtime context rewriting can save substantial input tokens on long-context stress workloads. | It does not beat specialist compressors yet, and it does not prove broad real-task savings. |
| `ModelDowngrade` on `gaia_router` | FrugalGPT, RouteLLM, LLMSelector, routing/cascades | Internal call-site model substitution can cut cost sharply. | Routing itself is not novel, and quality preservation needs stronger uncertainty treatment. |
| `StateDrop` on iterative refinement | Program slicing, data-flow, liveness, memory/context systems | Stale state can be pruned for modest input-token savings. | It is not sound compiler slicing unless AgentC defines dependencies/read windows precisely. |
| Real HotpotQA near-zero savings | Compression gating and conservative runtime policy | The rule can decline near the activation boundary, which is a useful systems behavior. | It is not a headline savings result. |
| Hotpot oracle compression | Compression headroom / idea generation | There is likely useful headroom if AgentC can identify irrelevant context better. | The current automated rule does not achieve oracle-level compression. |
| `CacheHit` | GPTCache, ContextCache, MeanCache, vCache | Important future direction and likely useful runtime pass. | Needs false-hit, invalidation, and context-key evidence before becoming a paper claim. |
| `ParallelBranch` | LLMCompiler, LLM-Tool Compiler, LangGraph, LLMOrch | Important future direction for latency. | Needs dependency, side-effect, and idempotence policy before strong claims. |
| Complete-call tail and overload diagnostic (`RES-013`) | Agentix, Murakkab, Parrot, SGLang, serving/runtime scaling work | Off-path audit removes the median bottleneck; thread-CPU/boundary controls attribute the residual to off-CPU scheduling/GIL delay; configurable admission fails open with exact accounting. The default gate cuts matched C=32 p99 another 34–82% and raises throughput 1.86–3.83×. | C=32 p99 is still 2.33–10.19ms, the 1.2ms target still fails, and safely rejecting 54.6–81.4% of overloaded calls sacrifices optimization coverage. This remains one-host synthetic Stage E0 evidence. |

## Publishability Read

| Target | Current readiness | Why |
|---|---|---|
| Workshop / short paper | plausible | The trace-optimizer framing, verified literature map, and two strong rule-level results are enough for useful feedback. |
| MLSys 2027 research target | conditional, not ready yet | Official October 30, 2026 deadline; use the earlier 19:00 UTC / 3 PM EDT while its clock conflict is resolved. See the [verified venue map](strategy-and-venues.md). |
| MLSys / EuroSys / strong systems venue | not yet | The fixed factorial gives a useful interaction hypothesis, but native joint admission previously failed and adaptive superiority is untested. Strong static/joint baselines, fresh-workload evaluation, charged risk/adaptation tests, and multi-host overhead/coverage evidence remain necessary. |
| COLM / LM-facing venue | possible later | Needs clearer cost-quality frontier, stochastic evaluation, and comparisons against routing/compression baselines. |
| Broad AI/ML main venue | weak right now | The contribution currently reads more like systems infrastructure than a new AI method. |

## What Is Real Alpha

- The verified typed factorial supplies a concrete conditional-quality pattern to investigate; its exact bills, raw responses, failure policy, and post-hoc stage trace are reproducible diagnostic evidence.
- `RES-001`: ContextCompress has the cleanest token-savings story.
- `RES-002`: ModelDowngrade has the cleanest dollar-savings story.
- `RES-005`: HotpotQA near-zero savings can be used as an activation-boundary diagnostic.
- `RES-013`: complete-call timing now includes synchronous/off-path comparison, scheduler attribution, and bounded overload admission. The mechanism has exact fallback/audit accounting; the remaining high-concurrency tail and coverage tradeoff are explicit.
- `literature-and-nearest-neighbors.md`: the related-work map is now strong enough to guide writing and experiment selection.

## What Is Not Ready

- A claim that AgentC is the first runtime optimizer for LLM agents.
- A claim that all rewrite rules are equally validated.
- A claim that the fixed factorial proves a learned joint planner or that typed validity guarantees correctness.
- A claim that five 20/0 native profiles establish admission or a damage guarantee, or that the 63-task fixed comparison establishes adaptive superiority.
- A CacheHit or ParallelBranch headline contribution.
- A strong behavior-preservation claim without paired/repeated uncertainty.
- A StateDrop soundness claim without a concrete dependency/read-window model.

## Best Next Contributions

The experiment priorities below are scientific requirements; Beads, not this document, owns task state.

1. Establish public-workload generalization in a separately designed complete study; the closed partial HotpotQA run supplies no quality evidence and cannot be resumed. Use natural workload structure, keep no-op and invalid paths, and separate CC-only/SD-only effects only where the predeclared design can identify them.
2. Build on the completed protected source/probe diagnostic without treating it as primary promotion. That transition remains unproved and must not invent a provider context bound. Then compare learned joint selection against the best fixed configuration, route-only, rewrite-only, both sequential orders, and current greedy under a new evaluation protocol. Charge every source probe and failed attempt.
3. Include a credible joint optimizer such as Cognify/OPTIMAS or a faithfully scoped matched control, not only a routing baseline. Disclose label access, model pools, adaptation requirements, and tuning budgets; resolve CORAL's full-methods uncertainty.
4. Test persistent invalidation and the advertised damage contract under declared drift and restart, with no-adaptation/nonpersistent controls. A small positive quality interval is not a risk guarantee.
5. Repeat the frozen admission-limit sweep on a quiet second host and report latency versus lost optimization coverage. Preserve the 1.2ms target; loaded-host abstention is not a free speedup.

The [native context-exhaustion receipt research](research-inbox/2026-09-07-openrouter-context-exhaustion-receipts.md) only proposes a narrowly attributable failure category. It supplies neither a context-fit bound nor permission for candidate-first retry, hold release, semantic feedback, or primary admission. No new classifier is enabled by this evidence consolidation.

The [September 9 public-workload proposal](research-inbox/2026-09-09-mlsys-public-workload-contract.md) chooses a natural two-call retrieval/program pipeline with deterministic execution, not added state to force rule activation. The separate FinQA offline contract `43fe7ab` passed independent review (`bd-6gjb.1`), using synthetic tests only. Real-corpus acquisition (`bd-lnai`) awaits licensing clearance (`bd-tprq`), and live integration (`bd-sjrg`) awaits the safe capacity/fallback path. There is no new public-workload quality result, native admission, or resumption of the failed HotpotQA study.

The roughly seven-week MLSys timeline makes a real public workflow and a separately reviewed safe candidate-first transition more decisive than another favorable generated result. Neither is completed or authorized by this map: candidate-first needs its own exact failure/accounting and safety review before use, and the present UNKNOWN path remains source-primary.

## Avery Read Path

Read these in order:

1. `README.md`
2. `current-fit-and-publishability.md`
3. `literature-and-nearest-neighbors.md`
4. `claims-gaps-and-risks.md`
5. `results-experiments-and-repro.md`
6. `strategy-and-venues.md`

That path gives the paper state without requiring a linear read through every ledger.
