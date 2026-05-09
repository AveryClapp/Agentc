---
title: Paper Intelligence Consolidation Spec
status: draft
last-updated: 2026-05-09
owner: paper-intelligence
---

# Paper Intelligence Consolidation Spec

## Goal

Condense `paper-intelligence/` from a broad scaffold of many small ledgers into a small, useful research workspace that Avery or William can understand after a pull.

The target folder should answer four questions quickly:

1. What current work is closest to AgentC?
2. Where is AgentC actually different?
3. Which evidence gaps would a reviewer attack?
4. Which literature checks, experiments, or venue decisions should happen next?

This is a refactor of research organization, not a deletion of paper context. The plan must preserve all useful IDs, claims, results, gaps, and source links.

## Current Problem

`paper-intelligence/` currently has 55 Markdown files:

- 44 top-level Markdown files.
- 7 files under `section-briefs/`.
- 3 files under `research-inbox/`.
- 1 file under `experiments/`.
- 4 source artifacts under `references/source/`.

The content is mostly useful, but the information architecture is too granular. Many files are 15-35 lines and represent one narrow table: claims, gaps, weak points, title ideas, question backlog, citation gaps, experiment candidates, and so on. That makes the folder feel larger and less trustworthy than the underlying content deserves.

## Non-Goals

- Do not write the paper.
- Do not delete original source artifacts in `references/source/`.
- Do not discard raw deep-research drops in `research-inbox/`.
- Do not remove stable IDs such as `LIT-###`, `CLM-###`, `GAP-###`, `RES-###`, `RR-###`, or `EXP-###`.
- Do not turn archived planning files into active guidance.
- Do not refactor experiment code or result CSVs as part of this cleanup.

## Target Shape

Keep a small active surface:

```text
paper-intelligence/
  README.md
  AGENTS.md
  handoff.md
  literature-review.md
  nearest-neighbors.md
  claims-and-gaps.md
  results-and-experiments.md
  venues.md
  reviewer-risks.md
  deep-research-prompts.md
  evidence-and-sources.md
  archive/
    README.md
    ...
  references/source/
    ...
  research-inbox/
    ...
```

`AGENTS.md` stays because it is a useful agent convention, but it should be short and point to the consolidated files.

## Target File Responsibilities

### `README.md`

Purpose: user-facing orientation.

Should contain:

- Purpose of the folder.
- Fast read order.
- Active file list.
- Promotion rule for new research.
- Clear warning that this is not a manuscript.

Sources to merge or reference:

- Existing `README.md`.
- Small pieces of `metadata-schemas.md`.
- Small pieces of `handoff.md`.

### `AGENTS.md`

Purpose: operating rules for agents touching this folder.

Should contain:

- Do not draft final prose by default.
- Preserve IDs.
- Keep raw research separate from verified ledgers.
- Update the right consolidated file when changing claims, gaps, literature, results, or venues.

Sources to merge or reference:

- Existing `AGENTS.md`.
- Relevant rules from `metadata-schemas.md`.
- Relevant rules from `citation-style-and-hygiene.md`.

### `handoff.md`

Purpose: one-page current state for William/Avery.

Should contain:

- Current framing: AgentC as runtime optimizer for compound AI / multi-step LLM traces.
- What is useful already.
- What is weak.
- Next 5 actions.
- Pointers to the consolidated files.

Sources to merge:

- Existing `handoff.md`.
- `decision-log.md`.
- Current recommendation from `paper-angle-matrix.md`.
- Current status from `weak-point-resolution-plan.md`.

### `literature-review.md`

Purpose: whole-paper literature map.

Should contain:

- Section shape for the full related work.
- Literature ledger table or condensed ledger preserving `LIT-###` IDs.
- A normalized blurb for each literature item, or an explicit queue entry saying the blurb is still needed.
- Each blurb should answer: what the work offers, how AgentC compares, what reviewer risk it creates, and what action it implies.
- Citation gaps preserving `CIT-###` IDs.
- Bibliography readiness status.
- Ingestion workflow summarized in 10-15 lines.
- Note that routing is one subsection, not the paper thesis.
- A `Must-Verify Before Novelty Claims` queue.
- A `StateDrop/Program-Analysis Search Queue`.
- A `Stochastic-Eval Search Queue`.
- A `Serving-Orthogonality Search Queue`.

