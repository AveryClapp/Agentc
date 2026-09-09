---
title: Evidence and Sources
status: active
last-updated: 2026-09-09
owner: paper-intelligence
---

# Evidence and Sources

This file is the source-hygiene and provenance map for Paper Intelligence. It tells future readers where raw inputs live, how evidence becomes paper-ready, and which files should not be treated as final claims.

Supersedes:

- `citation-style-and-hygiene.md`
- `deep-research-inbox.md`
- `quote-and-evidence-bank.md`
- `literature-ingestion-workflow.md`
- `repo-source-map.md`
- `metadata-schemas.md`
- source/provenance portions of `artifact-inventory.md`

## Source Rule

Do not cite or claim from generated research output alone. Generated research can identify candidates and structure the map, but paper claims need primary sources, source artifacts, code paths, or validated result artifacts.

Preferred sources:

1. Official proceedings pages, arXiv/OpenReview/ACM/USENIX/ACL pages, publisher pages, or official docs.
2. Repo code, specs, benchmark scripts, and committed result CSVs.
3. Preserved local source artifacts under `references/source/`.
4. Raw deep-research drops only as provenance, not final evidence.

## Verification Levels

| Level | Meaning | Allowed use |
|---|---|---|
| `candidate` | Found in search or deep research, not checked. | Inbox only. |
| `metadata-verified` | Title/authors/year/venue checked. | Planning and ledgers. |
| `claim-verified` | Relevant claim checked in source. | Claim bank and related-work map. |
| `bib-ready` | Citation metadata ready for manuscript use. | Future bibliography. |
| `cited` | Used in approved paper prose. | Manuscript only after approval. |

No-go phrases until verified: `first`, `novel`, `state of the art`, `to our knowledge`, `no prior work`, `universally preserves quality`, `drop-in for all agent systems`.

## September Evidence Snapshot And Replay Boundary

The active maps preserve the [32-task fixed factorial](../bench/repro/openrouter-typed-factorial-2026-09-05/README.md), [same-question repeat](../bench/repro/openrouter-typed-factorial-repeat-2026-09-05/README.md), and terminal [public FAILED/UNSCORED archive](../bench/repro/openrouter-public-model-sd-2026-09-06/README.md). None is replaced by the newer generated development study, and the public labels remain unopened.

| Imported evidence | Exact source / artifact | Scope |
|---|---|---|
| Protected acquisition and post-completion analysis | [Archive README](../bench/repro/openrouter-protected-native-learning-2026-09-06/README.md), raw `d6242ab`, completed archive `a609f30` | 63 development tasks, 508 study calls plus 36 pilot calls; source 58/63, fixed HR 61/63, paired interval crossing zero; full USD2.400538 acquisition bill. |
| Exposure and selection | [Exposure disclosure](../bench/repro/openrouter-protected-generated-2026-09-06/exposure-disclosure.json), [provenance](../bench/repro/openrouter-protected-generated-2026-09-06/provenance.json) | Preserve all 69 IDs and the eight-context offline generation exposure; not an unseen holdout. |
| Exact native mechanisms | [Control report](../bench/repro/protected-native-controls-2026-09-06.json), [native census](../bench/repro/openrouter-protected-native-learning-2026-09-06/native-profile-census.json) | Synthetic zero-loss reachability is separate from 130 actual source-paired probes and acquisition-time guard disables. No UNKNOWN primary promotion or current admission assessment. |
| Analysis / current figures | [Analysis](../bench/repro/openrouter-protected-native-learning-2026-09-06/analysis.json), [execution record](../bench/repro/openrouter-protected-native-learning-2026-09-06/analysis-execution.json), [figures-v2](../bench/repro/openrouter-protected-native-learning-2026-09-06/figures-v2/rendering.json) | Reviewed analysis source `63bc464`, figure sources `60e32d3`/`9f522f3`; original figures retained unchanged. Local audit/analysis elapsed time is not provider latency. |

The protected manifest's canonical SHA-256 is `c5a38945224decfbc1ea03b582213f992b7742c7179121529bc7a3f5de85d477`; the saved analysis raw SHA-256 is `0d27c6306a5584ff69de1de521055166d1be00bc23823538760c38944b64df7b`. [Terminal metadata](../bench/repro/openrouter-protected-native-learning-2026-09-06/terminal-snapshot.json) pins the raw ten-artifact map and original 22,310-event snapshot, raw `1abbb47dcbcd4df40932fcf5aa5ca2a99e5be03d6b66bed32832453cb9998204`, canonical `16c091d70ace784d64082fc89156780de0f9535dac66c240e7b4543bb75abdbe`. Its USD12.275019 historical uncertainty is retained, not forgiven by completion.

The coherent integration (`bd-bpsv.1`) starts from `c53bd24`, imports the archive byte-for-byte, and restores only the twelve missing/differing members needed for its **exact 84-source frozen closure**, plus reviewed analysis/figure code and tests. It does not import newer batch-audit implementations, refreeze enrollment, or alter original acquisition artifacts. The native source changes are the previously reviewed protected-only prototype and exact float round-trip prerequisite, not newly relaxed guards.

