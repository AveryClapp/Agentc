# Reproduction: accuracy guard and cross-model selectivity

Committed drivers for the shadow-divergence accuracy-guard results. Each writes
`gsweep_*.csv` summary files into `bench/paper_results/` (plus `*.per_task.csv`
pass/fail vectors). See the top-level `Reproducibility` section / `tab:repro` for
the rule-validation tables; this directory covers the guard and cross-model results.

## Prerequisites

- Python env: `.venv` with the project installed (`PYTHONPATH=python`).
- `OPENAI_API_KEY` for the gpt-4o-mini runs.
- For cross-model runs, an OpenAI-compatible endpoint + key (see below). No code
  changes are needed: routing is via `BENCH_OPENAI_BASE_URL` + `BENCH_BASELINE_MODEL`.

## Script -> paper artifact map

| Script | Reproduces | Notes |
|---|---|---|
| `guard_frontier.sh` | `tab:guard`, `fig:metric-tradeoff` | gpt-4o-mini, n=200, budget frontier tau in {0.10,0.20,0.30,0.50} |
| `crossmodel_selectivity.sh` | `tab:xmodel` (one row) | n=100; run once per model family (below) |
| `../guard_overhead_bench.py` | guard overhead (18 us/sample) | `python -m bench.guard_overhead_bench`; CPU only, no API |
| `../paper_figures/fig9_metric_tradeoff.py` | renders `fig:metric-tradeoff` | reads the `gsweep_tradeoff_*` CSVs |

## Reproducing `tab:guard` + `fig:metric-tradeoff`

```bash
OPENAI_API_KEY=... bash bench/repro/guard_frontier.sh
python bench/paper_figures/fig9_metric_tradeoff.py
```

## Reproducing `tab:xmodel` (4 families)

Each family is one invocation of `crossmodel_selectivity.sh`. The operating budget
`TAU` is a per-model hyperparameter (the verbose Qwen3 regime calibrates tighter):

```bash
# gpt-4o-mini (OpenAI), tau=0.20  -- default endpoint
MODEL=gpt-4o-mini-2024-07-18 BASE=https://api.openai.com/v1 TAU=0.20 PREFIX=gpt \
  OPENAI_API_KEY=... bash bench/repro/crossmodel_selectivity.sh

# Llama-3.3-70B (Meta) via Together, tau=0.20
MODEL=meta-llama/Llama-3.3-70B-Instruct-Turbo BASE=https://api.together.xyz/v1 \
  TAU=0.20 PREFIX=xmodel TOGETHER_API_KEY=... bash bench/repro/crossmodel_selectivity.sh

# Claude Haiku 4.5 (Anthropic) via the OpenAI-compat endpoint, tau=0.20
MODEL=claude-haiku-4-5 BASE=https://api.anthropic.com/v1/ \
  TAU=0.20 PREFIX=claude ANTHROPIC_API_KEY=... bash bench/repro/crossmodel_selectivity.sh

# Qwen3-235B (Alibaba) via Together, tau=0.10 (its operating point)
MODEL=Qwen/Qwen3-235B-A22B-Instruct-2507-tput BASE=https://api.together.xyz/v1 \
  TAU=0.10 PREFIX=qwen3 TOGETHER_API_KEY=... bash bench/repro/crossmodel_selectivity.sh
```

## Notes

- `AGENTC_OPTIMIZE_SHADOW=1` (full sampling) is set so the guard's auto-disable
  triggers within a single run; production default is 0.02. Raw `cost_savings_pct`
  is therefore not directly comparable across cells -- use the behavioral columns
  (`cc_fire_count`, `guard_disable_count`, `input_token_savings_pct`, `acc_delta_pp`),
  which are sampling-rate-independent.
- Temperature-1 stochasticity gives ~+/-2-3 pp accuracy noise at these n; the
  decision (rule kept vs disabled) is robust, recovery completeness is not.
- Qwen3-235B served via Together throughput tier can occasionally stall a request;
  re-run the affected single cell if a run hangs with no progress.
