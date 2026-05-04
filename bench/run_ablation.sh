#!/usr/bin/env bash
# Run the per-agent ablation sweep end-to-end with budget guards.
# Usage:   caffeinate -is bash bench/run_ablation.sh
# Or:      nohup caffeinate -is bash bench/run_ablation.sh &
set -uo pipefail

cd "$(dirname "$0")/.."

# Setup
chflags nohidden .venv/lib/python3.12/site-packages/agentc.pth 2>/dev/null || true
if [[ -f .env ]]; then
    set -a; . ./.env; set +a
fi
export PYTHONPATH=python

# ModelDowngrade routes gpt-4o → gpt-4o-mini. The reference agents
# hardcode gpt-4o-mini as their default, which would leave the
# downgrade rule with nothing to do. Force the baseline to gpt-4o so
# the rule has a real ratio to optimize against; the optimized side
# still routes back to mini after the cost model warms.
export BENCH_BASELINE_MODEL="${BENCH_BASELINE_MODEL:-gpt-4o}"

PY=".venv/bin/python"
RESULTS=bench/results
mkdir -p "$RESULTS"
LOG="$RESULTS/ablation-$(date +%Y%m%d-%H%M%S).log"

# Hard stop — abort the whole sweep if combined spend exceeds this.
HARD_STOP_USD=28

# Cheapest → most expensive. Reorder if you want a different order.
AGENTS=(
    multiagent_research
    swebench_planner
    rag_summarizer
    gaia_router
)

cost_so_far() {
    # Sum optimized_cost_usd; skip values ≥ 1 (which are n_tasks from old-format CSVs).
    awk -F, 'FNR>1 && $7+0 < 1 {sum+=$7} END {printf "%.4f", sum+0}' \
        "$RESULTS"/ablation-*.csv 2>/dev/null || echo "0"
}

over_budget() {
    awk -v t="$1" -v cap="$HARD_STOP_USD" 'BEGIN {exit !(t+0 > cap+0)}'
}

log() {
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"
}

run_agent() {
    local agent="$1"
    local fixture="bench/fixtures/${agent}.json"
    local out="$RESULTS/ablation-${agent}.csv"

    if [[ ! -f "$fixture" ]]; then
        log "skip $agent (no fixture at $fixture)"
        return 0
    fi
    if [[ -f "$out" ]] && [[ $(wc -l < "$out") -gt 1 ]]; then
        log "skip $agent (csv already populated at $out — delete to re-run)"
        return 0
    fi

    log "=== START $agent ==="
    PYTHONPATH=python "$PY" -m bench.optimizer_ablation \
        "bench.agents.$agent" \
        --storage-root "/tmp/agentc-ablation" \
        --out "$out" 2>&1 | tee -a "$LOG"

    local total
    total=$(cost_so_far)
    log "=== DONE  $agent  |  cumulative \$$total ==="

    if over_budget "$total"; then
        log "ABORT: cumulative \$$total exceeds hard stop \$$HARD_STOP_USD"
        exit 1
    fi
}

log "ablation sweep starting (hard stop \$$HARD_STOP_USD)"
log "log: $LOG"

for agent in "${AGENTS[@]}"; do
    run_agent "$agent"
done

# Combine per-agent CSVs into one master file.
COMBINED="$RESULTS/ablation.csv"
first=$(ls "$RESULTS"/ablation-*.csv 2>/dev/null | head -1 || true)
if [[ -n "$first" ]]; then
    {
        head -1 "$first"
        for f in "$RESULTS"/ablation-*.csv; do
            [[ "$f" == "$COMBINED" ]] && continue
            tail -n +2 "$f"
        done
    } > "$COMBINED"
    log "combined → $COMBINED ($(($(wc -l < "$COMBINED") - 1)) rows)"
fi

log "FINAL spend: \$$(cost_so_far)"
log "ablation sweep complete"
