---
title: Paper Intelligence Style Guide
status: draft
last-updated: 2026-05-09
owner: paper-intelligence
---

# Paper Intelligence Style Guide

This guide is for research notes and literature-review support. It is not a manuscript style guide.

## Literature Review Posture

- Write to clarify the map, not to sound impressive.
- Separate **what prior work does** from **how AgentC differs**.
- Do not claim novelty with words like `first`, `only`, or `no prior work` until `nearest-neighbor-comparison.md` supports it.
- Prefer this pattern:
  - Prior work establishes X.
  - AgentC shares Y with that work.
  - AgentC differs because Z.
  - Therefore AgentC should be compared on A, not overclaim B.
- Organize the literature review by rewrite class and abstraction level, not by a flat list of papers.

## Whole-Paper Literature Review Shape

The literature review should cover the whole AgentC paper:

1. Compound AI systems and agent frameworks.
2. Runtime optimization for LLM applications.
3. The five rewrite families: routing, compression, state pruning, caching, and parallel execution.
4. Serving/inference systems as orthogonal systems work.
5. Stochastic LLM evaluation methodology.

Routing is one subsection under rewrite families. Do not let routing become the thesis.

## Routing Framework Comparison

Use this table to support the `ModelDowngrade` part of the related-work section. These are candidate comparisons until the primary papers are verified.

| Framework | What it optimizes | Similarity to AgentC | Difference from AgentC | Safe comparison wording |
|---|---|---|---|---|
| FrugalGPT | API cost through model cascades and prompt adaptation. | Supports the basic idea that cheaper models can handle some requests. | Mostly routes user-facing queries through a cascade; AgentC routes internal hot call sites as one pass inside a broader runtime optimizer. | FrugalGPT motivates cost-aware routing; AgentC embeds a routing-style decision inside a multi-rule trace optimizer. |
| RouteLLM | Learned routing between weaker and stronger models. | Direct prior art for `ModelDowngrade`. | Focuses on query-level routing; AgentC can use call-site locality, structured-output shape, and runtime observations from agent traces. | RouteLLM is a strong routing baseline, but AgentC's contribution is not routing alone. |
| Optimizing Model Selection for Compound AI Systems | Model choice for components in compound AI systems. | Very close conceptual neighbor because it treats compound systems as model-selection targets. | AgentC operates as a transparent runtime layer and also handles context compression, state dropping, caching, and parallelism. | This is the closest model-selection framing; AgentC should distinguish runtime interception plus multiple rewrite classes. |
| Language Model Cascades | Sequential deferral from cheap to expensive models under uncertainty. | Supports quality-risk and fallback framing for downgrade decisions. | Cascades are usually designed as explicit query-time policies; AgentC's downgrade is selected by a planner for observed call sites and coexists with other rewrite rules. | Cascades justify risk-aware model choice; AgentC needs its own calibration/guardrail evidence. |

## Routing Claims To Avoid

- Do not say `ModelDowngrade is novel`.
- Do not say `AgentC solves model routing`.
- Do not imply the current evidence beats RouteLLM, FrugalGPT, or cascades unless a direct baseline exists.

## Routing Claims That Are Safer

- Model routing and cascades show that not every LLM call needs the most expensive model.
- AgentC uses that idea at the internal call-site level inside multi-step agent traces.
- The paper contribution is the runtime control plane and evaluation of rewrite rules together, not the existence of routing by itself.

## Related-Work Section Shape

For each cluster:

1. Name the prior-work family.
2. State the shared problem.
3. State the key difference in intervention point.
4. State the reviewer risk it creates.
5. Link the cluster to a claim, gap, or experiment.
