---
title: Paper Intelligence Decision Log
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Decision Log

| ID | Date | Decision | Rationale | Consequences |
|---|---|---|---|---|
| `DEC-001` | 2026-05-08 | Use `paper-intelligence`, not `paper-writeup`. | The directory supports human writing through research, gap finding, and evidence organization; agents should not draft final prose by default. | Files focus on ledgers, maps, prompts, and briefs. |
| `DEC-002` | 2026-05-08 | Keep manuscript scaffolding out of scope for now. | Repo guardrails discourage new top-level directories without discussion. | No `paper/agentc/` directory created. |
| `DEC-003` | 2026-05-08 | Move root paper artifacts into `references/source/`. | Root-level untracked paper context would be lost or overlooked. | Artifacts now have checksums and inventory entries. |
| `DEC-004` | 2026-05-08 | Use stable IDs across ledgers. | Prevents Markdown sprawl and lets claims link to evidence. | All new ledgers use prefixes defined in `metadata-schemas.md`. |
| `DEC-005` | 2026-05-08 | Adapt Pizza's paper-process structure, not its domain content. | The useful part is the review/citation/weak-point discipline; AgentC needs its own claims and evidence. | `pizza-import-plan.md`, reviewer-risk files, weak-point plan, and section briefs are AgentC-specific. |
| `DEC-006` | 2026-05-09 | Move Paper Intelligence out of `specs/` and into top-level `paper-intelligence/`. | `specs/` should mean implementation specs; Paper Intelligence is an active research/evidence workspace. | Internal paths now point to `paper-intelligence/`; `specs/` remains reserved for technical specs. |
