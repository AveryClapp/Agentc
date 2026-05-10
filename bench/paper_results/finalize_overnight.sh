#!/bin/bash
# Finalize overnight session: run paired analysis on both ablations
# and emit summary tables. Run after both ablation processes have
# exited.
#
# Usage:
#   bash bench/paper_results/finalize_overnight.sh
set -euo pipefail

cd "$(dirname "$0")/../.."

VENV=.venv/bin/python

echo "================================================================"
echo "ITERATIVE_REFINER paired matrix (n=50)"
echo "================================================================"
PYTHONPATH=python $VENV -m bench.summarize_paired \
  bench/paper_results/iterative_refiner-statedrop-n50-paired.csv

echo ""
echo "================================================================"
echo "MULTIRULE paired matrix (n=20, EXP-006)"
echo "================================================================"
PYTHONPATH=python $VENV -m bench.summarize_paired \
  bench/paper_results/multirule_qa-cc_sd-n20-paired.csv

echo ""
echo "================================================================"
echo "Optimizer overhead (refreshed across all current audit DBs)"
echo "================================================================"
$VENV /tmp/overhead_analysis.py 2>/dev/null || \
  cat bench/paper_results/optimizer_overhead.txt

echo ""
echo "================================================================"
echo "Multi-rule activation breakdown (plan_audit per config)"
echo "================================================================"
for db in /tmp/agentc-paired-multirule/bench_agents_multirule_qa/*/optimized/optimizer_audit.db; do
  config=$(basename "$(dirname "$(dirname "$db")")")
  echo "--- $config ---"
  sqlite3 "$db" "SELECT plan_kind, COALESCE(rule,'(none)'), COUNT(*) FROM plan_audit GROUP BY plan_kind, rule ORDER BY 1,2;" 2>/dev/null
done

echo ""
echo "Done. Files:"
ls -la bench/paper_results/iterative_refiner-statedrop-n50-paired*.csv \
       bench/paper_results/multirule_qa-cc_sd-n20-paired*.csv \
       bench/paper_results/optimizer_overhead.txt 2>/dev/null
