---
title: Paper Intelligence Agent Instructions
status: active
last-updated: 2026-05-09
owner: paper-intelligence
---

# Agent Instructions

This folder is a paper intelligence base, not a manuscript directory. Improve the evidence base for a human-written AgentC paper.

## Prime Directive

Do not write final manuscript prose by default. Produce research notes, maps, ledgers, gap analyses, source checks, and briefs that help William and Avery decide what to write by hand.

## Active Source Of Truth

Use these files first:

- `README.md`
- `current-fit-and-publishability.md`
- `literature-and-nearest-neighbors.md`
- `claims-gaps-and-risks.md`
- `results-experiments-and-repro.md`
- `strategy-and-venues.md`
- `evidence-and-sources.md`
- `research-prompts.md`

Use `research-inbox/` and `references/source/` for provenance. Use `archive/` only for historical context.

## Stable IDs

Use existing IDs whenever possible. Do not reuse abandoned IDs.

| Prefix | Entity |
|---|---|
| `ART` | Artifact |
| `RES` | Result |
| `CLM` | Claim |
| `GAP` | Gap, risk, missing evidence |
| `LIT` | Literature source |
| `CIT` | Citation gap |
| `EXP` | Experiment candidate |
| `RUN` | Experiment run |
| `VEN` | Venue/workshop |
| `FIG` | Figure/table idea |
| `DEC` | Decision |
| `QST` | Open question |
| `IDEA` | Paper idea |
| `DRP` | Deep research drop |
| `ANG` | Paper angle |
| `RR` | Reviewer risk |
| `WP` | Weak-point work item |
| `TTL` | Title/abstract idea |
| `QTE` | Quote/evidence-bank entry |
| `AE` | Artifact-evaluation item |
| `STAT` | Statistical-analysis item |
| `NEG` | Negative result or rejected angle |

## Literature Blurb Style

For each source, preserve:

- what the source says or contributes;
- how AgentC is similar;
- how AgentC differs;
- what claim it supports;
- what reviewer risk it creates;
- whether it is must-cite, optional, background, runnable baseline, or cite-only.

Do not let routing dominate the literature map. Routing is one subsection inside runtime optimization for compound AI / multi-step LLM agent traces.

## Evidence Hygiene

- Prefer primary sources for literature and venue work.
- Treat generated deep research as discovery/provenance, not citation evidence.
- Link claims to evidence IDs.
- Link gaps to claims, results, literature, or experiments.
- Treat new CSVs as `quarantined` until validated.
- Partial matrices stay `partial`.
- Record dirty-tree state for any new experiment run.

## File Hygiene

- Do not create new active Markdown files unless consolidation truly needs a new durable surface.
- Do not leave PDFs, Markdown notes, or research drops at repo root.
- Do not add ad hoc CSVs to `bench/paper_results/`; use scratch or ignored output paths for exploratory runs.
- Do not delete or discard local artifacts without explicit approval.
- When archiving, add or update `archive/README.md` with a source-to-destination map.

## Handoff Format

When finishing paper-intelligence work, report:

- files changed;
- IDs created or updated;
- source artifacts used;
- claims, gaps, or results changed;
- remaining verification needed;
- files intentionally archived or left as raw/provenance.
