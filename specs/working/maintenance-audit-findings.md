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

## Pass 4 — Engineering Practice (2026-07-17)

Four lanes: docs consolidation, Rust practice, the fail-open guarantee, and CI/secrets/tooling.
Four further lanes (bench duplication, test quality, Python swallowed-errors, determinism) are in flight.

### Self-correction

| ID | Finding | Status |
|---|---|---|
| MNT-066 | **MNT-033 is itself stale.** It says bd is "frozen since 2026-05-09, 6 open issues, all empty epics." That became false the same day the audit was written — bd now holds 59 open issues mapping 1:1 to the remediation plan. **bd is the only tracker that is current.** The other three must be re-scoped to non-tracking roles, not merged. | SUPERSEDES MNT-033 |

### BUILD & CI — the artifact dies before the science

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-091 | **`cargo test` is RED on main and has been for ~19 days.** `test_pricing_staleness_not_stale` hardcodes the author's calendar into an assertion: *"Bundled pricing is dated 2026-03-19, **which is today**"*. `STALENESS_DAYS = 90`; `data/pricing.json` is dated 2026-03-19; it is now +117 days. The test **self-detonated with zero code change** and gets worse daily. REPRODUCED: `test result: FAILED`. Anyone who clones the artifact and runs `cargo test` fails on line one. | `crates/agentc-analyzer/src/cost.rs:623-628`, `:23` | verified defect | OPEN |
| MNT-092 | **`cargo build --release` — the README's FIRST command — fails on a clean clone.** PyO3 libpython link error (`Undefined symbols: _PyBool_Type…`) on `agentc-profiler` (`crate-type=["cdylib","rlib"]` + `extension-module`). **CI cannot catch it because CI runs `cargo check`** (no link step). The maintainers already know — `ci.yml:36-40` documents the caveat verbatim and excludes the crate — but never propagated it to the README. REPRODUCED. | `README.md:125`; `.github/workflows/ci.yml:35` | verified defect | OPEN |
| MNT-093 | **No `LICENSE` file**, while `Cargo.toml:16` and `pyproject.toml:9` both declare MIT. MIT requires the notice be distributed with the work; metadata alone is an incomplete grant. Blocks crates.io/PyPI publish and is the first item on an artifact checklist. | root | verified defect | OPEN |
| MNT-094 | **CI is green-washed.** It exists (2-OS matrix, ~40 runs) but is configured so it structurally cannot catch the two failures above: `cargo check` not `cargo build` (misses the link error); `pytest tests/` not bare `pytest` (dodges the `bench/` undeclared-dep gap, since `pyproject.toml` sets `testpaths=["tests","bench"]`); clippy deliberately non-blocking (`-D warnings` removed). No Makefile/justfile/pre-commit/dependabot. | `.github/workflows/ci.yml:11-14,35,62` | verified defect | OPEN |
| MNT-095 | `TOGETHER_API_KEY` gates **6 bench scripts including the cross-model runs that are a paper claim**, and is documented **nowhere** (not README, PRESUBMIT, or specs). There is **no `.env.example`**, despite scripts erroring with *"add it to .env"*. README documents only `OPENAI_API_KEY` and `HF_TOKEN`. | `bench/` | verified defect | OPEN |
| MNT-096 | **7 env vars are documented nowhere**, including `AGENTC_SHADOW_DIVERGENCE_MODE` and `AGENTC_SHADOW_DIVERGENCE_BUDGET` — both knobs behind the **headline guard result**. `AGENTC_OPTIMIZE` (63 code references) gets zero README mentions. Two competing config systems: a declared map in `_config.py` (4 vars) vs ad-hoc `os.environ.get`/`env::var` scattered across 4 files. 18 `AGENTC_*` + 9 `BENCH_*` + undocumented `GSWEEP_*`/`AB_*`/`GE_*` families. | `_config.py:18-23`; `optimizer/config.rs`; `wiring.rs` | verified defect | OPEN |
| MNT-097 | No toolchain pinning: no `rust-toolchain.toml`, no `.python-version`. CI floats on `dtolnay/rust-toolchain@stable`, so a future Rust release silently breaks the artifact. (Lockfiles ARE committed — the one thing done right.) | — | observation | OPEN |
| MNT-098 | Inverted dependency: `wikipedia>=1.4.0` is a **core runtime dep** but is imported by exactly one bench script. Every `pip install agentc` pulls a web scraper the runtime never touches — while the deps the benchmarks actually need are undeclared (MNT-012). | `pyproject.toml:12` | verified defect | OPEN |
| MNT-099 | **POSITIVE — secrets are clean.** Regex sweep for `sk-`, `sk-ant-`, `hf_`, `ghp_`, `AKIA`, `xox*`, `AIza`, PEM blocks over the working tree **and the full git history** (`git log --all -p`, all branches): **zero hits.** Nothing was ever committed then removed. `.env` is gitignored and untracked. Committed dataset fixtures: **zero** — no redistribution risk; HotpotQA/GAIA/Wikipedia are fetched at build time. | — | verified (clean) | CLOSED |

### RUNTIME SAFETY — the fail-open guarantee is false as stated