Current status: the first normalized candidate pass exists in `paper-intelligence/literature-blurb-todo.md`, and the primary-source checked pass exists in `paper-intelligence/literature-verified-blurbs.md`. Together they cover `LIT-002` through `LIT-070`, including the original `DRP-001` sources and the new `DRP-004` sources.

Sources to merge:

- `literature-review-section-plan.md`.
- `literature-ledger.md`.
- `literature-blurb-todo.md`.
- `literature-verified-blurbs.md`.
- `related-work-map.md`.
- `bibliography-ledger.md`.
- `citation-gap-list.md`.
- `citation-style-and-hygiene.md`.
- `literature-ingestion-workflow.md`.
- Literature-related parts of `style-guide.md`.

## Literature Blurb Protocol

After new deep-research results arrive, every source that survives triage should get a concise normalized blurb. The goal is not to summarize papers for its own sake. The goal is to decide what each source does to AgentC's positioning.

### Per-Source Blurb Template

Each `LIT-###` entry should eventually have:

```text
LIT-###: Short title
Status: candidate | metadata-verified | claim-verified | bib-ready | cited | discarded
Cluster: compound systems | runtime optimization | routing | compression | state/liveness | caching | parallelism | serving | evaluation | venue/context
One-line takeaway:
What it offers:
How AgentC compares:
Reviewer risk:
How we use it:
Baseline decision: run | cite-only | not-comparable | unknown
Evidence strength: 1-5
Novelty threat: 1-5
Action: verify metadata | read claim | add to nearest-neighbors | add to gap | run baseline | discard
```

### Required Blurb Fields

| Field | Purpose |
|---|---|
| `One-line takeaway` | The plain-English point William/Avery should remember. |
| `What it offers` | The actual contribution of the source, not generic topic tags. |
| `How AgentC compares` | Similarity and difference in one or two concrete sentences. |
| `Reviewer risk` | The objection this source enables, if any. |
| `How we use it` | Must-cite background, direct comparison, baseline, limitation support, evaluation support, or discard. |
| `Baseline decision` | Whether we may need to run it, only cite it, or mark it non-comparable. |
| `Evidence strength` | How reliable/useful the source is for our paper. |
| `Novelty threat` | How dangerous it is to the novelty claim. |
| `Action` | The next concrete step for this source. |

### Scoring Rubric

Use small integer scores. The point is triage, not false precision.

#### Evidence Strength

| Score | Meaning |
|---:|---|
| 1 | Weak background, bloggy, vague, or only tangential. |
| 2 | Relevant but not central, or unverified. |
| 3 | Useful supporting citation for a cluster. |
| 4 | Strong must-cite or strong evaluation/methodology support. |
| 5 | Central anchor or direct baseline that can shape paper claims. |

#### Novelty Threat

| Score | Meaning |
|---:|---|
| 1 | Background only; no threat. |
| 2 | Same area but different problem level. |
| 3 | Overlaps one rewrite family or evaluation concern. |
| 4 | Close systems/method comparison; needs explicit contrast. |
| 5 | Direct nearest neighbor; could force the novelty claim to narrow. |

#### Baseline Feasibility

| Label | Meaning |
|---|---|
| `run` | We should seriously consider executing it or reproducing a comparable baseline. |
| `cite-only` | Important comparison, but not realistic or necessary to run. |
| `not-comparable` | Useful background but not an evaluation baseline. |
| `unknown` | Need more reading before deciding. |

### Source Priority Formula

Use this as a rough sort key for human attention:

```text
priority = novelty_threat + evidence_strength + baseline_bonus + unresolved_gap_bonus
```

Where:

