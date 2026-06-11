#!/usr/bin/env bash
# Reproduces one row of Table tab:xmodel (4-family cross-model selectivity).
# Runs the decisive 6 cells for ONE model: the unguarded baseline plus the
# lexical and normalized metrics at the model's operating budget TAU, on both
# the benign (research_planner/ContextCompress) and catastrophic
# (analyst_qa/StateDrop) agents, n=100.
#
# Provider routing uses the existing OpenAI-compatible plumbing. Set MODEL,
# BASE (base_url) and the matching key, then run. The exact per-family
# invocations used in the paper are documented in bench/repro/README.md.
#
#   MODEL  e.g. meta-llama/Llama-3.3-70B-Instruct-Turbo
#   BASE   e.g. https://api.together.xyz/v1   (key: TOGETHER_API_KEY)
#                https://api.anthropic.com/v1/ (key: ANTHROPIC_API_KEY, claude-haiku-4-5)
#   TAU    operating budget (0.20 for instruct models; 0.10 for Qwen3-235B)
#   PREFIX result-file tag prefix (e.g. xmodel, claude, qwen3)
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
PY=.venv/bin/python3
export PYTHONPATH="$PWD/python"

MODEL="${MODEL:?set MODEL (see header)}"
BASE="${BASE:?set BASE base_url (see header)}"
TAU="${TAU:-0.20}"
PREFIX="${PREFIX:-xmodel}"
export BENCH_OPENAI_BASE_URL="$BASE" BENCH_BASELINE_MODEL="$MODEL"
export GE_W="0" GE_N="${GE_N:-100}"
# The Anthropic OpenAI-compat endpoint authenticates via OPENAI_API_KEY:
case "$BASE" in *anthropic*) export OPENAI_API_KEY="${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY}";; esac

run_cell () {  # mode agent tau shadow
  local mode="$1" ag="$2" t="$3" sh="$4"
  export AGENTC_OPTIMIZE_SHADOW="$sh" AGENTC_SHADOW_DIVERGENCE_MODE="$mode"
  if [ "$t" = "off" ]; then
    unset AGENTC_SHADOW_DIVERGENCE_BUDGET; export GE_TAG="gsweep_${PREFIX}_off_${ag}"
  else
    export AGENTC_SHADOW_DIVERGENCE_BUDGET="$t"; export GE_TAG="gsweep_${PREFIX}_${mode}_${ag}_${t}"
  fi
  if [ "$ag" = "rp" ]; then
    export GE_AGENT="bench.agents.research_planner" GE_CONFIGS="ContextCompress-only"
    export GE_FIXTURE="$PWD/bench/fixtures/long_context_qa_n300.json"
  else
    export GE_AGENT="bench.agents.analyst_qa" GE_CONFIGS="StateDrop-only"; unset GE_FIXTURE
  fi
  echo "=== $(date +%H:%M:%S)  $GE_TAG (model=$MODEL) ==="
  $PY -m bench.run_guard_eval || echo "  !! FAILED $GE_TAG"
}

for ag in an rp; do
  run_cell lexical    "$ag" off  0      # unguarded baseline
  run_cell lexical    "$ag" "$TAU" 1
  run_cell normalized "$ag" "$TAU" 1
done
echo "=== cross-model selectivity ($PREFIX @tau=$TAU) complete ==="
