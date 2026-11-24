---
title: Paper Intelligence Agent Instructions
status: active
last-updated: 2026-11-24
owner: paper-intelligence
---

# Agent Instructions

This folder supports a human-written paper. Produce research notes, literature maps, source
checks, and gap analyses; do not write final manuscript prose unless asked.

## Evidence Hygiene

- Prefer primary sources for literature and venue facts.
- Treat generated deep research as discovery, not citation evidence.
- `LIT-###` IDs feed the bibliography: reuse existing IDs and never reuse abandoned ones.
  Other ID prefixes in older notes are historical and frozen.
- Do not add ad hoc CSVs to `bench/paper_results/`; use ignored output paths for
  exploratory runs.

## File Hygiene

- Prefer updating an existing file over adding a new one.
- Put dated raw research in `research-inbox/` with a `YYYY-MM-DD-` prefix.
- Do not leave PDFs or notes at the repo root.
