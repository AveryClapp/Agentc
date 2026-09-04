---
title: Claims, Gaps, and Risks
status: active
last-updated: 2026-09-04
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

AgentC has a solid workshop/short-paper shape and is approaching plausibility for a systems venue short paper. The claim is now sharper: **a runtime control plane for framework-emitted, multi-step LLM agent traces that jointly considers several rewrite classes under a cost-driver compatibility policy, then abstains when evidence, risk, or local runtime capacity is insufficient.** The current controlled planner ablation shows parity with first-match, not superiority; the held-out joint-policy campaign must establish the benefit.

The current evidence supports: warmup-corrected targeted savings for `ContextCompress` (36.3% input tokens rule-only, RES-001) and `ModelDowngrade` (11.4% cost rule-only, RES-002); a direct LLMLingua-2 comparison with exact paired statistics showing favorable-fixture improvement (68%→100%, p=4.7×10⁻¹⁰) and natural-prose abstention (94.9%→94.9%, p=1.0); and controlled evidence that first-match and composition modes produce the same accuracy on the current planner fixture (RES-010). Stage E0 diagnostics under `RES-013` now progress from synchronous audit through off-path persistence, thread-CPU attribution, and non-blocking admission. The default gate reduces matched C=32 p99 34–82% and raises throughput 1.86–3.83× relative to the attributed off-path run, with exact fallback/audit accounting, but C=32 p99 remains 2.33–10.19ms and 54.6–81.4% of overloaded calls abstain. The evidence does not yet support a broad claim that all eight rules are validated, that behavior is preserved in a semantics-level sense, that AgentC scales across hosts, that the joint policy beats separate/sequential controls, or that AgentC is the first optimizer for LLM agents.

## Safe Claims

