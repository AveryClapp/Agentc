---
title: Maintenance Remediation Plan
status: active
last-updated: 2026-07-17
---

# Maintenance Remediation Plan

> **STATUS: this document is NARRATIVE ONLY. bd is authoritative for scope and status.**
> The audit grew to **144 findings across 7 passes** and the plan to **15 phases**; the
> phase-by-phase task detail below covers only Phases 0–5 (the first pass) and is retained
> for its dependency-order and governing-rules narrative. For the full, current picture use:
> - **Findings:** [maintenance-audit-findings.md](maintenance-audit-findings.md) (MNT-001..144, incl. the Pass-6 risk buckets + freeze policy and the Pass-7 meta-audit).
> - **Tasks/status:** `bd list` — 15 epics, ~150 tasks. Never restate a count or status here; it will drift (this line already did — it said "65 findings" for weeks).
> - **Freeze policy + risk buckets (INERT/ADDITIVE/BEHAVIOR-CHANGING/EVIDENCE-INVALIDATING):** findings file, Pass 6.

Scope: maintenance and fixes only. No new experiments except where a claim cannot otherwise
be backed, and no new features.

Target: MLSys 2027, est. deadline late October 2026 (CFP not public as of 2026-07-17).
Runway: ~14 weeks.

## Phases 6–15 (summary; detail lives in bd + the findings file)

Phase 0 Blind spots · 1 Paper claim integrity · 2 Artifact-eval · 3 Code defects ·
4 Doc truth · 5 Evidence hygiene · 6 Build & CI · 7 Fail-open · 8 Rust schema/hot-path ·
9 Docs consolidation · 10 Determinism · 11 Test coverage · 12 Bench correctness ·
13 Silent failure · 14 Stats/pricing/scoring · 15 Durability & skills.
The freeze (P0-FREEZE, bd-3w1) is foundational and now blocks every behavior-changing and
evidence-invalidating bead. The critical path and governing rules below still hold.

## Governing Rules

1. **Evidence is immutable.** Never edit a result CSV that backs a published claim. Correct
   the claim, not the data. Retracted CSVs are quarantined and renamed, never deleted.
2. **No paid experiment inside a cleanup batch.** Re-running an experiment is its own bead
   with its own cost budget.
3. **Both `.tex` files, always.** Any manuscript edit lands in `main.tex` *and*
   `main_trimmed.tex` in the same batch.
4. **The paper outranks the README.** When they disagree, the README is the defect.
5. Execution is governed by `agentc-cleanup-workflow`: dry-run manifest, consumer scan,
   rollback, per-batch validation.

## Phase 0 — Close The Blind Spots (gates scope of everything else)

Four checks have never been run. Until they are, the scope of Phases 1 and 2 is estimated,
not known. Cheap; do these first.

| ID | Task | Closes | Why it gates |
|---|---|---|---|
| V1 | Clean-venv `pytest` run | "~250 tests pass" is unverified; MNT-012 predicts collection failure | Determines whether the artifact is broken or merely undeclared |
| V2 | `mypy --strict` run | `strict = true` configured, never executed | Unknown type-debt volume |
| V3 | `tlmgr install acmart` + `latexmk main.tex` | Paper has **never been compiled on this machine** | Reveals true page count, undefined refs, overfull boxes. **Page count decides how much Phase 1 must cut.** |
| V4 | Determine whether ParallelBranch fires (paid: rerun `rag_summarizer`, persistent storage, `SELECT rule, plan_kind, COUNT(*) FROM plan_audit`) | MNT-058 | Decides whether MNT-018 is a latent bug or **actively suppressing CC/SD/OB in committed results** |

V4 is the only paid item and the only one that could invalidate a result. Run it early.

## Phase 1 — Paper Claim Integrity (P0, blocks submission)

Ordered by severity. Every item is a manuscript edit backed by a committed CSV.

### 1a. Claim-level falsifications (must fix)

