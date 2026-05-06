#!/usr/bin/env bash
# Per-rule purpose-built ablation sweep — paper-quality results.
# Self-caffeinates: re-execs under caffeinate -is if not already.
#
# Validates two of the four runtime-rewriter rules on workloads built
# specifically for them. ModelDowngrade was already validated by
# bench/run_targeted_ablation.sh on gaia_router.
#
#   Experiment 1 — long_context_qa (n=100, ContextCompress)
#       Workload: HotpotQA-distractor extended to 20 paragraphs/task
#       (~13-18 KB prompts, well above the 8 KB activation gate).
#
#   Experiment 2 — iterative_refiner (n=30, StateDrop)
#       Workload: 10-step refinement chain. Each step's call sees a
#       growing message list of state-tagged prior revisions; only the
#       latest is in the read-window, so older state-tagged messages
#       become drop-eligible.
#
# CacheHit is descoped: the rule only fires when the cache is
# pre-populated (typically by @memoize). ParallelBranch is descoped:
# the parallelism is already provided by the user-side parallel_map
# helper; the rule is observability, not cost.
#
# Usage: bash bench/scripts/run_paper_ablation.sh
set -uo pipefail

cd "$(dirname "$0")/../.."

if ! pgrep -x caffeinate > /dev/null 2>&1; then
    exec caffeinate -is bash "$0" "$@"
fi

if [[ -f .env ]]; then set -a; . ./.env; set +a; fi
export PYTHONPATH=python

# ContextCompress and StateDrop fire on prompt structure, not cost
# routing — leave the agents on their default model (gpt-4o-mini).
unset BENCH_BASELINE_MODEL

PY=".venv/bin/python"
RESULTS=bench/results
mkdir -p "$RESULTS"
LOG="$RESULTS/paper-$(date +%Y%m%d-%H%M%S).log"
HARD_STOP_USD=15

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }

cost_so_far() {
    awk -F, 'FNR>1 && $7+0 < 1 {sum+=$7} END {printf "%.4f", sum+0}' \
        "$RESULTS"/paper-*.csv 2>/dev/null || echo "0"
}

run_experiment() {
    local agent="$1"
    local rule="$2"
    local max_tasks="$3"
    local out="$RESULTS/paper-${agent}.csv"

    if [[ -f "$out" ]] && [[ $(wc -l < "$out") -gt 1 ]]; then
        log "skip $agent (results at $out — delete to re-run)"
        return 0
    fi

    log "=== EXPERIMENT: $agent (target rule: $rule, n=$max_tasks) ==="
    BENCH_MAX_TASKS="$max_tasks" "$PY" -m bench.optimizer_ablation \
        "bench.agents.$agent" \
        --storage-root "/tmp/agentc-paper-$agent" \
        --out "$out" 2>&1 | tee -a "$LOG"
    log "$agent done | cumulative \$$(cost_so_far)"

    local total
    total=$(cost_so_far)
    if awk -v t="$total" -v cap="$HARD_STOP_USD" 'BEGIN {exit !(t+0 > cap+0)}'; then
        log "ABORT: \$$total exceeds hard stop \$$HARD_STOP_USD"
        exit 1
    fi
}

# Build long-context fixture if missing.
if [[ ! -f "bench/fixtures/long_context_qa.json" ]]; then
    log "building long_context_qa fixture..."
    "$PY" -m bench.build_long_context_fixture --total 100 --extras 10 2>&1 | tee -a "$LOG"
fi

# Order: cheaper / faster first so failures surface early.
run_experiment "long_context_qa"    "ContextCompress"  100
run_experiment "iterative_refiner"  "StateDrop"        30

log "FINAL spend: \$$(cost_so_far)"
log "paper ablation complete"
