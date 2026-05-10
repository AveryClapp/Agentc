---
title: Pizza Import Plan
status: active
last-updated: 2026-05-08
owner: paper-intelligence
---

# Pizza Import Plan

This plan records what to adapt from `pizza_at_the_pentagon` into AgentC paper intelligence. The import goal is structure, not domain content. Do not copy Pizza-specific forecasting claims, results, citations, or prose into AgentC unless William explicitly asks.

## Source Directory

Primary source inspected:

`/Users/williamboudy/Desktop/pizza_at_the_pentagon/specs/paper-snapshot-writeup/`

## Import Decisions

| Source file | Decision | AgentC destination | Reason |
|---|---|---|---|
| `CLAUDE.md` | `adapted` | `AGENTS.md`, `README.md` | Strong local operating rules and read-order pattern. |
| `paper-review-checklist.md` | `adapted` | `red-team-review-prompts.md`, `reviewer-risk-register.md` | Useful review posture, but AgentC needs paper-intelligence review instead of manuscript review. |
| `information-review-criteria.md` | `adapted` | `reviewer-risk-register.md`, `weak-point-resolution-plan.md` | Good distinction between factual support, fit, reader orientation, and voice. |
| `section-writing-workplan.md` | `adapted` | `outline-options.md`, `section-briefs/` | Good section-by-section discipline, but AgentC should use briefs before prose. |
| `citation-seed-plan.md` | `adapted` | `citation-style-and-hygiene.md`, `bibliography-ledger.md`, `citation-gap-list.md` | Strong citation verification pattern. |
| `academic-writing-prompts.md` | `adapted` | `red-team-review-prompts.md`, `deep-research-prompt-templates.md` | Reusable prompt discipline, shifted toward research and gap-finding. |
| `pre-prose-reviewer-audit.md` | `adapted` | `reviewer-risk-register.md` | Good pre-writing skepticism pattern. |
| `weak-point-resolution-plan.md` | `adapted` | `weak-point-resolution-plan.md` | Directly matches AgentC's current need. |
| `latex-rendering-checklist.md` | `defer` | none yet | AgentC has no approved manuscript directory yet. |
| `style-guide.md` | `defer` | possible future `style-and-voice-guide.md` | Useful only after the paper angle and venue are chosen. |
| `emnlp-arr-formatting.md` | `skip-for-now` | none | AgentC venue is not chosen and may not be EMNLP/ARR. |

## Import Rules

- Import process patterns, not paper claims.
- Keep AgentC's default mode as evidence, gaps, ideas, and briefs.
- Do not create final manuscript scaffolding until the repo owner approves it.
- Every imported pattern should point to AgentC evidence IDs, not Pizza paths.

## Done So Far

- Agent instructions and read order adapted.
- Result, claim, gap, literature, and artifact ledgers created.
- Reviewer-risk and weak-point scaffolds created.
- Citation hygiene moved into a verification-first ledger workflow.
- Section-writing structure translated into section briefs rather than paper prose.

