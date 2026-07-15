---
title: EXECUTION STATUS — resume here
status: active
last-updated: 2026-07-15
---

# EXECUTION STATUS — read this first after any compaction

**Mode: EXECUTING all cleanup.** Audit + decomposition + review are DONE. Do NOT run more
review/audit waves — the last three review passes returned GO/GO/PASS. Just execute.

**Goal:** clean up the whole repo (the user wants ALL cleanup, not just the submission core).
Submission is far off. No paid re-runs needed (see [rerun-ledger.md](rerun-ledger.md)).

## The one rule that still binds

Only **7 beads** touch a committed experiment number. When one of those lands, add a row to
[rerun-ledger.md](rerun-ledger.md) (bug → fix → affected CSV → disposition). Everything else:
just fix + `cargo test`/`cargo clippy` green + commit + `br close`.

The 7: **bd-4xr** (overhead, free re-verify), **bd-lu8** (SD-58 caveat), **bd-y1v** (guard W=0 caveat),
**bd-0qm** (window caveat), **bd-yqr** (C3 — HAZARD, preserve the `*0.5`), **bd-lcd** (ParallelBranch,
only affects dropped/uncited rag), **bd-n8s** (driver consolidation).
**bd-yqr and bd-n8s are BLOCKED by bd-xlqh (P8-13, the golden ranking-order test) — build that test first.**

## Execution order (safe → risky)

1. **Safe code fixes** (touch no numbers, provably never fired): Phase 3 latent (bd-jfs, bd-nj3,
   bd-5r5, bd-2wn, bd-j8c, bd-lz1), Phase 7 (bd-ezj, bd-7b9, bd-8jm, bd-3pt, bd-jvk, bd-4pn, bd-nr5),
   Phase 13 logging (bd-tfz, bd-w3h, bd-bpp, bd-kq7, bd-8ln, bd-876, bd-ybd), Phase 8 plumbing
   (bd-gzm, bd-ul9, bd-77x, bd-p0i, bd-6sk, bd-c0l, bd-scy, bd-smg, bd-4xr*), Phase 11 tests
   (bd-inc, bd-rj7, bd-gzf, bd-004, bd-c45, bd-1cb).
2. **Docs/artifact** (bd-o4q DATA_MANIFEST, bd-u50 repro appendix, bd-609 .env.example, bd-jz4 CI,
   Phase 9 docs consolidation, Phase 4 remainder, Phase 5 figures).
3. **Paper-number edits** (Phase 1, Phase 10, Phase 14) — RE-VERIFY each number against its CSV
   before editing (calibration-grade gate); several need an author decision (which run is canonical,
   e.g. bd-dka +4 vs +9). These + Phase 2 template/bib need the user or Overleaf.
4. **The 7 number-touchers + the 2 refactors** — LAST, with ledger entries; refactors after bd-xlqh.

## Done this session (~18 beads, all verified, tests green throughout)
Freeze tag `submission-evidence-base`; bd-xr5 (green test + pricing pin), bd-s4n (LICENSE),
bd-3w1 (freeze), bd-miv (P11-7 clean), bd-124 (CLAUDE.md), bd-n36 (quarantine), bd-3ql (deps),
bd-f67 (V1), bd-r1m (build), bd-utz (specs V2), bd-jy7 (README numbers), bd-vbp (rule count),
bd-ghi (Google), bd-l7d (README quickstart), bd-3yf (clippy clean), bd-rti (dead deps).
Plus: retired the freeze gate, wrote the rerun-ledger, hardened per the 3 reviewers.

