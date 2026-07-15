---
title: Venue & Roadmap Decision
status: draft
last-updated: 2026-07-14
---

# Venue & Roadmap Decision

A decision document for Will Boudy + Avery Clapp. It does **not** decide the venue —
it lays the choice out so the decision is easy when you return to it after cleanup.

Read alongside, and reconcile with:

- `paper-intelligence/strategy-and-venues.md` — the existing venue ladder (VEN-001..011)
  and `DEC-007`, which **locked MLSys 2027** on 2026-05-14. That lock was made on
  topical-fit grounds and **predates** the hostile reviewer simulation summarized here.
  This document revisits it in light of that new evidence; it does not unilaterally
  override `DEC-007`. Treat the lock as *open for re-decision*.
- `specs/working/maintenance-audit-findings.md` — Phase 16 (strategic reviewer verdict),
  the Pass-6 risk buckets + freeze policy, the Pass-9 refutations.
- `bd list` — authoritative scope/status (15 epics, ~150 tasks).

---

## 1. One-Page Summary — the core tension

**The 144-fix cleanup makes the paper correct. It does not make it accepted.** A hostile
MLSys-reviewer simulation (bd-323l, Phase 16) returned **WEAK REJECT for MLSys main,
explicitly assuming every one of the 144 correctness findings is already fixed.** The
objections are structural — contribution shape, evaluation design, venue fit — and no
amount of number-fixing touches them. The same paper flips to **weak-accept at COLM** and
**clear-accept at a workshop**.

Stated plainly: **the current evidence honestly supports a workshop or COLM paper today.
An MLSys-main paper requires one experiment we have not run — the runtime executed blind,
at scale, on real un-engineered agent traffic, over multiple trials with confidence
intervals.** Everything else is polish.

Where the strength and the weakness sit:

- **Strongest asset (the reviewer credited all three):** the **LLMLingua-2 dual-regime
  head-to-head** (clean, mechanistic, ~4-orders-of-magnitude overhead win — the real
  finding); the **evaluation methodology** (shared-baseline ablation, input-token as a
  deterministic attribution axis, documented failure modes — the most MLSys-native,
  transferable piece, and currently *undersold*); and the **label-free divergence guard**
  (contains a −42.7pp degradation with no labels while preserving a benign rule).
- **Deepest weakness (fixture inversion):** four generic agents produced near-zero savings,
  so the benchmark fixtures were **engineered to activate the rules.** On un-engineered
  traffic, the demonstrated behavior is **abstention, not savings.** The headline "9-rule
  runtime, up to 34%" is validated on 3 rules in isolation + 2 in composition, on workloads
  built to trigger them. Real-world savings are **unproven.** Compounding it: a
  marketing/candor mismatch — honest limitations in the back, an oversold 9-rule/34% claim
  in the front, from which an area chair can assemble the rejection out of the paper's own text.

The venue question therefore reduces to **one fork** (Section 4): pay for the at-scale blind
experiment and earn a shot at MLSys, or accept the honest ceiling and ship the corrected
paper to its natural home (COLM / workshop). The cleanup in Section 3 is required either way.

---

## 2. Venue Options

Every row assumes the **shared core (Section 3) is done.** "Delta" is the *additional* work
beyond that shared core. Reviewer verdicts are from the Phase 16 red-team; venue calendar
dates are best-estimate — most CFPs for the target cycle are not yet public and must be
re-confirmed.

