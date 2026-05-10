---
title: Outline Options
status: active
last-updated: 2026-05-08
owner: paper-intelligence
---

# Outline Options

This file stores possible paper structures. It does not authorize drafting final prose.

## Option A: Systems-First

1. Introduction and motivation
2. AgentC system overview
3. Runtime profiling and planner
4. Rewrite rules
5. Evaluation methodology
6. Results
7. Related work
8. Limitations and future work

Strength: makes the runtime contribution clear.

Risk: requires strong systems framing and reproducibility details.

Best fit: systems, ML systems, agent infrastructure venues.

## Option B: Results-First

1. Introduction with headline savings
2. Problem setting: waste in repeated agent call sites
3. AgentC design
4. Evaluation workloads
5. Cost and quality results
6. Rule activation boundaries
7. Related work and limitations

Strength: quickly communicates why AgentC matters.

Risk: may look empirical-only unless the planner and rule design are explained well.

Best fit: applied AI, agent tooling, workshops.

## Option C: Compiler/JIT Analogy

1. Introduction: LLM calls as optimizable runtime traces
2. Profiling and hot-call detection
3. Rule proposal and safety validation
4. Rewrite rules as optimization passes
5. Benchmarks and ablations
6. Results and failed activations
7. Related work

Strength: clean intellectual frame.

Risk: compiler analogy can be attacked if too literal.

Best fit: programming systems, ML systems, software engineering.

## Option D: Evaluation-Methodology Paper

1. Introduction: evaluating LLM-runtime optimizers is hard
2. AgentC as testbed
3. Rule-specific workloads and ablation matrix
4. Safety and quality metrics
5. Results
6. Methodological lessons
7. Related work

Strength: turns partial or boundary results into a contribution.

Risk: weaker if venues expect a production system or broader benchmark.

Best fit: workshops, empirical ML systems, evaluation venues.

## Current Lean

Use Option A or C for a serious systems paper. Use Option B for a faster workshop-style submission. Keep Option D as a backup if deep research shows many close systems already exist.

