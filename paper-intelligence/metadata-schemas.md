---
title: Paper Intelligence Metadata Schemas
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Metadata Schemas

This file defines stable IDs, statuses, and row shapes used across the paper intelligence hub.

## Frontmatter

Every Markdown file in this directory uses:

```yaml
---
title: Human-Readable Title
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---
```

Allowed `status` values: `draft`, `active`, `deprecated`, `archive`.

## ID Prefixes

| Prefix | Entity | Example |
|---|---|---|
| `ART` | Artifact | `ART-001` |
| `RES` | Result | `RES-001` |
| `CLM` | Claim | `CLM-001` |
| `GAP` | Gap, risk, missing evidence | `GAP-001` |
| `LIT` | Literature source | `LIT-001` |
| `CIT` | Citation gap | `CIT-001` |
| `EXP` | Experiment candidate | `EXP-001` |
| `RUN` | Experiment run | `RUN-20260508-001` |
| `VEN` | Venue or workshop | `VEN-001` |
| `FIG` | Figure/table idea | `FIG-001` |
| `DEC` | Decision | `DEC-001` |
| `QST` | Open question | `QST-001` |
| `IDEA` | Paper idea | `IDEA-001` |
| `DRP` | Deep research drop | `DRP-001` |
| `ANG` | Paper angle | `ANG-001` |
| `RR` | Reviewer risk | `RR-001` |
| `WP` | Weak-point work item | `WP-001` |
| `TTL` | Title or abstract idea | `TTL-001` |
| `QTE` | Quote or evidence-bank entry | `QTE-001` |

IDs are never reused. If an item is abandoned, mark it `discarded`, `superseded`, or `archive`.

## Artifact Row

| Field | Meaning |
|---|---|
| ID | `ART-###` |
| Path | Repo-relative or absolute path |
| Type | `csv`, `md`, `pdf`, `script`, `spec`, `source`, `generated-note`, `code`, `directory` |
| Source | Where it came from |
| Checksum | SHA-256 when applicable |
| Status | `source`, `tracked`, `local-only`, `generated`, `archive`, `needs-triage` |
| Importance | `high`, `medium`, `low` |
| Linked IDs | Related `RES`, `CLM`, `GAP`, `LIT`, or `EXP` |
| Notes | Short explanation |

## Result Row

| Field | Meaning |
|---|---|
| ID | `RES-###` |
| Status | `headline-ready`, `canonical`, `partial`, `diagnostic`, `appendix-only`, `quarantined`, `needs-rerun`, `do-not-use-yet` |
| Rule | Optimizer rule |
| Workload | Benchmark workload |
| n | Sample size |
| Model | Model(s) used |
| Source Artifact | `ART-###` and path |
| Headline Numbers | Cost/input-token/accuracy summary |
| Caveats | Known limitations |
| Supports Claims | `CLM-###` |
| Related Gaps | `GAP-###` |
| Validation | Checklist status |

## Claim Row

| Field | Meaning |
|---|---|
| ID | `CLM-###` |
| Status | `supported`, `promising`, `needs-analysis`, `needs-experiment`, `needs-citation`, `too-strong`, `discarded` |
| Claim | Proposed claim |
| Allowed Wording | Safe wording for human drafting |
| Forbidden Wording | Stronger wording to avoid |
| Evidence | `RES`, `ART`, `LIT`, or code path |
| Minimum Evidence To Publish | Required support before use |
| Caveats | What must travel with the claim |
| Related Gaps | `GAP-###` |

## Gap Row

| Field | Meaning |
|---|---|
| ID | `GAP-###` |
| Status | `open`, `in-progress`, `blocked`, `resolved`, `deferred`, `wont-fix` |
| Type | `result`, `method`, `citation`, `reviewer-risk`, `positioning`, `artifact`, `figure`, `prose-framing` |
| Severity | `blocker`, `high`, `medium`, `low` |
| Description | What is missing or risky |
| Blocks | Claims, venues, figures, or results |
| Fix Path | Experiment, literature, analysis, or framing |
| Owner | Person or agent if known |
| Next Action | Concrete next step |

## Literature Row

| Field | Meaning |
|---|---|
| ID | `LIT-###` |
| Status | `candidate`, `skimmed`, `read`, `verified`, `cited`, `discarded` |
| Citation Key | BibTeX-style key |
| Title | Paper/source title |
| Authors/Year | Citation metadata |
| Venue/Source | Conference, journal, arXiv, docs, etc. |
| Link | Primary source URL |
| Source Type | `paper`, `official-doc`, `cfp`, `blog`, `code`, `other` |
| Relevance | Why it matters to Agentc |
| Supports/Challenges | `CLM` or `GAP` IDs |
| Verification | Whether primary source was read |

## Deep Research Drop

| Field | Meaning |
|---|---|
| ID | `DRP-###` |
| Date | Date received |
| Topic | Research topic |
| Tool/Source | Perplexity, web, model, human, etc. |
| Raw Path | Where the raw drop is stored |
| Summary | Short digest |
| Promotion Status | `raw`, `triaged`, `partially-promoted`, `promoted`, `discarded` |
| Promoted To | Ledger IDs or files updated |
| Follow-Up | Open next steps |