| Venue | Current readiness | Likely reviewer verdict | Delta beyond shared core | Deadline / timing (est., unconfirmed) | Risk |
|---|---|---|---|---|---|
| **MLSys 2027 (main)** | Not ready — structural, not cosmetic | **Weak reject** even fully-fixed | **Move A** (at-scale blind real-workload run, multi-trial + CIs) **+ Move B re-center.** Weeks + real $ + a live chance the result is null. | **Est. late-Oct 2026**; CFP not public. Format-chain long pole is 2–4 wks and blocked on the unpublished MLSys template. | **High.** Move A may still land abstention (→ still weak); deadline realistically missable with a 2-author team. |
| **COLM 2027** | Promising | **Weak accept** | Small: sharpen the cost-quality frontier framing + Move B; LM-facing baselines (LLMLingua family) already in hand. No new at-scale run required. | **Est. ~March 2027** (annual spring cycle) — **~5 months more runway than MLSys.** | **Moderate.** Must lift from weak-accept; an LM venue may undervalue the runtime/systems angle. |
| **Workshop** (ES-FoMo @ ICML, MLSys workshop, or an efficient-LLM workshop) | Ready-ish | **Accept** | Near-zero — a page-limit trim at most. | Rolling; ES-FoMo tracks ICML (~May 2027 est.), MLSys workshops co-locate with the conference. | **Low.** Non-archival / lower prestige, but the fastest real reviewer feedback and a de-risking fallback. |
| **EuroSys 2027 fall** (systems fallback) | Not ready | Reject without Move A **and** an overhead/scaling story | **Largest delta:** Move A **+** deeper systems evidence — overhead/tail-latency, multi-framework, operational realism. Systems reviewers are hardest on fixture-inversion and under-scaling. | Fall deadline **~late-Sep 2026** (per strategy doc) — **tighter than MLSys.** | **Highest** for a main systems venue. Use only as a resubmit lane, not a primary. |
| **ARR → EMNLP-efficiency** | Possible | Borderline | Reframe as NLP methodology with the LLMLingua head-to-head as the centerpiece; broaden baselines. The methodology + head-to-head travel well here. | ARR rolling/monthly; EMNLP 2026 commitment likely passed → next ACL/EACL 2027 window. | **Moderate.** NLP reviewers may read it as engineering; but the two strongest assets are LM-native. |

**Reading of the table:** MLSys and EuroSys are gated on the same unrun experiment (Move A)
and the tightest calendars. COLM and the workshops accept the paper you can write from
today's evidence, and both give *more* runway than MLSys, not less.

---

## 3. The Shared Core — do this now, venue-independent

This is the **~34-bead "minimal viable submission" set** the scope-cutter identified: the
work **every** venue needs regardless of the fork. It maps to Pass-6 **Bucket 1 (INERT,
~60)** plus the **ADDITIVE diagnostics (Bucket 2, 13)** and the single paid **T2**
experiment. It **excludes** Bucket 3 (behavior-changing, 25 beads) and Bucket 4
(evidence-invalidating, 11 beads) — those are post-submission, behind the freeze tag, and
re-running is a trap (fixtures gone, snapshots unpinned).

Execute this first. It is the same work whether you end up at MLSys, COLM, or a workshop.

| Group | What it covers | Representative beads |
|---|---|---|
| **Freeze the evidence** | Tag the submission commit; hash-pin `pricing.json`; reconcile the second price table. Nothing behavior-changing lands on main until this is set. | bd-3w1, bd-yuxr |
| **Paper honesty (Phase 1)** | The falsifications and unbacked rows: fix the `research_planner` row (remove "not significant"), rebuild `tab:mdcc-orthogonality` from the warmup CSV, resolve the guard headline onto its backed frontier, back-or-cut the cold-agent and cachehit rows, the mechanical single-number corrections, and **reframe** (not delete) the CC+SD mechanism. | bd-dka, bd-ctk, bd-lck, bd-pzv, bd-ah2, bd-uk8, bd-k12, … |
| **Artifact first-contact (Phase 2)** | Commit `references.bib`; port the template; declare the 9 undeclared deps; fix the fixture bootstrap so the reviewer's first command runs; point README at the warmup harness; rebuild `DATA_MANIFEST.txt`; fix the repro appendix; bundle-or-fail the model weights; execute the real trim. | bd-gb3, bd-99w, bd-3ql, bd-bjs, bd-l7d, bd-o4q, bd-u50, bd-4p1, bd-6lo |
| **Build / CI health (Phase 6)** | The clone-and-run basics: un-RED `cargo test` (a hardcoded date self-detonated it), fix the release build, add a LICENSE. | bd-xr5, bd-r1m, bd-s4n |
| **Doc truth (Phases 4–5)** | Rewrite README status tables from warmup CSVs; fix `CLAUDE.md` ("no implementation code exists yet"); quarantine the retracted CSVs (root cause of the worst paper defects). | bd-jy7, bd-124, bd-n36, bd-xzs |
| **The one paid experiment** | **[T2] naive-baseline attribution ablation.** *Additive* — new evidence, invalidates nothing — and the acceptance lever reviewers will actually demand. Money spent here is well spent for any venue. | bd-fim |
| **Re-center the framing (Move B)** | Demote the 9-rule/34% marketing; re-center on **structure-gated abstention + the divergence guard**, so the honest limitations become peripheral instead of load-bearing. Cheap (a rewrite), and it lifts the ceiling at *every* venue. | bd-323l (Move B) |

