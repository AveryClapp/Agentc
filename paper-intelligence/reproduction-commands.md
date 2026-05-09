---
title: Reproduction Commands
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Reproduction Commands

This file records commands for regenerating or extending Agentc results. Commands may spend API money when real keys are set.

## Fixture Builders

```bash
python -m bench.build_hotpot_fixture
python -m bench.build_gaia_fixture
python -m bench.build_long_context_fixture
```

## Single-Agent Benchmark

```bash
python -m bench.optimizer_bench bench.agents.long_context_qa
```

## 11-Config Ablation

```bash
BENCH_MAX_TASKS=100 python -m bench.optimizer_ablation bench.agents.long_context_qa
```

## Scripted Runs

```bash
bash bench/scripts/run_paper_ablation.sh
bash bench/scripts/run_pushback_ablation.sh
bash bench/scripts/run_targeted_ablation.sh
bash bench/scripts/run_ablation.sh
```

## Oracle Baseline

```bash
BENCH_MAX_TASKS=300 python -m bench.run_oracle_baseline bench.agents.hotpot_oracle
```

## Required Before Running

- Confirm target gap in `paper-gap-register.md`.
- Create or update an `EXP-###` entry in `experiment-priority-board.md`.
- Record expected output path.
- Capture `git rev-parse HEAD` and `git status --short`.
- Confirm API cost/rate limit assumptions.