Source closure is necessary but not sufficient for original replay. The archive manifest pins an external protected bridge with SHA-256 `3803d2d0c8c6c804f4144a8d369f3bdd955d638f744d73db4d3f4486a3ac9d30` and typed materializer `b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122`, their exact acquisition paths, and the original campaign journal. Binaries are not rebuilt or vendored here, and the original journal is **not** copied into the archive. The archived SQLite state and receipts do not substitute for that journal. Full read-only audit requires those exact dependencies; label-free synthetic regressions and figure-data verification can run separately. Do not weaken source/binary identities, create a replacement budget ledger, or retry/resume a closed phase to make replay portable.

## September Research Provenance

The [joint-routing refresh](research-inbox/2026-09-05-joint-routing-novelty-refresh.md) remains dated provenance, including its then-prospective experiment descriptions. The active results map, not that historical note, owns completion status.

- [CORAL full-paper follow-up](research-inbox/2026-09-07-coral-full-paper-followup.md), source `059297d`: on September 7 the official Frontiers abstract was retrievable, but full methods, supplement, preprint and runnable artifact were not obtained. Promote its explicit joint prompt/router formulation as conceptual overlap with an **abstract-only** flag; do not promote abstract quantitative claims or infer absence of online learning, drift, fallback, or accounting. The note records retrieval responses and a full HTML hash; it is not a current availability or novelty certificate.
- [OpenRouter native context-exhaustion receipt review](research-inbox/2026-09-07-openrouter-context-exhaustion-receipts.md), source `cdef03d`: primary provider/API documentation plus frozen local code support a proposed narrow failure-attribution contract. No live wire mapping or new classifier was tested. A native context-stop category does not supply a numeric capacity bound, successful semantic observation, fallback authorization, or permission to release a failed hold.

Both notes remain raw research provenance, with technical claims traced to their primary links. No new active tracker document or manuscript prose is introduced; Beads owns the unfinished research and implementation gates.

The [September 9 MLSys/public-workload contract note](research-inbox/2026-09-09-mlsys-public-workload-contract.md), source `05eb7bfb` with dated clarification `7ac17ece`, records official MLSys 2027 requirements and the unresolved one-hour deadline discrepancy, plus primary FinQA documentation/evaluator/license inspection without corpus acquisition. The separate offline contract `43fe7ab` passed independent review (`bd-6gjb.1`); its stricter duplicate-label/`none` argument policies, source-order evidence serialization, and full-public-table execution are explicit rather than blanket upstream parity. No FinQA code, corpus, live driver, native admission, or paid result is imported here. The clarification permits local-only research acquisition (`bd-lnai`) under an explicit author-published MIT/CDLA provenance assumption, not a legal guarantee. It retains separate redistribution/external-provider-sharing review (`bd-tprq`) and the Permissive-versus-Sharing discrepancy (`bd-wjbv`), and discloses published examples that future enrollment must exclude. `bd-sjrg` live integration still depends on the safe capacity/fallback path; `bd-2r1p` owns the deadline clarification.

## Deep Research Drops

| ID | Date | Topic | Raw path | Summary | Current promotion |
|---|---|---|---|---|---|
| `DRP-001` | 2026-05-09 | Literature map | `research-inbox/2026-05-09-literature-map.md` | Compound-AI/runtime framing; rewrite-specific related work; StateDrop/evaluation gaps. | Promoted into literature, claims, gaps, and risks. |
| `DRP-002` | 2026-05-09 | Venue fit | `research-inbox/2026-05-09-venue-research.md` | MLSys, ATC, COLM, and workshop fit. | Promoted into strategy/venues with verification caveats. |
| `DRP-003` | 2026-05-09 | Post-June venue plan | `research-inbox/2026-05-09-post-june-venue-plan.md` | Later 2026/2027 venue plan: MLSys, AAAI, ICLR, EuroSys, NSDI, CIDR/ARR. | Promoted as watchlist/strategy, not final CFP facts. |
| `DRP-004` | 2026-05-09 | Full literature review map v2 | `research-inbox/2026-05-09-full-literature-review-map-v2.md` | Added major novelty threats and stronger stochastic-evaluation sources. | Promoted into `literature-and-nearest-neighbors.md`. |

Keep `research-inbox/` as raw/provenance storage. Do not archive it during consolidation.

## Local Source Artifacts

| Artifact | SHA-256 | Use |
|---|---|---|
| `references/source/agentc-paper-reference-v2.md` | `0c16a6b1be3f3345d0e4e0f539d705e65acb3482a30ae6d24fe7ec773b227eee` | Master paper reference and experiment interpretation. |
| `references/source/agentc-feedback.md` | `bbcc0876311ef9a2c61838fab60eaf4e0487a0ed25ed88d24e38ef1cac73d7d9` | Feedback/research critique context. |
| `references/source/agentc-response.pdf` | `0fed0bf36308e080effc72def1f706682f7759ba15b9f2ba833c08df8c305a2d` | Response artifact; summarize before claim use. |
| `references/source/readme-local-before-upstream.md` | `522fc9dc5946968a51c39f756a5609c0824ac603390eb1b4f43443d90f9a0cb8` | Older local framing; triage before use. |

