# Retracted / Superseded Evidence

These files are **preserved, never deleted** — they document corrections and are required
for traceability. They are quarantined here so they cannot be confused with canonical data
or cited by the manuscript. Nothing in `main.tex`, the repro scripts, or `DATA_MANIFEST.txt`
may reference a file under `retracted/`.

| File | Superseded by | Why retracted |
|---|---|---|
| `md_cc_composed.csv` | `md_cc_orthogonality_warmup.csv` | Non-warmup (cold-start) run. Its accuracy block was mistakenly used in `tab:mdcc-orthogonality`; the warmup run disagrees on every value (MNT-041). |
| `unified_agent_summary.csv` | per-agent `*_warmup_*` CSVs | V1 non-warmup artifact; still publishes the retracted SD input-token = 9.6% (canonical 10.8%). Backs the cold-start provider-generalization MD numbers (MNT-053). |
| `new_agents_ablation.csv` | (needs warmup re-run) | Non-warmup; backs `debug_agent`. Cold-start (MNT-053/017). |
| `planner_ablation_contaminated_original.csv` | `planner_ablation.csv` / `planner_ablation_rerun.csv` | State leaked between configs (the original contamination). Superseded by the clean rerun (MNT-029). |
| `planner_ablation.summary.txt` | (summary of the clean `planner_ablation.csv`) | Carries the contaminated table and the retracted "V1 greedily mis-picks" prose that the clean rerun disproved (MNT-029). |
| `iterative_refiner-statedrop-n50-partial10of11.csv` | `iterative_refiner-statedrop-n50-warmup.csv` | Partial run (10 of 11 configs). Superseded (MNT-053). |

Root cause these fix: retracted CSVs previously sat under names indistinguishable from
canonical data, which is how a disavowed accuracy block reached a headline table (MNT-041).