- `baseline_bonus = 2` if baseline decision is `run`, `1` if `unknown`, `0` otherwise.
- `unresolved_gap_bonus = 2` if it touches `GAP-010`, `GAP-012`, `GAP-013`, `GAP-014`, or `GAP-016`.

Interpretation:

- `9+`: read/verify immediately.
- `6-8`: keep in the main related-work map.
- `3-5`: background or optional citation.
- `<3`: archive or discard unless needed later.

### Cluster-Level Synthesis

After individual blurbs, write one short cluster blurb for each literature area:

```text
Cluster:
What this literature already establishes:
What it does not cover:
What AgentC can safely claim:
What AgentC must avoid claiming:
Must-cite sources:
Closest novelty threats:
Open gaps:
```

Clusters to synthesize:

1. Compound AI systems and agent frameworks.
2. Runtime optimization for LLM applications.
3. Model routing/cascades/model selection.
4. Prompt/context compression.
5. State pruning, memory management, liveness, data-flow, and slicing.
6. Semantic caching, memoization, and cache correctness.
7. Tool-call scheduling, parallel execution, and dependency/side-effect analysis.
8. Serving/inference systems and orthogonality.
9. Stochastic LLM evaluation and reliability.

### Whole-Literature High-Level Blurb

After the cluster blurbs, draft a single high-level related-work positioning blurb. This should be 1-2 paragraphs, not manuscript prose. It should answer:

- What does the literature make obvious?
- What is already solved by prior work?
- What is fragmented across separate lines of work?
- Where does AgentC plausibly contribute?
- What claim would be too strong?
- What evidence would most improve the paper?

Working shape:

```text
The literature supports framing AgentC as a runtime optimizer for compound AI systems rather than as a routing, compression, or caching paper. Prior work strongly covers individual mechanisms: routing/cascades, prompt compression, semantic caching, parallel tool execution, and serving-level inference optimization. The gap is that these lines are mostly separate, and many operate at the query, prompt, workflow, or model-server layer rather than as one transparent runtime control plane over multi-step agent traces.

AgentC should therefore claim integration and trace-level runtime control carefully, not novelty for the individual tricks. The most important unresolved risks are nearest-neighbor systems such as Autellix/Halo, StateDrop's need for liveness/program-analysis grounding, and evaluation credibility under stochastic LLM behavior.
```

This blurb is a planning artifact. It should inform human writing but should not be pasted into the paper without human editing.

### `nearest-neighbors.md`

Purpose: differentiation against closest work.

Should contain:

- Ranked nearest-neighbor table.
- Per-neighbor comparison axes: optimization target, intervention point, runtime/offline, agent scope, quality metric, AgentC distinction.
- Verification status.
- Threat level.
- Baseline decision: `run`, `cite-only`, `not-comparable`, or `unknown`.
- Experiment implication, especially for `EXP-008`.
- A section for the four routing/model-selection comparators.
- A section for "closest systems threats" such as Autellix, Halo, SGLang, DSPy, and LLMCompiler.
- Safe novelty wording and forbidden novelty wording.

Sources to merge:

- `nearest-neighbor-comparison.md`.
- `positioning-taxonomy.md`.
- Differentiation sections from `style-guide.md`.
- Relevant rows from `paper-angle-matrix.md`.
- Relevant risks from `reviewer-risk-register.md`.

### `claims-and-gaps.md`

Purpose: canonical truth table for what the paper can claim and what blocks those claims.

Should contain:

- Claim bank preserving `CLM-###` IDs.
- Gap register preserving `GAP-###` IDs.
- Weak-point work items preserving `WP-###` IDs.
- Open questions preserving `QST-###` IDs.
- Clear "do not say" section.

Sources to merge:

- `claim-bank.md`.
- `paper-gap-register.md`.
- `weak-point-resolution-plan.md`.
- `question-backlog.md`.
- Claim-related parts of `manual-writing-brief.md`.
- Unsafe wording notes from `title-and-abstract-idea-bank.md`.

### `results-and-experiments.md`

Purpose: current results plus what to run next.

Should contain:

- Results ledger preserving `RES-###` IDs.
- Experiment priority board preserving `EXP-###` IDs.
- Experiment run log preserving `RUN-###` IDs if any exist.
- Result validation checklist summarized.
- Statistical analysis plan.
- Reproduction commands.
- Artifact evaluation notes.
- Negative results.

Sources to merge:

- `results-ledger.md`.
- `experiment-priority-board.md`.
- `experiment-run-log.md`.
- `result-validation-checklist.md`.
- `statistical-analysis-plan.md`.
- `reproduction-commands.md`.
- `artifact-evaluation-plan.md`.
- `negative-results-ledger.md`.
- `experiments/README.md`.
- Results-related section briefs.

### `venues.md`

Purpose: venue strategy.

Should contain:

- Top venue lanes: ATC, MLSys, COLM.
- Post-June options and why they are weaker.
- Venue family matrix preserving `VEN-###` IDs.
- Paper-angle decision surface preserving `ANG-###` IDs where venue-dependent.
- Evidence requirements per venue.
- Next venue decision.

Sources to merge:

- `venue-positioning-matrix.md`.
- Venue sections from `paper-angle-matrix.md`.
- Venue prompts from `deep-research-prompt-templates.md`.
- Venue/evidence tradeoffs from `outline-options.md`.

### `reviewer-risks.md`

Purpose: reviewer objection and red-team surface.

Should contain:

- Reviewer risk register preserving `RR-###` IDs.
- Red-team prompts.
- Limitations brief.
- Cheapest mitigation and strongest mitigation per major risk.
- Explicit "what could sink the paper" section.

Sources to merge:

- `reviewer-risk-register.md`.
- `red-team-review-prompts.md`.
- `section-briefs/limitations.md`.
- Risk-related parts of `weak-point-resolution-plan.md`.

### `deep-research-prompts.md`

Purpose: reusable prompts to send to deep research tools.

Should contain:

- Full-paper literature review prompt.
- Venue sweep prompt.
- Routing comparator prompt.
- Digest-to-repo prompt.
- Compressed versions of these older prompts if still useful:
  - closest related work and differentiation;
  - rule-specific literature review;
  - evaluation methodology and reviewer risk;
  - citation gap filler;
  - conference and venue fit;
  - workshop finder;
  - post-June venue sweep;
  - full literature sweep;
  - routing comparators;
  - digest-to-repo.

Sources to merge:

- `deep-research-prompt-templates.md`.
- `deep-research-inbox.md` should not merge fully here; it belongs as a short index in `evidence-and-sources.md` or `literature-review.md`.

### `evidence-and-sources.md`

Purpose: provenance, repo source pointers, and evidence inventory.

Should contain:

- Artifact inventory preserving `ART-###` IDs.
- For archived generated artifacts, record `original_path` and `archived_path`.
- Repo source map.
- Quote/evidence bank preserving `QTE-###` IDs.
- Figure/table idea bank preserving `FIG-###` IDs.
- Figure idea IDs can stay here; they are evidence/planning pointers, not active writing tasks.
- Raw research drop index preserving `DRP-###` IDs.
- Pointers to `references/source/` and `research-inbox/`.

Sources to merge:

- `artifact-inventory.md`.
- `repo-source-map.md`.
- `quote-and-evidence-bank.md`.
- `figure-idea-bank.md`.
- `deep-research-inbox.md`.
- `research-inbox/README.md`.

### `handoff.md` Strategy Sections

`handoff.md` should also preserve lightweight strategy state that would otherwise make `claims-and-gaps.md` too broad:

- Decision ledger summary preserving `DEC-###` IDs.
- Top paper angles preserving `ANG-###` IDs.
- Important idea rows preserving `IDEA-###` IDs when they affect framing or next action.
- Next 5 actions in priority order.

## Archive Policy

After merging content into active files:

- Move superseded files to `paper-intelligence/archive/`.
- Add `paper-intelligence/archive/README.md` explaining that archived files are historical inputs.
- Do not leave both the active consolidated file and the old source file as competing guidance.
- Preserve relative paths under archive to avoid collisions:
  - `section-briefs/README.md` becomes `archive/section-briefs/README.md`.
  - `experiments/README.md` becomes `archive/experiments/README.md`.