| ID | Task | Findings |
|---|---|---|
| P1-1 | Rewrite the `research_planner` row (+9.0pp, p=0.0117, 41.5%, 37.5%) and **remove "not statistically significant"** from abstract, `tab:summary` caption, and conclusion. Reconcile the n=150-vs-100 discrepancy. | MNT-040 |
| P1-2 | Rebuild `tab:mdcc-orthogonality` accuracy block from `md_cc_orthogonality_warmup.csv`. Replace "no accuracy interference (all p=1.0)" with the honest underpowered statement (−20pp, p=0.125, n=20). Re-derive or drop the unbacked per-call savings column. | MNT-041, MNT-044 |
| P1-3 | Resolve the guard headline. **Recommended:** restate the abstract on the backed n=200 frontier (−49.5pp → −3.5pp, 93% prevented). **Alternative:** commit the n=150 unguarded + lexical CSVs. Fix the −0.7/−1.3 split and the 92.7%→92.0% baseline. | MNT-042, MNT-043, MNT-052 |

### 1b. Unbacked results — back or cut

| ID | Task | Findings |
|---|---|---|
| P1-4 | `support_qa` cold-agent row: write `bench/coldagent_eval.py`, re-run (abstention result, near-free), commit CSV — or cut the row and the abstention claims that lean on it. | MNT-001 |
| P1-5 | `sec:eval-cachehit`: write `bench/cachehit_eval.py`, re-run, commit CSV — or cut the section. | MNT-002 |
| P1-6 | `tab:attribution`: locate the backing data or cut the table. | MNT-047 |
| P1-7 | Agentc's "53% token reduction" on the distractor fixture: measure it (`agentc_hotpot_n100.csv` has no token column) or drop the parity claim. | MNT-048 |
| P1-8 | Provider generalization: the two MD numbers are cold-start measurements from the disavowed regime. Re-run warmup-corrected, or label them explicitly. | MNT-053 |

### 1c. Single-number corrections (mechanical, CSVs already in repo)

| ID | Task | Findings |
|---|---|---|
| P1-9 | autogen CC-only tok 23.5% → **26.00%** | MNT-045 |
| P1-10 | `tab:oracle` EM column — rebuild; it currently inverts the sign of CC's effect | MNT-046 |
| P1-11 | Caption baselines: `tab:cc-matrix` 58.3%→**58.0%**; `tab:hotpot-matrix` 57.3%→**58.7%** | MNT-050 |
| P1-12 | CC fire range 87–95% → **85–95%** (4 sites) | MNT-051 |
| P1-13 | Relabel p=0.0013 as continuity-corrected χ², or recompute exact (≈0.00073); reconcile with the "exact throughout" assertion | MNT-049 |
| P1-14 | Rounding/range fixes: p≥0.39→0.3877; SE 3.4→3.8; x≥96%→95.8%; 91%→93% | MNT-055 |
| P1-15 | `analyst_qa` is not "unseen" — the authors built it | MNT-054 |
| P1-16 | Delete the `DROPIN-INDEX` block from both `.tex` files | MNT-007 |

### 1d. Outstanding experiment

| ID | Task | Findings |
|---|---|---|
| P1-17 | `[T2]` naive-baseline attribution (random / recency / BM25 vs IDF). The only unrun experiment. P1 acceptance lever — reviewers will demand it. | MNT-008 |

## Phase 2 — Artifact-Eval Readiness

A reviewer's **first command currently fails**. This phase is what makes the repo runnable.

