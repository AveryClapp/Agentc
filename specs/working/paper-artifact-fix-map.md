---
title: Paper artifact fix map (repro appendix + DATA_MANIFEST + canonical runs)
status: active
last-updated: 2026-07-15
---

# Paper artifact fix map

Detective work by three read-only agents (2026-07-15), matching the paper's
PRINTED numbers to CSV contents. The stale-named files in the paper NEVER
existed (git: 0 commits) — the paper used a `<experiment>_n<size>` convention
the saved files don't follow, so every mapping below is by CONTENT/NUMBER match.

## tab:repro (main.tex ~L1962-2035) — corrections (VERIFIED by number match)

| Row (\ref) | Wrong in paper | Correct | Evidence |
|---|---|---|---|
| llmlingua (L1994-97) | `llmlingua_comparison_n100.csv`, `wikipedia_qa_n39.csv`, `python -m bench.llmlingua_baseline` | `llmlingua_accuracy_n100.csv`, `wikipedia_qa_comparison.csv`, `python -m bench.agents.llmlingua_baseline` | distractor 68.0/53.0/Δ-15/p=0.0013/tok53.1% match llmlingua_accuracy_n100; wiki 94.9/94.9/97.4/tok53.5% match wikipedia_qa_comparison |
| cold agent (L2015-17) | `support_qa_n30.csv`, `python -m bench.coldagent_eval` | `generalization_evals.csv` (support_qa row) + `generalization_activation.csv`, `python -m bench.run_generalization_evals` | support_qa row: 0.0pp, p=1.0, ~0% cost/tok; OutputBudget 72/78, CC abstains (matches §6.5) |
| cachehit (L2020-24) | `batch_classifier_n20.csv`, `python -m bench.cachehit_eval --run 1 && agentc shutdown` | run `python -m bench.agents.batch_classifier` twice; results from `traces.db` plan_audit (plan_kind='cached'); NO CSV; remove `agentc shutdown` (not a CLI subcommand) and the `--run` flags | batch_classifier.py IS the eval (2×10=20 tasks); seeding is the in-script agentc.shutdown() call |
| autogen (L2010-12 vs L2032-34) | TWO conflicting entries: n200 (L2010-12) and n300 (L2032-34) | keep n300 only; drop the n200 entry | n300 matches tab:summary on every field (14.01% composed, 350/184/260 fires, p=0.55/0.29); n200 matches nothing |

`agentc shutdown` is confirmed NOT a CLI subcommand (main.rs Commands enum: record/traces, Embed, Migrate, Export, Cache, Optimize). The paper's prose correctly uses the Python `agentc.shutdown()` call.

Cosmetic (optional): rows 1-3,7-9 cite hyphenated "canonical" CSV renames while the scripts emit underscore "source" names — harmless, could document the rename step.

## DATA_MANIFEST.txt — corrections (VERIFIED)

- L19 & fig3 note L93: MD-only savings `11.2%` → **`11.4%`**. Units bug: the manifest printed the absolute 11.23 mUSD as a percent; real = 11.2326/98.9100 = 11.36% → 11.4%. Paper's 11.4% is correct. (VERIFIED vs gaia_router_warmup_n127.csv.)
- Autogen (L50-56): declares canonical = n200 "tok saved 38.5%" — STALE. Canonical is n300; the paper reports no 38.5% number. Update to n300.
- (2 dead script paths already fixed this session: run_gaia_warmup.py, run_refiner_warmup.py.)
- Full 18-table regeneration: pending agent 2's complete table→CSV→script map (guard/cross-model/overhead sections currently missing).

## GENUINE author decisions (data can't resolve — need William)

1. **autogen CC-only 23.5%** (tab:summary L828, prose L1452/L1826): matches NEITHER n300 (aggregate 26.0%) nor n200 (38.02%). Every OTHER field of that row matches n300 exactly (350 fires, -1.3pp, p=0.29, BF=6/FB=2). Likely a per-call mean-of-ratios whose source data wasn't committed, OR it should read **26.0%** to match the canonical CSV. → decide: is 23.5% a real (uncommitted) per-call figure, or a typo for 26.0%?
2. **cold-agent n**: paper prose says **n=30**; the real run/CSV is **n=39**. → reconcile text to n=39, or confirm a separate n=30 run.
3. **llmlingua accuracy CSVs have no committed regenerator**: llmlingua_accuracy_n100.csv + wikipedia_qa_comparison.csv were committed without a generator script (bench.agents.llmlingua_baseline only writes the raw compression CSV). → restore the accuracy/McNemar generator, or mark these as archived artifacts in tab:repro.

## DATA_MANIFEST 18-table regeneration map (agent 2 — VERIFIED unless noted)