- Keep raw drops and imported source artifacts out of archive:
  - `research-inbox/` remains raw generated research.
  - `references/source/` remains original source artifacts.

Archived files should get either:

- a short frontmatter status update to `archive`, if they remain readable as standalone notes; or
- an archive README mapping, if editing every archived file would create churn.

Preferred low-churn approach: archive README mapping plus no per-file edits unless needed.

## Proposed Consolidation Mapping

| Current file | Target |
|---|---|
| `README.md` | keep, rewrite as orientation |
| `AGENTS.md` | keep, shorten |
| `handoff.md` | keep, rewrite as current-state brief |
| `metadata-schemas.md` | merge essentials into `README.md`, `AGENTS.md`, and active files |
| `agentc-paper-intelligence-workplan.md` | archive after extracting north star and completed setup decisions |
| `artifact-evaluation-plan.md` | merge into `results-and-experiments.md` |
| `artifact-inventory.md` | merge into `evidence-and-sources.md` |
| `bibliography-ledger.md` | merge into `literature-review.md` |
| `citation-gap-list.md` | merge into `literature-review.md` |
| `citation-style-and-hygiene.md` | merge into `literature-review.md` and `AGENTS.md` |
| `claim-bank.md` | merge into `claims-and-gaps.md` |
| `decision-log.md` | merge concise `DEC-###` summary into `handoff.md`; archive full source |
| `deep-research-inbox.md` | merge index into `evidence-and-sources.md`; keep raw drops in `research-inbox/` |
| `deep-research-prompt-templates.md` | merge selected prompts into `deep-research-prompts.md` |
| `experiment-priority-board.md` | merge into `results-and-experiments.md` |
| `experiment-run-log.md` | merge into `results-and-experiments.md` |
| `figure-idea-bank.md` | merge into `evidence-and-sources.md` |
| `idea-bank.md` | merge `IDEA-001` and `IDEA-002` into `handoff.md`; merge `IDEA-003` into `evidence-and-sources.md`; archive remainder |
| `idea-generation-protocol.md` | archive after extracting any useful recurrence rule to `handoff.md` or `AGENTS.md` |
| `literature-ingestion-workflow.md` | summarize into `literature-review.md` |
| `literature-ledger.md` | merge into `literature-review.md` |
| `literature-blurb-todo.md` | merge into `literature-review.md` as the first normalized source-blurb pass |
| `literature-review-section-plan.md` | merge into `literature-review.md` |
| `manual-writing-brief.md` | merge manual-writing boundary into `README.md`; merge claim/do-not-say rules into `claims-and-gaps.md`; merge next inputs into `handoff.md` |
| `nearest-neighbor-comparison.md` | merge into `nearest-neighbors.md` |
| `negative-results-ledger.md` | merge into `results-and-experiments.md` |
| `outline-options.md` | extract venue/evidence tradeoffs into `venues.md`; archive full source |
| `paper-angle-matrix.md` | preserve `ANG-###` decision surface in `handoff.md` and venue mapping in `venues.md` |
| `paper-gap-register.md` | merge into `claims-and-gaps.md` |
| `pizza-import-plan.md` | archive; it is historical process context |
| `positioning-taxonomy.md` | merge into `nearest-neighbors.md` |
| `question-backlog.md` | merge into `claims-and-gaps.md` |
| `quote-and-evidence-bank.md` | merge into `evidence-and-sources.md` |
| `red-team-review-prompts.md` | merge into `reviewer-risks.md` |
| `related-work-map.md` | merge into `literature-review.md` |
| `repo-source-map.md` | merge into `evidence-and-sources.md` |
| `reproduction-commands.md` | merge into `results-and-experiments.md` |
| `result-validation-checklist.md` | merge into `results-and-experiments.md` |
| `results-ledger.md` | merge into `results-and-experiments.md` |
| `reviewer-risk-register.md` | merge into `reviewer-risks.md` |
| `statistical-analysis-plan.md` | merge into `results-and-experiments.md` |
| `style-guide.md` | split useful rules into `literature-review.md`, `nearest-neighbors.md`, and `AGENTS.md` |
| `title-and-abstract-idea-bank.md` | extract unsafe wording and title-angle constraints into `claims-and-gaps.md` and `handoff.md`; archive full source |
| `venue-positioning-matrix.md` | merge into `venues.md` |
| `weak-point-resolution-plan.md` | merge into `claims-and-gaps.md` and `reviewer-risks.md` |
| `section-briefs/README.md` | archive |
| `section-briefs/contribution-framing.md` | merge into `claims-and-gaps.md` or `handoff.md` |
| `section-briefs/limitations.md` | merge into `reviewer-risks.md` |
| `section-briefs/methodology.md` | merge into `results-and-experiments.md` |
| `section-briefs/related-work.md` | merge into `literature-review.md` |
| `section-briefs/results.md` | merge into `results-and-experiments.md` |
| `section-briefs/system-and-rules.md` | merge rule/mechanism claims into `claims-and-gaps.md`; merge code pointers into `evidence-and-sources.md` |
| `experiments/README.md` | merge into `results-and-experiments.md`; remove empty dir if no other files |

