---
title: Methodology Brief
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Methodology Brief

## Current Method Shape

- Use rule-specific workloads to isolate behavior.
- Compare baseline, all-rules, target-rule-only, and combinations through ablation matrices.
- Track cost savings, input-token savings, output-token behavior, and accuracy.
- Label partial matrices clearly.

## Current Gaps

- Need paired uncertainty analysis.
- Need row-level validation for any stronger accuracy claim.
- Need a clear explanation of pricing and prompt-caching assumptions.

## Evidence Pointers

- `bench/optimizer_ablation.py`
- `bench/scripts/run_paper_ablation.sh`
- `bench/scripts/run_pushback_ablation.sh`
- `result-validation-checklist.md`
- `statistical-analysis-plan.md`

