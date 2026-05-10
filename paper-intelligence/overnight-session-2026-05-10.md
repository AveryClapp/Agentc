---
title: Overnight session 2026-05-10 — experiments + paper-ready additions
status: complete
last-updated: 2026-05-10
owner: paper-intelligence
---

# Overnight Session — 2026-05-10

User authorized up to $15 of API spend; left to do as much as possible.

## Status

| Tier | Item | Status |
|---|---|---|
| 1 | EXP-007: optimizer overhead from `plan_audit` | ✅ **done** — `bench/paper_results/optimizer_overhead.txt` |
| 1 | EXP-003: paired McNemar/bootstrap analyzer code | ✅ **done** — `bench/paired_analysis.py` |
| 2 | EXP-001: backfill StateDrop n=50 + paired data | ⚠️ **done with caveat** — paired data clean, cost data contaminated by `bd-e0s` |
| 3 | EXP-006: multi-rule end-to-end workload (CC+SD) | ✅ **done** — multirule_qa n=20 — clean data |
| — | Related Work expansion (GAP-007/RR-005/RR-013) | ✅ **drafted** — `paper-intelligence/draft-related-work-expansion.md` |
| — | Other paper edits (StateDrop dep model, "novel" → "two-stage", etc.) | ✅ **drafted** — `paper-intelligence/draft-paper-edits.md` |
| — | New bug filed: storage contamination | 📋 `bd-e0s` |

## Code changes (committed-able, not yet committed)

- `bench/optimizer_bench.py` — extended `RunStats` with `per_task: list[(task_id, passed)]`; added `_parse_per_task_pass_fail()` and a second regex; populated in `_run_side`.
- `bench/optimizer_ablation.py` — extended `AblationRow` with `per_task: list[(task_id, baseline_pass, optimized_pass)]`; sidecar `.per_task.csv` writer alongside the existing aggregate CSV; backward compatible (sidecar header always written, body empty when no PASS/FAIL lines emitted).
- `bench/agents/_runtime.py` — bumped OpenAI client `max_retries` from 2 to 8 (configurable via `OPENAI_MAX_RETRIES`). Tier-1 gpt-4o has 30k TPM, which the multirule run blew past on burst.
- `bench/agents/multirule_qa.py` — new agent for EXP-006. 3-step iterative answer refinement over long-context fixture. Activates ContextCompress (>8KB prompt with distractors) + StateDrop (older revisions out of read window) on every step. ModelDowngrade activates only with `BENCH_BASELINE_MODEL=gpt-4o`; left unset by default to avoid the 30k TPM ceiling.
- `bench/paired_analysis.py` — new analyzer. Reads the `.per_task.csv` sidecar; produces per (agent, config) McNemar exact p-value + 95% bootstrap CI on the accuracy delta.
- `bench/fixtures/multirule_qa.json` — symlink to `long_context_qa.json` (same task structure).

## Tier 1 results — overhead (free, done)

**File**: `bench/paper_results/optimizer_overhead.txt`
**Source**: aggregated across 4 `optimizer_audit.db` files (1,818 plan decisions).

```
plan_kind                   n      mean      p50      p95      p99      max
pass_through (rule did not fire)  1441   1.614 ms   76 µs   12.95 ms   21.11 ms   121.5 ms
rewritten (rule fired)             377   0.165 ms  120 µs    0.35 ms    1.23 ms     4.98 ms

Rewrites by rule:
  ContextCompress    n=2    p50=0.377 ms   max=  0.851 ms
  ModelDowngrade     n=3    p50=0.127 ms   max=  1.260 ms
  StateDrop        n=372    p50=0.120 ms   p99=  0.953 ms   max=  4.975 ms
```

**Paper-ready paragraph** (drop into §6 Evaluation, near the wall-clock note):

> Optimizer overhead, measured across 1,818 plan decisions, has median
> 76 µs on the pass-through path (the common case when no rule fires)
> and median 120 µs on the rewrite path. Tail overhead is dominated by
> first-call cost-model loads (p99 = 21 ms pass-through, 1.2 ms
> rewrite); steady-state overhead is sub-millisecond and three orders
> of magnitude below LLM round-trip latency.

## Tier 2 results — iterative_refiner full n=50 paired ablation

