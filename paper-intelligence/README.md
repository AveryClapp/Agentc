---
title: Paper Intelligence README
status: draft
last-updated: 2026-05-09
owner: paper-intelligence
---

# AgentC Paper Intelligence

This directory is the research and planning base for a human-written AgentC paper. It stores evidence, result ledgers, literature-review notes, gap analysis, venue strategy, experiment priorities, and writing briefs. It is not the manuscript.

It lives at repo root on purpose. `specs/` is for technical implementation specs; `paper-intelligence/` is an active research/evidence workspace.

## Purpose

Use this folder to answer four paper-planning questions:

1. What current work is closest to AgentC?
2. Where is AgentC actually different?
3. Which evidence gaps would a reviewer attack?
4. Which experiments, literature checks, or venue choices should we do next?

The folder is intentionally a map, not a paper draft. It should help William and Avery decide what to write by hand and where to push the project next.

## Fast Read For Avery

If you only have 20 minutes, read these first:

1. `handoff.md` - current state and next useful actions.
2. `literature-review-section-plan.md` - whole-paper literature-review shape.
3. `literature-verified-blurbs.md` - primary-source checked source blurbs and differentiation notes.
4. `paper-gap-register.md` - main missing evidence and positioning gaps.
5. `reviewer-risk-register.md` - likely reviewer objections.
6. `experiment-priority-board.md` - what to run next if spending tokens.
7. `venue-positioning-matrix.md` - where the paper might fit.

## Read Order

1. `AGENTS.md` - operating rules for research/gap-finding agents.
2. `metadata-schemas.md` - IDs, statuses, and row templates.
3. `agentc-paper-intelligence-workplan.md` - full workplan.
4. `artifact-inventory.md` - source artifacts currently known.
5. `repo-source-map.md` - where paper evidence lives in the repo.
6. `results-ledger.md` - validated or candidate experiment results.
7. `paper-gap-register.md` - canonical gaps, risks, and missing evidence.
8. `literature-ingestion-workflow.md` - how deep research becomes durable notes.
9. `literature-review-section-plan.md` - whole-paper literature review structure.
10. `literature-verified-blurbs.md` - checked source blurbs and citation-use notes.
11. `literature-blurb-todo.md` - original candidate checklist and scoring queue.
12. `deep-research-prompt-templates.md` - reusable prompts.
13. `venue-positioning-matrix.md` - venue strategy and evidence expectations.
14. `reviewer-risk-register.md` and `weak-point-resolution-plan.md` - red-team risks and concrete fixes.
15. `style-guide.md` - literature-review and comparison-writing rules.
16. `section-briefs/` - human-writing briefs, not final prose.

## Storage Model

- `references/source/` stores source artifacts: copied Markdown, PDFs, attachments, and other original inputs.
- `research-inbox/` stores raw generated research drops before triage.
- `section-briefs/` stores human-writing briefs, not final prose.
- `experiments/` stores per-run notes for experiments, not large generated outputs.

## Manual Writing Boundary

Agents may summarize, map, audit, compare, and propose. Agents should not draft final manuscript prose unless William explicitly asks for prose. The default output is evidence, gaps, ideas, and briefings.

## Promotion Rule

Use this path for any new research or result:

```text
chat/research output -> inbox -> verification -> ledger/gap/claim update -> validation -> brief/use
```

New evidence is not paper-ready until it is linked to stable IDs and appears in the relevant ledger.

## Current Bootstrap State

The workspace currently has:

- Stable ID/schema rules.
- Migrated source artifacts from the repo root.
- Initial artifact inventory.
- Initial source map.
- Initial result ledger.
- Deep-research prompt templates.
- Literature ingestion workflow.
- Venue-positioning scaffold.
- Pizza-derived paper-process structure adapted into AgentC-specific control files.
- Section briefs, reviewer-risk register, weak-point plan, outline options, and title/abstract idea bank.

Next high-value work is to promote verified metadata from `literature-verified-blurbs.md` into `literature-ledger.md` and turn the strongest runnable baselines into concrete experiment tickets.
