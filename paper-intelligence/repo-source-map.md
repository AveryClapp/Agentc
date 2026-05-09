---
title: Agentc Paper Repo Source Map
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Repo Source Map

This file maps paper-relevant questions to source paths.

## What Agentc Is

- `README.md`: public-facing description, status, rule summary, benchmark summary, and quick-start commands.
- `AGENTS.md`: project context and repository conventions.
- `specs/README.md`: component overview.

## Runtime And Interception Path

- `python/agentc/_intercept.py`: high-level interception flow for LLM SDK calls.
- `python/agentc/_optimizer.py`: Python shim over native optimizer FFI.
- `python/agentc/_executor.py`: dispatch behavior for optimizer plans.
- `python/agentc/_patches/_optimizer_glue.py`: request/response conversion between SDK calls and optimizer call dictionaries.
- `crates/agentc-optimizer/src/ffi.rs`: Rust FFI plan/observe adapters.
- `crates/agentc-optimizer/src/wiring.rs`: production optimizer construction and rule registration.

## Planner, Safety, And Activation

- `crates/agentc-optimizer/src/planner.rs`: enabled flag, hot threshold, rule proposal/ranking, safety-check selection, overhead kill switch, pass-through behavior.
- `crates/agentc-optimizer/src/config.rs`: optimizer config and env overrides such as `AGENTC_OPTIMIZE_HOT_THRESHOLD`.
- `crates/agentc-optimizer/src/budget.rs`: accuracy-budget state and auto-disable behavior.
- `crates/agentc-optimizer/src/shadow.rs`: shadow sampling support.
- `crates/agentc-optimizer/src/audit.rs`: optimizer audit logging.

## Rewrite Rules

- `crates/agentc-optimizer/src/rules/context_compress.rs`: ContextCompress implementation, 8 KB default prompt gate, attention scores, dead-fraction gate, and safety checks.
- `crates/agentc-optimizer/src/rules/model_downgrade.rs`: ModelDowngrade implementation, routing table, short/structured-output gate, probation, and divergence budget.
- `crates/agentc-optimizer/src/rules/state_drop.rs`: StateDrop implementation, state-tagged message pruning, read-window checks, retention floor, and system-prompt safety.
- `crates/agentc-optimizer/src/rules/cache_hit.rs`: CacheHit rule.
- `crates/agentc-optimizer/src/rules/parallel_branch.rs`: ParallelBranch rule.

## Benchmark Harness

- `bench/optimizer_ablation.py`: 11-config shared-baseline ablation matrix.
- `bench/optimizer_bench.py`: baseline vs optimized benchmark runner.
- `bench/run_oracle_baseline.py`: oracle baseline CSV generator.
- `bench/run_hotpot_ablation.py`: HotpotQA-specific ablation runner.
- `bench/agents/`: benchmark agents and purpose-built workloads.
- `bench/build_hotpot_fixture.py`: HotpotQA fixture builder.
- `bench/build_gaia_fixture.py`: GAIA fixture builder.
- `bench/build_long_context_fixture.py`: long-context QA fixture builder.

## Experiment Scripts

- `bench/scripts/run_paper_ablation.sh`: paper-quality ContextCompress and StateDrop runs.
- `bench/scripts/run_pushback_ablation.sh`: reviewer-pushback runs for oracle, real HotpotQA, and larger StateDrop.
- `bench/scripts/run_targeted_ablation.sh`: targeted ModelDowngrade/ContextCompress experiments.
- `bench/scripts/run_ablation.sh`: broader ablation sweep.

## Canonical Result CSVs

- `bench/paper_results/long_context_qa-contextcompress-n100.csv`: ContextCompress n=100.
- `bench/paper_results/gaia_router-modeldowngrade-n127.csv`: ModelDowngrade n=127.
- `bench/paper_results/iterative_refiner-statedrop-n30.csv`: StateDrop n=30.
- `bench/paper_results/iterative_refiner-statedrop-n50-partial10of11.csv`: StateDrop n=50 partial.
- `bench/paper_results/hotpot_real-contextcompress-n300-partial7of11.csv`: real HotpotQA partial.
- `bench/paper_results/hotpot_oracle-n300.csv`: oracle/manual-compression baseline.

## Design Specs

- `specs/profiler.md`: profiler design, span schema, and evaluation criteria.
- `specs/memoization.md`: semantic memoization design and evaluation criteria.
- `specs/optimizer.md`: optimizer design, rewrite-rule framing, and evaluation plan.
- `specs/future-work.md`: out-of-scope items.

## Local Paper Reference Sources

- `paper-intelligence/references/source/agentc-paper-reference-v2.md`: master paper reference and table/figure plan.
- `paper-intelligence/references/source/agentc-feedback.md`: feedback context.
- `paper-intelligence/references/source/agentc-response.pdf`: response artifact.
- `paper-intelligence/references/source/readme-local-before-upstream.md`: older local framing.

## Paper Intelligence Control Files

- `paper-intelligence/metadata-schemas.md`: ID and schema rules.
- `paper-intelligence/artifact-inventory.md`: artifact tracking.
- `paper-intelligence/results-ledger.md`: result tracking.
- `paper-intelligence/paper-gap-register.md`: canonical gap/risk tracking.
- `paper-intelligence/literature-ingestion-workflow.md`: deep research promotion workflow.
