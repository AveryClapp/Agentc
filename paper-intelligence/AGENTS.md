---
title: Paper Intelligence Agent Instructions
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Agent Instructions

This directory is a paper intelligence base. Your job is to improve the evidence base for a human-written paper.

## Prime Directive

Do not write final manuscript prose by default. Produce structured research notes, maps, ledgers, gap analyses, and briefs that help William write the paper by hand.

## Required Behavior

- Read `metadata-schemas.md` before creating or editing ledgers.
- Use stable IDs such as `RES-001`, `CLM-001`, `GAP-001`, `LIT-001`, `ART-001`, `EXP-001`, `VEN-001`, and `FIG-001`.
- Link claims to evidence IDs.
- Link gaps to claims, results, literature, or experiments.
- Keep raw research separate from verified source artifacts.
- Update `artifact-inventory.md` when importing, moving, or creating paper-relevant artifacts.
- Run `git status --short` before and after substantial work.
- Preserve uncertainty. Mark unsupported claims as `needs-evidence`, not `supported`.
- Prefer primary sources for literature and venue work.

## File Hygiene

- Do not leave PDFs, Markdown notes, or research drops at repo root.
- Do not create new top-level directories for paper work without explicit approval.
- Do not add ad hoc CSVs to `bench/paper_results/`; use scratch or ignored output paths for exploratory runs.
- Do not mix copied source artifacts with generated research notes.
- Do not delete or discard local artifacts without explicit approval.

## Result Hygiene

- Treat new CSVs as `quarantined` until validated.
- Partial matrices stay `partial`; they do not support headline claims.
- Every result entry needs source path, command or script, model, sample size, and caveat.
- Record dirty-tree state for any new experiment run.

## Literature Hygiene

- Raw deep-research output goes to `research-inbox/` or `deep-research-inbox.md`.
- Verified papers go to `literature-ledger.md`.
- Citation metadata goes to `bibliography-ledger.md`.
- Use `citation-gap-list.md` for claims that still need sources.

## Handoff Format

When finishing paper-intelligence work, report:

- files changed
- IDs created or updated
- source artifacts used
- claims or gaps changed
- remaining verification needed
- any files intentionally left untracked or local-only
