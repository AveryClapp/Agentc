---
title: Guard Frontier Sweep Findings
status: active
last-updated: 2026-09-03
---

## CROSS-MODEL GENERALIZATION (added 2026-06-10, commits a981394 + 7bf2673)

Ran the decisive selectivity cells on Llama-3.3-70B (Together, OpenAI-compat endpoint,
n=100, ~$2.39) via existing BENCH_OPENAI_BASE_URL + BENCH_BASELINE_MODEL plumbing (no
code). Result: the metric selectivity is MODEL-AGNOSTIC.
- benign CC: unguarded fires 93/100 (+3.0pp ns, 29.9% savings); lexical disables it
  (5 fires, savings ->1.6%); normalized keeps it (93 fires, 29.9% preserved, +2.0pp ns).
- catastrophic SD: both metrics disable (-26.0pp -> -12/-17pp); catch generalizes but
  recovery less complete than gpt-4o-mini (honest caveat, stated in the paper).
Data: gsweep_xmodel_{off,lexical,normalized}_{rp,an}*.csv. Wired into sec:eval-guard
("The selectivity is model-agnostic" para) + abstract (reproduces across model families
+ bounded 2% cost + 97% damage-prevented reframe).

## 4-FAMILY CROSS-MODEL + OVERHEAD (added 2026-06-11, commits a981394..ddabccf)

Extended the cross-model study to FOUR families and measured guard overhead.
Selectivity (lexical over-disables benign CC / normalized keeps it) at each model's
operating budget; normalized keeps benign on 4/4 (93/100 fires), lexical over-disables
on 4/4. Catastrophe-recovery completeness is model-dependent (35-93%).
- gpt-4o-mini (OpenAI) tau=0.20: lex disabled/8%, norm kept/32%, SD 93% prevented
- Llama-3.3-70B (Meta) tau=0.20: lex 1.6%, norm 30%, SD 35%
- Claude Haiku 4.5 (Anthropic) tau=0.20: lex 17%, norm 31%, SD 73%
- Qwen3-235B (Alibaba) tau=0.10: lex 1.7%, norm 31%, SD 66%
KEY FINDING: the divergence budget is a PER-MODEL hyperparameter. Qwen3's verbose
output regime needs tau=0.10 (not 0.20); at 0.10 the full pattern reproduces on both
axes (lexical collapses benign to 1.7%, normalized catches SD 20%->66%). The apparent
Qwen3 anomaly was an operating-point shift, not a failure -- characterized via a
deliberate sweep (reported in full, not fished). Data: gsweep_{xmodel,claude,qwen3}_*.csv.
Integrated: tab:xmodel + upgraded "selectivity generalizes" para + abstract (4 families).

GUARD OVERHEAD (SUPERSEDED 2026-09-03): the historical 18us result is invalid
for the current controller. The harness replayed one synthetic observation
token, so its 0.7us fold measured the legacy idempotence fast path and skipped
fresh complete-plan profiling, exposure accounting, and durable writes. The
corrected harness issues one canonical composed-plan token per sample and times
the synchronous SQLite persistence boundary. Its output is a single-machine
Stage E0 diagnostic, explicitly not paper evidence; remove the 18us / five-orders
claim until a release-mode, end-to-end, contention-aware measurement is run.
Shadow inference remains excluded from this local diagnostic and must be
accounted as a real provider call.

The corrected development-machine E0 run (2,000 fresh tokens) measured 304.0us
mean / 401.4us p99 for accepted complete-plan feedback and 5.1us mean for the
separate normalized-containment metric. The committed structured output is
`bench/repro/complete-plan-guard-overhead-preflight-2026-09-03.json`; these
numbers characterize one local run and are not promoted into the paper.

Providers used: Together (Llama, Qwen3; ~$2.39+$2) and Anthropic compat endpoint
(Claude Haiku; HF PRO plan was cancelled -- use Together/Anthropic, not HF). All via
existing BENCH_OPENAI_BASE_URL+BENCH_BASELINE_MODEL env plumbing, no code changes.

ARTIFACT-EVAL READINESS (commit e043c6c): the guard/cross-model drivers were
ephemeral (/tmp) -- committed them so the headline tables are reproducible (MLSys AE
track): bench/repro/guard_frontier.sh (tab:guard + fig:metric-tradeoff),
bench/repro/crossmodel_selectivity.sh (tab:xmodel, parametrized per family),
bench/repro/README.md (script->artifact map + per-family invocations). tab:repro
updated with these rows. Scripts validated (bash -n + guard-error check).

REMAINING (author): (1) latexmk compile check -- all edits structure-verified only,
no TeX toolchain on the dev machine. (2) bd/dolt sync when server returns.

