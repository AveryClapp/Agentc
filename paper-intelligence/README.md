---
title: Paper Intelligence README
status: active
last-updated: 2026-05-09
owner: paper-intelligence
---

# AgentC Paper Intelligence

This folder is the paper-planning and evidence base for AgentC. It is not the manuscript.

Use it to answer:

1. What is AgentC's current paper shape?
2. What experiments and artifacts support that shape?
3. What prior work is closest?
4. What claims are safe or unsafe?
5. What should be run, verified, or written next?

`specs/` is for implementation specs. `paper-intelligence/` is for literature, evidence, venue strategy, and reviewer-risk planning.

## Fast Read Paths

### 10 minutes

1. `current-fit-and-publishability.md`
2. `claims-gaps-and-risks.md`
3. skim the top of `results-experiments-and-repro.md`

### 30 minutes

1. `current-fit-and-publishability.md`
2. `literature-and-nearest-neighbors.md`
3. `claims-gaps-and-risks.md`
4. `results-experiments-and-repro.md`
5. `strategy-and-venues.md`

### 60 minutes

Read the 30-minute path, then add:

1. `evidence-and-sources.md`
2. `research-prompts.md`
3. `archive/README.md` if you need to understand what was consolidated
4. `research-inbox/` only if you need raw deep-research provenance

## Active Files

| File | Purpose |
|---|---|
| `current-fit-and-publishability.md` | Short reality check: alpha, publishability, and strongest next moves. |
| `literature-and-nearest-neighbors.md` | Whole-paper literature map, verified blurbs, nearest-neighbor matrix, baseline table. |
| `claims-gaps-and-risks.md` | Safe claims, unsafe claims, gaps, reviewer risks, citation gaps, questions, weak-point plan. |
| `results-experiments-and-repro.md` | Current results, artifact map, experiment queue, statistics/repro rules. |
| `strategy-and-venues.md` | Venue ladder, paper angles, section guidance, title/figure ideas. |
| `evidence-and-sources.md` | Source hygiene, raw-provenance map, repo source map, ID prefixes. |
| `research-prompts.md` | Reusable deep-research, red-team, and idea-generation prompts. |
| `AGENTS.md` | Maintenance rules for agents working in this folder. |

## Raw And Provenance Areas

| Path | Purpose |
|---|---|
| `references/source/` | Original local source artifacts, copied Markdown, and PDFs. Keep. |
| `research-inbox/` | Raw deep-research drops. Keep as provenance. |
| `archive/` | Superseded granular files after consolidation. Do not use as active source of truth. |

## Current Paper Read

AgentC is currently strongest as a **runtime trace optimizer for multi-step LLM agents**. The individual rewrite families are known in the literature; AgentC's possible contribution is the control plane over framework-emitted calls.

The best current evidence is:

- `RES-001`: `ContextCompress` saves substantial tokens/cost on long-context stress workloads.
- `RES-002`: `ModelDowngrade` saves substantial dollars on a routing workload.
- `RES-004`: `StateDrop` is promising but partial/caveated.
- `RES-005`: real HotpotQA near-zero savings is useful activation-boundary evidence.

The paper is not ready to claim:

- first runtime optimizer for LLM agents;
- all five rewrites equally validated;
- CacheHit or ParallelBranch as headline contributions;
- semantic behavior preservation without uncertainty treatment;
- StateDrop as sound compiler slicing.

## Promotion Rule

Use this path for new material:

```text
raw input -> research-inbox or references/source -> verification -> consolidated doc update -> claim/gap/result linkage
```

Do not add a new top-level paper-intelligence file unless it will stay active. Prefer updating one of the active files above.

## Next Work

The durable task graph starts at `bd-399`: consolidate Paper Intelligence into a reader-first packet. The highest-value follow-up after consolidation is still experimental: end-to-end multi-rule evidence, overhead/tail-latency measurement, baseline feasibility, paired uncertainty, and a StateDrop dependency model.