## Preserve These Details

Do not lose:

- `RES-001`: ContextCompress headline result.
- `RES-002`: ModelDowngrade headline result.
- `RES-003` and `RES-004`: StateDrop caveated results.
- `RES-005`: real HotpotQA activation-boundary result.
- `RES-006`: oracle compression diagnostic with caveat.
- All `LIT-###` candidate rows and their verification status.
- All `GAP-###` rows, especially `GAP-010` through `GAP-016`.
- All `RR-###` rows, especially routing/compression/cache/parallel/serving/stochastic-eval risks.
- All `AE-###`, `NEG-###`, and `STAT-###` rows.
- `VEN-009`, `VEN-001`, and `VEN-010` venue recommendations.
- `ANG-###` rows from the angle matrix.
- The rule that routing is not the paper thesis.
- The manual writing boundary: agents prepare evidence and briefs; humans write final prose.
- Raw research drops and source references.

## Execution Plan

### Phase 0: Snapshot current untracked tree

The current `paper-intelligence/` directory is untracked. Before moving files, create a safety snapshot or commit the current tree. Do not rely on `git mv` history until the files are tracked.

Minimum preflight:

```sh
git status --short
ID_RE='(ART|AE|RES|CLM|GAP|LIT|CIT|EXP|RUN-[0-9]{8}|VEN|FIG|DEC|QST|IDEA|DRP|ANG|RR|WP|TTL|QTE|NEG|STAT)-[0-9]{3}'
rg -o --no-filename "\\b$ID_RE\\b" paper-intelligence | sort -u > /tmp/pi.ids.before
find paper-intelligence -type f -print0 | sort -z | xargs -0 shasum -a 256 > /tmp/pi.sha256.before
find paper-intelligence/references/source paper-intelligence/research-inbox -type f -print0 | sort -z | xargs -0 shasum -a 256 > /tmp/pi.provenance.sha256.before
```

### Phase 1: Create consolidated files

Create the new active files while leaving old files in place:

- `literature-review.md`
- `nearest-neighbors.md`
- `claims-and-gaps.md`
- `results-and-experiments.md`
- `venues.md`
- `reviewer-risks.md`
- `deep-research-prompts.md`
- `evidence-and-sources.md`

Rewrite:

- `README.md`
- `AGENTS.md`
- `handoff.md`

### Phase 2: Verify no key IDs are lost

Run checks that every existing concrete ID still appears somewhere after the refactor. Prefix presence is not enough.

- `ART`
- `AE`
- `RES`
- `CLM`
- `GAP`
- `LIT`
- `CIT`
- `EXP`
- `RUN-YYYYMMDD`
- `VEN`
- `FIG`
- `DEC`
- `QST`
- `IDEA`
- `DRP`
- `ANG`
- `RR`
- `WP`
- `TTL`
- `QTE`
- `NEG`
- `STAT`