| ID | Task | Findings |
|---|---|---|
| P2-1 | Pull `references.bib` from Overleaf, commit it. The repo is not currently the source of truth for the paper. **Gates V3 and the whole submission build.** | MNT-010 |
| P2-2 | Port `acmart` → MLSys template. **Gates the page budget, which gates the trim (MNT-009).** | MNT-011 |
| P2-3 | Declare the 9 undeclared Python deps (`crewai`/`langgraph` are in the *shipped package*); make a clean `uv sync --extra dev && pytest` collect | MNT-012 |
| P2-4 | Add fixture bootstrap to `bench/repro/README.md` + `PRESUBMIT.md`; write the missing `rag_summarizer` fixture generator | MNT-013, MNT-064 |
| P2-5 | Point README Quick Start at the **warmup** harness. It currently reproduces the regime the paper disavows. | MNT-014 |
| P2-6 | Rebuild `DATA_MANIFEST.txt`: 6 of 18 tables covered, zero guard/cross-model. Fix the 2 dead script paths. | MNT-015, MNT-016 |
| P2-7 | Fix the repro appendix: 3 dead module paths, 4 wrong CSV filenames, `agentc shutdown` (not a CLI verb), 2 conflicting autogen sources | MNT-003, MNT-005, MNT-057 |
| P2-8 | Commit `guard_overhead_bench` output + an API-spend ledger (both cited, neither committed) | MNT-056 |
| P2-9 | Model weights: spec claims bundled, code downloads. On a fresh machine embeddings are NULL and the LSH tier + 2 detectors **silently no-op**. Bundle, or fail loudly, or document. | MNT-063 |
| P2-10 | Commit the `/tmp` guard-frontier drivers or mark the section non-reproducible | MNT-017 |
| P2-11 | Execute the trim. `main_trimmed.tex` is 12 lines *longer* than `main.tex`; no prose was cut. **Blocked on P2-2.** | MNT-009 |

## Phase 3 — Code Defects

| ID | Task | Findings | Notes |
|---|---|---|---|
| P3-1 | ParallelBranch: `projected_savings_usd` is a latency **ratio** (0.500001), outranking every cost rule | MNT-018 | **Blocked on V4.** If PB fires, this is corrupting results, not latent. |
| P3-2 | Add PB fire instrumentation to the bench harness (`pb_fire_count`) so this can never be invisible again | MNT-058 | Guardrail against recurrence |
| P3-3 | `build_call_dict_anthropic` never sets `parallel_peer` — PB structurally cannot fire on Anthropic | MNT-059 | |
| P3-4 | `StructuredTruncation` rewrite silently discarded when composed (merge only fires when msg count shrinks) | MNT-019 | Audit rows overclaim savings |
| P3-5 | Async `_executor.dispatch` has no `"composed"` branch — V2 composition silently disabled on the async OpenAI path | MNT-020 | |
| P3-6 | `hit_count`/`last_hit_at` never written in production → `memoization_stats` always 0; "LRU" eviction is **FIFO** | MNT-060 | |
| P3-7 | `agentc cache stats` reads span attributes nothing writes → structurally zero | MNT-061 | |
| P3-8 | LSH bucket + embedding rows orphaned forever on eviction (no CASCADE, FKs off) | MNT-062 | Stale buckets feed LSH retrieval |
| P3-9 | `_google.py` is a 19-byte stub: implement Gemini, or strip it from README + specs | MNT-021 | Paper does **not** claim Gemini |
| P3-10 | 4 clippy warnings (all one-line, mechanical) | MNT-022 | |
| P3-11 | 6 dead Rust deps (`thiserror` ×4 unused; `safetensors`/`tokenizers` string-literal only) | MNT-023 | |

## Phase 4 — Documentation Truth

