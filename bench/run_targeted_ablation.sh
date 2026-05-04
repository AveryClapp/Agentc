#!/usr/bin/env bash
# Targeted per-rule ablation.
# Self-caffeinated: re-execs under caffeinate if not already running under it.
#
#   Experiment 1 — gaia_router (n=127)
#     Rule under test: ModelDowngrade
#     Expected: ~24% cost savings, 0pp accuracy delta
#
#   Experiment 2 — hotpot_distractor 3-arm (n=150)
#     Rule under test: ContextCompress
#     Expected: positive input-token savings, <=1.5pp accuracy delta
#
# Usage: caffeinate -is bash bench/run_targeted_ablation.sh
set -uo pipefail

cd "$(dirname "$0")/.."

# Re-exec under caffeinate if not already running under it (prevents sleep
# mid-run when the machine is idle and no explicit caffeinate wrapper was used).
if ! pgrep -x caffeinate > /dev/null 2>&1; then
    exec caffeinate -is bash "$0" "$@"
fi

if [[ -f .env ]]; then set -a; . ./.env; set +a; fi
export PYTHONPATH=python
export BENCH_BASELINE_MODEL="${BENCH_BASELINE_MODEL:-gpt-4o}"

PY=".venv/bin/python"
RESULTS=bench/results
mkdir -p "$RESULTS"
LOG="$RESULTS/targeted-$(date +%Y%m%d-%H%M%S).log"
HARD_STOP_USD=20

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }

cost_so_far() {
    awk -F, 'FNR>1 && $7+0 < 1 {sum+=$7} END {printf "%.4f", sum+0}' \
        "$RESULTS"/targeted-*.csv 2>/dev/null || echo "0"
}

log "=== EXPERIMENT 1: gaia_router (n=127, ModelDowngrade) ==="

# Rebuild fixture to 127 tasks (idempotent if already done)
"$PY" -m bench.build_gaia_fixture --total 127 2>&1 | tee -a "$LOG"

OUT1="$RESULTS/targeted-gaia_router.csv"
if [[ -f "$OUT1" ]] && [[ $(wc -l < "$OUT1") -gt 1 ]]; then
    log "skip gaia_router (results at $OUT1 — delete to re-run)"
else
    "$PY" -m bench.optimizer_ablation \
        bench.agents.gaia_router \
        --storage-root /tmp/agentc-targeted \
        --out "$OUT1" 2>&1 | tee -a "$LOG"
    log "gaia_router done | cumulative \$$(cost_so_far)"
fi

total=$(cost_so_far)
if awk -v t="$total" -v cap="$HARD_STOP_USD" 'BEGIN {exit !(t+0 > cap+0)}'; then
    log "ABORT: \$$total exceeds hard stop \$$HARD_STOP_USD"
    exit 1
fi

log "=== EXPERIMENT 2: hotpot_distractor (n=150, ContextCompress) ==="

OUT2="$RESULTS/targeted-hotpot.csv"
if [[ -f "$OUT2" ]] && [[ $(wc -l < "$OUT2") -gt 1 ]]; then
    log "skip hotpot (results at $OUT2 — delete to re-run)"
else
    "$PY" -m bench.run_hotpot_ablation \
        --out "$OUT2" \
        --storage-root /tmp/agentc-targeted-hotpot 2>&1 | tee -a "$LOG"
    log "hotpot done | cumulative \$$(cost_so_far)"
fi

log "FINAL spend: \$$(cost_so_far)"
log "targeted ablation complete"
