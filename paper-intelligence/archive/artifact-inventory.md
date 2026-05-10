---
title: Paper Intelligence Artifact Inventory
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Artifact Inventory

This inventory tracks paper-relevant source artifacts, result files, scripts, specs, and generated research notes.

## Source Artifacts Imported From Repo Root

| ID | Path | Type | Source | SHA-256 | Status | Importance | Linked IDs | Notes |
|---|---|---|---|---|---|---|---|---|
| `ART-001` | `paper-intelligence/references/source/agentc-paper-reference-v2.md` | `md` | Messages attachment copied into repo root, then moved here | `0c16a6b1be3f3345d0e4e0f539d705e65acb3482a30ae6d24fe7ec773b227eee` | `source` | high | `RES-001`, `RES-002`, `RES-003`, `GAP-001` | Master paper reference, table/figure plan, and experiment interpretation. |
| `ART-002` | `paper-intelligence/references/source/agentc-feedback.md` | `md` | Local root artifact | `bbcc0876311ef9a2c61838fab60eaf4e0487a0ed25ed88d24e38ef1cac73d7d9` | `source` | medium | `GAP-001` | Feedback/research critique context. |
| `ART-003` | `paper-intelligence/references/source/agentc-response.pdf` | `pdf` | Local root artifact | `0fed0bf36308e080effc72def1f706682f7759ba15b9f2ba833c08df8c305a2d` | `source` | medium | `GAP-001` | Response artifact; needs summary if it will inform claims. |
| `ART-004` | `paper-intelligence/references/source/readme-local-before-upstream.md` | `md` | Local README preserved before upstream checkout | `522fc9dc5946968a51c39f756a5609c0824ac603390eb1b4f43443d90f9a0cb8` | `source` | low | none | Older local framing; needs triage before use. |

## Canonical Result Artifacts

| ID | Path | Type | Source | Checksum | Status | Importance | Linked IDs | Notes |
|---|---|---|---|---|---|---|---|---|
| `ART-010` | `bench/paper_results/long_context_qa-contextcompress-n100.csv` | `csv` | committed repo result | pending | `tracked` | high | `RES-001`, `CLM-002` | ContextCompress isolation matrix, 11 data rows. |
| `ART-011` | `bench/paper_results/gaia_router-modeldowngrade-n127.csv` | `csv` | committed repo result | pending | `tracked` | high | `RES-002`, `CLM-003` | ModelDowngrade isolation matrix, 11 data rows. |
| `ART-012` | `bench/paper_results/iterative_refiner-statedrop-n30.csv` | `csv` | committed repo result | pending | `tracked` | medium | `RES-003` | StateDrop n=30 matrix. |
| `ART-013` | `bench/paper_results/iterative_refiner-statedrop-n50-partial10of11.csv` | `csv` | committed repo result | pending | `tracked` | high | `RES-004`, `GAP-002` | StateDrop n=50 partial matrix, 10 data rows. |
| `ART-014` | `bench/paper_results/hotpot_real-contextcompress-n300-partial7of11.csv` | `csv` | committed repo result | pending | `tracked` | high | `RES-005`, `GAP-003` | Real HotpotQA ContextCompress partial matrix, 7 data rows. |
| `ART-015` | `bench/paper_results/hotpot_oracle-n300.csv` | `csv` | committed repo result | pending | `tracked` | high | `RES-006` | Oracle/manual-compression baseline. |

## Core Implementation Artifacts

| ID | Path | Type | Source | Checksum | Status | Importance | Linked IDs | Notes |
|---|---|---|---|---|---|---|---|---|
| `ART-020` | `crates/agentc-optimizer/src/planner.rs` | `code` | repo | pending | `tracked` | high | `CLM-001` | Planner hot threshold, rule ranking, pass-through behavior. |
| `ART-021` | `crates/agentc-optimizer/src/rules/context_compress.rs` | `code` | repo | pending | `tracked` | high | `CLM-002` | ContextCompress implementation. |
| `ART-022` | `crates/agentc-optimizer/src/rules/model_downgrade.rs` | `code` | repo | pending | `tracked` | high | `CLM-003` | ModelDowngrade implementation. |
| `ART-023` | `crates/agentc-optimizer/src/rules/state_drop.rs` | `code` | repo | pending | `tracked` | high | `CLM-004` | StateDrop implementation. |
| `ART-024` | `python/agentc/_intercept.py` | `code` | repo | pending | `tracked` | high | `CLM-001` | Python interception flow. |
| `ART-025` | `python/agentc/_optimizer.py` | `code` | repo | pending | `tracked` | high | `CLM-001` | Python optimizer FFI shim. |

## Paper Intelligence Artifacts