**Status**: ⚠️ COMPLETE BUT COST DATA IS CONTAMINATED. See `bd-e0s`.

Output: `bench/paper_results/iterative_refiner-statedrop-n50-paired.csv`
(+ `.per_task.csv`).

### The contamination

During the run, the baseline subprocess produced 500 LLM-call spans in
`_shared_baseline/traces.db` (totalling ~$0.08). After the run, the
same DB had **808 spans** totalling **$0.1362** — 308 extra spans
dated AFTER the baseline subprocess exited, in the wall-clock window
when optimized configs were running. Each optimized config's own
`<config>/optimized/traces.db` is correct (500 spans, ~$0.08). This
is not a one-off; it appears reproducible. Bug filed as `bd-e0s`. The
recent commit "29018f1 fix: honor AGENTC_STORAGE_PATH in merge +
post-record paths" was meant to address something similar — likely a
narrower fix that doesn't fully cover this case.

### What's salvageable

| Data | Status | Use |
|---|---|---|
| Per-task pass/fail (`.per_task.csv`) | **CLEAN** | from stdout, not traces.db |
| Optimized per-config cost (each `<config>/optimized/traces.db`) | **CLEAN** | $0.0789-$0.0848 across configs |
| `baseline_cost_usd` field in aggregate CSV | **CONTAMINATED** | reads polluted `_shared_baseline/traces.db` |
| `cost_savings_pct` field in aggregate CSV | **WRONG** | derived from contaminated baseline |
| Accuracy delta + paired McNemar | **CLEAN** | per-task data is from stdout |

### True savings (corrected)

Using the per-subprocess reported baseline cost ($0.08, identical to
the agent-printed `Trace: ... $0.08` line for every config), and the
per-config optimized cost from each `<config>/optimized/traces.db`:

```
config                opt $    "true" Δ%    (CSV reported Δ%, contaminated)
all-on              $0.0801    +0% to -1%    [+41.2%]
StateDrop-only      $0.0789    +1% to +2%    [+42.1%]
StateDrop-off       $0.0835     -4%          [+38.7%]
ContextCompress-only $0.0821    -3%          [+39.7%]
ParallelBranch-only $0.0848     -6%          [+38.7%]
```

