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

## Concerns for the author

1. **Metric-selectivity claim is threshold-sensitive and undertested.** The paper's
   `tab:guard` presents lexical-over-conservative-vs-normalized-keeps as a property
   of the operating point. The data shows it is only true in a narrow, noisy budget
   band (tau~=0.05 at n=50), not at tau=0.15. To be load-bearing, the metric ablation
   needs higher n to stabilize disable timing, at a stated threshold.

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
