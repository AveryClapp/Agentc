# MLSys Presubmit Checklist

This document is the submission gate for an MLSys-targeted Agentc paper. It is
not the task tracker: mirror actionable work in `bd`, and use this file as the
final checklist before submission.

Target: **MLSys 2027 research track**. The paper deadline is **2026-10-30 at
20:00 UTC**. Submissions are double blind, use the
[MLSys 2025 style](https://media.mlsys.org/Conferences/MLSYS2025/mlsys2025style.zip),
and allow ten content pages excluding references; an appendix is uploaded
separately and is not required reading. Artifact evaluation is voluntary and
does not affect the paper decision. See the official
[call for papers](https://mlsys.org/Conferences/2027/CallForResearchPapers) and
[dates](https://mlsys.org/Conferences/2027/Dates).

## Decision Rule

Submit only when every P0 gate is marked pass or explicitly waived by the
authors. P1 items are not all mandatory, but each unresolved P1 item should have
a one-sentence rationale in the final submission notes.

Status labels:

| Label | Meaning |
|---|---|
| `PASS` | Complete and verified from a clean checkout. |
| `WAIVED` | Intentionally not done; rationale recorded. |
| `BLOCKED` | Cannot be completed without external state or author decision. |
| `OPEN` | Still needs work before submission. |

## P0 Submission Blockers

| Gate | Required outcome | Status |
|---|---|---|
| Target CFP verified | MLSys 2027 research track: deadline 2026-10-30 20:00 UTC; double blind; ten content pages excluding references; separate optional appendix; voluntary artifact evaluation; MLSys 2025 style. | `PASS` |
| Manuscript compiles | `main.tex` or the final submission source compiles cleanly with all figures, tables, bibliography, and references resolved. | `OPEN` |
| Bibliography present | Add and verify the tracked BibTeX file required by `\bibliography{references}`. No submission build may rely on local-only bibliography files. | `OPEN` |
| No draft scaffolding | Remove the `DROPIN-INDEX`, temporary comments, stale TODOs, and any author-facing notes from the submitted manuscript. | `OPEN` |
| Claims match evidence | Every headline number in abstract, intro, tables, captions, conclusion, README, and artifact docs matches the verified source CSV/manifest. | `OPEN` |
| Guard sampling is truthful | Reconcile the paper's "2% production shadow sampling" language with the experiments that use `AGENTC_OPTIMIZE_SHADOW=1`, either by adding a rate sweep/simulation or by clearly labeling full-sampling experiments. | `OPEN` |
| Validated vs implemented rules clear | The paper must not imply that all nine rules are equally evaluated. Headline claims are limited to validated rules and the guard/composition evidence. | `OPEN` |
| Repro appendix checked | Every command in the reproducibility appendix exists, runs from a clean checkout, and points to the exact artifact used in the paper. | `OPEN` |
| Artifact smoke test | A clean machine or clean venv can build the package, run tests, and execute a no-API or stubbed smoke path. | `OPEN` |
| Final PDF inspected | Generated PDF has correct page count, legible figures, no overfull table damage, no missing citations, and no anonymous-policy violations. | `OPEN` |

## P1 Acceptance Levers

These are the highest-value improvements for MLSys review strength.

| Item | Why it matters | Minimum acceptable fix |
|---|---|---|
| Real end-to-end multi-rule workload | Reviewers will discount purpose-built isolation tasks. | Add or foreground one public/reproducible real-agent workload where multiple rules naturally activate, with labels and paired stats. |
| Guard rate evidence | The guard is a central contribution; sampling-rate realism will be challenged. | Run or simulate 2%, 5%, 10%, and 100% shadow rates; report disable latency, damage before disable, retained savings, and overhead. |
| Cheap pruning baselines | The LLMLingua comparison is strong but the distractor fixture favors message-level IDF. | Compare ContextCompress against random drop, oldest/recency, BM25/lexical relevance, and maybe oracle upper bound on the same fixtures. |
| Real-agent repeated uncertainty | Real-agent rows are the broadest generalization evidence. | Add repeated or bootstrapped uncertainty where feasible; keep "not statistically significant" wording. |
| Routing baseline feasibility | ModelDowngrade can look like ordinary routing. | Include a concise feasibility matrix for RouteLLM/FrugalGPT/LLMSelector; run one if cheap, otherwise explain why not comparable. |
| Artifact package polish | MLSys rewards runnable systems work. | Provide `reproduce-lite` and `reproduce-paper` paths with expected runtime, API-key needs, costs, and generated outputs. |
| Related-work narrowing | Broad novelty claims are risky. | Use exact distinction: Agentc rewrites framework-emitted application-level traces at SDK boundaries under one runtime control plane. |

## Manuscript Gates

### Framing

| Gate | Required outcome | Status |
|---|---|---|
| One-sentence thesis | The intro and abstract converge on: structure-gated JIT optimization for multi-step LLM agent calls, with abstention and a label-free guard. | `OPEN` |
| No broad firstness | Avoid "first runtime optimizer for LLM agents" or equivalent. | `OPEN` |
| No semantic preservation overclaim | Use "does not significantly degrade under measured metrics" or "guarded by divergence," not "preserves behavior" without qualification. | `OPEN` |
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
| Abstract shortened | Reduce density; keep only the main thesis, strongest CC/LLMLingua result, guard result, and composition result. | `OPEN` |
| Contribution bullets scoped | Four bullets maximum: runtime/control plane, compression comparison, methodology, guard. | `OPEN` |
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
| Optimizer scaling checked | The complete-call 4–64 KiB x C=1/8/32 matrix meets the frozen tail target after audit-path remediation. | `FAIL`: median/throughput fixed; C=8/C=32 p99 still exceeds 1.2ms |

Recommended final experiment commands:

```bash
python bench/run_lcqa_warmup_n300.py
python bench/run_gaia_warmup.py
python bench/run_refiner_warmup.py
python bench/run_cc_sd_subadditivity_warmup.py
python bench/run_planner_ablation_rerun.py
python -m bench.guard_overhead_bench
python -m bench.optimizer_e2e_overhead
python -m bench.optimizer_e2e_scaling
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
| Python/native checks | `maturin build --release -m crates/agentc-profiler/Cargo.toml`, wheel install, and `pytest tests/ -v --tb=short` pass. | `OPEN` |
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
pip install -e ".[dev]"
pytest tests/ -v --tb=short
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
latexmk -pdf main.tex
grep -R "TODO\\|DROPIN\\|??\\|undefined" main.tex main_trimmed.tex *.log
```

Use the actual submission source filename if it differs from `main.tex`.

## Final Day Protocol

| Step | Required outcome | Status |
|---|---|---|
| Freeze branch | Create or identify the exact submission branch/commit. | `OPEN` |
| Close or defer issues | All MLSys-blocking `bd` issues are closed; all waived items have rationale. | `OPEN` |
| Re-run quality gates | Code, artifact, and PDF gates are re-run after final edits. | `OPEN` |
| Tag source | Create a local tag or commit marker for the submitted version. | `OPEN` |
| Save submitted PDF/source | Store the exact submitted PDF and source bundle in the agreed artifact location. | `OPEN` |
| Push everything | Push Git and Beads state so the submitted version is not stranded locally. | `OPEN` |
| Post-submit note | Record final submission ID, title, authors, artifact status, and any waived gates. | `OPEN` |

## Current Known Issues To Resolve

These were visible at the time this checklist was created.

| Issue | Why it matters |
|---|---|
| No tracked `references.bib` found. | `main.tex` calls `\bibliography{references}`; submission build will fail without it. |
| `main.tex` still contains a `DROPIN-INDEX`. | Draft scaffolding must not be submitted. |
| Guard experiments use full shadow sampling in repro scripts. | The paper must not imply that all reported guard behavior was measured at 2% sampling unless that is true. |
| Repo docs contain stale implementation/result statements. | Not always a paper blocker, but artifact reviewers may read README/specs. |
| Some appendix regeneration commands may not match actual script names. | Artifact reviewers will try them literally. |