Manifest currently covers only Tables 5/6/7/13/14 + mdcc + figs 3/6. MISSING (add these):
- **tab:guard** (accuracy guard): lcqa_cc_guard.csv, analyst_qa_sd_gen.csv, gsweep_tradeoff_{lexical,normalized,off}_{rp,an}_*.csv; `run_guard_eval.py` via repro/guard_frontier.sh.
- **tab:xmodel** (cross-model): gsweep_{xmodel,claude,qwen3}_{lexical,normalized}_{rp,an}_*.csv (+ gpt row reuses gsweep_tradeoff_*); via repro/crossmodel_selectivity.sh.
- **fig:metric-tradeoff (fig9)**: gsweep_tradeoff_* ; fig9_metric_tradeoff.py.
- **overhead (76µs/120µs) + fig:overhead(fig7) + fig:throughput(fig8)**: optimizer_overhead.txt, overhead_scaling.csv, concurrency_bench_summary.csv; overhead.py / run_concurrency_bench.py. The historical 18µs guard figure is invalid (replayed-token legacy fast path); the corrected guard harness is Stage E0 only and must not supply a paper claim.
- **tab:oracle**: hotpot_oracle-n300.csv + hotpot_real-contextcompress-n300-warmup.csv; run_oracle_baseline.py + run_hotpot_warmup_n300.py.
- **tab:hotpot-matrix**: hotpot_real-contextcompress-n300-warmup.csv; run_hotpot_warmup_n300.py.
- **tab:llmlingua-***: llmlingua_accuracy_n100.csv, llmlingua_comparison.csv, wikipedia_qa_comparison.csv; bench.agents.llmlingua_baseline.

## ADDITIONAL author decisions surfaced by agent 2

4. **research_planner accuracy (this is bd-dka)**: research_planner_warmup_n150.csv reports **+9.0pp, p=0.0117**; the paper says **+4.0pp, ns, p=0.22**. Cost 41.7%/tok 37.7% DO match the CSV. → which accuracy is canonical (+4 or +9)?
5. **debug_agent row (tab:summary, 8.3%/16.8%)**: only backing is retracted/new_agents_ablation.csv (cold-start, needs warmup re-run per MNT-053/017). No committed warm aggregate. → re-run or drop the row.
6. **fig:provider (fig4)**: the n=50 OpenAI/HF/Anthropic numbers (CC 34/34, MD 31.1/14.7) are HARDCODED in fig4_provider_generalization.py with NO backing CSV in paper_results. The one figure whose data can't be traced to a file. → commit the data or add an explicit "hardcoded from run logs" note.
7. **tab:mdcc-orthogonality accuracy block is DISAVOWED** (retracted/REASON.md, MNT-041): the paper table's accuracy column (55/60/50/60) came from the retracted md_cc_composed.csv; the canonical md_cc_orthogonality_warmup.csv gives 80/70/70/60. Only the 95.2% efficiency ties out. → correct the accuracy column or caveat it.
8. **tab:llmlingua-distractor Agentc-CC 100% row**: no committed CSV (only baseline + LLMLingua rows are in llmlingua_accuracy_n100.csv). → commit or note.

## Applied this session (verified-factual)
tab:repro: llmlingua row (CSVs+module), cold-agent row (script+CSV), cachehit row (traces.db + removed the bogus `agentc shutdown`), autogen (dropped stale n200 entry; n300 entry is canonical). DATA_MANIFEST: MD 11.2%->11.4% (L19+L93, units bug); 2 dead script paths (earlier). The 8 decisions above and the full manifest regeneration remain for William.

## Second (forensic) pass — recovery results (git-history traced)

- **llmlingua Agentc-CC 100% row — RECOVERED (canonical, not retracted):** `bench/paper_results/agentc_hotpot_n100.csv` (commit 7a26eb9) backs it exactly — baseline 68/100, AgentcV2-CC 100/100, +32pp, BF=0/FB=32; paper's exact McNemar p=4.66e-10 = 2*(0.5)^32. First pass missed it because it's a *separately named* file (not inside llmlingua_accuracy_n100.csv). APPLIED: tab:repro llmlingua row now also cites agentc_hotpot_n100.csv. Resolves decision #7 (add to DATA_MANIFEST too). (Related bead bd-vg7.)
- **fig4 provider HF/Anthropic — RECOVERED but RETRACTED:** `retracted/unified_agent_summary.csv` (commit 3280917) backs CC HF 34.0, MD HF 31.1, MD Anthropic 14.7, CC Anthropic abstain — but it is COLD-START (n=50, non-warmup) and quarantined; the repo's own rule bars figures from citing retracted/. So the numbers exist but from disavowed data. Decision #4 stands: re-run warm or explicitly label cold-start. (Beads bd-ude, bd-6xrq.)
- **autogen 23.5% — NOT recoverable:** it is a per-call mean-of-ratios; per-call token data was ephemeral in traces.db (reset between phases) and never committed. Committed aggregate is 26.0% (ratio-of-sums). Decision #2 stands (bd-hig3): keep 23.5% with a "per-call mean" note, or change to 26.0%.
- **debug_agent 8.3%/16.8% — NOT recoverable:** only backing is retracted/new_agents_ablation.csv, byte-identical across all 4 committed versions (never re-run warm). ALSO the value is the *all-on* config but the paper labels the row "StateDrop" — a second error. Decision #3 stands (bd-ec0e): re-run warm or drop.
