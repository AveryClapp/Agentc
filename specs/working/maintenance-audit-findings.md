---
title: Maintenance Audit Findings
status: active
last-updated: 2026-07-17
---

# Maintenance Audit Findings

Living census of Agentc repository health ahead of the MLSys 2027 submission
(est. deadline late October 2026; CFP not yet public as of 2026-07-17).

Produced by `agentc-maintenance-audit`. Append findings; never overwrite. Update
`Status` in place as items are resolved. Cleanup execution is governed by
`agentc-cleanup-workflow` and requires the preconditions in that skill.

## Audit Frame

| Field | Value |
|---|---|
| Snapshot | `16e395c` (main), 2026-07-17 |
| Dirty paths | `paper-intelligence/strategy-and-venues.md` (M); `AgentcV1.pdf`, `AgentcV2.pdf`, `.beads/dolt`, `.beads/dolt-wal` (untracked) |
| Audit question | What blocks MLSys submission and artifact evaluation? |
| Method | Six parallel read-only scouts (structure, links/paths, doc signal, code hygiene, rule reality, doc-vs-results drift); load-bearing findings independently re-verified by reading source |
| Not observed | `mypy` (not installed); `pytest` collection (deps missing); `latexmk` on `main.tex` (`acmart.cls` absent locally); `bd` dolt sync (server state unknown) |

### Evidence States

`verified defect` — read the file, traced the logic, confirmed.
`observation` — read the file, reporting without asserting a defect.
`inferred risk` — pattern-level signal, not yet confirmed by reading.

### Blast Radius

`PAPER` — affects the manuscript's integrity. `ARTIFACT` — affects artifact evaluation.
`CODE` — a real code defect. `DOCS` — internal only.

## Standing Verdicts (do not re-litigate)

Two things the audit **cleared**, recorded so future passes don't re-open them:

- **All nine rewrite rules are real, registered, tested code.** Zero stubs, no `todo!()`
  or `unimplemented!()` anywhere in `crates/` or `python/`. The P0 gate "validated vs
  implemented rules clear" **passes**: `main.tex:141` states precisely that 3 rules are
  validated in isolation, 2 in composition, 4 implemented-not-benchmarked, and that matches
  the code row by row. The 9th rule is `DeadOutputTruncation`, which the README omits.
