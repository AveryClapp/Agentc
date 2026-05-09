---
title: Literature Ingestion Workflow
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Literature Ingestion Workflow

This workflow turns raw deep research into verified paper intelligence.

## Pipeline

```text
raw drop -> source extraction -> identity verification -> citation normalization -> skim notes -> claim/gap mapping -> ledger promotion
```

## Step 1: Capture Raw Drop

Store the raw output in one of:

- `research-inbox/<date>-<topic>.md`
- `deep-research-inbox.md` if it is short

Assign a `DRP-###` ID. Record topic, source/tool, date, and whether links are present.

## Step 2: Extract Sources

For each cited paper/source:

- extract title
- extract authors/year if available
- extract URL
- classify source type: `paper`, `official-doc`, `cfp`, `blog`, `code`, `other`
- mark whether it is primary or secondary

Do not promote secondary summaries as citations unless the primary source is unavailable and that limitation is explicit.

## Step 3: Verify Identity

Before adding a source to `literature-ledger.md`, verify:

- title matches the linked source
- authors/year are correct
- URL points to primary source where possible
- arXiv/OpenReview/proceedings version is clear
- the paper is actually relevant to Agentc

If not verified, status remains `candidate`.

## Step 4: Normalize Citation

Add or update `bibliography-ledger.md` with:

- citation key
- title
- authors/year
- venue/source
- DOI/arXiv/OpenReview/proceedings link
- BibTeX status
- date accessed

## Step 5: Map To Claims And Gaps

For each verified source, decide:

- which `CLM-###` it supports
- which `GAP-###` it helps close
- which claim it challenges
- whether it belongs in nearest-neighbor comparison
- whether it changes venue fit

## Step 6: Promote Or Discard

Promotion destinations:

- `literature-ledger.md` for verified source metadata
- `related-work-map.md` for conceptual placement
- `nearest-neighbor-comparison.md` for closest systems
- `citation-gap-list.md` for still-missing citations
- `claim-bank.md` for supported/challenged claims
- `paper-gap-register.md` for new risks or evidence gaps
- `idea-bank.md` for paper angles or experiments

Discard or archive sources that are unrelated, non-primary, duplicated, or misleading. Record important failed searches in `negative-results-ledger.md` once that file exists.

## Promotion Criteria

| Status | Meaning |
|---|---|
| `raw` | Captured but not processed |
| `triaged` | Sources extracted |
| `candidate` | Source seems relevant but not verified |
| `verified` | Primary source checked |
| `mapped` | Linked to claims/gaps/positioning |
| `cited` | Ready for bibliography use |
| `discarded` | Not useful or not comparable |

## Required Output After Ingestion

Every ingestion pass ends with:

- raw drop path
- sources promoted
- claims updated
- gaps created or resolved
- citations still missing
- follow-up research tasks
