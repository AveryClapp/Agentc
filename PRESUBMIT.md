# MLSys Presubmit Checklist

This document is the submission gate for an MLSys-targeted Agentc paper. It is
the final checklist before submission.

Target: **MLSys 2027 research track**. Rechecked **2026-10-10**: the CFP states
**2026-10-30 at 20:00 UTC**, while the official Dates page states **12:00 PM
PDT (19:00 UTC / 3:00 PM EDT)**. Use **19:00 UTC** as the working cutoff until
the venue resolves this one-hour discrepancy. Submissions are double blind, use the
[MLSys 2025 style](https://media.mlsys.org/Conferences/MLSYS2025/mlsys2025style.zip),
and allow ten content pages excluding references; an appendix is uploaded
separately and is not required reading. Artifact evaluation is voluntary and
does not affect the paper decision. See the official
[call for papers](https://mlsys.org/Conferences/2027/CallForResearchPapers) and
[dates](https://mlsys.org/Conferences/2027/Dates).

OpenReview submission opens October 19. All authors need complete, active
profiles; the CFP recommends allowing at least two weeks to create them.
The dedicated 2027 artifact-evaluation details have not been announced.
The [October source check](paper-intelligence/research-inbox/2026-09-03-mlsys-field-relevance.md)
records the verified rules and distinguishes archival 2026 guidance.

## Current Assessment — 2026-10-10

The current `main` checkout (`4e5da0a` before this review) implements joint
model-and-rewrite selection, exact complete-plan profiles, persistent guards,
and bounded reference-visible exploration. The manuscript now centers that
mechanism, including its actual ranking and admission rules, acquisition limits,
and unresolved empirical value. It uses the official anonymous MLSys template,
separates the appendix, and includes 48 verified complete-author bibliography
entries (22 cited in the shortened main text). The [frozen evaluation protocol](bench/repro/mlsys-2027-evaluation-protocol.md)
requires held-out whole-system comparisons; engineering preflights do not meet
those gates, and its October 1 decision date has passed. The reviewed evidence
does not establish an MLSys main-track GO.

More recent evidence exists on the already-created experiment branches. It
must be considered without treating it as a merged or confirmatory result:

| Source | What it establishes | Remaining evidence boundary |
|---|---|---|
| `exp/typed-factorial-opportunity` at `acd3334`, `bench/repro/openrouter-typed-factorial-2026-09-05/README.md` | A completed 32-task, four-cell fixed model-by-rewrite development study: Sonnet original / rewritten 30/32 and 29/32; Haiku original / rewritten 25/32 and 31/32. | Generated development tasks and one realization per cell; the best fixed configuration already wins. This does not establish adaptive-planner value. |
| `exp/protected-native-learning` at `a609f30`, `bench/repro/openrouter-protected-native-learning-2026-09-06/README.md` | Native paired-profile acquisition and a completed 63-task fixed-workflow comparison: source 58/63 versus fixed combined 61/63; descriptive paired interval [-1.59, +11.11] pp; total acquisition cost USD 2.400538. | Generated development tasks; source remained user-visible during learning. No adaptive-serving, non-inferiority, unseen-input, or deployment-cost claim follows. |

Reproduce these read-only summaries with `git show <commit>:<path>`. Keep their
raw artifacts and earlier failed attempts intact. Integrating their code or
results into `main` needs a scoped integration change; this review does not
merge the existing branches.

The decisive review question is whether a learned complete-plan policy improves
on route-only, rewrite-only, best static joint, both sequential orders, and
greedy selection on unmodified held-out workloads after charging exploration,
shadow, failures, and fallback. Output divergence is a guard signal, not task
quality. A nonsignificant McNemar result does not establish non-inferiority.

The paired-analysis input audit accepted 81 committed per-task sidecars and
found `bench/paper_results/autogen_bridge-n300-ALLON.per_task.csv` contains no
observations. A follow-up recovered the correct source mapping: the combined
`autogen_bridge-n300-warmup.per_task.csv` contains both 300-task configurations,
with identical task IDs and baseline outcomes. Recomputed CC-only / all-on
McNemar p-values are 0.2891 / 0.5488, matching the summary CSV; unadjusted paired
bootstrap intervals are [-3.33, +0.33] / [-3.33, +1.00] pp. The paper now names
that combined sidecar. The empty standalone artifact is preserved and is not
used for inference. The tool rejects empty input, duplicate tasks within an
agent/config group, nonbinary pass flags, and malformed CSV shape.

The 23.5% Autogen per-call token-savings claim is withdrawn: its denominator is
not present in committed evidence. The summary CSV's captured-token sums give
26.0% CC-only and 14.0% all-on reductions. Those values are labeled as captured
aggregates; missing span counts prevent certification of per-call or complete
workload savings. No historical result file was changed. Nonsignificant tests
are no longer described as proof of accuracy preservation.

The follow-up implementation adds a reproducible ten-arm campaign runner,
isolated task-cost accounting, held-out selection-lock validation, and a
distinct native greedy comparator. The ContextCompress rule now preserves the
first user instruction even when provenance tags are absent or incomplete.
The [no-key fixture campaign](bench/repro/README.md#joint-campaign-engineering-check-no-api-key)
checks scheduling, records, resume validation, and report generation. Its
simulated metrics are Stage E0 engineering evidence only. The generated
intervals are unadjusted exploratory summaries, and repeated trials do not
receive a pooled McNemar p-value. These changes do not close the held-out
evaluation gates.

The rewritten draft builds with Tectonic: eight body pages, references starting
on a separate page nine, ten main-PDF pages in total, and a four-page supplement.
All citations and cross-references resolve; PDF author metadata is `Anonymous
Authors`. Both documents have zero overfull-box warnings, zero Type 3 fonts,
and fully embedded fonts. The new architecture diagram describes the exact-plan
control loop; the old rule-centered source is preserved in the historical archive.
The build audit requires agreement between an explicit reference-page boundary
and the PDF heading before reporting the exact body count. Otherwise it keeps
conservative bounds for shared reference pages. Use the
[isolated paper build](bench/repro/README.md#building-the-paper-sources-no-api-key)
to reproduce this draft. This remains a working paper: its held-out result is
missing and `submission_ready` remains false. The unused two-page body allowance
can accommodate verified confirmatory results without compressing the template.

The active supplement uses raw recorded milliUSD differences from the isolation
CSVs rather than unreconstructed normalized-cost denominators. Its composition
table now shows the separate warmup and rerun CSVs: the former projected table
mixed inconsistent sources, including a retracted composed result. It is retained
only in the historical draft. The current text makes no additive-gain claim.

Local verification for this continuation: 33 format/paired-analysis tests,
selected Ruff lint/format checks, and `git diff --check` pass. A fresh release
native preflight measured 20,000 decisions with 19,998 expected joint identities,
two reference fallbacks, zero unexpected identities, and zero network calls.
It remains Stage E0 with `paper_evidence=false`; its timing is not a provider or
whole-workload performance result. A clean-checkout full quality gate has not
been rerun, and the new bibliography/style/appendix files remain local changes.

## Decision Rule

Submit only when every P0 gate is marked pass or explicitly waived by the
authors. P1 items are not all mandatory, but each unresolved P1 item should have
a one-sentence rationale in the final submission notes.

Status labels:

| Label | Meaning |
|---|---|
| `PASS` | Complete and verified from a clean checkout. |
| `LOCAL` | Verified in the current working checkout; clean-checkout reproduction or final experimental text remains pending. |
| `WAIVED` | Intentionally not done; rationale recorded. |
| `BLOCKED` | Cannot be completed without external state or author decision. |
| `OPEN` | Still needs work before submission. |

## P0 Submission Blockers

| Gate | Required outcome | Status |
|---|---|---|
| Target CFP verified | MLSys 2027 rules verified; CFP and Dates page disagree on 20:00 versus 19:00 UTC. Use the earlier cutoff pending resolution; double blind, ten content pages, separate optional appendix, voluntary artifact evaluation, MLSys 2025 style. | `OPEN` |
| Manuscript compiles | Anonymous main/supplement compile with all labels and citations resolved; current body is eight pages. | `LOCAL` |
| Bibliography present | Add and verify the tracked BibTeX file required by `\bibliography{references}`. No submission build may rely on local-only bibliography files. | `OPEN` |
| No draft scaffolding | Remove the `DROPIN-INDEX`, temporary comments, stale TODOs, and any author-facing notes from the submitted manuscript. | `OPEN` |
| Claims match evidence | Every headline number in abstract, intro, tables, captions, conclusion, README, and artifact docs matches the verified source CSV/manifest. | `OPEN` |
| Guard sampling is truthful | Reconcile the paper's "2% production shadow sampling" language with the experiments that use `AGENTC_OPTIMIZE_SHADOW=1`, either by adding a rate sweep/simulation or by clearly labeling full-sampling experiments. | `OPEN` |
| Validated vs implemented rules clear | The paper must not imply that all nine rules are equally evaluated. Headline claims are limited to validated rules and the guard/composition evidence. | `OPEN` |
| Repro appendix checked | Every command in the reproducibility appendix exists, runs from a clean checkout, and points to the exact artifact used in the paper. | `OPEN` |
| Artifact smoke test | A clean machine or clean venv can build the package, run tests, and execute a no-API or stubbed smoke path. | `OPEN` |
| Final PDF inspected | Current working draft fits the page budget and passes layout/font/metadata checks; rerun after final results and artifact anonymization. | `LOCAL` |

## P1 Acceptance Levers

These are the highest-value improvements for MLSys review strength.

| Item | Why it matters | Minimum acceptable fix |
|---|---|---|
| Real end-to-end multi-rule workload | Reviewers will discount purpose-built isolation tasks. | Add or foreground one public/reproducible real-agent workload where multiple rules naturally activate, with labels and paired stats. |
| Guard rate evidence | The guard is a central contribution; sampling-rate realism will be challenged. | Run or simulate 2%, 5%, 10%, and 100% shadow rates; report disable latency, damage before disable, retained savings, and overhead. |
| Cheap pruning baselines | The LLMLingua comparison is strong but the distractor fixture favors message-level IDF. | Compare ContextCompress against random drop, oldest/recency, BM25/lexical relevance, and maybe oracle upper bound on the same fixtures. |
| Real-agent repeated uncertainty | Real-agent rows are the broadest generalization evidence. | Report repeated paired effects and intervals; assess a prospectively fixed quality-loss margin. A nonsignificant difference alone is not evidence of equivalence. |
| Routing baseline feasibility | ModelDowngrade can look like ordinary routing. | Include a concise feasibility matrix for RouteLLM/FrugalGPT/LLMSelector; run one if cheap, otherwise explain why not comparable. |
| Artifact package polish | MLSys rewards runnable systems work. | Provide `reproduce-lite` and `reproduce-paper` paths with expected runtime, API-key needs, costs, and generated outputs. |
| Related-work narrowing | Interception, workflow graphs, routing, and joint optimization already have close systems. | Distinguish online evidence and admission for complete model-and-rewrite plans over opaque APIs; measure value against feasible competitors. |

## Manuscript Gates

### Framing

| Gate | Required outcome | Status |
|---|---|---|
| One-sentence thesis | The intro and abstract converge on complete call-plan selection using exact profiles and constrained admission, with reference fallback. Comparative benefit is explicitly unestablished. | `LOCAL` |
| No broad firstness | Avoid "first runtime optimizer for LLM agents" or equivalent; narrow against direct client-side and joint-search prior art. | `LOCAL` |
| No semantic preservation overclaim | Nonsignificance does not establish preservation; divergence is a guard signal, not task quality. Historical paired intervals are stated. | `LOCAL` |
| Rule inventory precise | Distinguish validated, characterized, implemented-only, and future work rules in one visible table. | `OPEN` |
| Purpose-built caveat visible | Purpose-built workloads are described as activation stress tests, not arbitrary production benchmarks. | `OPEN` |
| Real-agent evidence visible | Real-agent and unseen-agent rows are in the main paper, not only appendix. | `OPEN` |
| Limitations retained | Keep the honest limitations on purpose-built fixtures, guard metric limits, warm cost model dependence, prefix caching, and descoped rules. | `OPEN` |

### Numbers And Tables

| Gate | Required outcome | Status |
|---|---|---|
| ContextCompress numbers verified | Main text, abstract, figures, and tables agree on n, cost savings, token savings, fire rate, accuracy delta, p-values, and CI. | `OPEN` |
| ModelDowngrade numbers verified | Use the warmup-corrected ~11.4% result and frame accuracy as directionally negative but nonsignificant at current n. | `OPEN` |
| StateDrop numbers verified | Frame as within-run/input-token attribution and guard-motivating failure mode; avoid treating it as sound slicing. | `OPEN` |
| Composition numbers verified | Keep MD+CC as mechanistic n=20 evidence and CC+SD as n=30 characterization; no powered accuracy claim. | `OPEN` |
| LLMLingua dual-regime paired | Always present distractor improvement and natural-prose abstention together. | `OPEN` |
| Guard table checked | Ensure all guard numbers specify n, metric, shadow rate, tau, model, and whether the row is full-sampling or production-rate. | `OPEN` |
| Repro table checked | Filenames, scripts, module names, and flags in the appendix exactly match the repository. | `OPEN` |

### Writing

| Gate | Required outcome | Status |
|---|---|---|
| Abstract shortened | Centers exact complete-plan admission and distinguishes historical rule evidence from missing whole-system results. | `LOCAL` |
| Contribution bullets scoped | Describe the complete-plan mechanism and its measured value; keep rule inventory and historical comparisons subordinate to that claim. | `OPEN` |
| Related work final | Nearest neighbors are cited and contrasted: Agentix/Autellix, Halo, Murakkab, Cognify/compound systems, LLMLingua, routing/cascade, caching, LLMCompiler/tool orchestration, serving systems. | `OPEN` |
| Captions self-contained | Figures and tables state n, model/provider, primary metric, and caveat where relevant. | `OPEN` |
| Terminology consistent | Use one name and casing: `Agentc` unless submission style requires `AgentC`. | `OPEN` |

## Experiment Gates

| Gate | Required outcome | Status |
|---|---|---|
| Manifest authoritative | `bench/paper_results/DATA_MANIFEST.txt` is updated to match final manuscript and regenerated figures. | `OPEN` |
| All headline results reproducible | Each headline table has a source CSV and regeneration command. | `OPEN` |
| Failed/dropped results handled | Unrecoverable, contaminated, or dropped runs are not silently used in claims. | `OPEN` |
| API-cost budget documented | Repro docs state approximate API spend for each paid experiment bundle. | `OPEN` |
| Seed/temperature policy documented | Every stochastic experiment states temperature, sample count, and paired/statistical method. | `OPEN` |
| Prefix-cache interaction documented | OpenAI prefix caching is disclosed; Agentc savings are measured on top of it. | `OPEN` |
| Provider generalization checked | Provider rows state what generalized and what abstained because preconditions were absent. | `OPEN` |
| Guard overhead checked | CPU microbenchmark and request-path overhead are both framed correctly. | `OPEN` |

Recommended final experiment commands:

```bash
python bench/run_lcqa_warmup_n300.py
python bench/run_gaia_warmup.py
python bench/run_refiner_warmup.py
python bench/run_cc_sd_subadditivity_warmup.py
python bench/run_planner_ablation_rerun.py
python -m bench.guard_overhead_bench
bash bench/repro/guard_frontier.sh
bash bench/repro/crossmodel_selectivity.sh
python -m bench.run_concurrency_bench --concurrency 1 8 32
```

Treat these commands as presubmit targets only after checking that their names and
flags match the final repository state.

## Artifact And Code Gates

| Gate | Required outcome | Status |
|---|---|---|
| Clean checkout build | Build succeeds from a fresh clone using documented commands. | `OPEN` |
| Rust checks | `cargo check --workspace`, `cargo test --workspace --exclude agentc-profiler`, and `cargo clippy --workspace --exclude agentc-profiler` pass. | `OPEN` |
| Python/native checks | `maturin build --release -m crates/agentc-profiler/Cargo.toml`, wheel install, and `pytest tests/ bench/ -v --tb=short` pass. | `OPEN` |
| Type checks | `uv run mypy python/agentc` or documented equivalent passes, or type gaps are waived. | `OPEN` |
| CLI smoke | `agentc record`, `agentc traces`, `agentc analyze`, `agentc report`, `agentc cache`, and `agentc optimize report` have smoke coverage. | `OPEN` |
| No generated junk | Working tree excludes local `.so`, `.dSYM`, `target/`, `.venv/`, fixtures, logs, and result scratch. | `OPEN` |
| Public artifact path | README or artifact docs explain install, quickstart, test, fixture regeneration, paid experiment reproduction, and expected outputs. | `OPEN` |
| Data licensing checked | Fixtures and committed results are compatible with public artifact release. | `OPEN` |
| Secrets checked | No API keys, `.env`, local paths, or private credentials in tracked files or submission bundle. | `OPEN` |

Suggested verification sequence:

```bash
git status --short --branch
cargo check --workspace
cargo test --workspace --exclude agentc-profiler
cargo clippy --workspace --exclude agentc-profiler
maturin build --release -m crates/agentc-profiler/Cargo.toml
pip install target/wheels/*.whl
pip install -e ".[test]"
pytest tests/ bench/ -v --tb=short
```

## PDF And Submission Gates

| Gate | Required outcome | Status |
|---|---|---|
| Template current | Use the current MLSys template and required metadata for the target year. | `OPEN` |
| Anonymity policy satisfied | If double-blind, anonymize authors, emails, repo URLs, acknowledgments, and artifact links according to policy. If not double-blind, confirm author metadata is final. | `OPEN` |
| Page budget met | Main text, references, appendix, and artifact appendix obey target-year limits. | `OPEN` |
| Figures legible | Every figure is readable at final PDF size in grayscale and color. | `OPEN` |
| Tables fit | No table overflows, unreadable font, or broken wrapping. | `OPEN` |
| Citations resolved | No `??`, missing refs, duplicate labels, or undefined citations. | `OPEN` |
| Source package complete | Final source bundle includes `.tex`, `.bib`, figures, style files if needed, and any required supplementary material. | `OPEN` |
| Final PDF diff reviewed | Authors inspect the final PDF, not only source. | `OPEN` |

Suggested build commands:

```bash
python bench/repro/build_submission.py --output /tmp/agentc-paper-presubmit
rg -n 'Overfull|undefined' /tmp/agentc-paper-presubmit/main.log \
  /tmp/agentc-paper-presubmit/appendix.log
```

Use the actual submission source filename if it differs from `main.tex`.

## Final Day Protocol

| Step | Required outcome | Status |
|---|---|---|
| Freeze branch | Create or identify the exact submission branch/commit. | `OPEN` |
| Close or defer issues | All MLSys-blocking work is done or deferred; all waived items have rationale. | `OPEN` |
| Re-run quality gates | Code, artifact, and PDF gates are re-run after final edits. | `OPEN` |
| Tag source | Create a local tag or commit marker for the submitted version. | `OPEN` |
| Save submitted PDF/source | Store the exact submitted PDF and source bundle in the agreed artifact location. | `OPEN` |
| Push everything | Push Git state so the submitted version is not stranded locally. | `OPEN` |
| Post-submit note | Record final submission ID, title, authors, artifact status, and any waived gates. | `OPEN` |

## Current Known Issues To Resolve

These were rechecked against the current checkout on October 10.

| Issue | Why it matters |
|---|---|
| The rewritten manuscript has no held-out complete-plan result. | Its eight-page body leaves room for the required evidence; historical rule results cannot validate the new selector. |
| Main-track experimental gates have not been demonstrated by the reviewed artifacts. | Fixed generated-workflow results and native learning diagnostics cannot establish held-out adaptive-planner efficacy. |
| Autogen per-call token-normalization denominators are unavailable. | The combined paired sidecar reproduces accuracy inference, but captured-token aggregates do not validate the old 23.5% per-call claim. |
| Sparse guard utility has not passed the frozen labeled evaluation. | The draft describes the 2% default without presenting dense-shadow results as evidence for that operating point. |
| Repo docs contain stale implementation/result statements. | Not always a paper blocker, but artifact reviewers may read README/specs. |
| Some appendix regeneration commands may not match actual script names. | Artifact reviewers will try them literally. |

The earlier `DROPIN-INDEX` warning is obsolete: a source search found no
`DROPIN` or `TODO` markers in `main.tex` during this review. This does not replace
the final compiled-PDF inspection.
