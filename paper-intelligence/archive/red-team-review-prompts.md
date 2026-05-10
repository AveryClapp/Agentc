---
title: Red Team Review Prompts
status: active
last-updated: 2026-05-08
owner: paper-intelligence
---

# Red Team Review Prompts

Use these prompts to ask agents or external models to pressure-test the paper intelligence base. These prompts should produce findings, not final prose.

## Skeptical Systems Reviewer

```text
Review the AgentC paper intelligence materials as a skeptical systems reviewer.

Read:
- paper-intelligence/README.md
- paper-intelligence/results-ledger.md
- paper-intelligence/repo-source-map.md
- paper-intelligence/claim-bank.md
- paper-intelligence/reviewer-risk-register.md

Find the strongest technical objections to the current paper story. Focus on workload representativeness, runtime overhead, planner correctness, safety checks, reproducibility, and whether the optimization analogy is overclaimed.

Return:
- top 5 findings, ordered by severity;
- exact files or IDs involved;
- what evidence would answer each objection;
- whether the fix needs code, experiment tokens, literature review, or framing.
```

## Skeptical ML Evaluation Reviewer

```text
Review the AgentC result story as a skeptical ML evaluation reviewer.

Read:
- paper-intelligence/results-ledger.md
- paper-intelligence/result-validation-checklist.md
- paper-intelligence/statistical-analysis-plan.md
- paper-intelligence/experiment-priority-board.md

Look for statistical, methodological, and benchmark-design weaknesses. Pay attention to sample sizes, partial matrices, paired comparisons, stochasticity, metric choice, accuracy preservation, and cost-accounting assumptions.

Return:
- which results are headline-safe;
- which results are appendix-only or diagnostic;
- what exact analysis should be run before paper drafting;
- which claims should be softened or blocked.
```

## Related Work Reviewer

```text
Review the AgentC positioning against related work.

Read:
- paper-intelligence/literature-ledger.md
- paper-intelligence/related-work-map.md
- paper-intelligence/nearest-neighbor-comparison.md
- paper-intelligence/citation-gap-list.md
- paper-intelligence/positioning-taxonomy.md

Identify missing comparator families and likely nearest-neighbor papers for runtime optimization of LLM applications, prompt/context compression, model routing, agent memory/state management, caching, and agent frameworks.

Return:
- missing literature areas;
- candidate papers or venues to verify;
- claims that need citations before prose;
- where AgentC appears meaningfully different.
```

## Reproducibility Reviewer

```text
Review AgentC's paper reproducibility posture.

Read:
- paper-intelligence/reproduction-commands.md
- paper-intelligence/artifact-inventory.md
- paper-intelligence/result-validation-checklist.md
- paper-intelligence/experiment-run-log.md

Check whether a future researcher could reproduce the reported CSVs and understand which runs are canonical, partial, diagnostic, or local-only.

Return:
- missing commands or environment variables;
- missing artifact checksums;
- unclear output locations;
- run-log fields that should be required before new results are cited.
```

## Clarity Reviewer

```text
Review the paper intelligence base for reader orientation.

Read:
- paper-intelligence/README.md
- paper-intelligence/manual-writing-brief.md
- paper-intelligence/current-fit-and-publishability.md
- paper-intelligence/paper-angle-matrix.md
- paper-intelligence/paper-gap-register.md

Ask whether a smart technical collaborator can understand the current paper state in under ten minutes.

Return:
- confusing names;
- missing definitions;
- duplicated or contradictory status labels;
- the shortest path from "new contributor" to "I know what to do next."
```
