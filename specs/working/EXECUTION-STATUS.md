---
title: EXECUTION STATUS — resume here
status: active
last-updated: 2026-07-15
---

# EXECUTION STATUS — read this FIRST after any compaction

## Session 2026-07-15 — "genuinely-safe (no-published-number) set" pass
Worked the ~24 beads that change no published number. **13 closed + committed this
session, 2 epics closed, 2 follow-ups filed. All LOCAL (nothing pushed).**
- **Closed:** bd-21w, bd-fr0, bd-d5d (gitignore/path hygiene); bd-3u2 (DROPIN removal);
  bd-m6e (MNT-033 superseded); bd-yka (rust-toolchain.toml + .python-version pins);
  bd-73t (audit ring-buffer doc = append-only reality); bd-wdk (new CONFIG.md, all env
  vars); bd-7st (dead shadow-divergence code removed + stale module docs fixed — broad
  156-item pub narrowing DEFERRED to bd-yqr); bd-7yft (CLI rejects unknown --rule names,
  drift-guard test); bd-d8m (froze paper-intelligence ID namespaces); bd-94j (mypy
  --strict = 16 errs; fixed 2 via _native.pyi stubs -> 14); bd-5nc (profiler.md +
  memoization.md rewritten to shipped behavior, verified vs code).
- **Epics closed (bd-516):** bd-5ze (Phase 7), bd-uod (Phase 13) — all children done.
  bd-323l (Phase 16) left OPEN (empty = unstarted strategic work, not stale).
- **Follow-ups filed:** bd-qc7d (14 remaining mypy errors). Also fixed 2 adjacent
  code-doc bugs (cost.rs pricing-update string, _intercept.py _adapters docstring).
- **Deferred with reasons (need a decision or are blocked):** bd-jz4 (un-greenwash CI =
  flip CI red to surface real gaps? decision); bd-4xr (hot-path perf on the FROZEN
  runtime); bd-yqr/bd-n8s (big refactors, golden-guarded, want fresh focus); bd-jml/
  bd-skp/bd-w5y (figure pipeline — coordinated, freeze-gated, PDF renames/deletes;
  NOTE: figures/ has NO collisions, the paper includes the correct variants; collisions
  are only in bench/paper_figures/); bd-50d/bd-8pq (docs consolidation — Tier-1 = 7 safe
  git mv's, but Tier-2 needs content-lifting and guard-frontier archival is blocked on
  the deferred DATA_MANIFEST regen); bd-yrp (needs bd-vs-br canonicality call + git rm
  --cached confirm); bd-i8v (blocked inside guard-frontier doc); bd-gb3/bd-0ri (need
  references.bib from Overleaf + acmart install — William); bd-3y2 (batch_classifier
  "dead" claim CONTRADICTS the cachehit experiment — do not archive it).

---

# EXECUTION STATUS (prior baseline) — read after the session note above

**All safe-code cleanup is DONE.** The only self-actionable code work left is the
**2 refactors** (bd-yqr, bd-n8s). Everything else open is paper/author decisions or epics.
Submission is far off; no paid re-runs were needed. **All commits are LOCAL — nothing is pushed.**

## Invariants (don't relearn the hard way)
- **Local commits only; do NOT push unless William explicitly says so.**
- Never edit `data/pricing.json` (frozen, hash-pinned by a test). Never edit a committed CSV in
  `bench/paper_results/`; retracted ones live in `bench/paper_results/retracted/`.
- Every executed change: verify before commit (run the test/build; numbers vs CSV).
- Commit msgs: `fix(Pn-m): ...` + MNT id; end with the Co-Authored-By line.
- `bd close` occasionally doesn't persist — verify the close took.