Also run an active-surface check excluding `archive/`. Any missing active ID must be listed in an explicit archive-only allowlist.

```sh
rg -o --no-filename "\\b$ID_RE\\b" paper-intelligence | sort -u > /tmp/pi.ids.after
comm -23 /tmp/pi.ids.before /tmp/pi.ids.after

rg -o --no-filename "\\b$ID_RE\\b" paper-intelligence --glob '!archive/**' | sort -u > /tmp/pi.ids.active
comm -23 /tmp/pi.ids.before /tmp/pi.ids.active
```

The first `comm` must be empty. The second may only contain IDs intentionally preserved as archive-only history, and those exceptions must be documented in `archive/README.md`.

### Phase 3: Archive superseded files

Create:

- `paper-intelligence/archive/README.md`

Move superseded files into `archive/`.

Do not move:

- active target files;
- `references/source/*`;
- `research-inbox/*`;
- any future raw deep-research output.

### Phase 4: Link cleanup

Update path references in:

- `README.md`
- `AGENTS.md`
- `handoff.md`
- active consolidated files
- root `README.md`
- root `CLAUDE.md`

Remove or rewrite references to deleted/superseded old files.

Generate the stale-file check from the consolidation mapping rather than a hand-picked list. Active files must not point to archived file paths except inside `archive/README.md`.

### Phase 5: Final validation

Run:

```sh
find paper-intelligence -type f | sort

find paper-intelligence/references/source paper-intelligence/research-inbox -type f -print0 | sort -z | xargs -0 shasum -a 256 > /tmp/pi.provenance.sha256.after
diff -u /tmp/pi.provenance.sha256.before /tmp/pi.provenance.sha256.after

rg -n "paper-intelligence/(agentc-paper-intelligence-workplan|artifact-evaluation-plan|artifact-inventory|bibliography-ledger|citation-gap-list|citation-style-and-hygiene|claim-bank|decision-log|deep-research-inbox|deep-research-prompt-templates|experiment-priority-board|experiment-run-log|figure-idea-bank|idea-bank|idea-generation-protocol|literature-ingestion-workflow|literature-ledger|literature-review-section-plan|manual-writing-brief|metadata-schemas|nearest-neighbor-comparison|negative-results-ledger|outline-options|paper-angle-matrix|paper-gap-register|pizza-import-plan|positioning-taxonomy|question-backlog|quote-and-evidence-bank|red-team-review-prompts|related-work-map|repo-source-map|reproduction-commands|result-validation-checklist|results-ledger|reviewer-risk-register|statistical-analysis-plan|style-guide|title-and-abstract-idea-bank|venue-positioning-matrix|weak-point-resolution-plan|section-briefs/|experiments/README)" README.md CLAUDE.md paper-intelligence --glob '!paper-intelligence/archive/**'
git status --short
```

Expected final active human-facing file count should be roughly 10-12, not 55. Total files may still be higher because archive, raw research drops, and source artifacts are preserved.

## Risks

| Risk | Mitigation |
|---|---|
| Losing detail during merge | Preserve IDs and copy tables before archiving old files. |
| Creating huge unreadable mega-files | Keep each consolidated file focused; use summaries plus tables, not pasted full workplans. |
| Breaking agent workflows that expect `AGENTS.md` | Keep `AGENTS.md` as a short active file. |
| Confusing raw research with verified literature | Keep `research-inbox/` separate and mark literature statuses clearly. |
| Hiding useful history | Use `archive/README.md` to map old files to new consolidated homes. |
| Refactor looks like deletion in git | Move files with `git mv` when practical, or clearly document archive moves. |

## Review Questions For Agents

1. Does the target structure match the actual purpose of paper intelligence?
2. Are any current files mapped to the wrong target?
3. Are any critical details at risk of being lost?
4. Is the archive policy clear enough?
5. Is the target still too many files, or too few?
6. What should change before executing the refactor?