| ID | Task | Findings |
|---|---|---|
| P4-1 | `CLAUDE.md`: delete "No implementation code exists yet"; rebuild the repo-structure tree. **Highest damage-per-byte in the repo.** | MNT-024 |
| P4-2 | Rewrite `README.md` Status tables from warmup-corrected CSVs. It currently publishes MD=35.3% (the paper calls it "spurious"), CC at superseded n, the **opposite** CC+SD conclusion, and the **retracted** planner numbers. | MNT-025, MNT-026, MNT-027, MNT-028 |
| P4-3 | Reconcile the rule count to **9** across README (8), `specs/README.md` (5), `specs/optimizer.md` (5) | MNT-030 |
| P4-4 | `specs/optimizer.md` explicitly **rejects** the paper's V2 contribution ("Rejected: greedy composition"). Rewrite for CompositionPlanner + the 4 V2 rules. | MNT-031 |
| P4-5 | `specs/profiler.md` + `specs/memoization.md`: heavy drift (nonexistent `agentc pricing update`, nonexistent `_adapters/`, httpx fallback that is a log string, schema mismatches, unimplemented config section) | MNT-065 |
| P4-6 | Archive the 13 stale `paper-intelligence` docs. Worst: `current-fit-and-publishability.md` (says MLSys "not ready", lists done work as future) and `results-experiments-and-repro.md` — the "authoritative evidence ledger" that **doesn't know the guard result exists**. | MNT-032 |
| P4-7 | Fix README minor claims: test count 250→282; "11-config"→19; 10 unlisted bench agents | MNT-034 |

## Phase 5 — Evidence & Structure Hygiene

| ID | Task | Findings | Notes |
|---|---|---|---|
| P5-1 | **Root-cause fix:** rename/quarantine retracted CSVs so they cannot be mistaken for canonical (`retracted/` subdir or `_PRE-WARMUP` suffix). This is what let a disavowed accuracy block into a headline table. | MNT-041, MNT-044, MNT-053 | **Do first in this phase** — prevents recurrence |
| P5-2 | Quarantine `planner_ablation.summary.txt` (carries retracted numbers beside the clean CSV). Preserve, never delete. | MNT-029 | |
| P5-3 | Repair the figures pipeline: 6 generators write to a dir the paper never reads | MNT-035 | |
| P5-4 | Resolve figure-number collisions (two fig4s, fig5s, fig9s with different content) | MNT-036 | |
| P5-5 | `fig8_throughput.pdf` has **no generator anywhere**. Reconstruct or drop the figure. | MNT-006 | Unreproducible published evidence |
| P5-6 | `.beads/.gitignore` uses a directory-only `dolt/` pattern; `dolt` is a 456K **file**. One `git add -A` from being committed. | MNT-037 | |
| P5-7 | Track or ignore `AgentcV{1,2}.pdf`; they are **stale** (2026-05-13, predate all June work) | MNT-038 | |
| P5-8 | Scrub hardcoded `/Users/` paths from tracked docs | MNT-039 | |
| P5-9 | Reconcile the 4 competing trackers. bd froze 2026-05-09; the bd IDs cited in June docs **do not exist**. Close the 6 stale epics; make `PRESUBMIT.md` mirror bd or declare itself the tracker. | MNT-033 | |

## Critical Path

```
V3 (compile) ──┐
P2-1 (bib) ────┴──> P2-2 (MLSys template) ──> page budget ──> P2-11 (real trim)

V4 (does PB fire?) ──> P3-1 (ratio-as-USD bug) ──> [if live: re-verify rag results]

P5-1 (quarantine retracted CSVs) ──> P1-2, P1-8  [prevents re-contamination during fixes]

P1-1, P1-3 (abstract falsifications) ── independent, start immediately
P2-3, P2-4 (artifact first-contact) ── independent, start immediately
P4-* (doc truth) ───────────────────── independent, safe to batch
```

## Parallelization

| Lane | Phases | Touches | Conflicts |
|---|---|---|---|
| A — Manuscript | Phase 0 (V3), Phase 1 | `main.tex`, `main_trimmed.tex`, `references.bib` | none |
| B — Artifact | Phase 0 (V1,V2), Phase 2 | `pyproject.toml`, `bench/repro/`, `DATA_MANIFEST.txt` | none |
| C — Code | Phase 0 (V4), Phase 3 | `crates/`, `python/agentc/` | none |
| D — Docs | Phase 4, Phase 5 | `README.md`, `CLAUDE.md`, `specs/`, `figures/` | P5-1 must precede Lane A's P1-2 |

Lanes A–D are file-disjoint and can run concurrently across panes.