| ID | Status | Safe wording | Evidence | Caveat |
|---|---|---|---|---|
| `CLM-001` | supported | AgentC sits between agent code and LLM APIs, intercepting calls and failing open when optimization is unsafe or unavailable. | `ART-020`, `ART-024`, `ART-025` | Transparency depends on SDK patches/framework coverage. |
| `CLM-002` | supported | On the warmup-corrected purpose-built `long_context_qa` workload, `ContextCompress` alone saves 36.1% cost and 36.3% input tokens; all-on saves 33.9%/34.0%. | `RES-001`, `ART-021` | Do not generalize to all real-world QA. |
| `CLM-003` | supported | On warmup-corrected `gaia_router`, `ModelDowngrade` alone saves 11.4% by routing 65/254 calls from `gpt-4o` to `gpt-4o-mini`. | `RES-002`, `ART-022` | Savings are price-ratio driven; −3.9pp quality delta is non-significant (p=0.1797) but uncertain. The old 35.3% cold-start estimate is retracted. |
| `CLM-004` | promising | In the complete warmup-corrected `iterative_refiner` matrix, `StateDrop` alone saves 6.1% cost and 10.8% input tokens with a 0.0pp accuracy delta. | `RES-004`, `ART-023` | The purpose-built metric is lenient; use within-run comparisons only. |
| `CLM-005` | supported | On real HotpotQA, `ContextCompress` fires rarely and produces near-zero savings, supporting the activation-gate story. | `RES-005`, `ART-001` | Present as diagnostic/gating evidence, not headline savings. |
| `CLM-006` | needs-analysis | Gold-label compression suggests HotpotQA distractors can be removed profitably and may improve answers. | `RES-006`, `ART-001` | Current automated rule does not achieve oracle compression. |
| `CLM-007` | supported | AgentC optimizes multi-call traces emitted by agent frameworks, using multiple rewrite classes under one runtime control plane. Both CC and SD activate on the same agent trace (RES-011). | `RES-009`, `RES-011`, `DRP-001`, `LIT-002`, `LIT-024`, `LIT-025`, `LIT-040`, `LIT-043`, `LIT-044` | Novelty must be narrowed against close systems (see §7 draft). |
| `CLM-008` | supported | McNemar exact tests and 95% bootstrap CIs are computed for all headline accuracy claims. No test rejects accuracy degradation at α=0.05 for StateDrop, CC on natural prose, or CC+SD composition. | `RES-007`, `RES-008`, `RES-009`, `RES-010`, `RES-012` | Strong wording should say "does not significantly degrade" not "preserves." |
| `CLM-009` | supported | ContextCompress operates at message granularity and correctly abstains when the structural precondition (identifiable low-attention messages) is absent. LLMLingua-2 compresses indiscriminately at token granularity regardless of fixture structure. | `RES-007`, `RES-008` | Dual-regime result; cite both fixtures together. |
| `CLM-010` | controlled non-difference | On the warmup-corrected `composition_qa` fixture, first-match CC+OB and composed CC+OB produce identical +14pp accuracy and 47 activations; CC alone is +18pp. | `RES-010` | This validates that composition does not change this fixture's outcome; it does not show superiority over greedy. The CC+PlannerDispatch row is unrecoverable. |
| `CLM-011` | diagnostic | On one arm64 host, off-path audit persistence cuts matched C=32 p50 89–93% and raises throughput 2.6–4.0×. Thread-CPU and boundary controls attribute the residual primarily to off-CPU scheduling/GIL delay. A default four-permit fail-open gate then cuts matched C=32 p50 85–98%, p99 34–82%, and raises throughput 1.86–3.83×; C=32 p50/p99 is 4–25µs/2.33–10.19ms, with 54.6–81.4% saturation fallback and exact audit/reason conservation. | `RES-013` | Synthetic one-host Stage E0 diagnostics with no provider; `paper_evidence=false`. All C=32 cells still miss 1.2ms, the host was loaded, and overload abstention is lost optimization coverage. Zero observed loss is not a universal guarantee; crash loss remains bounded by the queue/batch contract. |

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
| `GAP-002` | ~~high~~ **closed** | ~~`StateDrop` n=50 matrix is partial 10/11.~~ | `CLM-004` | Warmup-corrected 11-config matrix is canonical; keep the retracted partial artifact out of claims. |
| `GAP-003` | ~~high~~ **closed** | ~~Real HotpotQA `ContextCompress` matrix is partial 7/11.~~ | `CLM-005` | Warmup-corrected 11-config matrix is canonical and supports only the activation-boundary claim. |
| `GAP-004` | high | Accuracy deltas need uncertainty framing. | `CLM-002`, `CLM-003`, `CLM-008` | Complete paired/statistical analysis. |
| `GAP-005` | medium | `StateDrop` accuracy check is lenient. | `CLM-004` | Limit claims or add stronger evaluation. |
| `GAP-006` | medium | Oracle ceiling claim needs trace-query evidence, not just CSV. | `CLM-006` | Locate or reproduce trace queries. |
| `GAP-007` | high | Nearest-neighbor metadata/baseline cleanup was incomplete. | Novelty claims | Now mostly handled by `literature-and-nearest-neighbors.md`; final BibTeX cleanup remains open. |
| `GAP-008` | high | Venue lane affects required evidence. | Paper angle | Use `strategy-and-venues.md` to choose target lane. |
| `GAP-009` | high | Bibliography metadata is not final. | Related work | Final citation cleanup remains. |
| `GAP-010` | high | Novelty must be narrowed against close systems and single-rewrite baselines. | Title, abstract, intro | Use nearest-neighbor matrix and avoid broad firstness. |
| `GAP-011` | ~~high~~ **closed** | ~~Main venues need end-to-end optimizer evidence, not only rule-isolation ablations.~~ | EXP-002/RES-009/RES-011 | CC and SD both fire on multirule_qa and real agent traces; savings measured; multi-rule section drafted in `draft-paper-edits.md §12`. |
| `GAP-012` | medium (compression closed, routing/caching open) | Direct baseline missing for routing (RouteLLM/FrugalGPT) and caching (vCache). LLMLingua-2 compression baseline done (RES-007/RES-008). | EXP-008 | Routing and caching baselines remain cite-only for now. |
| `GAP-013` | medium | `StateDrop` needs a concrete dependency/read-window model. | `CLM-004` | Dependency model paragraph drafted in `draft-paper-edits.md §3`; needs to be inserted into §4 of the .tex. |
| `GAP-014` | ~~high~~ **closed** | ~~Stochastic evaluation needs repeated-run or paired uncertainty treatment.~~ | EXP-006/RES-007/RES-008/RES-009/RES-010/RES-012 | McNemar exact tests (statsmodels) and bootstrap CIs computed for all headline accuracy claims. Methodology paragraph drafted in `draft-paper-edits.md §6`. |
| `GAP-015` | high, mechanism characterized; confirmation open | Complete-call scaling now covers synchronous/off-path audit, boundary and thread-CPU attribution, and bounded fail-open admission. Off-CPU scheduler/GIL delay dominates the residual. Default admission improves the tail but C=8/C=32 still misses 1.2ms in some/all cells and rejects 55–81% at C=32; quiet multi-host behavior, admission-policy utility, cold start, and confirmatory end-to-end latency remain open. | `CLM-011` | Repeat the fixed protocol on a quiet second host, select the latency-versus-coverage operating point, and execute the frozen Stage C/P/T campaign without weakening the target. |
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
| `RR-010` | ~~high~~ **mitigated** | `ContextCompress` looks like LLMLingua. | Direct comparison done (RES-007/RES-008): CC outperforms LLMLingua-2 on distractor fixture (68%→100% vs 68%→53%), correctly abstains on natural prose; LLMLingua-2 compresses indiscriminately at 13.7s overhead. Dual-regime paragraph drafted in `draft-paper-edits.md §11`. | Deploy dual-regime framing; never cite RES-007 without RES-008. |
| `RR-011` | medium | CacheHit unsafe in multi-turn/stateful contexts. | Needs call-site/state-aware keys. | Keep caveated until correctness story exists. |
| `RR-012` | medium | ParallelBranch independence is unsound. | Needs dependency/side-effect policy. | Treat as future unless evaluated. |
| `RR-013` | high | Agentix/Halo/Murakkab/Cognify/serving systems subsume the story. | AgentC works above server/API layer and rewrites application semantics. | Make application-level trace rewriting central. |
| `RR-014` | ~~high~~ **mitigated** | Single-run evaluation is underpowered. | McNemar exact tests and bootstrap CIs now computed for all headline claims (GAP-014 closed). No test rejects accuracy degradation. Framing is "does not significantly degrade" not "preserves." | Verify all p-values in paper draft use statsmodels exact=True, not chi-squared approximation. |

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
| `QST-001` | experiment | Is the StateDrop n=50 matrix complete and warmup-corrected? | Answered: the 11-config warmup-corrected matrix is canonical; the older partial artifact is retracted. |
| `QST-002` | venue | Is the first target ATC, longer-run MLSys, or LM-native COLM? | Needs venue choice in `strategy-and-venues.md`. |
| `QST-003` | artifact | Where is the trace evidence for the Hotpot oracle ceiling? | Still open; needed before strong `CLM-006`. |
| `QST-004` | positioning | How narrow should novelty be against close systems? | Very narrow: framework-call interception plus multi-rule trace rewriting. |
| `QST-005` | method | What does behavior-preserving mean? | Use metric/tolerance-bounded wording, not semantic equivalence. |
| `QST-006` | experiment | Can we produce one workload where multiple rewrite rules fire together? | Answered mechanistically by `RES-009`/`RES-011`; the open question is whether a held-out joint policy beats single, sequential, greedy, and static controls. |
| `QST-007` | systems | What are interception overhead and latency-tail effects? | The progression isolates synchronous audit as the median bottleneck and off-CPU scheduler/GIL delay as the residual tail. Default admission produces 4–25µs C=32 p50 and 2.33–10.19ms p99 with exact accounting, while passing through 54.6–81.4% of overloaded calls. Quiet multi-host, cold-start, coverage-utility, and campaign-level measurements remain required. |