**Scope note:** the fail-open claim is **not in `main.tex`** — it exists only at `README.md:210`. This is an
engineering + README defect, **not a paper defect**. The Rust/FFI half of the guarantee is genuinely solid:
double-netted `catch_unwind`, and all 10 production-path `unwrap`/`expect` sites are provably unfirable.
The Python half has holes.

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-085 | **Anthropic streaming `__exit__` closes the stream, then reads it — outside any try/except.** Line 380 calls `self._stream_mgr.__exit__()` (closes); line 384 calls `get_final_message()` (which in the real SDK runs `until_done()` and contains a bare `assert`); the `try:` doesn't start until **line 397**. A user who breaks out of `with client.messages.stream(...)` early gets **an exception that exists only because Agentc is installed** — and if their `with` body raised, Agentc's exception **masks their original one**. This is precisely the failure fail-open promises cannot happen. | `python/agentc/_patches/_anthropic.py:380,384,397` (sync) and `:561,574` (async) | verified defect | OPEN |
| MNT-086 | **Async Anthropic streaming never `await`s `get_final_message()`** (it is `async def` in the SDK). It returns an un-awaited coroutine; `_extract_response_attrs(coroutine)` silently yields `{}`. **Every async Anthropic streaming span captures zero response attributes** — no model, no tokens. Silent data loss, plus a `RuntimeWarning`. | `_anthropic.py:561` | verified defect | OPEN |
| MNT-087 | `_wrap_create_async` invokes **no optimizer at all** — async Anthropic is never optimized. | `_anthropic.py:454` | verified defect | OPEN |
| MNT-088 | crewai async adapter: unguarded `await` (`crewai.py:44`), and `install()` gates only on `callable()` — there is no `iscoroutinefunction` check anywhere in `_provenance_frameworks/`. If `Task.execute_async` is sync, Agentc mutates its return type AND raises `TypeError` into the user's agent. No test exercises the async branch of crewai or autogen. | `crewai.py:44` | verified defect | OPEN |
| MNT-089 | **The fail-open test is tautological.** `rule_panic_is_converted_to_pass_through` re-implements `catch_unwind` **inside the test body** rather than calling the production binding. **Deleting `catch_unwind` from `agentc-profiler/src/lib.rs:762` would not fail this test.** It proves the Rust stdlib works. For a safety-critical property this is false assurance. No end-to-end test injects an optimizer fault and asserts a patched OpenAI/Anthropic call still returns the user's response. | `crates/agentc-optimizer/tests/fail_open.rs:120-141` | verified defect | OPEN |
| MNT-090 | `maybe_shadow_record` issues a **real, billed LLM call synchronously inside the user's call** on 2% of rewritten calls by default. Correctly guarded, but it is a latency and cost surprise, not a free sample. Worth an explicit note in the paper and README. | `_optimizer_glue.py:591` | observation | OPEN |
| MNT-100 | `fail_open` is a **config flag** (`_config.py:32`, default True); with `fail_open=False`, span-emit failures re-raise. The README states the guarantee unconditionally. | `_openai.py:466` | verified defect | OPEN |