Keep `references/source/` as source storage. It is not part of the active reader path, but it is not archive noise.

## Repo Source Map

### What AgentC is

- `README.md`: public framing, status, rules, benchmark summary, quick-start commands.
- `AGENTS.md`: project context, conventions, guardrails.
- `specs/README.md`: component overview.

### Runtime and interception path

- `python/agentc/_intercept.py`: LLM SDK interception flow.
- `python/agentc/_optimizer.py`: Python shim over native optimizer FFI.
- `python/agentc/_executor.py`: optimizer-plan dispatch.
- `python/agentc/_patches/_optimizer_glue.py`: request/response conversion.
- `crates/agentc-optimizer/src/ffi.rs`: Rust FFI plan/observe adapters.
- `crates/agentc-optimizer/src/wiring.rs`: production optimizer construction and rule registration.

### Planner, safety, and activation

- `crates/agentc-optimizer/src/planner.rs`: enabled flag, hot threshold, rule proposal/ranking, safety-check selection, overhead kill switch, pass-through behavior.
- `crates/agentc-optimizer/src/config.rs`: optimizer config and env overrides.
- `crates/agentc-optimizer/src/budget.rs`: accuracy-budget state and auto-disable behavior.
- `crates/agentc-optimizer/src/shadow.rs`: shadow sampling.
- `crates/agentc-optimizer/src/audit.rs`: audit logging.

### Rewrite rules

- `crates/agentc-optimizer/src/rules/context_compress.rs`: `ContextCompress`.
- `crates/agentc-optimizer/src/rules/model_downgrade.rs`: `ModelDowngrade`.
- `crates/agentc-optimizer/src/rules/state_drop.rs`: `StateDrop`.
- `crates/agentc-optimizer/src/rules/cache_hit.rs`: `CacheHit`.
- `crates/agentc-optimizer/src/rules/parallel_branch.rs`: `ParallelBranch`.

### Benchmark harness

- `bench/optimizer_ablation.py`: 11-config shared-baseline ablation matrix.
- `bench/optimizer_bench.py`: baseline vs optimized runner.
- `bench/run_oracle_baseline.py`: oracle baseline CSV generator.
- `bench/run_hotpot_ablation.py`: HotpotQA-specific runner.
- `bench/agents/`: benchmark agents and workloads.
- `bench/scripts/*.sh`: paper, pushback, targeted, and broad ablation runs.
- `bench/paper_results/*.csv`: canonical current result artifacts.

## ID Prefixes

| Prefix | Entity |
|---|---|
| `ART` | Artifact |
| `RES` | Result |
| `CLM` | Claim |
| `GAP` | Gap, risk, missing evidence |
| `LIT` | Literature source |
| `CIT` | Citation gap |
| `EXP` | Experiment candidate |
| `RUN` | Experiment run |
| `VEN` | Venue/workshop |
| `FIG` | Figure/table idea |
| `DEC` | Decision |
| `QST` | Open question |
| `IDEA` | Paper idea |
| `DRP` | Deep research drop |
| `ANG` | Paper angle |
| `RR` | Reviewer risk |
| `WP` | Weak-point work item |
| `TTL` | Title/abstract idea |
| `QTE` | Quote/evidence-bank entry |
| `AE` | Artifact-evaluation item |
| `STAT` | Statistical-analysis item |
| `NEG` | Negative result or rejected angle |

IDs are never reused. If an item is abandoned, mark it discarded, superseded, or archived.

## Quote And Evidence Bank

| ID | Source | Use | Status |
|---|---|---|---|
| `QTE-001` | not populated yet | related work, motivation, method definition, limitation, reviewer response | Add only short primary-source quotes that have been checked against the original source. |

Current quote/evidence needs:

- definition of compound AI systems or agentic workflows;
- prior work on model routing/cascades;
- prior work on prompt/context compression;
- prior work on state/memory pruning;
- reproducibility expectations for systems papers.

## Ingestion Workflow

Use this promotion path:

```text
raw drop -> source extraction -> identity verification -> citation normalization -> skim notes -> claim/gap mapping -> consolidated docs
```

Every ingestion pass should end with:

- raw drop path;
- sources promoted;
- claims/gaps updated;
- citations still missing;
- follow-up research tasks.

## Idea Bank

| ID | Status | Type | Idea | Current disposition |
|---|---|---|---|---|
| `IDEA-001` | promising | paper angle | Frame AgentC as a runtime optimizer for compound AI systems. | Promoted into `strategy-and-venues.md`. |
| `IDEA-002` | promising | methodology | Emphasize shared-baseline ablation as a contribution. | Promoted into results/evaluation strategy. |
| `IDEA-003` | raw | figure | Rule activation map showing which workloads trigger which rules. | Tracked as `FIG-002`/activation-boundary figure idea. |