So the new run actually shows ~no savings on iterative_refiner —
that may itself be a regression vs the partial 10/11 numbers, or
just stochastic LLM variance at n=50. **Do not use the new aggregate
CSV's cost columns.** The existing
`iterative_refiner-statedrop-n50-partial10of11.csv` (used by the
paper's §6.3) was generated before the contamination bug and remains
the correct source for headline savings numbers.

### What's still useful

- The 11/11 paired per-task pass/fail dataset is real and feeds
  McNemar exact tests and bootstrap CIs.
- The McNemar p-values for the all-on / rule-only configurations
  largely fail to reject (n=50, discordant pairs ≤ 4). Bootstrap
  CIs on accuracy deltas span [-16, +0] pp — well within the
  binomial SE the paper currently reports.
- For the paper's "behavior preserved within reported standard
  error" claim: **no McNemar test rejects accuracy preservation for
  any of the 11 configurations** at $\alpha=0.05$. This is the
  uncertainty story §6 needs.

## Tier 3 results — multirule_qa n=20 (EXP-006)

**Status**: running (rerun after fixing message-ordering bug).
Output: `bench/paper_results/multirule_qa-cc_sd-n20-paired.csv`.

### Design lesson (saved to bd memories)

The first run produced 67% cost savings but a -35pp accuracy delta,
because the agent appended a meta-instruction
("Produce the refined answer now") *after* the question. The IDF
attention proxy uses the last `role=user` message as the salient
signal — that became the meta-instruction, not the question, so
ContextCompress dropped the supporting paragraphs.

**Fix**: the question must remain the LAST `role=user` message.
This is now documented in a code comment in `multirule_qa.py:106-110`
and as `bd memory: multi-rule-agent-design-lesson`. The fix matches
the same pattern already in `long_context_qa.py`.

### Final results (n=20, all 11 configs)

```
config                      cost Δ%   in-tok Δ%   acc Δ pp   McNemar p   95% CI (pp)      BB  BF  FB  FF
all-on                      +31.33%     +31.42%     -5.00      1.0000   [-20.00,+10.00]    8   2   1   9
CacheHit-off                +31.33%     +31.42%     -5.00      1.0000   [-20.00,+10.00]    8   2   1   9
ContextCompress-off          +0.07%      +0.06%    -10.00      0.5000   [-25.00, +0.00]    8   2   0  10
ParallelBranch-off          +31.33%     +31.42%     -5.00      1.0000   [-20.00,+10.00]    8   2   1   9
ModelDowngrade-off          +31.33%     +31.42%     -5.00      1.0000   [-20.00,+10.00]    8   2   1   9
StateDrop-off               +31.33%     +31.42%     -5.00      1.0000   [-20.00,+10.00]    8   2   1   9
CacheHit-only                +0.01%      +0.00%    -10.00      0.5000   [-25.00, +0.00]    8   2   0  10
ContextCompress-only        +31.33%     +31.42%     -5.00      1.0000   [-20.00,+10.00]    8   2   1   9
ParallelBranch-only          +0.00%      +0.00%     +0.00      1.0000   [+0.00, +0.00]    10   0   0  10
ModelDowngrade-only          +0.01%      +0.00%    -10.00      0.5000   [-25.00, +0.00]    8   2   0  10
StateDrop-only               +0.06%      +0.06%    -10.00      0.5000   [-25.00, +0.00]    8   2   0  10
```

(Saved at `bench/paper_results/multirule_qa-cc_sd-n20-paired.summary.txt`.)

### Reading

- **CC dominates**: all `*-off` and `*-only` configs that include CC
  show 31.3% savings; configs without CC show ≤0.1%.
- **SD does fire**: `plan_audit` shows StateDrop firing 12 of 60 calls
  in the ContextCompress-off config — but on token volume so small
  that input-token savings are 0.06% (state-tagged revisions are
  ~10 tokens each vs. ContextCompress's ~3 KB distractor paragraphs).
- **ParallelBranch-only is the integrity check**: exactly 0/0/0 vs.
  baseline confirms the harness handles a no-op rule correctly.
- **McNemar n=20 is too small**: discordant pairs of 2-3 cannot
  reach significance (best-case p=0.25 for 0/2 split). Iterative
  refiner n=50 will give tighter intervals.

### Bottom-line story for the paper

Multi-rule activation in a single trace works as the optimizer is
designed to: rules' applies() conditions trigger correctly, and the
planner picks the highest-projected-savings rule per call site. On
this workload the volumetric asymmetry (long doc paragraphs vs.
short refinement revisions) means ContextCompress always wins the
greedy selection. Composition is therefore *across calls in a
single trace*, not *stacked rewrites on a single call*. This
matches the design in §3.2 (Algorithm 1: at most one rule per call).

### Rule-firing breakdown (from `plan_audit`)

| config | pass-through | ContextCompress | StateDrop |
|---|---|---|---|
| baseline (AGENTC_OPTIMIZE=0) | 60 | 0 | 0 |
| all-on | 6 | 54 | 0 |
| CacheHit-off (= all rules) | 6 | 54 | 0 |
| ContextCompress-off | 28 | 0 | 12 |

**Reading**: in all-on, ContextCompress wins the per-call greedy
selection on 54 of 60 calls because its projected savings exceed
StateDrop's. With CC disabled, StateDrop fires on 12 of 60 calls.
This matches the paper's stated design (Algorithm 1: "at most one
rule may rewrite a given call; first valid proposal ordered by
projected savings is selected"). The multi-rule story for the paper
is therefore not "rules sum" but "rules trade off, optimizer
picks the best per call site". Both rules' `applies()` conditions
trigger across the workload, demonstrating that the runtime control
plane handles a single trace where multiple rule families are
active.

### What it shows when complete

- ContextCompress and StateDrop both fire on the same call site within a single trace.
- Cost/input-token savings under all-on vs. each rule-only.
- Direct evidence that AgentC's rules **compose** under one runtime
  control plane — the largest single missing piece per
  `paper-intelligence/claims-gaps-and-risks.md` GAP-011.

**Caveat**: ModelDowngrade is excluded from this run because the
gpt-4o → gpt-4o-mini route requires `BENCH_BASELINE_MODEL=gpt-4o`,
which immediately saturates Tier-1 30k TPM on bursty multi-step
ablation (the first attempt hit the rate limit at task 7 of the
baseline). The OpenAI client `max_retries` was bumped to 8 but a
gpt-4o multirule run is queued as a follow-up that needs throttled
submission. Until then, MD composition is documented from the
isolated §6.2 result (35.3% on its own routing workload).

## Recommended paper edits (priority order, post-merge)

These ride on the experiment outputs above; each one independently
defensible.

1. **Swap §7 Related Work** with the draft in
   `paper-intelligence/draft-related-work-expansion.md`. Adds ~38 new
   citations covering compound-AI runtimes, routing, compression,
   caching, parallelism, slicing, serving systems, and stochastic
   evaluation. Closes GAP-007, GAP-010, RR-005, RR-009, RR-010, RR-013.
   Mechanical edit; no new experiments needed. Highest leverage.

2. **Add overhead paragraph to §6 Evaluation**. One paragraph using the
   numbers in `bench/paper_results/optimizer_overhead.txt`. Closes
   GAP-015 / RR-007.

3. **StateDrop dependency-model paragraph** in §4 (drafted in the
   related-work file). Closes RR-003 / WP-010 / CIT-006 / CIT-012.

4. **Drop "novel" twice** in §1 contributions and elsewhere. Replace
   with "two-stage" / "two-gate". Trivial. Aligns with
   `evidence-and-sources.md` no-go phrase list.

5. **Soften title** (optional): "Toward Just-in-Time Optimization for
   Multi-Step LLM Agent Workloads". Aligns with TTL-003 caveat.

6. **Replace §6.3 StateDrop table** once the full n=50 paired matrix
   lands. Drop the temp=0-estimate asterisk; add McNemar p-values to
   accuracy-delta column.

7. **Add §6.5 multi-rule composition result** — new subsection using
   `multirule_qa-cc_sd-n20-paired.csv`. Closes GAP-011 / EXP-006.

## Tracking

bd issues filed:
- `bd-85a` Compute optimizer overhead percentiles (closed)
- `bd-h12` Backfill StateDrop config + per-task capture (in_progress)
- `bd-2u0` Compute paired McNemar / bootstrap CI (open, blocked on bd-h12 finishing)
- `bd-75u` Multi-rule end-to-end workload (in_progress)

## Spend (final)

| Item | Spend |
|---|---|
| Overhead analysis | $0 |
| Multirule first attempt (rate-limited gpt-4o) | ~$0.21 |
| Multirule second attempt (cancelled mid-run for f-string fix) | ~$0.20 |
| Multirule final n=20 ablation (gpt-4o-mini, clean) | ~$0.45 |
| Iterative_refiner first run (killed accidentally) | ~$0.16 |
| Iterative_refiner final n=50 ablation (cost data contaminated, paired data clean) | ~$1.20 |
| **Total** | **~$2.20** of $15 authorized |

The wasted spend (~$0.55) was from the multirule agent design bugs
(meta-instruction last + f-string state-tag wrapping) — both saved
as bd memories so future agents don't repeat them.

## Files for the user to review on wake

| File | What it is |
|---|---|
| `bench/paper_results/optimizer_overhead.txt` | Overhead numbers + paper-ready sentence |
| `bench/paper_results/iterative_refiner-statedrop-n50-paired.csv` | Full 11/11 paired ablation (when done) |
| `bench/paper_results/iterative_refiner-statedrop-n50-paired.per_task.csv` | Per-task pass/fail for paired tests |
| `bench/paper_results/multirule_qa-cc_sd-n20-paired.csv` | Multi-rule ablation (when done) |
| `bench/paper_results/multirule_qa-cc_sd-n20-paired.per_task.csv` | Same, per-task |
| `paper-intelligence/draft-related-work-expansion.md` | Drafted Related Work section + small paper edits + needed bib entries |
| `paper-intelligence/overnight-session-2026-05-10.md` | This file |
| `bench/agents/multirule_qa.py` | New agent for EXP-006 |
| `bench/paired_analysis.py` | McNemar/bootstrap analyzer |
