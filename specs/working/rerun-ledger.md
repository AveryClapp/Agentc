---
title: Re-run Ledger — code fixes vs committed experiments
status: active
last-updated: 2026-07-15
---

# Re-run Ledger

Goal: this repo is being **cleaned up** (submission is far off). Code bugs get fixed as part
of cleanup. This ledger answers the only question that matters when a code fix lands: **does it
change what a committed experiment outputs, and if so, which one, and how do we keep the paper
honest?**

Determined by a 4-subsystem impact review (2026-07-15) that read every bug's code and cross-checked
the 221 committed CSVs. **Headline finding: no committed experiment requires a PAID re-run.** The
committed CSVs are largely self-validating — fire-count and disable columns are recorded *in the
data*, so a bug that would have silently disabled a rule would show `fire_count=0`; none do.

## Re-run status legend

- **NONE** — pure latent (never fired on the committed fixtures) or pure plumbing/docs. Fix freely; no follow-up.
- **RE-VERIFY (free)** — changes a measured number, but re-measurable by a **local, no-API** benchmark.
- **CAVEAT** — touches one illustrative/committed number; a one-line paper caveat is sufficient (re-run optional).
- **CONTINGENT** — only affects a **dropped / uncited** CSV; no paper number depends on it.
- **HAZARD** — no inherent output change, but a *botched* implementation would change one; get the impl right.

## The short list — fixes that touch a committed number (7 of ~40)

| Bead | Fix | Affects | Status | Note |
|---|---|---|---|---|
| **bd-4xr** P8-8 | remove per-plan heap allocations on the hot path | `overhead_scaling.csv` / `optimizer_overhead.txt` (the 76µs/120µs figure) | **RE-VERIFY (free)** | Fix only *lowers* overhead, so the "3 orders below an LLM call" claim survives regardless. Re-run the local overhead bench (no API). |
| **bd-lu8** P12-3 | reset `optimizer_audit.db` between phases | the illustrative "SD fires 58 times" in `cc_sd_subadditivity_warmup.csv` (`main.tex:1357`) | **CAVEAT** | The 58 is warmup-inflated. Headline token-savings (32.5%/32.8%) come from `traces.db` (correctly reset) and are clean. Caveat the count, or optional paid re-run for an exact number. |
| **bd-y1v** P12-4 | add warmup to the guard sweeps (currently W=0) | ~60 `gsweep_*` CSVs | **CAVEAT** | The guard is *online* — disable decisions and fire-retention are sampling-rate-independent, so the behavioral results stand. Savings magnitude already cited from clean runs (`lcqa_cc_guard.csv`). State the config; no re-run required. |
| **bd-0qm** P12-5 | align composition vs single-rule task windows | `cc_sd_subadditivity_warmup.csv`, `md_cc_orthogonality_warmup.csv` | **CAVEAT** | Within each file the compared rows share a window, so the efficiency ratio (100.6% additive) is like-for-like. Cross-table window mismatch is a methodology caveat, not a validity re-run. |
| **bd-yqr** P8-11 (C3) | shared `project_savings()` helper across rules | `planner_ablation.csv`, `generalization_activation.csv` (OutputBudget fires 92–97%) | **HAZARD** | OB and DeadOutputTruncation carry an extra `*0.5` the other 7 rules lack (MNT-145). Preserve it or plan selection flips. Done right → no output change. DeadOutputTruncation never fires in committed data, so only OB is live. |
| **bd-lcd** P3-1 | ParallelBranch projects a latency ratio as USD | only `rag_summarizer_warmup_n200.csv` | **CONTINGENT** | rag is **dropped from the paper / not cited quantitatively**. PB is structurally incapable of firing on any cited-table agent (none register a parallel peer). Blocked on **V4** (does PB fire?). A rag re-run is warranted only if that dropped file is ever resurrected. |
| **bd-n8s** P12-6 | consolidate 25 drivers → 1 | (none to land) | **NONE to land / equivalence unprovable** | Landing it changes no committed CSV. But "the CSVs reproduce from the new driver" can't be proven without a re-run (fixtures gone). Treat as a future-apparatus change; verify against a fresh run when convenient. |

## Everything else — apply now, NO re-run (pure latent / plumbing / docs)

Verified never-fired-on-committed-fixtures or pure infrastructure:

- **Phase 3:** bd-jfs (Anthropic peer — native-Anthropic never used with parallel_map), bd-nj3 (StructuredTruncation never fires), bd-5r5 (async path never used), bd-2wn / bd-j8c (memo eviction never triggers at bench scale, 100k-entry limit vs a few hundred rows), bd-lz1 (cache-stats CLI surface, no CSV).
- **Phase 7/13 (all 14):** every silent-disable path is **proven not to have fired** — `cc_fire_count` is 261–280 (not 0), `sd_fire_count` 179–197, guard disables decay monotonically with threshold, `input_tokens_baseline` identical across configs (no double-exec). Anthropic streaming / async / crewai paths never exercised.
- **Phase 8 plumbing:** bd-gzm (WAL on audit.db — **fsync is captured AFTER the stopwatch closes**, `lib.rs:766`→`770`, so it does NOT affect the overhead figure — correction to an earlier session assumption), bd-ul9 (connection reuse), bd-77x (migration — benches make fresh DBs), bd-p0i / bd-6sk (merge lock/poison — single-process isolated runs never contend), bd-c0l (swallowed ALTER — fresh DBs have the column), bd-scy (dead shadow reporting — guard uses in-memory DashMap, not `rule_divergence`), bd-smg (build-graph dependency direction).
- **Phase 12 recording/schema:** bd-xv5 / bd-yxv (emitter/query change future *recording* only — Pass 9: no committed number moves), bd-nu7 (manifest-text mislabel, no CSV), bd-3y2 (column rename + dead-agent hygiene).

## Consequence for the plan

Since submission is far off and no cited experiment needs re-running, the blanket "post-submission
freeze gate" is **retired** — it was built on a submission-imminent assumption that does not hold.
Code fixes proceed as normal cleanup. This ledger is the flagging mechanism: when one of the **7
short-list** beads lands, add a row recording the bug, the fix, the affected CSV, and the re-run
disposition. Everything else needs no ledger entry.