## Ordered Weak-Point Plan

| ID | Type | Weak point | Cheapest fix | Strongest fix |
|---|---|---|---|---|
| `WP-001` | decision | Main paper angle not locked. | Use runtime optimizer for compound AI systems. | Choose explicit venue lane. |
| `WP-002` | analysis | Accuracy preservation lacks paired analysis. | Add simple standard errors/paired tests. | Run paired bootstrap or McNemar-style tests. |
| `WP-003` | experiment | StateDrop's complete matrix still uses a lenient task metric. | Keep the within-run, caveated claim. | Rerun on a stronger held-out metric. |
| `WP-004` | experiment | Real HotpotQA is complete but activates ContextCompress only 0–1/300 times. | Use it as activation-boundary evidence. | Build a representative workload spanning both sides of the activation boundary. |
| `WP-005` | literature | Nearest-neighbor comparison needs cleanup. | Use verified blurbs. | Final metadata and baseline cleanup. |
| `WP-006` | briefing | Rule mechanism explanation needs compact version. | Expand system/rules brief. | Add rule activation figure. |
| `WP-007` | local audit | CacheHit/ParallelBranch status can confuse contribution count. | Label future/implementation only. | Add evidence if they become claims. |
| `WP-008` | briefing | Imported source artifacts not summarized. | Summarize `ART-001` and `ART-002`. | Extract every claim/table/figure idea. |
| `WP-009` | literature | Novelty too broad. | Mark firstness unsafe. | Write exact distinction against closest systems. |
| `WP-010` | literature | StateDrop lacks soundness story. | Cite program analysis carefully. | Define dependency/read-window model. |
| `WP-011` | experiment | Main venues want end-to-end and overhead evidence. | Add focused experiment plan. | Run multi-rule workload plus overhead/tail measurement. |
| `WP-012` | analysis | Evaluation should handle stochasticity. | Add uncertainty requirements. | Run repeated trials/paired bootstrap where possible. |

## Highest-Priority Next Fixes

1. Run the frozen held-out joint-policy campaign against route-only, rewrite-only, both sequential orders, greedy, and best-static controls: `GAP-010`, `QST-006`, `RR-009`, `RR-013`.
2. Repeat the frozen admission/coverage protocol on a quiet second host and measure the chosen policy in provider-backed workloads without weakening the target: `GAP-015`, `QST-007`, `WP-011`.
3. Validate the risk controller at its advertised 2% sampling rate with counterfactual cost and cumulative damage charged: `RR-002`, `WP-012`.
4. Run the missing routing baseline and blind unengineered-workload gate: `GAP-012`, `RR-001`, `RR-009`.
5. Keep CacheHit and ParallelBranch out of headline claims and retain careful StateDrop semantics unless new evidence lands: `GAP-013`, `RR-008`, `RR-011`, `RR-012`, `WP-007`, `WP-010`.