## DONE this session (~41 beads closed, workspace green throughout, 56 commits since `submission-evidence-base`)
- **Phase 3 latent (6):** bd-jfs, bd-nj3, bd-5r5, bd-j8c, bd-2wn, bd-lz1.
- **Phase 7 error-handling (7 + autogen):** bd-ezj, bd-7b9, bd-8jm, bd-3pt, bd-jvk, bd-4pn, bd-nr5.
- **Phase 8 DB plumbing (8):** bd-c0l, bd-gzm, bd-p0i, bd-6sk, bd-scy, bd-ul9, bd-77x, bd-smg. (bd-4xr = number-toucher, deferred.)
- **Phase 11 test coverage (6):** bd-inc, bd-gzf, bd-1cb, bd-004, bd-c45, bd-rj7.
- **Phase 13 logging/safety (7):** bd-ybd, bd-876, bd-tfz, bd-w3h, bd-bpp, bd-kq7, bd-8ln.
- **Golden test bd-xlqh** (mutation-proven; UNBLOCKS bd-yqr + bd-n8s).
- **Safe docs:** bd-609 (.env.example), bd-ciw (README claims), bd-xzs (already quarantined).
- **Paper-artifact detective work:** 2 agent waves (see `specs/working/paper-artifact-fix-map.md`).
  Applied verified-factual fixes to `tab:repro` + `DATA_MANIFEST` (11.2%->11.4% units bug); fixed the
  generalization 100%-accuracy code bug (bd-r0ln, generator only — committed CSV still needs a re-run).
- Filed bd-v02z (async shadow idea), bd-hig3 (autogen 23.5%), bd-ec0e (debug_agent).

## LEFT — the 2 self-actionable refactors (golden-test-guarded)
- **bd-yqr (P8-11, C1-C5, ~300 lines):** C1 (open_tuned PRAGMA helper) + C2 (schema owner + MIGRATIONS
  table) are PARTLY done by bd-gzm/bd-ul9/bd-smg/bd-77x — reconcile, don't redo. **C3 is the MNT-145
  HAZARD**: a shared `project_savings(profile, fraction)` must PRESERVE OutputBudget/DeadOutputTruncation's
  `*0.5` — the golden test (`tests/rules_integration.rs::golden_*`) catches a drop. C4 (one hex codec),
  C5 (shared cfg(test) testkit) are safe. Number-toucher → add a rerun-ledger row when it lands.
- **bd-n8s (P12-6, ~4000 LOC):** consolidate 25 drivers -> 1 parameterized driver. Big; golden test guards
  selection. Recommend FRESH focus, not the tail of a long session.

## LEFT — paper/author decisions (~59 beads; need William or paid re-runs)
Full evidence map: `specs/working/paper-artifact-fix-map.md`. The forensic passes gave clear
recommendations (author confirms):
- **bd-dka** research_planner: committed evidence = **+9.0pp/p=0.0117** (paper's +4/ns is an uncommitted
  earlier run). Using +9 flips "ns"->"significant" (update main.tex :830/:1832/comment :29 + cost/tok).
- **bd-pzv** cold-agent: **n=30 -> n=39** (verified always 39; 30 is a slip). Claim itself is sound.
- **bd-ctk** mdcc accuracy: swap 55/60/50/60 -> canonical **80/70/70/60**, keep 95.2%, but REWRITE the
  "all p=1.0 / no interference" prose (canonical deltas are -10/-10/-20, p=0.5/0.5/0.125).
- **bd-hig3** autogen 23.5%: unrecoverable (per-call, never committed); committed aggregate = 26.0%.
- **bd-ec0e** debug_agent 8.3/16.8: cold-start only + mislabeled "StateDrop" (is all-on) — re-run or drop.
- **bd-ude/bd-6xrq** fig4 provider: data only in retracted/unified_agent_summary.csv (cold-start).
- Other Phase 1/10/12/14 number beads (bd-uk8 mechanical corrections, bd-127 temperature, bd-vdj snapshots,
  bd-q74 planner-ablation, bd-onal 0.00 cost column, bd-hfuj se_pp, bd-jq6c 2% shadow, etc.) — author work.
- **bd-o4q / bd-u50** partly applied; residual = the 18-table manifest regen + the decisions above.

## WHERE TO RESUME
- If continuing code cleanup autonomously: **bd-yqr** (reconcile C1/C2 vs already-done work; do C3 carefully
  under the golden test; C4/C5 safe), then **bd-n8s** (fresh focus).
- If William has decided the paper numbers: apply per the per-bead annotations + fix-map, then regenerate
  `generalization_evals.csv` (bd-r0ln) and the DATA_MANIFEST 18-table coverage (bd-o4q).
- Related durable docs: `rerun-ledger.md`, `paper-artifact-fix-map.md`, `maintenance-audit-findings.md`.
