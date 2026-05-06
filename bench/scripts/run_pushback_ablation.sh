#!/usr/bin/env bash
# Reviewer-pushback experiments — second round.
# Self-caffeinates (re-execs under caffeinate -is if not already).
#
# Targets the three workshop-paper push-backs:
#
#   1. StateDrop n bigger     — iterative_refiner at n=50 (was n=30; synthetic fixture caps at 50)
#                               Tightens accuracy SE from ±9pp to ±6.5pp.
#
#   2. ContextCompress real   — hotpot_qa on the public HotpotQA-distractor
#                               fixture extended to n=300. Real questions,
#                               gold answers, public benchmark — counter to
#                               "your fixture is synthetic" review.
#
#   3. Oracle baseline        — hotpot_oracle drops the supporting=false
#                               paragraphs upfront (using gold labels). Gives
#                               the upper-bound manual-compression number;
#                               ContextCompress's job is to come close to it
#                               without seeing the labels.
#
# Usage: bash bench/scripts/run_pushback_ablation.sh
set -uo pipefail

cd "$(dirname "$0")/../.."

if ! pgrep -f "caffeinate -is" > /dev/null 2>&1; then
    exec caffeinate -is bash "$0" "$@"
fi

if [[ -f .env ]]; then set -a; . ./.env; set +a; fi
export PYTHONPATH=python

unset BENCH_BASELINE_MODEL

PY=".venv/bin/python"
RESULTS=bench/results
mkdir -p "$RESULTS"
LOG="$RESULTS/pushback-$(date +%Y%m%d-%H%M%S).log"
HARD_STOP_USD=25

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }

cost_so_far() {
    awk -F, 'FNR>1 && $7+0 < 1 {sum+=$7} END {printf "%.4f", sum+0}' \
        "$RESULTS"/pushback-*.csv 2>/dev/null || echo "0"
}

run_ablation() {
    local agent="$1"
    local label="$2"
    local max_tasks="$3"
    local out="$RESULTS/pushback-${label}.csv"

    if [[ -f "$out" ]] && [[ $(wc -l < "$out") -gt 1 ]]; then
        log "skip $label (results at $out — delete to re-run)"
        return 0
    fi

    log "=== EXPERIMENT: $label  (agent=$agent, n=$max_tasks) ==="
    BENCH_MAX_TASKS="$max_tasks" "$PY" -m bench.optimizer_ablation \
        "bench.agents.$agent" \
        --storage-root "/tmp/agentc-pushback-$label" \
        --out "$out" 2>&1 | tee -a "$LOG"
    log "$label done | cumulative \$$(cost_so_far)"

    local total
    total=$(cost_so_far)
    if awk -v t="$total" -v cap="$HARD_STOP_USD" 'BEGIN {exit !(t+0 > cap+0)}'; then
        log "ABORT: \$$total exceeds hard stop \$$HARD_STOP_USD"
        exit 1
    fi
}

run_oracle() {
    # Oracle agent doesn't need the rule sweep — it manually drops
    # distractor paragraphs upfront, so the rules have nothing to add.
    # We just want one cost+accuracy number to compare against.
    local max_tasks="$1"
    local out="$RESULTS/pushback-hotpot-oracle.csv"

    if [[ -f "$out" ]] && [[ $(wc -l < "$out") -gt 1 ]]; then
        log "skip oracle (results at $out — delete to re-run)"
        return 0
    fi

    log "=== EXPERIMENT: hotpot-oracle (manual-compression baseline, n=$max_tasks) ==="
    BENCH_MAX_TASKS="$max_tasks" "$PY" -m bench.run_oracle_baseline \
        bench.agents.hotpot_oracle \
        --storage-root "/tmp/agentc-pushback-oracle" \
        --out "$out" 2>&1 | tee -a "$LOG"
    log "oracle done | cumulative \$$(cost_so_far)"
}

# Order: shortest first so the earlier results are durable if a later
# one explodes.
log "pushback ablation starting (hard stop \$$HARD_STOP_USD)"
log "log: $LOG"

run_oracle 300
run_ablation "hotpot_qa"          "hotpot-real-n300"   300
run_ablation "iterative_refiner"  "refiner-n50"        50

log "FINAL spend: \$$(cost_so_far)"
log "pushback ablation complete"