## QUEUED FOLLOW-UP (file as bd issues when dolt server is back; queued 2026-06-10)

bd server was unreachable at queue time, so these live here until they can be
filed. Both are paper-integration blockers, not research.

- **[#1, BLOCKER] Reconcile `tab:guard` to one self-consistent run family.** The
  paper's guard table cites rp/CC 37.7% off -> 11.0% (lexical) / 37.5% (norm.) at
  n=150, but no committed CSV produces those numbers and the off baseline (37.7%)
  does not match this run family (27.8%) -- different fixture/n/config. Replace the
  operating-point row with numbers drawn from the n=200 tradeoff frontier (this
  family), OR re-run the table's exact cells at matched config. Every number in the
  guard section must trace to one committed, reproducible run. Resolve the n=150 vs
  n=200 mismatch in the same pass.
- **[#2, RESOLVED 2026-06-10 -- no new cell needed].** Earlier worry was that
  shadow_rate=1.0 inflates savings. CORRECTION: the negative-savings artifact is a
  *disable* artifact, not a shadow-twin artifact -- it appears ONLY in cells where
  the rule got disabled mid-run (n300 lexical 49 fires -> disabled -> -15%). For the
  NON-disabled (kept) benign case the savings are clean and corroborated two ways:
  off_rp (shadow=0, zero twins) = 30.15% cost / 33.82% input-token savings; the
  full-shadow normalized frontier rp cells (shadow=1.0, kept) = 29-30% cost /
  32-34% input tokens. They agree to ~1pp, so shadow overhead is negligible when the
  rule is kept, and the production-rate (0.02) number lies in the same band.
  **Honest guarded-benign savings = ~30% cost / ~34% input tokens** (n=200, same run
  family as the frontier and the off baseline). Use input-token savings as the
  headline magnitude (pricing-independent). Disabled-cell savings are still garbage
  and must not be reported -- but we never report savings for a disabled rule anyway.
- **[#3, DECIDED] Lock normalized (dependency-free) as the hero metric**, embedding
  framed as optional semantic tightening. Author agreed 2026-06-10; the frontier
  supports it (normalized is both dependency-free and the metric that beats lexical).
- **[#4, CONSIDER PRE-SUBMISSION] bd-ljd (CC proxy quality).** ContextCompress is
  now load-bearing for the benign half of the headline. If CC's compression quality
  is shaky a reviewer can attack the foundation under the frontier ("you preserve a
  rule that produces worse outputs"). Hardening CC quality directly armors the lead
  result. The only deferred issue that would strengthen *this* paper; pull forward if
  time allows. (bd-j3k memoization eval and bd-4hy writer queue stay post-submission.)

## INTEGRATION CHECKLIST (run before declaring the artifact clean)

- [x] #1 guard-table reconciliation DONE (scoped, commit 401d49f): research_planner
      guard rows -> n=200 frontier (off -1.5/33.8, lexical disabled/8.1, normalized
      +-0.0/32.4 kept); caption reconciles the n=100 +4pp per-agent result.
- [x] #2 savings magnitude RESOLVED (no new cell): ~30% cost / ~34% input tokens,
      corroborated by shadow=0 off_rp. Wired into the rows above.
- [x] #3 normalized-hero framing wired into prose (normalized = selective hero,
      embedding = optional learned upgrade).
- [x] Drop frontier figure DONE: fig9_metric_tradeoff (figures/) + caption + the
      rewritten 'divergence metric governs selectivity' paragraph in sec:eval-guard.

### Residual flagged for author (NOT auto-edited; abstract/intro left untouched)

- Intro (main.tex ~L175) still says CC "stays enabled at 37.5--37.6% savings on two
  agents". The research_planner figure is now 32.4% at n=200 in the body/table; the
  37.5% there was the old n=100 number. Left as-is per the scoped decision (no
  intro edits). If you want the intro internally consistent with the table, change
  "37.5--37.6\%" -> "32.4--37.6\%" (one number, not the headline claim).
- Abstract (L60) + intro (L173): StateDrop "-42.7pp -> non-significant -0.7pp" kept
  at n=150 by design. The n=200 result (-49.5 -> -3.5, ~91% prevented, borderline
  significant) appears only as corroboration in the eval body. If you ever want the
  whole paper at n=200, that is the full-replacement path and it edits the abstract.
- [x] **bd-e0s artifact-cleanliness check (DONE 2026-06-10, clean).** Verified in
      bench/run_guard_eval.py: STORAGE_ROOT=/tmp/agentc-<TAG> (L45) is rmtree'd +
      recreated per cell (L201-203); disable readout/wipe scoped to
      reason='shadow_divergence' in the per-cell opt_dir, not ~/.agentc (L137-178).
      The shared-DB contamination class cannot affect committed frontier CSVs.

### bd-ljd sequencing (pulled BEFORE integration so the paper is written once)

CC is load-bearing for the benign half, so any change to *what CC outputs* would
make the running n=200 frontier (and the n=300 cells) stale and force a full
re-run. Therefore split bd-ljd into assess-then-maybe-harden:

- [x] **CC quality assessed (DONE 2026-06-10): SOUND, no re-run.** On the benign
      normalized cells where CC fires ~190x (max exposure), pass->fail flips are
      balanced by fail->pass flips and NONE are significant: rp 0.10 BF12/FB9 p=0.66;
      0.20 BF10/FB10 p=1.00; 0.30 BF9/FB11 p=0.82; 0.50 BF6/FB8 p=0.79. acc delta
      -1.5..+1.0pp. The flips are temperature-1 noise, not systematic compression
      damage (which would show BF>>FB, p<0.05). CC compressions are faithful.
- [x] **Harden+re-run NOT triggered** -- audit found no real problem. The only
      re-run path in the endgame is closed.

Sequence: finish frontier -> e0s (done) -> CC audit -> (harden+re-run only if
needed) -> production-rate savings cell (#2) -> integrate prose ONCE (#1/#3 + figure).

## POST-PAPER BACKLOG (only after #1/#2/#3 + checklist are complete)

Deferred engineering/scope items. None block submission; revisit once the paper is
done. File as bd issues when dolt is back.

- **bd-ljd (CC proxy quality)** -- if not already pulled forward as pre-submission #4.
- **bd-e0s (traces.db contamination)** -- the durable fix to the shared-DB state leak
  (the checklist above is just the one-time artifact verification, not the fix).
- **bd-j3k (memoization eval)** -- second optimization axis; additive scope, a
  follow-on contribution rather than reinforcement of the guard claim.
- **bd-4hy (writer queue)** -- runtime internals; no paper relevance.


# Guard Frontier Sweep: Findings and Open Decisions

Date: 2026-06-10 (overnight autonomous run)
Status: data committed; paper integration NOT done (held for author review)

## What was run

A threshold frontier for the shadow-divergence accuracy guard, using the
`run_guard_eval` harness with full shadow sampling (`AGENTC_OPTIMIZE_SHADOW=1`)
and per-cell isolated storage. Two targets:

- **benign**: `research_planner` / ContextCompress-only (rp), n=50
- **catastrophic**: `analyst_qa` / StateDrop-only (an), n=200

Metrics swept via `AGENTC_SHADOW_DIVERGENCE_MODE`: embedding (full frontier),
normalized and lexical (operating-point + diagnostics).

Result CSVs: `bench/paper_results/gsweep_{embedding,normalized,lexical}_{rp,an}_*.csv`,
plus `emb_ckpt_{rp,an}.csv` (the 0.15 embedding point) and
`frugal_cascade_gaia.csv` (FrugalGPT cascade baseline, n=127).

## Result 1 (STRONG, clean): embedding frontier

Catastrophic StateDrop recovers monotonically as the budget tightens; benign
ContextCompress is preserved across the entire frontier.

| tau  | an/SD acc d (n=200) | rp/CC acc d (n=50) | rp/CC savings |
|------|---------------------|--------------------|---------------|
| 0.05 | -3.5                | +4.0               | 21.7%         |
| 0.10 | -3.0                | +8.0               | 27.1%         |
| 0.15 | -3.5                | +6.0               | 28.1%         |
| 0.20 | -2.5                | +8.0               | 25.0%         |
| 0.30 | -5.5                | +4.0               | 27.6%         |
| 0.50 | -11.0               | +4.0               | 27.5%         |
| off  | **-48.0**           | +4.0               | 27.8%         |

Unguarded StateDrop at n=200 is **-48 pp** (far worse than the -42.7 pp the paper
reports at n=150; full damage only appears at scale). The guard recovers it to
about -2.5 pp at tau=0.10-0.20 while leaving benign CC untouched. This is the
headline figure: `bench/paper_figures/fig9_guard_frontier.pdf`.

## Result 2 (operating-point ablation): the three metrics CONVERGE at tau=0.15

Matched n (rp=50, an=200), full sampling:

| metric     | rp/CC (benign)            | an/SD (catastrophic)      |
|------------|---------------------------|---------------------------|
| lexical    | +10.0 pp, 26.5% (kept)    | -4.0 pp (caught)          |
| normalized | +6.0 pp, 27.3% (kept)     | -4.5 pp (caught)          |
| embedding  | +6.0 pp, 28.1% (kept)     | -3.5 pp (caught)          |

At tau=0.15, **all three metrics keep benign CC and catch catastrophic SD**.
There is no metric differentiation at this operating point.

## Result 3 (diagnostic): lexical over-conservatism is real but noisy

Lexical metric, rp/CC, n=50, tightening budget:

| tau  | acc d | savings | CC fires | disables |
|------|-------|---------|----------|----------|
| 0.02 | +2.0  | 24.0%   | 40       | 1        |
| 0.05 | 0.0   | **3.0%**| **5**    | 1        |
| 0.10 | +6.0  | 26.3%   | 44       | 1        |
| 0.15 | +10.0 | 26.5%   | 44       | 1        |

At tau=0.05 lexical collapses benign CC to 3% savings (disables after 5 fires) --
this **reproduces the paper's over-conservatism failure mode** (paper reports
37.7% -> 11.0%). But it is **non-monotonic**: tau=0.02 keeps the rule, 0.05 kills
it, 0.10/0.15 keep it. At n=50 the disable *timing* is high-variance.

## UPDATE 2026-06-10: metric-selectivity claim CONFIRMED at n=300

The "undertested / threshold-sensitive" worry below was an **n=50 small-sample
artifact**. Re-run at n=300 (research_planner/CC, fixture long_context_qa_n300,
full shadow sampling), the selectivity is robust and clean:

| metric @0.15 | acc d | CC fires (of 300) | disabled? |
|--------------|-------|-------------------|-----------|
| lexical      | -0.7  | 49                | YES       |
| normalized   | -0.7  | 289               | no (kept) |
| embedding    | +0.7  | 290               | no (kept) |

Lexical also disables CC at tau=0.05 (10 fires). So at n=300 **lexical
over-disables benign CC at both thresholds tested, while normalized and embedding
keep it** -- all at ~0 accuracy cost. The naive metric cannot preserve a benign
output-changing rule; the selective metric can.

**Savings numbers are NOT usable from these cells.** With shadow_rate=1.0 (set so
disables actually trigger), each rewritten call gets a full-cost shadow twin for
measurement. cost_savings_pct is dominated by that overhead, not compression:
lexical@0.05 (10 fires) shows +25% while lexical@0.15 (49 fires) shows -15% --
non-monotonic, driven purely by fire-count -> shadow-twin count. Use **behavioral
axes** (rule retention = fires/total; disabled yes/no; accuracy delta) which are
sampling-rate-independent. A clean savings magnitude needs a production-rate
(shadow~=0.02) run, which is a separate measurement.

Data: bench/paper_results/gsweep_n300_{lexical,normalized,embedding}_rp_0.15.csv,
gsweep_n300_lexical_rp_0.05.csv.

## Concerns for the author

1. ~~**Metric-selectivity claim is threshold-sensitive and undertested.**~~
   RESOLVED at n=300 (see UPDATE above). The earlier n=50 non-reproduction was a
   small-sample artifact; the claim holds robustly at scale.

2. **Paper's rp/CC guard numbers lack a committed backing CSV.** Commit a80f4b7
   (which introduced "rp 37.5%") changed no result CSV. No committed CSV in
   `bench/paper_results/` contains the 37.5% / 11.0% rp guard figures. The unguarded
   savings baseline also differs: paper says rp/CC off = 37.7%, this frontier's
   rp/CC off = 27.8% -- indicating a different fixture/n/config than these cells.

3. **n mismatch with the paper.** Frontier analyst cells are n=200; the paper's
   guard table is n=150. The frontier figure and `tab:guard` are therefore not the
   same configuration.

## Open decisions (author only)

- **Positioning fork**: the paper currently makes the *normalized containment*
  metric the dependency-free hero and frames embedding as a future tightening "at
  the cost of the dependency-free property." The elevation plan wanted embedding as
  the headline. These conflict. Decide before wiring fig9 into the prose.
- Whether to re-run the metric ablation at higher n (e.g. n>=200 both agents) at a
  single fixed threshold to make the selectivity claim stable, or to soften the
  claim to "metric choice governs the *operating band*, not the operating point."
- Whether the frontier replaces, or sits alongside, the existing `tab:guard`.

## Reproduction

```
# embedding frontier + normalized point + cascade (~3 hr, ~$1)
AGENTC_SHADOW_DIVERGENCE_MODE=embedding bash /tmp/emb_frontier_driver.sh
# lexical operating point and tight diagnostic
AGENTC_SHADOW_DIVERGENCE_MODE=lexical bash /tmp/lexical_ablation.sh
bash /tmp/lexical_tight.sh
# figure
.venv/bin/python3 bench/paper_figures/fig9_guard_frontier.py
```
(driver scripts are in /tmp and not committed; the env-var recipe above is the
durable record.)
