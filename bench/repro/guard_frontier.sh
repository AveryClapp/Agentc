#!/usr/bin/env bash
# Reproduces the accuracy-guard results on gpt-4o-mini:
#   - Table tab:guard         (operating point tau=0.20)
#   - Figure fig:metric-tradeoff  (the lexical-vs-normalized budget frontier)
#
# Two agents: research_planner / ContextCompress (benign, long-context) and
# analyst_qa / StateDrop (catastrophic). For each, runs the unguarded baseline
# (shadow off) plus the lexical and normalized divergence metrics across the
# budget frontier tau in {0.10,0.20,0.30,0.50}, n=200, full shadow sampling
# (AGENTC_OPTIMIZE_SHADOW=1) so disables trigger within the short run. These
# historical cells characterize dense-feedback selectivity, not behavior or
# net cost at the runtime's 0.02 default.
#
# Requires OPENAI_API_KEY. Outputs gsweep_tradeoff_*.csv in bench/paper_results/.
# Build the figure afterwards with: python bench/paper_figures/fig9_metric_tradeoff.py
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
PY=.venv/bin/python3
export PYTHONPATH="$PWD/python"
export GE_W="0" GE_N="200"

run_cell () {  # mode agent tau shadow
  local mode="$1" ag="$2" t="$3" sh="$4"
  export AGENTC_OPTIMIZE_SHADOW="$sh" AGENTC_SHADOW_DIVERGENCE_MODE="$mode"
  if [ "$t" = "off" ]; then
    unset AGENTC_SHADOW_DIVERGENCE_BUDGET; export GE_TAG="gsweep_tradeoff_off_${ag}"
  else
    export AGENTC_SHADOW_DIVERGENCE_BUDGET="$t"; export GE_TAG="gsweep_tradeoff_${mode}_${ag}_${t}"
  fi
  if [ "$ag" = "rp" ]; then
    export GE_AGENT="bench.agents.research_planner" GE_CONFIGS="ContextCompress-only"
    export GE_FIXTURE="$PWD/bench/fixtures/long_context_qa_n300.json"
  else
    export GE_AGENT="bench.agents.analyst_qa" GE_CONFIGS="StateDrop-only"; unset GE_FIXTURE
  fi
  echo "=== $(date +%H:%M:%S)  $GE_TAG ==="
  $PY -m bench.run_guard_eval || echo "  !! FAILED $GE_TAG"
}

for ag in an rp; do
  run_cell lexical "$ag" off 0          # unguarded baseline (shadow off)
  for t in 0.10 0.20 0.30 0.50; do
    run_cell lexical    "$ag" "$t" 1
    run_cell normalized "$ag" "$t" 1
  done
done
echo "=== guard frontier complete; build fig with bench/paper_figures/fig9_metric_tradeoff.py ==="