**Pass-9 already shrank this.** The fire-count correctness program (Phase 12) was **refuted
and downgraded** to post-submission nice-to-have, and one paid guard re-run **fell away**
(disclose the ~100% experimental shadow rate in one line instead of re-running at 2%). That
was the single biggest scope reduction in the review — bank it.

---

## 4. The Fork — the one decision that changes everything

Everything above is shared. **This is the only branch point:**

> **Do you invest in Move A — running the runtime blind, at scale, on real un-engineered
> agent traffic (tau-bench / GAIA-agentic / SWE-bench Verified, or real LangChain/AutoGen
> traces you did not construct), letting structure-gating decide where rules fire, and
> reporting aggregate cost + accuracy over multiple trials with confidence intervals?**

This is the single experiment that converts *"helps on benchmarks I built for it"* into
*"helps in the wild"* — the exact sentence a meta-reviewer needs to accept.

### Branch A — Invest (→ MLSys / EuroSys viable)

- **Cost:** real money (paid LLM calls at scale × multiple trials for CIs), and weeks of
  the 14-week runway that the shared core already largely consumes.
- **Benefit:** the *only* path off weak-reject at a top systems venue. If savings survive
  on un-engineered traffic → you have a systems paper.
- **Honest risk:** the Phase 16 reviewer's own expectation is that un-engineered traffic
  shows **abstention, not savings.** If so, that is the true ceiling — and it routes you
  right back to the workshop/COLM home, just later and poorer.
- **Option value:** the result is not wasted either way. A null result is a *publishable,
  honest* negative ("the do-no-harm discipline holds; savings don't generalize") that
  *strengthens* the COLM/workshop paper. So Move A is best understood not as a bet on
  MLSys but as **the instrument that tells you which venue you are writing for.**

### Branch B — Don't invest (→ COLM / workshop honest home)

- **Cost:** you forgo MLSys 2027 this cycle.
- **Benefit:** ship the corrected paper to a venue where today's evidence is already
  weak-accept (COLM) or accept (workshop). Cheaper, faster, lower-risk, and the two
  strongest assets (LLMLingua head-to-head, methodology, guard) are LM-native — they land
  *better* at COLM than at MLSys.
- **Runway:** COLM (~March 2027) and the workshops have deadlines **after** late-Oct 2026,
  so this branch actually *relaxes* the calendar.

### The hidden timing coupling (read this before deferring)

The shared core (~34 beads) plausibly runs **weeks 1–8+**; the format chain alone is a
**2–4 week long pole blocked on the unpublished MLSys template.** Move A takes **weeks +
money** on top. **Therefore: deferring the fork until after cleanup is itself a decision —
it forecloses MLSys**, because Move A cannot be squeezed into the weeks left before
late-Oct. To keep MLSys alive you must start Move A **in parallel with** cleanup, now. If
you are content to let MLSys 2027 go, deferring is completely safe — COLM and the workshops
wait for you.

---

## 5. Per-Venue Roadmap (rough, weeks 1–14)

### Track 1 — Aim MLSys (commit to Move A now)