| ID | Path | Type | Source | Checksum | Status | Importance | Linked IDs | Notes |
|---|---|---|---|---|---|---|---|---|
| `ART-040` | `paper-intelligence/agentc-paper-intelligence-workplan.md` | `md` | created locally | pending | `tracked-candidate` | high | none | Master workplan. |
| `ART-041` | `paper-intelligence/deep-research-prompt-templates.md` | `md` | created locally | pending | `tracked-candidate` | high | none | Reusable deep research and venue prompts. |
| `ART-042` | `paper-intelligence/README.md` | `md` | created locally | pending | `tracked-candidate` | high | none | Directory orientation. |
| `ART-043` | `paper-intelligence/AGENTS.md` | `md` | created locally | pending | `tracked-candidate` | high | none | Agent operating rules. |
| `ART-044` | `paper-intelligence/pizza-import-plan.md` | `md` | adapted locally from Pizza structure | pending | `tracked-candidate` | medium | `DEC-003` | Import/skip decisions for reusable paper-process patterns. |
| `ART-045` | `paper-intelligence/reviewer-risk-register.md` | `md` | created locally | pending | `tracked-candidate` | high | `RR-001` | Reviewer objections and mitigation paths. |
| `ART-046` | `paper-intelligence/weak-point-resolution-plan.md` | `md` | created locally | pending | `tracked-candidate` | high | `WP-001` | Ordered work items for known weak points. |
| `ART-047` | `paper-intelligence/red-team-review-prompts.md` | `md` | created locally | pending | `tracked-candidate` | medium | `RR-001` | Reusable adversarial review prompts. |
| `ART-048` | `paper-intelligence/paper-angle-matrix.md` | `md` | created locally | pending | `tracked-candidate` | high | `ANG-001` | Candidate paper framings and decision criteria. |
| `ART-049` | `paper-intelligence/section-briefs/` | `directory` | created locally | pending | `tracked-candidate` | high | `CLM-001`, `RES-001` | Human-writing briefs by paper area. |
| `ART-050` | `paper-intelligence/citation-style-and-hygiene.md` | `md` | created locally | pending | `tracked-candidate` | medium | `CIT-001` | Citation verification rules. |
| `ART-051` | `paper-intelligence/research-inbox/2026-05-09-literature-map.md` | `generated-note` | user-pasted deep research | pending | `generated` | high | `DRP-001`, `LIT-002`, `GAP-010` | Condensed intake note for literature map. |
| `ART-052` | `paper-intelligence/research-inbox/2026-05-09-venue-research.md` | `generated-note` | user-pasted deep research plus spot-checks | pending | `generated` | high | `DRP-002`, `VEN-001`, `VEN-009` | Condensed intake note for venue research. |
| `ART-053` | `paper-intelligence/style-guide.md` | `md` | created locally | pending | `tracked-candidate` | medium | `CIT-002`, `RR-009` | Literature-review style guide and routing-framework comparison. |
| `ART-054` | `paper-intelligence/literature-review-section-plan.md` | `md` | created locally | pending | `tracked-candidate` | high | `LIT-002`, `GAP-009` | Whole-paper literature review structure. |
| `ART-055` | `paper-intelligence/research-inbox/2026-05-09-post-june-venue-plan.md` | `generated-note` | user-pasted deep research | pending | `generated` | high | `DRP-003`, `VEN-001`, `VEN-009`, `VEN-010` | Condensed intake note for post-June venue plan; facts need official verification before promotion. |
| `ART-056` | `paper-intelligence/literature-blurb-todo.md` | `md` | created locally from current `LIT` candidates | pending | `tracked-candidate` | high | `LIT-002`, `LIT-040`, `GAP-009`, `GAP-010`, `GAP-012`, `GAP-013`, `GAP-014`, `GAP-016` | Original normalized source-blurb checklist for `LIT-002` through `LIT-070`; superseded by `literature-verified-blurbs.md` for citation-use wording. |
| `ART-057` | `paper-intelligence/research-inbox/2026-05-09-full-literature-review-map-v2.md` | `generated-note` | user-pasted deep research | pending | `generated` | high | `DRP-004`, `LIT-040`, `GAP-010`, `GAP-012`, `GAP-013`, `GAP-014` | Condensed intake note for second full-paper literature map. |
| `ART-058` | `paper-intelligence/literature-verified-blurbs.md` | `md` | primary-source verification pass via 10 literature shards | pending | `tracked-candidate` | high | `LIT-002`, `LIT-070`, `GAP-009`, `GAP-010`, `GAP-012`, `GAP-013`, `GAP-014` | Checked citation-use blurbs, differentiation notes, baseline decisions, evidence/threat scores, and metadata corrections for all 69 active literature sources. |
| `ART-059` | `paper-intelligence/current-fit-and-publishability.md` | `md` | created after literature verification and result review | pending | `tracked-candidate` | high | `RES-001`, `RES-002`, `RES-005`, `LIT-008`, `LIT-024`, `LIT-040`, `GAP-010`, `GAP-011`, `GAP-014` | Concise current-state memo explaining how experiments fit the literature and what publication level is realistic. |

## Next Inventory Actions

- Fill checksums for committed CSV/code artifacts if they become direct source references in the paper.
- Summarize `ART-002` and `ART-003` before using them for claims.
- Add new deep research outputs as `generated-note` artifacts before promotion.