## Done in the full-cleanup execution run (ALL closed, workspace green, verified)
**Phase 3 latent (6):** bd-jfs (Anthropic parallel_peer), bd-nj3 (ST composition-excluded), bd-5r5
(async Composed dispatch), bd-j8c (eviction clears companion rows), bd-2wn (record hits / true LRU),
bd-lz1 (cache stats reads real memo DB, phantom span readers deleted).
**Phase 7 (7 + autogen):** bd-ezj/bd-7b9 (Anthropic streaming no-mask + async await), bd-8jm (async
Anthropic optimized), bd-3pt (crewai+autogen async adapters guarded), bd-jvk (fail-open test made
non-tautological, mutation-proven; catch_unwind moved into ffi::optimize_plan), bd-4pn (README
fail-open claim narrowed), bd-nr5 (shadow billed-inline-call disclosed: README+docstring+main.tex).
**Phase 13 (7):** bd-ybd (degradation logging policy: agentc._degradation.log_degraded), bd-876
(traced fn no longer double-executes), bd-tfz/bd-w3h/bd-bpp (silent-disable paths → WARNING),
bd-kq7 (no fabricated divergence sample), bd-8ln (cached None/failed decode → real-call fallback,
no None-to-app).

All Python fixes verified via isolated harnesses (no maturin/native here); durable pytest tests
added and run in CI. No ledger entries needed (all pure-latent / plumbing / docs). main.tex edit in
bd-nr5 was a disclosure only (no number changed). bd-lcd (P3-1 ParallelBranch USD-ratio) still
deferred to the number-toucher phase.

**Phase 8 DB plumbing — DONE (8 of 9; bd-4xr deferred):** bd-c0l (p99 migration propagates non-dup
errors), bd-gzm (WAL on audit+cost DBs), bd-p0i (merge lock no longer defeatable — removed unsafe
unlink-recovery), bd-6sk (failed merge rolls back before DETACH; mutation-proven), bd-scy (report
projected savings honestly), bd-ul9 (process-global memo cache pool per path + per-call threshold),
bd-77x (migrate_db refuses loudly instead of silently bricking; +hardening test fallout fixed),
bd-smg (agentc-core owns traces.db memoization schema; core no longer depends on memo — memo->core
now, single owner). All cargo test + clippy green; all latent (no re-run).
- **bd-4xr** (P8-8): number-toucher (overhead heap allocs) — RE-VERIFY overhead bench (free); do in
  the number-toucher phase, add a ledger row.

**Phase 11 test-coverage — DONE (6):** bd-inc (planner disable-gate test, mutation-proven),
bd-gzf (composition driver-conflict gate reached, mutation-proven), bd-1cb (guard now runs on native
Anthropic + _response_output_text handles Anthropic shape), bd-004 (4 false-assurance tests fixed:
V2 composition gate now requires real Composed, CC follow-on de-vacuumed, dead _shadow.py+test
deleted → idea filed as bd-v02z, fail_open was bd-jvk), bd-c45 (CI installs .[test] + builds CLI +
loud guards so provider-patch & CLI suites can't silently skip), bd-rj7 (Python entry of the guard
loop tested). Full workspace green (15 groups), clippy clean.

Filed during this run: **bd-v02z** (async background shadow sampling — future improvement over the
sync inline maybe_shadow_record; recoverable from git at the bd-004 deletion commit).

Next lane: docs/artifact (bd-o4q DATA_MANIFEST, bd-u50 repro appendix, bd-609 .env.example, Phase 9
docs, Phase 4/5) → paper-number edits (Phase 1/10/14, RE-VERIFY vs CSV) → the 7 number-touchers +
2 refactors (bd-yqr, bd-n8s after golden test bd-xlqh) last. bd-jz4 CI partly addressed by bd-c45.

## Invariants (from the whole audit — don't relearn the hard way)
- Every executed change: verify before commit (run the test/build; check numbers vs CSV).
- `pricing.json` is frozen evidence — never edit it (a test pins its hash).
- Never edit a committed CSV; retracted ones live in `bench/paper_results/retracted/`.
- `bd close` occasionally doesn't persist — verify the close took.
- Commit messages: `fix(Pn-m): ...` + the MNT id; end with the Co-Authored-By line.
- Local commits only; do NOT push unless the user says so.