| Weeks | Focus |
|---|---|
| 1–2 | Freeze; start shared-core Phase 1/2 in parallel; **scope + launch a time-boxed Move A spike** (one public agentic benchmark, small multi-trial pilot) — this is the venue-decider, run it early and cheap. |
| 3–5 | Shared core continues (bib, template port, artifact first-contact). **Move A pilot result in hand.** Decision gate: durable savings with CIs → full Move A; abstention-dominant → abort to Track 2/3. |
| 6–9 | If GO: full-scale Move A run + CIs; Move B re-center so abstention is peripheral; T2 ablation. |
| 10–12 | Assemble the systems narrative; overhead/tail-latency; trim to template budget. |
| 13–14 | Internal review, artifact packaging, submit. **High risk of slipping past late-Oct.** |

### Track 2 — Aim COLM (primary honest default)

| Weeks | Focus |
|---|---|
| 1–4 | Shared core: freeze, paper honesty, artifact first-contact, doc truth. |
| 5–7 | Move B re-center on the cost-quality frontier + guard; T2 ablation; sharpen the LLMLingua-2 head-to-head as the centerpiece. |
| 8–10 | Cost-quality frontier framing, LM-facing baseline table (routing/compression/caching), stochastic-uncertainty treatment. |
| 11–14 | Draft to COLM shape, internal review. **Deadline is ~March 2027 — you have slack; use it to strengthen, not to idle.** Optionally submit the current cut to a workshop in parallel for early feedback. |

### Track 3 — Aim workshop (fastest, lowest-risk)

| Weeks | Focus |
|---|---|
| 1–4 | Shared core (a workshop needs *less* of it — artifact-eval bar is lower). |
| 5–6 | Move B re-center; trim to workshop page limit. |
| 7–8 | Submit to the next open efficient-LLM / agent-systems workshop; bank the reviews. |
| 9–14 | Fold workshop feedback into the COLM or MLSys 2028 version. Workshop is the on-ramp, not the destination. |

---

## 6. Recommendation

**Default: execute the shared core now (it is venue-independent), and run Move A as an
early, time-boxed decision spike — not as a full MLSys campaign. Let the spike pick the
venue.**

- If the blind, at-scale pilot shows **durable savings with confidence intervals** →
  escalate to the full Move A run and aim **MLSys 2027**, accepting the calendar risk.
- If it shows **abstention-dominant behavior** (the reviewer's own expectation, and
  consistent with the fixture-inversion weakness) → **that is your answer.** Target **COLM
  2027 as the primary venue** (weak-accept today, ~5 months more runway, LM-native fit for
  your strongest assets), with a **workshop submitted in parallel** for fast, real reviewer
  feedback and as a de-risking fallback.

**Reasoning, honestly:**

1. **MLSys is weak-reject even after all 144 fixes** — the block is structural, and only
   Move A can move it. With two authors and ~14 weeks mostly consumed by the shared core
   (format chain being a 2–4 wk long pole on an unpublished template), an unconditional
   MLSys commitment is the highest-risk option on the board.
2. **The strongest assets are LM-native.** The LLMLingua-2 head-to-head, the evaluation
   methodology, and the label-free guard read *better* at COLM than at a systems venue
   steeped in vLLM/Orca — which is exactly where the fixture-inversion weakness bites hardest.
3. **The spike is cheap insurance.** Move A's result is the actual venue-decider, and it
   pays off in either direction — a null result is a publishable honest ceiling that
   strengthens the COLM/workshop paper. Running a scoped version early makes the "decide
   later" the authors want into a *well-informed* later, not a foreclosed one.
4. **This respects the intent to decide after cleanup** — the cleanup is identical for
   COLM and workshop, and the one thing that *can't* wait for cleanup (Move A, if MLSys is
   to stay alive) is surfaced explicitly so the choice is deliberate, not accidental.

**Reconciliation with `DEC-007`:** the MLSys lock was set 2026-05-14 on best-topical-fit
grounds, before the Phase 16 red-team existed. Recommend **re-opening it** and treating
MLSys as *conditional on Move A landing positive*, with **COLM (VEN-010) elevated from
"next cycle" to primary** and a workshop (VEN-005/006) as the parallel feedback/fallback
lane. **EuroSys 2027 fall remains the systems resubmit lane only** — its delta is the
largest and its deadline the tightest.

**The final call is Will + Avery's.** This document is the map, not the move.
