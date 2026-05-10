---
title: Citation Style And Hygiene
status: active
last-updated: 2026-05-08
owner: paper-intelligence
---

# Citation Style And Hygiene

This file defines how citation candidates become paper-ready evidence.

## Verification Levels

| Level | Meaning | Allowed use |
|---|---|---|
| `candidate` | Found in search or deep research, not checked. | Inbox only. |
| `metadata-verified` | Title, authors, year, and venue checked against a stable source. | Literature ledger and planning. |
| `claim-verified` | The relevant paper claim was checked in the source. | Claim bank and related-work map. |
| `bib-ready` | BibTeX or citation metadata is ready for manuscript use. | Future manuscript bibliography. |
| `cited` | Used in approved paper prose. | Manuscript only after approval. |

## Source Preference

Prefer primary sources:

- official proceedings page;
- arXiv or OpenReview page;
- publisher or project page;
- official documentation for frameworks/APIs.

Use secondary sources only as discovery leads. Do not use model-generated research output as a citation source by itself.

## Citation Keys

Use lowercase BibTeX-style keys:

`lastnameYYYYshorttopic`

Examples:

- `liu2023llmlingua`
- `madaan2023selfrefine`
- `shinn2023reflexion`

Exact keys can change later, but `bibliography-ledger.md` should record the current key and status.

## No-Go Claims Until Verified

- "first"
- "novel"
- "state of the art"
- "to our knowledge"
- "no prior work"
- "universally preserves quality"
- "drop-in for all agent systems"

These require direct related-work verification and reviewer-risk review before prose.

## Promotion Path

```text
deep research output -> research-inbox raw note -> literature-ledger candidate -> primary source check -> bibliography-ledger -> related-work-map / claim-bank
```