### RUST PRACTICE — schema, hot path, concurrency

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-072 | **The paper's overhead number excludes the audit write.** The stopwatch stops immediately after `rust_plan`, *before* `write_plan_audit` — which re-parses **both** JSON payloads from scratch (two full deserializations of the entire prompt, to recover fields the caller already had typed) and then does a synchronous SQLite `INSERT`. `main.tex` reports p50=76µs/120µs and calls overhead "sub-millisecond." **Magnitude of the unmeasured part is UNKNOWN — needs a real end-to-end measurement.** | `crates/agentc-profiler/src/lib.rs:762-772` | verified defect | OPEN |
| MNT-073 | `optimizer_audit.db` is opened with a bare `Connection::open` — **no PRAGMAs at all** — so it runs `journal_mode=DELETE` + `synchronous=FULL`: a rollback journal created, written, **fsynced**, and unlinked **per plan call**. A comment at `wiring.rs:185` asserts *"SQLite's WAL mode handles concurrent access."* WAL is never enabled. (Second self-refuting comment found, after ParallelBranch's.) | `crates/agentc-optimizer/src/wiring.rs:179,185` | verified defect | OPEN |
| MNT-074 | **Worst hot-path defect in the tree.** Every memoization cache lookup calls `SqliteCache::from_shared`, which **opens a NEW SQLite connection and re-runs the entire 9-statement DDL batch** (3 CREATE TABLE, 5 CREATE INDEX, 1 CREATE VIEW) — then does the actual SELECT, then closes — **all while holding the process-global profiler mutex**. `insert` and `maintenance` do it too. The optimizer gets this right (one long-lived cache at wiring); the memo FFI does not. | `agentc-memo/src/ffi.rs:279-286`, `cache.rs:93-94` | verified defect | OPEN |
| MNT-075 | **There is no working migration path.** `migrate_db` has `let migrations_applied = 0;` hardcoded and a comment where migrations "would go" — it bumps `user_version` to 1 **without creating a single table**. Meanwhile `create_db` `bail!`s on any version mismatch. So bumping `SCHEMA_VERSION` to 2 **bricks every existing `~/.agentc/traces.db`**, and the documented escape hatch (`agentc migrate`) prints "0 migrations applied". Three DBs, three incompatible schema disciplines (only `traces.db` has versioning or PRAGMAs at all). | `agentc-core/src/db.rs:123-136,320-351`; `cli/main.rs:1068` | verified defect | OPEN |
| MNT-076 | **The cross-process merge lock is defeatable.** `try_acquire` opens the lockfile with `.truncate(false)` and never writes to it, so its mtime is frozen at first creation. `is_lockfile_stale` declares it stale at >60s. So after the first minute of an install's life the lock is **permanently "stale"**: process B times out, deletes the lockfile, creates a fresh one, and `flock`s a **different inode** while A still holds the old one. **Both processes then merge into `traces.db` concurrently.** The staleness check keys on the lockfile's mtime rather than the holder's liveness. | `crates/agentc-core/src/merge.rs:62,84-89,125-136` | verified defect | OPEN |
| MNT-077 | **The shadow/accuracy reporting surface is structurally dead.** `rule_divergence` is `SELECT`ed at `reporting.rs:162,414` but there is **no `INSERT INTO rule_divergence` anywhere in the tree**. `plan_audit.shadow_sampled` is hardcoded `false` and `measured_savings_usd` hardcoded `None` at the only production writer. So `if row.shadow_sampled` is never true, and "measured savings" **always silently falls back to projected savings**. NOTE: this does NOT invalidate the guard results — the guard folds divergence through an in-memory DashMap and demonstrably works. What is dead is `agentc optimize report`'s accuracy surface. | `reporting.rs:143,149,162`; `profiler/lib.rs:834-837`; `budget.rs:160-197` | verified defect | OPEN |
| MNT-078 | The audit "ring buffer" never prunes. `audit::prune` and `audit::insert_batch` have **no production caller** (tests only). `RING_BUFFER_CAP = 10_000` is enforced nowhere. `optimizer_audit.db` grows one row per LLM call, forever. | `crates/agentc-optimizer/src/audit.rs:19,86,122` | verified defect | OPEN |
| MNT-079 | **Layering inversion:** `agentc-core` (the lowest-level crate) depends on `agentc-memo`, solely so `merge.rs:277` can call `agentc_memo::ensure_schema`. Meanwhile memo's DDL declares `REFERENCES output_content(content_id)` — a table owned by core. **Circular schema ownership of `traces.db`**, and it is the root cause of MNT-075 (no crate owns the file, so its `user_version` cannot describe its contents). | `agentc-core/Cargo.toml`; `memo/schema.rs:30` | verified defect | OPEN |
| MNT-080 | Per-plan allocations on the hot path: `Budget::is_disabled` builds **two `(String,String)` tuples — 4 heap allocs — per rule per plan** → **36 String allocations per `plan()`** with 9 rules, just to probe a HashMap. Every rule's `propose` does `call.clone()`, a **deep copy of the entire prompt** (`Call` owns `Vec<Message>` with `String` content) — 4 applicable rules on a 50KB prompt = 200KB copied per plan, and only one proposal wins. | `budget.rs:139-152`; `planner.rs:220,246,248` | verified defect | OPEN |
| MNT-081 | Swallowed `ALTER TABLE` → **silent total optimizer disable**. `optimizer/schema.rs:105-108` swallows *every* error from the `ADD COLUMN output_token_p99` ALTER, not just "duplicate column". If it fails for any other reason, `CostModel::warm_from_db` errors → `build_optimizer` errors → the profiler **silently falls back to `Optimizer::empty()`**. The optimizer disables itself completely and prints one stderr line. | `optimizer/schema.rs:105-108` → `profiler/lib.rs:724-737` | verified defect | OPEN |
| MNT-082 | One failed merge poisons every subsequent merge in the run: `merge_all_pending` reuses one connection; a failed `COMMIT` leaves the transaction open, the `DETACH` is `let _ =` and fails silently, and the next `ATTACH` fails with "already in use" — cascading. | `merge.rs:338,457-477` | verified defect | OPEN |
| MNT-083 | **Consolidation proposal (~300–350 lines removable, and each item closes a live defect):** <br>**C1** `open_tuned()` — one function applying WAL + `synchronous=NORMAL` + `busy_timeout`. Replaces 4 copy-pasted PRAGMA blocks **and fixes the 5 sites that apply none** (fixes MNT-073). <br>**C2** One schema owner per DB file + a real `MIGRATIONS` table (fixes MNT-075, severs MNT-079). <br>**C3** `RewriteRule` trait defaults + a shared `project_savings(profile, fraction)` helper — **this makes the ParallelBranch units bug (MNT-018) impossible to write**; 7 rules currently reimplement the savings math independently. <br>**C4** One hex codec (3 implementations exist; one does `format!` per byte = 32 allocs/hash). <br>**C5** Shared `#[cfg(test)] testkit` — ~130-150 lines of duplicated `Call` literals across 9 rule files. | — | proposal | OPEN |
| MNT-084 | Stale module docs: `rules/mod.rs:1` says *"The five rewrite rules"* (there are nine) and *"Rules never compose in a single plan"* (composition ships and is the default). `optimizer/lib.rs:8-9` says rules "ship in later beads (O3–O5)" — they shipped. `agentc-optimizer` exports its entire internals: all 14 modules `pub`, ~45 re-exported types, 156 `pub` items; `shadow::text_divergence` is `pub` and called from nowhere. | `rules/mod.rs:1`; `optimizer/lib.rs:8-9` | verified defect | OPEN |

### DOCS CONSOLIDATION

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-067 | **Every step of the artifact reviewer's read path is broken.** (1) `README.md` → build command fails (MNT-092). (2) `bench/repro/README.md` → first command fails, no fixture bootstrap (MNT-013). (3) `DATA_MANIFEST.txt` → covers 6 of 18 tables (MNT-016). (4) `specs/optimizer.md` → explicitly rejects the paper's own contribution (MNT-031). Four steps, four defects. **This is the highest-priority repair in the audit.** | — | verified defect | OPEN |
| MNT-068 | Consolidation map produced: **36 active docs → 22**. Single owners: bd = status; `DATA_MANIFEST.txt` = the one evidence ledger; `bench/repro/README.md` = the one repro path; `PRESUBMIT.md` re-scoped to a final pre-flight gate with its Status column deleted. 7 docs archived, 1 deleted. Full disposition table in the pass-4 scout output; see remediation plan Phase 6. | — | proposal | OPEN |
| MNT-069 | **The `paper-intelligence` ID system has zero external consumers — retire it.** `RES-`/`CLM-`/`GAP-`/`EXP-`/`RR-`/`WP-`/`QST-` IDs appear in **6 files, all inside `paper-intelligence/`**. `main.tex` has **never cited one** (0 occurrences); neither has `DATA_MANIFEST.txt`, `bench/repro/`, `PRESUBMIT.md`, or the guard findings. It did not "silently break" in June — it was **load-bearing for nothing**, and the July audit responded by minting a *fifth* namespace (`MNT-`) rather than extending it. **Keep `LIT-`** (feeds the bibliography; literature doesn't rot) **and `DEC-`** (append-only decision log; `DEC-007` is load-bearing). Retire the rest — they survive in git history and `archive/`. | — | proposal | OPEN |
| MNT-070 | `orchestration-CLAUDE.md` is **not autoloaded** (needs a manual symlink), last touched 2026-03-19, and instructs agents to use `br` while root `CLAUDE.md` says `bd` — an active tool-name conflict. | root | verified defect | OPEN |
| MNT-071 | `.beads/README.md` is upstream **vendor marketing** (`curl\|bash` install, "✨ AI-Native Design", emoji feature list). Zero Agentc content. | `.beads/README.md` | verified defect | OPEN |

### DETERMINISM & REPRODUCIBILITY — P0 gate FAILS on temperature

MLSys P0 gate: *"every stochastic experiment states temperature, sample count, and paired/statistical method."*
Sample count PASSES (paper discloses "each configuration is run once"). Stats method PASSES (McNemar exact +
5,000-iter bootstrap, stated and implemented). **Temperature FAILS.**

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-101 | **The two headline isolation matrices run at temperature 1.0, and the paper never says so.** `bench/agents/long_context_qa.py` (ContextCompress, n=300, §6.1) and `bench/agents/gaia_router.py` (ModelDowngrade, n=127, §6.2) — plus the shared `_runtime.py` `call_llm` helper — contain **zero occurrences of `temperature`**. They call `client.chat.completions.create(model=..., messages=...)`, so they run at the OpenAI **default of 1.0**. `batch_classifier` (CacheHit), `long_context_qa_{anthropic,concurrent,hf}` are the same. There is **no global determinism/seed policy statement anywhere in `main.tex`**. <br>**Severity note:** this does NOT invalidate the results — paired McNemar on identical tasks absorbs run-to-run noise, and single-trial is disclosed. The failure is **omission, not misstatement**: every experiment where the paper *claims* temp=0, the code genuinely sets temp=0. But the paper's determinism framing will be read as covering the headline matrices, and it does not. | `bench/agents/long_context_qa.py:72`; `gaia_router.py`; `_runtime.py:200` | verified defect | OPEN |
| MNT-102 | **No file in the repo sets a `seed`, a `top_p`, or a model-snapshot pin on any LLM call.** (The `seed=` hits in `bench/` are all `random.Random(42)` for fixture construction, not decoding.) Model-snapshot pinning is the control that is actually missing — see MNT-103. | repo-wide | verified defect | OPEN |
| MNT-103 | **The authors' own artifact documents that temperature=0 did not buy reproducibility.** `planner_ablation.summary.txt`: *"Re-runs at temperature=0 yield 100% baseline accuracy — the model's behavior on this fixture at temp=0 has drifted since the original ablation, making a valid shared-baseline comparison impossible."* `composition_qa.py` has set `temperature=0` since its first commit, so this is **not** a temperature bug: the same fixture on the same model moved **32% → 100% baseline accuracy** with decoding stochasticity nominally eliminated. Consequences: (a) the paper's determinism argument (`main.tex:766`, "temp-0 isolates deterministic behavior") is contradicted by its own artifact; (b) **`tab:planner-ablation` is not reproducible today by the authors' own note** — if baseline is now 100%, "+18pp" cannot be recovered; (c) the real missing control is **model-snapshot pinning**, which the paper does not do. | `bench/paper_results/planner_ablation.summary.txt` | verified defect | OPEN |
| MNT-104 | Corroboration on a **second provider**: `DATA_MANIFEST.txt:55` attributes a *significant* result (autogen `ModelDowngrade-off` p=0.031) to a "LLaMA temperature variance artifact" — yet `autogen_bridge.py:116,134` **does** set `temperature=0`. Two independent artifacts now document that temp=0 ≠ determinism in this harness (Together/LLaMA MoE batching nondeterminism). | `DATA_MANIFEST.txt:55`; `autogen_bridge.py:116` | verified defect | OPEN |

### TEST COVERAGE OF PAPER CLAIMS

Safe abstention (all 9 rules have real negative tests) and warmup/hot-threshold gating are
**genuinely well tested**. The two claims the paper leans hardest on are not.

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-105 | **The guard's disable gate is tested for 1 rule out of 9.** `planner.rs:240` (`if budget.is_disabled(..) { continue; }`) is the **only** thing that disables CC, SD, OutputBudget, PromptDedup, StructuredTruncation, DeadOutputTruncation, CacheHit, and ParallelBranch. ModelDowngrade self-defends in its own `applies()`, so it is covered; the other eight are not. **Deleting `planner.rs:240-242` would fail no test** — grepping `crates/agentc-optimizer/tests/` for `disable` returns zero hits. The `Budget` unit itself is well tested (auto-disable, cooldown, streak reset) — what is untested is the wiring that makes it do anything. The guard is the paper's headline. | `planner.rs:240` | verified defect | OPEN |
| MNT-106 | **The guard does not run on the native Anthropic path.** `maybe_shadow_record` is called from exactly one site: `_openai.py:415`. `_anthropic.py` never calls it. <br>**SCOPE:** this does NOT invalidate the paper's Claude Haiku cross-model guard result — that experiment routed Claude through the **OpenAI-compatible endpoint** (`BENCH_OPENAI_BASE_URL`), so it exercised the OpenAI patch. The paper stands; the shipped product is what is broken. | `_openai.py:415`; `_anthropic.py` | verified defect | OPEN |
| MNT-107 | The Python→Rust guard loop is exercised by **zero tests**. `maybe_shadow_record` has no test references; `record_divergence` is called only by a *benchmark*; `optimize_record_divergence` is called by no test — **and `cargo test` excludes its crate** (`--exclude agentc-profiler`). Tests cover the middle link (`Budget` in isolation) only. | — | verified defect | OPEN |
| MNT-108 | **The composition orthogonality gate has no test that reaches it.** `composition.rs:108-114` (the `driver_conflict` check) is unreachable: every test short-circuits at the `EXPLICIT_SAFE`/`EXPLICIT_UNSAFE` tables (`:101-106`) first. All three `InputTokens` pairs among (CC, PromptDedup, StateDrop) sit in one of those tables. **Deleting `:108-114` would fail no test.** Genuinely untested same-driver pairs: `(OutputBudget, DeadOutputTruncation)` and `(StructuredTruncation, PromptDedup/StateDrop)`. | `composition.rs:101-114` | verified defect | OPEN |
| MNT-109 | **Four false-assurance tests** (same shape as `fail_open.rs`): (1) `rules_integration.rs:432-501`, **self-labelled "V2 paper gate"**, accepts *both* `Plan::Composed` AND `Plan::Rewritten` — if composition breaks entirely it still passes; (2) `context_compress.rs:356-380` nests all assertions inside `if let Some(p)`, so it passes **vacuously** when the rule returns `None` (the comment admits it: *"Either is acceptable"*); (3) **`tests/test_shadow.py` tests dead code** — `python/agentc/_shadow.py` has **no production caller**; its only importer is the test file, creating the appearance of shadow coverage while the real shadow path is untested; (4) `fail_open.rs:120-141` (= MNT-089). | — | verified defect | OPEN |
| MNT-110 | **CI silently skips the CLI tests and both provider-patch groups.** `tests/cli/test_cache_subcommands.py:32` skips if `target/debug/agentc` is missing — CI's python job never builds it, so **246 lines / 33 asserts are skipped on every run**. `test_anthropic_patch.py:469,482` and `test_openai_patch.py:342` use `importorskip`, but CI installs only `.[dev]` (pytest/pytest-asyncio/mypy) — `anthropic` and `openai` are separate extras, never installed. Plus `cargo test --exclude agentc-profiler` excludes the crate holding every optimizer FFI binding. | `.github/workflows/ci.yml` | verified defect | OPEN |
| MNT-111 | `crates/agentc-optimizer/src/wiring.rs` (273 LOC) has **zero tests** — and `build_optimizer` reads **`AGENTC_ENABLED_RULES`** (`:205`), the ablation knob **every per-rule sweep in the paper depends on**. If it silently mis-parses, every ablation config is wrong and nothing would catch it. | `wiring.rs:163,205` | verified defect | OPEN |

### BENCH HARNESS — copy-paste drift that changed paper numbers

25 drivers, 6,992 LOC, **~85% boilerplate**. The duplication drifted, and the drift is not cosmetic.

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-112 | **Fire counts undercount composed plans.** 13 of 25 drivers query `plan_kind='rewritten'` only. A composed plan is `plan_kind='composed'` with `rule` = **only the first rule** (`profiler/lib.rs:820-823`), and `AGENTC_COMPOSE=1` is the default everywhere. **Proof in committed data** — `autogen_bridge_warmup_n200.csv`: all-on `sd_fire=0`, but ContextCompress-off `sd_fire=197` and StateDrop-only `sd_fire=197`. SD never stopped firing; its fires were absorbed into composed rows attributed to CC. The 4 drivers using `IN ('rewritten','composed')` get it right. Tables 5/6/7/13 come from the undercounting group. <br>**BLAST RADIUS:** cost and input-token savings are **UNAFFECTED** (they come from `traces.db`, not the audit predicate). Only fire counts, and only in multi-rule configs — `*-only` rows cannot compose and are correct. | `autogen_bridge_warmup_n200.csv`; `profiler/lib.rs:820` | verified defect | OPEN |
| MNT-113 | **The manifest's CacheHit result is ContextCompress's number wearing CacheHit's name.** `profiler/lib.rs:809`: `Plan::Cached { .. } => (PlanKind::Cached, None, None)` — **`rule` is NULL**, so `WHERE rule='CacheHit'` returns 0 **by construction, in every driver**. Yet `DATA_MANIFEST.txt:53` claims *"CacheHit fires 396/400 (98%)"* — and **396 is exactly the `cc_fire_count`** for `all-on` in that CSV. The same bug structurally hides `ParallelBranch` (`plan_kind='parallel'`, rule NULL) — **which explains MNT-058**: PB may have been firing all along and nothing could ever have counted it. | `DATA_MANIFEST.txt:53`; `profiler/lib.rs:809` | verified defect | OPEN |
| MNT-114 | **Two drivers never reset `optimizer_audit.db`** — `run_md_cc_orthogonality_warmup.py:264-270` and `run_cc_sd_subadditivity_warmup.py:254-260` delete `traces.db` but not the audit DB. Their fire counts sum W=30 warmup fires + N measurement fires. These are the sources for §6.4 (MD+CC) and Table 13 (CC+SD). Same class as the original contamination. Cost/token columns are clean. | — | verified defect | OPEN |
| MNT-115 | **Warmup W is not uniform, and the guard sweeps run with W=0 — no warmup at all.** `run_guard_sweep.py:59`, `repro/guard_frontier.sh:18`, and `repro/crossmodel_selectivity.sh:27` all export `GE_W="0"`. **All ~60 committed `gsweep_*`/`checkpoint_*` CSVs are cold-start** — and they back `tab:guard` and `tab:xmodel`, the paper's headline. Four drivers have no warmup phase at all, including `run_planner_ablation_rerun.py` (**the Table 14 source**). <br>**MITIGATION TO VERIFY:** `guard-frontier-findings.md` already argues the guard results should be read on *behavioral* axes (fire retention, disabled y/n, accuracy delta), which are sampling-rate independent, and states "savings numbers are NOT usable from these cells." If so, W=0 is defensible for the behavioral claims — **but the paper does cite savings from them** ("CC stays enabled at 32.4–37.6%"). That is the exposure. | `run_guard_sweep.py:59` | verified defect | OPEN |
| MNT-116 | Composition results are measured on a **different task window** than the single-rule results they are compared against: the warmup family uses `BENCH_TASK_OFFSET=0` for both phases (overlap), but the two composition drivers measure at `offset=W` (disjoint). | — | verified defect | OPEN |
| MNT-117 | Column unit collision: `run_guard_eval.py:58` emits `cost_savings_pct` (a percentage) in the same schema slot where 10 other drivers emit `cost_savings_mUSD` (millidollars). | — | verified defect | OPEN |
| MNT-118 | **~85% boilerplate; ~4,000 of 6,992 LOC removable.** 11 drivers form a near-clone family (mean pairwise identical-line ratio **80%**, peak 89%). `_load_env` is duplicated in 20/25 files, `mcnemar_exact` in 17/25, `_run_phase` in 14/25. `run_guard_eval.py` is already the right shape (fully env-parameterized) — generalize it. **The consolidation kills MNT-112/113/114/117 at once and makes W an explicit column so MNT-115 becomes visible.** | — | proposal | OPEN |
| MNT-119 | Dead bench agents (zero references repo-wide): `parallel_research.py`, `adaptive_router.py`, `batch_classifier.py`. Runners producing no committed CSV: `run_hotpot_ablation.py`, `run_oracle_baseline.py` (both write to `bench/results/`, which does not exist). | — | verified defect | OPEN |

### SILENT FAILURE — swallowed errors that can corrupt the paper's numbers

~130 `except` sites audited. Catching broadly is **correct** for a library that monkey-patches other
people's SDKs. The bug is the **log level** and the corrupt-state continuations.

**The root cause is a logging-level policy, not a logic bug.** `_executor.py` gets it right (WARNING on
fallback). Every sibling path performing the *same* fallback logs at DEBUG — **including the sync path the
benchmarks actually use.** Today, *"Agentc broke"* is indistinguishable from *"Agentc correctly chose not to
optimize"* — and the paper's numbers cannot tell the difference either.

| ID | Finding | Evidence | State | Status |
|---|---|---|---|---|
| MNT-120 | **An attention failure silently disables ContextCompress.** `except BaseException` sets `attn_scores=[]`; the next line `if attn_scores:` is false, so `attention_scores`/`follow_on_tokens`/`dead_attention_epsilon` are **never written into `parameters.extra`** — and the Rust CC rule reads exactly those keys. Any exception in `compute_attention_scores` converts CC from "firing" to "never fires", **at DEBUG**. A benchmark would report honest-looking zero savings for the headline rule. | `_optimizer_glue.py:233` (OpenAI), `:401` (Anthropic) | verified defect | OPEN |
| MNT-121 | Same shape: a `get_recommendations` failure skips injection of `inferred_state_reads`, `output_is_dead_branch`, `shared_prefix_messages` → **StateDrop, DeadOutputTruncation, PrefixAlign all silently stop firing**. Compounded by `_openai.py:294` (`trace_opt.record`) and `:333` (cache auto-seed), also swallowed at DEBUG — starving the systems that *feed* those rules. | `_optimizer_glue.py:222,396` | verified defect | OPEN |
| MNT-122 | A failed rewrite logs at **DEBUG on the sync path** but **WARNING on the async path** (`_executor.py:66`, "retrying original call once") — and **the benchmarks use the sync path**. A systematically broken mutation reverts to the original on every call and reports 0% savings with no signal. | `_optimizer_glue.py:659` | verified defect | OPEN |
| MNT-123 | **OVER-reporting:** `_decode_cached_openai` catches internally and **returns `None` instead of raising**, defeating the `except`-driven fallback at `_optimizer_glue.py:651`. `dispatch_sync` returns `None` to the application while **the run books a cache hit with full savings credited**. | `_openai.py:402` | verified defect | OPEN |
| MNT-124 | A swallowed `getattr` in `_response_output_text` returns `""` → `_text_divergence("", real)` = **1.0 (max)** → feeds `record_divergence` → after `BREACH_STREAK` the Rust budget **auto-disables a working rule**. The guard is being fed a lie, and its audit trail shows a legitimate breach. | `_optimizer_glue.py:508` | verified defect | OPEN |
| MNT-125 | **Any `@agentc.trace` function that raises is executed TWICE.** `_run_traced_sync:227` catches the *user's* exception, logs, and re-raises; the wrapper at `:172` catches that same re-raised exception, cannot tell it from an internal failure, and with `fail_open=True` (default) **calls the user's function again**. Duplicate LLM calls, duplicate token spend, and a span already emitted with `status=ERROR`. At DEBUG. | `_span.py:150,172` | verified defect | OPEN |
| MNT-126 | Also silently degrading, all at DEBUG: `_memoize.py:116/139/147/159/172/237/265` (cache degrades to a **permanent 0% hit rate**); `_writer.py:302/318/365` (dropped cache inserts; a failed `merge_all_pending` means per-process DBs never fold into `traces.db`); `_lifecycle.py:184` (`install_all()` failure → no provenance tags → **ParallelBranch and StateDrop conservatively refuse to fire**); `langgraph.py:66` (falls through and forwards the **unwrapped** node — provenance silently vanishes); `_config.py:69` (a malformed `config.toml` silently reverts to defaults, discarding `storage_path` *and* `fail_open`). | — | verified defect | OPEN |

## Pass 6 — Functional-Risk Review + Corrections (2026-07-17)

### Corrections to this audit

| ID | Correction |
|---|---|
| MNT-113-CORR | **The stated ParallelBranch mechanism was WRONG.** MNT-113 claimed PB is hidden because `Plan::Parallel` writes `rule=NULL`. **False** — `profiler/lib.rs:815` records `Some(rule.clone())` for Parallel. **Only `Plan::Cached` nulls it.** PB is invisible because *no driver ever queries for it* and the `plan_kind='rewritten'` filter excludes `'parallel'`. The conclusion holds; a fix written against the original mechanism would itself have been wrong. The CacheHit half of MNT-113 stands — `Plan::Cached` genuinely nulls the rule. |
| MNT-033/066 | This findings document is now itself the **fifth tracker**, and MNT-033 went stale *within one day* (retracted by MNT-066 in the same file). **bd holds the state; this file is narrative.** Do not add status here. |

### The finding that reframes everything

| ID | Finding | Evidence | State |
|---|---|---|---|
| **MNT-127** | **Cost/token/accuracy and fire counts come from DIFFERENT DATABASES.** `bench/run_*.py`: `_aggregate_from_db(storage_dir/"traces.db")` produces cost and tokens; `optimizer_audit.db` + `plan_audit` produces fire counts. Accuracy comes from per-task pass/fail. **THEREFORE THE FIRE-COUNT BUG (MNT-112/113) TOUCHES ZERO COST, TOKEN, OR ACCURACY NUMBERS.** Every headline quantitative result survives intact: 33.9% CC savings, 34.0% token savings, 95.2% of additive ideal, 100.5% super-additivity, the guard's damage-prevention deltas, and every McNemar p-value. **What breaks is only the mechanistic diagnostic layer.** | `bench/run_lcqa_warmup_n300.py:188` vs `:220-226` | verified |

### The two landmines the audit missed

| ID | Finding | Evidence | State |
|---|---|---|---|
| **MNT-128** | **`data/pricing.json` is EVIDENCE, not config — and P6-1's "obvious fix" would detonate it.** `cost.rs:4-5`: *"Cost is computed at query time (not capture time) so pricing updates **retroactively apply to old spans**."* Chain: `pricing.json` → `model_pricing` → `backfill_costs()` → `spans.cost_usd` → `SUM(cost_usd)` → **every `cost_savings_mUSD` column in every committed CSV** — and it also feeds `CostModel` → `planner.rs` projected savings → **rule ranking**. The red test (MNT-091) fails because pricing is 117 days old vs `STALENESS_DAYS=90`. **Refreshing the pricing file to make the test pass would silently rewrite every cost number in the paper and change which rules fire.** The only safe fix is to change the *test*, and hash-pin `pricing.json`. | `crates/agentc-analyzer/src/cost.rs:4-5,17` | verified defect |
| **MNT-129** | **The paper gives a causal mechanism for a measurement artifact.** `main.tex:1357-58`: *"SD fires 58 times when run alone; in composition, SD fires only 1 of 90 calls **because CC already removes the messages SD would otherwise target**."* That "1 of 90" **is the composed-undercount signature** (MNT-112). SD never stopped firing — its fires were logged as composed rows attributed to the first rule. A reviewer who opens `autogen_bridge_warmup_n200.csv` (`sd_fire=0` all-on, `197` alone) sees this in thirty seconds. **Not an erratum — a retraction risk.** Same class: `tab:mdcc-orthogonality`'s "MD fires 15%/25%". | `main.tex:1357-58,1371` | verified defect |

### Freeze policy (recommended)

Three options were evaluated. **(c) recompute from retained DBs is IMPOSSIBLE** — no `optimizer_audit.db`,
`traces.db`, or `~/.agentc` exists anywhere; drivers `rmtree` their storage root on startup, delete the audit
DB mid-run by design, and wrote to `/tmp`. The per-task sidecars carry no plan information.
**(b) fix + re-run is a trap** — `bench/fixtures/` was never committed, model snapshots were never pinned, and
the authors' own artifact documents a 32%→100% baseline drift. **You cannot re-run "just one column."**

**Adopted: (a′) — freeze the numbers, delete the mechanism, disclose the instrument.**

1. **Freeze now.** Tag the commit that produced the CSVs. Hash-pin `data/pricing.json` (MNT-128).
2. **Run the two free diagnostics first:** V4 (does ParallelBranch fire?) and **P11-7 — does `AGENTC_ENABLED_RULES` parse correctly?** If that knob mis-parses, *every ablation config in the paper is wrong* and the freeze question is moot. Both are cheap. Do them before deciding anything else.
3. **Do not report corrected fire counts. Delete the uncorrectable ones** (MNT-129). Costs nothing scientifically — see MNT-127.
4. **Disclose the instrument:** temperature, per-experiment W, no model pinning, and that composed-plan fire attribution is first-rule-only. *Reviewers forgive a documented limitation; they do not forgive a mechanism invented from an artifact.*
5. **Land INERT + ADDITIVE freely (≈73 beads).** Land BEHAVIOR-CHANGING and EVIDENCE-INVALIDATING behind the freeze tag, on a post-submission branch — so the artifact reviewer gets code that reproduces the CSVs.
6. **Spend money on exactly one thing:** the `[T2]` attribution ablation (P1-17). It is *additive* — new evidence, invalidates nothing — and it is the acceptance lever reviewers will actually demand.

### Bead risk buckets

| Bucket | Count | Policy |
|---|---|---|
| **1 — INERT** (docs, paper text from committed CSVs, archiving, LICENSE) | ~60 | Land freely on main |
| **2 — ADDITIVE** (tests, CI, `.env.example`, toolchain pin, V4 diagnostic) | 13 | Land freely; expect CI to go red — that is the point |
| **3 — BEHAVIOR-CHANGING** (ParallelBranch USD, C1–C5, WAL, async dispatch, memo LRU) | 25 | **Post-submission branch, behind the freeze tag** |
| **4 — EVIDENCE-INVALIDATING** (driver consolidation, fire-count fixes, figure regeneration, V5) | 11 | **Post-submission branch.** Re-running is a trap — fixtures are gone. |

**Three "inert" beads with teeth:** P1-9..15 contains a *rounding correction to a number that is structurally
wrong* (the CC fire range) — do not ship it as a fix. P1-2 rebuilds `tab:mdcc-orthogonality` from a CSV whose
**fire counts are double-contaminated**. P10-1 is inert *as disclosure*, but if anyone *sets* `temperature=0`
on the headline matrices, Tables 5 and 6 must be re-run entirely.

**Ordering hazards:** P3-3 (enable PB on Anthropic) must never land before P3-1 (the USD fix), or the
`0.500001` bug would suppress CC/SD on Anthropic too. P8-6 (enable audit-ring pruning at cap 10,000) must not
land alongside the fire-count fix, or it will silently truncate counts on long runs.

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
