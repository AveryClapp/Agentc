#!/usr/bin/env bash
# Guard threshold sweep — safety/savings frontier figure.
# Runs research_planner (ContextCompress, benign) and analyst_qa
# (StateDrop, catastrophic) at 7 divergence-budget thresholds.
#
# Self-caffeinates: re-execs under caffeinate -is if not already.
#
# Usage: bash bench/scripts/run_guard_sweep.sh
# Quick smoke test: GSWEEP_N=3 GSWEEP_THRESHOLDS=0.10,0.20 bash bench/scripts/run_guard_sweep.sh
set -uo pipefail

cd "$(dirname "$0")/../.."

if ! pgrep -x caffeinate > /dev/null 2>&1; then
    exec caffeinate -is bash "$0" "$@"
fi

if [[ -f .env ]]; then set -a; . ./.env; set +a; fi
export PYTHONPATH=python

PY=".venv/bin/python"
RESULTS=bench/paper_results
mkdir -p "$RESULTS"
LOG="$RESULTS/gsweep-$(date +%Y%m%d-%H%M%S).log"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }

log "guard sweep starting (N=${GSWEEP_N:-100})"
log "log: $LOG"

"$PY" bench/run_guard_sweep.py 2>&1 | tee -a "$LOG"

log "guard sweep complete"