- **The paper handled the planner-ablation contamination correctly.** `main.tex:1403-1410`
  reports the honest null ("CC+OB first-match and CC+OB composition are identical... *the
  surviving claim*") and its table matches the clean rerun CSV exactly. The stale numbers
  survive only in the README and a committed summary txt (MNT-014, MNT-020).

## Findings

### PAPER — manuscript integrity

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-001 | `tab:summary` cold-agent row (`support_qa`) has **no committed CSV and no driver**. Appendix cites `python -m bench.coldagent_eval`; that module has **zero matches in all git history**. `main.tex:497,809` lean on this row for the safe-abstention thesis. | `main.tex:833`, `:2013`; `git log --all` | verified defect | OPEN |
| MNT-002 | `sec:eval-cachehit` (whole section, n=20, "all 20 tasks return `plan_kind=cached`") has **no committed CSV and no driver**. Appendix cites `bench.cachehit_eval`; **never existed in git history**. | `main.tex:1620ff`, `:2021` | verified defect | OPEN |
| MNT-003 | Repro appendix names the wrong module path and wrong CSV filenames for the LLMLingua result. Data **does** exist as `llmlingua_accuracy_n100.csv` / `wikipedia_qa_comparison.csv`; appendix cites `llmlingua_comparison_n100.csv` / `wikipedia_qa_n39.csv`, and `bench.llmlingua_baseline` (actual: `bench.agents.llmlingua_baseline`). | `main.tex:1995` | verified defect | OPEN |
| MNT-004 | Intro contribution bullet says the guard yields **−0.7pp**; that is the *StateDrop-off control* value. The actual guard result is **−1.3pp** (eval + abstract agree). | `main.tex:174` vs `:1477`, `:1498` | verified defect | OPEN |
| MNT-005 | `agentc shutdown` is chained as a shell command in the repro appendix. No such CLI subcommand exists; it is a Python API (`agentc.shutdown()`). | `main.tex:2021`; `crates/agentc-cli/src/main.rs` | verified defect | OPEN |
| MNT-006 | `figures/fig8_throughput.pdf` is included in the paper but **no generator anywhere in the repo produces it**. Unreproducible published evidence. | `main.tex:871`; no `.py` writes `fig8` | verified defect | OPEN |
| MNT-007 | `DROPIN-INDEX` draft scaffolding — self-labeled "delete before submission" — still present in both `.tex` files. | `main.tex:26-37` | verified defect | OPEN |
| MNT-008 | `[T2]` naive-baseline attribution ablation (random / recency / BM25 vs IDF) **not yet run**. The paper argues for IDF scoring without benchmarking the cheap alternatives. Also a P1 acceptance lever in `PRESUBMIT.md`. | `main.tex:36` | observation | OPEN |
| MNT-009 | `main_trimmed.tex` is **12 lines longer** than `main.tex`. No prose was cut; content was relocated into two new appendices. The three `TRIM-CONDENSE` notes are unexecuted. Compiled V2 PDF is 17 pages. | `main_trimmed.tex:80,1196,1318` | verified defect | OPEN |

### ARTIFACT — artifact evaluation readiness

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-010 | **No `references.bib` in the repo — never committed in history.** 43 cite keys resolve to nothing; `main.tex` cannot build a bibliography from a clean checkout. The bib *does* exist off-repo: `AgentcV2.pdf` renders all 43 references correctly, so it lives in Overleaf. The repo is not the source of truth for the paper. | `main.tex:2089`; `git log --all -- "*.bib"` empty; `pdftotext AgentcV2.pdf` | verified defect | OPEN |
| MNT-011 | Template is `\documentclass[sigconf,9pt]{acmart}` — the ACM format. MLSys uses its own PMLR-lineage style. Reformatting will invalidate the page-budget assumption behind the trim, so it gates MNT-009. | `main.tex:1` | verified defect | OPEN |
| MNT-012 | **9 undeclared Python dependencies.** `crewai` and `langgraph` are imported by the *shipped package*, not just bench. `pyproject.toml` sets `testpaths = ["tests","bench"]`, so a clean `uv sync --extra dev && pytest` **fails at collection**. Also violates the `CLAUDE.md` no-new-deps guardrail. | `pyproject.toml`; `python/agentc/_provenance_frameworks/{crewai,langgraph}.py` | verified defect | OPEN |
| MNT-013 | Repro scripts assume `bench/fixtures/` exists, but it is gitignored and never built. `bench/repro/README.md` prerequisites list only venv + API keys. A reviewer's **first command fails**. | `bench/repro/guard_frontier.sh:30`; `.gitignore:161` | verified defect | OPEN |
| MNT-014 | README Quick Start reproduces the **disavowed cold-start regime**. `run_paper_ablation.sh` has zero warmup logic; the paper's numbers come from `run_lcqa_warmup_n300.py` et al., which the README never mentions. A reviewer following the README gets the numbers `main.tex:670-688` exists to disavow. | `README.md:162`; `bench/scripts/run_paper_ablation.sh` | verified defect | OPEN |
| MNT-015 | `DATA_MANIFEST.txt` points Tables 6 and 7 at **nonexistent scripts** (`run_gaia_warmup_n127.py`, `run_iterative_refiner_warmup_n50.py`; actual: `run_gaia_warmup.py`, `run_refiner_warmup.py`). | `DATA_MANIFEST.txt:21,28` | verified defect | OPEN |
| MNT-016 | `DATA_MANIFEST.txt` is stale: dated 2026-05-17, covers **6 of 18 tables**, and contains **zero** mention of the guard, cross-model, or overhead work — which is now the paper's headline. | `DATA_MANIFEST.txt` | verified defect | OPEN |
| MNT-017 | Guard-frontier driver scripts referenced in the findings doc live in `/tmp` and are gone. Those results are not reproducible from the repo as documented. (Partially mitigated by `bench/repro/*.sh`, committed 2026-06-11.) | `specs/working/guard-frontier-findings.md:282-285` | verified defect | OPEN |

### CODE — real defects

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-018 | **ParallelBranch emits a latency *ratio* denominated as USD.** `projected = 0.000001.max(mean_cost * 0.0) + latency_gain_ratio` → **0.500001 "USD"** for a 2-call fanout. The planner sorts by `projected_savings_usd` desc, so PB outranks ContextCompress (~0.0099) by 50×, wins the plan, then **no-ops on dispatch**. Latent only because PB never fires: zero `parallel` plans across all 221 result CSVs. One working `parallel_map` away from silently zeroing CC/MD/SD. The comment directly above claims "The rule never claims dollars it didn't save." | `crates/agentc-optimizer/src/rules/parallel_branch.rs:107`; `planner.rs:261` | verified defect | OPEN |
| MNT-019 | `StructuredTruncation`'s rewrite is **silently discarded when composed**. `apply_rewrite` only merges `messages` when the count shrinks; ST is a content-only, same-count mutation. Audit rows credit savings that never materialize. No paper claim depends on this. | `crates/agentc-optimizer/src/composition.rs:215` | verified defect | OPEN |
| MNT-020 | `_executor.dispatch` (async path) has no branch for `"composed"` — a `Plan::Composed` falls through to `run_original()`. **V2 composition is silently disabled on the async OpenAI path.** Latent: benches use the sync path. | `python/agentc/_executor.py` | verified defect | OPEN |
| MNT-021 | `python/agentc/_patches/_google.py` is a **19-byte stub** (`# Deferred to V1.1`), yet README advertises Google/Gemini SDK patching in three places. The **paper does not claim Gemini** — this is confined to the README. | `_google.py`; `README.md:14,62,95` | verified defect | OPEN |
| MNT-022 | 4 clippy warnings, all trivial: `double_ended_iterator_last` (`structured_truncation.rs:71`), `unnecessary_map_or` (`wiring.rs:212`), `let_and_return` (`planner.rs:~286`), `type_complexity` (agentc-memo). | `cargo clippy --workspace --exclude agentc-profiler` | observation | OPEN |
| MNT-023 | 6 dead Rust deps: `thiserror` declared in 4 crates with **zero uses repo-wide** (all error handling is `anyhow`); `safetensors` + `tokenizers` in agentc-embed appear only as string literals. | crate `Cargo.toml`s | observation | OPEN |

### DOCS — truth-in-documentation

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-024 | **`CLAUDE.md` says "No implementation code exists yet."** This is the file auto-loaded into every agent session. Reality: 7 crates, 443 Rust tests, 282 Python tests, a shipping CLI. Highest damage-per-byte defect in the repo. | `CLAUDE.md:54` | verified defect | OPEN |
| MNT-025 | README publishes **ModelDowngrade = 35.3%**, which `main.tex:681` explicitly calls **"spurious"** — a cold-start measurement bug. Warmup-corrected value is **11.4%**. The README is publishing a number its own paper documents as an artifact. Sub-errors: SE is 2.8 not 3.1; "n/a (unpaired)" is false (McNemar p=0.1797 exists). | `README.md:32` vs `main.tex:681,825` | verified defect | OPEN |
| MNT-026 | README cites ContextCompress at superseded n=100; paper uses warmup-corrected n=300 (33.9% cost / 34.0% tok). Accuracy **sign-flips**: −2pp → **+1.7pp**. | `README.md:33` vs `long_context_qa_warmup_n300.csv` | verified defect | OPEN |
| MNT-027 | README tells the **opposite scientific story** on CC+SD composition: "21.7%... sub-additive". Canonical data and paper: **CC+SD = 100.5% of additive ideal — super-additive**. | `README.md:42` vs `main.tex:1354,1370` | verified defect | OPEN |
| MNT-028 | README quotes the **retracted, contaminated** planner-ablation numbers (V1-CC+OB −2pp / V2 +0pp). Clean rerun: V1 and V2 are **identical** (+14pp, p=0.0156). The "V2's gate corrects V1's greedy mispick" narrative has no support in clean data — and the paper already retracted it. | `README.md:43` vs `planner_ablation_rerun.csv` | verified defect | OPEN |
| MNT-029 | `bench/paper_results/planner_ablation.summary.txt` still carries the **contaminated** table and V1-mispick prose, sitting beside `planner_ablation.csv` which holds clean numbers. Internal contradiction inside the evidence directory. Lifecycle: `retracted` — quarantine and label, **do not delete**. | `planner_ablation.summary.txt` | verified defect | OPEN |
| MNT-030 | Rule count disagrees across four docs: README says **8**, `specs/README.md` and `specs/optimizer.md` say **5**, `PRESUBMIT.md` says **9**. Code ships **9**. | — | verified defect | OPEN |
| MNT-031 | `specs/optimizer.md` **explicitly rejects the paper's V2 contribution**: "Rules never compose in a single plan... *Rejected: greedy composition with cumulative budget.*" Shipped code has `CompositionPlanner`, `Plan::Composed`, `AGENTC_COMPOSE`. README points reviewers at this spec. | `specs/optimizer.md:422` | verified defect | OPEN |
| MNT-032 | 13 of 90 tracked `.md` files are stale; only ~12 are trustworthy. Worst: `current-fit-and-publishability.md` (says MLSys "not yet ready"; lists as future work five things already done) and `results-experiments-and-repro.md` — the self-described **"authoritative evidence ledger" that does not know the guard result exists**. | `paper-intelligence/*` | verified defect | OPEN |
| MNT-033 | **Four competing trackers, none agreeing.** `bd` (frozen 2026-05-09; 6 open issues, all empty epics), `PRESUBMIT.md` (~50 gates, all `OPEN`), `guard-frontier-findings.md` (the de-facto real tracker), `claims-gaps-and-risks.md` (self-contradictory). Worse: bd IDs cited in the June docs (`bd-ljd`, `bd-e0s`, `bd-j3k`, `bd-4hy`) **do not exist in the tracker** — the dolt sync never happened, so `PRESUBMIT.md`'s "all bd issues closed" gate is unsatisfiable. | `.beads/issues.jsonl` | verified defect | OPEN |
| MNT-034 | README "~250 Python tests" → actual **282** `def test_` in `tests/` (+32 in `bench/`). "11-config sweep" → code now yields **19** configs (9 rules). bench/agents list omits 10 existing agents. | `README.md:78,85` | observation | OPEN |

### STRUCTURE — layout and repo state

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-035 | **Figures pipeline is split-brain.** The paper includes only from `figures/`, but 6 of 8 generators in `bench/paper_figures/` write to their *own* directory. Re-running a generator updates a PDF the paper never reads; the 6 duplicates were hand-copied. | `bench/paper_figures/fig{1,2,3,5,6,7}*.py` | verified defect | OPEN |
| MNT-036 | Figure **numbers collide with different content** across the two dirs: two different `fig4`s, `fig5`s, and `fig9`s. Easy to ship the wrong plot. | both dirs | verified defect | OPEN |
| MNT-037 | `.beads/dolt` (456K binary) + `.beads/dolt-wal` are untracked **and unignored**. Root cause: `.beads/.gitignore:2` has `dolt/` — a directory-only pattern — but `dolt` is a regular file. One `git add -A` from being committed. | `git check-ignore -v .beads/dolt` → exit 1 | verified defect | OPEN |
| MNT-038 | `paper-intelligence/AgentcV{1,2}.pdf` (1.4M total) untracked and unignored. These are the compiled paper snapshots (2026-05-13, **predating all June guard work** — do not read as current). Track or ignore. | `git status` | observation | OPEN |
| MNT-039 | Hardcoded absolute paths in tracked docs: `specs/working/HANDOFF.md:21` (`/Users/will/...` — a username that does not even match this machine) and `paper-intelligence/archive/pizza-import-plan.md:16`. | — | observation | OPEN |

## Pass 2 — Full Claim-Traceability Sweep (2026-07-17)

Every numeric claim in the abstract, intro, conclusion, and all 27 table/figure captions
traced to its backing CSV. **The evidence base is strong** — the three headline rule
matrices (110 cells), `tab:xmodel` in full, `tab:ccsd-composition`, `tab:planner-ablation`,
`tab:llmlingua-wikipedia`, and the overhead/throughput figures all reconcile exactly with
warmup-corrected CSVs. The defects are concentrated in **derived summary rows and captions** —
the surfaces reviewers read first.

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-040 | **The abstract is falsified by its own data.** Abstract, `tab:summary` caption, and conclusion all claim "real-agent accuracy deltas are **not statistically significant**." The `research_planner` row is **p=0.0117 — significant**. All four numbers on that row are also wrong: paper says 41.7% / 37.7% / **+4.0pp / p=0.22**; CSV says 41.5% / 37.5% / **+9.0pp / p=0.0117** (BF=1/FB=10). The error is in the paper's *favour* (a significant improvement), but the sentence as written is false. Also: CSV `n_total=100`, but the paper and filename say n=150. | `main.tex:58,810,830,1830,1939` vs `research_planner_warmup_n150.csv` | verified defect | OPEN |
| MNT-041 | **`tab:mdcc-orthogonality` is mixed-provenance; its accuracy block comes from the disavowed cold-start regime.** The 95.2%-of-additive-ideal figure (which is in the **abstract**) correctly derives from `md_cc_orthogonality_warmup.csv` (74.1234/77.8277). But the accuracy column matches `md_cc_composed.csv` — a **non-warmup** run — exactly. The two disagree on every value: <br>• non-warmup: baseline 55.0%, CC +5.0, MD −5.0, CC+MD **+5.0pp, all p=1.0** <br>• canonical warmup: baseline 80.0%, CC −10.0, MD −10.0, CC+MD **−20.0pp, p=0.125** <br>The claim "no accuracy interference (all McNemar p=1.0 at n=20)" is drawn from the run `main.tex:670-688` exists to disavow. Honest restatement: accuracy is directionally negative (−20pp, p=0.125, n=20, underpowered). | `main.tex:1322,1339-1345` vs both CSVs | verified defect | OPEN |
| MNT-042 | **The abstract's headline guard number has no committed CSV.** "−42.7pp → −1.3pp (97% of damage prevented)" — **both endpoints unbacked at n=150**. `analyst_qa_sd_gen.csv` contains exactly **one row**: StateDrop-only, 91.3%, **−0.7pp**, p=1.0000. No committed CSV holds the n=150 unguarded run (49.3%, −42.7pp) or the n=150 lexical guard run (−1.3pp). Only the **n=200 frontier is backed**: `gsweep_tradeoff_off_an.csv` = −49.5pp → `..._normalized_an_0.20.csv` = −3.5pp (93% prevented). | `main.tex:60-61,1476,1498` | verified defect | OPEN |
| MNT-043 | **Supersedes MNT-004 — the discrepancy resolves in favour of the INTRO, not the eval.** The intro's **−0.7pp** is exactly what `analyst_qa_sd_gen.csv` records. The eval's **−1.3pp** has no committed backing. (Earlier reading of MNT-004 was backwards.) | `analyst_qa_sd_gen.csv` | verified defect | OPEN |
| MNT-044 | `tab:mdcc-orthogonality` "Proj. savings/call" column (2.82/7.51/9.88/10.33 mUSD) has **no backing CSV**, inverts the CC:MD ordering vs the warmup CSV, and its own printed values imply **95.6%**, not the 95.2% stated two rows below. `DATA_MANIFEST.txt:98` records "95.6%→95.2%" as a correction — the table still carries pre-correction values. | `main.tex:1340-1345` | verified defect | OPEN |
| MNT-045 | `tab:summary` autogen CC-only token savings: paper says **23.5%**, CSV says **26.00%**. Every other cell on that row matches exactly. | `main.tex:828,1452,1824` vs `autogen_bridge_warmup_n300.csv` | verified defect | OPEN |
| MNT-046 | `tab:oracle` EM column is wrong and **inverts the sign of CC's effect**. Paper: baseline 57.0% → CC-active 58.3% (CC improves EM). CSV: shared baseline is **58.7%**; CC-active configs span 56.7–57.7% (**−1.7pp**). 58.3% is the *CacheHit-only* config, which has **0 CC fires**. Caption claims "all figures read directly from `traces.db`; no values are derived." | `main.tex:1204-1206` vs `hotpot_real-contextcompress-n300-warmup.csv` | verified defect | OPEN |
| MNT-047 | `tab:attribution` (−7.0/+6.7, +1.9/+6.7, +34.8/+34.9) has **no backing CSV**. Same numbers repeated at `:131,505,758`. | `main.tex:765-779` | verified defect | OPEN |
| MNT-048 | Agentc's **"53% token reduction"** on the distractor fixture is unbacked — `agentc_hotpot_n100.csv` has **no token column at all**. LLMLingua's 53.1% *is* backed. The parity claim "both achieve 53%" rests on an unmeasured half. | `main.tex:66,1254,1291` | verified defect | OPEN |
| MNT-049 | p=0.0013 is labelled **"McNemar exact"** but the summary txt records a **continuity-corrected χ²** (chi2=10.316). `main.tex:786` asserts "the exact binomial formulation is used throughout (`exact=True`)". Exact binomial for BF=17/FB=2 is ≈**0.00073**. The companion 4.66e-10 *is* exact — so two cells of the same column use different tests. | `llmlingua_accuracy_n100.summary.txt:11-13` | verified defect | OPEN |
| MNT-050 | Two table captions state the wrong baseline: `tab:cc-matrix` says 58.3% (true **58.0%**, 174/300); `tab:hotpot-matrix` says 57.3% (true **58.7%**, 176/300). Both quote an all-on or off-config value as the baseline. | `main.tex:937,1161` | verified defect | OPEN |
| MNT-051 | CC fire-rate range "261–286 (87–95%)" excludes a config at **255 (85%)**. True range **255–286 = 85–95%**. Repeated 4× (`:459,909,966,1650`). | `long_context_qa-contextcompress-n300-warmup.csv` | verified defect | OPEN |
| MNT-052 | `analyst_qa` baseline stated as 92.7%; per-task count gives **92.0%** (138/150). The sentence is self-contradictory: 92.7 − 49.3 = 43.4 ≠ 42.7, whereas 92.0 − 42.7 = 49.3 ✓. | `main.tex:1476` | verified defect | OPEN |
| MNT-053 | Provider-generalization (n=50) rests **only** on `unified_agent_summary.csv` — a non-warmup V1 artifact that still publishes the retracted SD tok = 9.6%. The two MD numbers (14.7%, 31.1%) are cold-start MD measurements — the exact condition that inflated MD to the "spurious 35.3%". No warmup replacement exists. | `main.tex:963,1038`, fig:provider | inferred risk | OPEN |
| MNT-054 | `analyst_qa` is called "an **unseen** agent" in the intro, but `:1472` says "we built a second agent". | `main.tex:174` vs `:1472` | verified defect | OPEN |
| MNT-055 | Rounding/range overstatements: "p ≥ 0.39" (true 0.3877); `tab:sd-matrix` "SE ±2.0–3.4" (true 3.8); "x ≥ 96%" / "96–100% retained" (true **95.8%**); "~91% of damage prevented" vs **93%** in `tab:xmodel`; `tab:md-matrix` "SE ±3.0" (true 2.8–3.3). | various | verified defect | OPEN |
| MNT-056 | Guard overhead "18 µs" and "total API spend under $25" have **no committed output**. `bench/guard_overhead_bench.py` exists but commits no CSV/txt; no spend ledger anywhere. | `main.tex:801,1492,1900` | verified defect | OPEN |
| MNT-057 | `tab:repro` points `tab:summary`'s autogen data at **two conflicting CSVs**: `autogen_bridge-n200-warmup.csv` (n=200, 38.48% tok) and `autogen_bridge_warmup_n300.csv` (the one actually used). | `main.tex:2009,2031` | verified defect | OPEN |

### Root cause of MNT-041, MNT-044, MNT-053

Retracted and superseded CSVs (`md_cc_composed.csv`, `unified_agent_summary.csv`,
`new_agents_ablation.csv`, `planner_ablation_contaminated_original.csv`) sit in
`bench/paper_results/` under names **confusable with canonical data**. Nothing in the
filename marks them as pre-warmup-correction. That is how a disavowed accuracy block ended
up in a headline table. Fix the naming convention (e.g. a `retracted/` subdirectory or a
`_PRE-WARMUP` suffix) and the class of error closes.

## Pass 3 — Spec Drift + ParallelBranch Root Cause (2026-07-17)

### MNT-058 supersedes the "latent" framing of MNT-018

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-058 | **"ParallelBranch never fires" was never measured — it is an instrumentation gap, not a firing failure.** No bench script counts PB: result CSVs carry only `cc_fire_count` and `sd_fire_count`, and a repo-wide grep for `rule='ParallelBranch'` / `plan_kind='parallel'` in `bench/` returns nothing. ("ParallelBranch" in the 221 CSVs is an *ablation config label*, never a fire count.) `optimizer_audit.db` lives under `/tmp` and is never committed. A traced Python-side repro shows the precondition chain **would fire** on `rag_summarizer` (fan-out n=2, concrete disjoint deps, hot gate passed). **Consequence: MNT-018's `0.500001` USD ranking bug may be LIVE, not latent** — PB would outrank every cost rule (~1e-4 USD), be selected solo, and suppress CC/SD/OB on those calls, then no-op. Cannot be settled without re-running `rag_summarizer` with a persistent storage dir (a paid experiment — not run). | `bench/paper_results/*.csv` headers; `rules/parallel_branch.rs:107`; `composition.rs:92-99` | inferred risk (high severity) | OPEN |
| MNT-059 | `build_call_dict_anthropic` never calls `get_parallel_peer()`; only the OpenAI builder does. ParallelBranch is **structurally unable to fire on any Anthropic-routed agent**. | `python/agentc/_patches/_optimizer_glue.py:382` vs `:189-191` | verified defect | OPEN |
| MNT-060 | **`hit_count` / `last_hit_at` are never updated in production code.** The only `UPDATE ... SET last_hit_at` in the repo is at `eviction.rs:159`, inside `#[cfg(test)] mod tests` (which begins at line 89). Consequences: `memoization_stats.total_hits` and `estimated_savings_usd` are **always 0**, and `lru_evict` (`ORDER BY last_hit_at ASC`) degenerates to insertion order — **it is FIFO, not LRU**. | `crates/agentc-memo/src/eviction.rs:64,89,159` | verified defect | OPEN |
| MNT-061 | `agentc cache stats` reads span attributes (`agentc.cache.result`, `.saved_cost_usd`, `.saved_tokens`) that **nothing in the repo ever writes** — `_memoize.py` does no span work. The entire hit-rate/savings section of the command is structurally zero. | `crates/agentc-cli/src/main.rs:1240-1298` vs `python/agentc/_memoize.py:206-268` | verified defect | OPEN |
| MNT-062 | Memoization TTL/LRU sweep relies on `ON DELETE CASCADE` per spec, but the real DDL has **no FK and no CASCADE**, and `db.rs:115` sets `PRAGMA foreign_keys = OFF`. `memoization_lsh_bucket` and `memoization_embedding` rows are **orphaned forever** on every eviction, and stale buckets keep feeding LSH candidate retrieval. | `agentc-memo/src/schema.rs:45-56`; `eviction.rs:29,61` | verified defect | OPEN |
| MNT-063 | **ARTIFACT-CRITICAL: model weights are not bundled.** `specs/profiler.md:175,845` and `memoization.md:384` claim potion-base-8M is compiled in via `include_bytes!()` with "**no download-on-first-use, no network dependency**." Reality: `agentc-embed/src/model.rs:27-51` loads from `~/.agentc/models/potion-base-8M/` at runtime and errors telling you to run `scripts/download_model.sh`. **On a fresh machine embeddings are NULL**, so the `redundant_call` + `retry_storm` waste detectors and memoization's entire LSH tier **silently no-op**. An artifact reviewer gets degraded behavior with no error. | `crates/agentc-embed/src/model.rs:3-6,27-51` | verified defect | OPEN |
| MNT-064 | `bench/fixtures/rag_summarizer.json` has **no committed generator** — no `bench/build_*.py` references `rag_summarizer` — yet `run_rag_warmup_n200.py:7` asserts the fixture exists. The rag experiment cannot be reproduced from a clean checkout. | `bench/run_rag_warmup_n200.py:7-8` | verified defect | OPEN |
| MNT-065 | Heavy drift in `specs/profiler.md` and `specs/memoization.md` (both predate the May code). Highlights: `agentc pricing update` subcommand **does not exist**; `_adapters/` directory **does not exist** (version dispatch is a log line, and the documented "httpx transport fallback" is a **log string with no httpx patching anywhere**); per-call memoize opt-in via `extra_headers` **not implemented** (zero grep hits); the `[memoization]` config section is **rejected** by `_config.py` as an unknown key; `max_bytes` has no implementation; schema types, the Rust `Cache` trait, and the 4-function FFI boundary (now 18 functions) all mismatch. | `specs/profiler.md`, `specs/memoization.md` | verified defect | OPEN |

**Note:** MNT-060 through MNT-063 are real defects in the memoization/profiler subsystems, but
**no headline paper claim depends on them** — CacheHit is not a validated rule, and `sec:eval-cachehit`
is already unbacked (MNT-002). They are ARTIFACT and CODE blast radius, not PAPER.

## Do Not Touch Yet

| Path | Reason | Unblock evidence needed |
|---|---|---|
| `bench/paper_results/*.csv` backing published claims | `protected` — immutable score-bearing evidence | Never edit. Correct the claim, not the data. |
| `planner_ablation_contaminated_original.csv` | `retracted` — documents a correction; required for traceability | Quarantine + label only. Deletion is never authorized. |
| `bench/paper_results/planner_ablation.summary.txt` | `retracted` content, but consumers unknown | Confirm nothing reads it, then quarantine alongside the contaminated CSV (MNT-029) |
| `figures/fig8_throughput.pdf` | `unknown` provenance — no generator | Locate or reconstruct the generator before touching (MNT-006) |
| `main.tex` / `main_trimmed.tex` | Shared with the co-author and Overleaf | Confirm which is the source of truth before editing (MNT-010) |

## Dependency Order

1. **MNT-010 (bib) and MNT-011 (template) gate everything downstream.** Template determines
   page budget; page budget determines what gets cut (MNT-009); what gets cut determines which
   experiments have room to be reported. Do these first.
2. **MNT-001 / MNT-002 (unbacked results) gate the claims-match-evidence P0 gate.** Independent
   of the above; can run in parallel.
3. **MNT-012 / MNT-013 (artifact first-contact failure)** gate any artifact-eval submission.
   Independent; can run in parallel.
4. Doc corrections (MNT-024 … MNT-034) depend on nothing and are safe to batch.
5. MNT-018 (ParallelBranch) should land before any future `parallel_map` work, or it becomes live.

## Blind Spots

- `mypy` never ran (`strict = true` is configured). Type state is **unverified**; expect failures given MNT-012.
- `pytest` was never executed — only statically counted. The "~250 tests pass" claim is **not observed**.
- `main.tex` has **never been compiled on this machine** (`acmart.cls` absent). All manuscript findings are structural, from reading source.
- Why `ParallelBranch` never fires despite correct plumbing is **unresolved** (MNT-018). Root cause unknown.
- The MLSys 2027 CFP is not public. Deadline (~late Oct 2026) is inferred from the 2026 cycle, not confirmed.
