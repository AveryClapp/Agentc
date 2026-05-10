---
title: Paper Intelligence Consolidation Spec
status: active
last-updated: 2026-05-09
owner: paper-intelligence
---

# Paper Intelligence Consolidation Spec

## Goal

Turn `paper-intelligence/` from a broad research workbench into a small paper intelligence packet that William or Avery can understand after a pull.

The consolidated folder should answer four questions quickly:

1. What can AgentC credibly claim right now?
2. Which current work is closest, and how is AgentC different?
3. Which evidence gaps would a reviewer attack?
4. Which literature, experiments, or venue decisions should happen next?

This is an information-architecture refactor. It is not a paper-writing pass and not a deletion of research context.

## Current Snapshot

As of the local 2026-05-09 review:

- `paper-intelligence/` has 46 top-level Markdown files.
- It also has 7 `section-briefs/` files, 5 `research-inbox/` files, 1 `experiments/README.md`, and 3 Markdown source artifacts plus 1 PDF under `references/source/`.
- Three important files are currently untracked and must be preserved before any archive move:
  - `paper-intelligence/current-fit-and-publishability.md`
  - `paper-intelligence/literature-blurb-todo.md`
  - `paper-intelligence/literature-verified-blurbs.md`
- `paper-intelligence/handoff.md` is deleted and should not come back as an active file.

The strongest current files are:

- `current-fit-and-publishability.md` - best current-state and publishability summary.
- `literature-verified-blurbs.md` - best related-work and differentiation source.
- `results-ledger.md` - best experiment truth table.
- `paper-gap-register.md` - best missing-evidence register.
- `nearest-neighbor-comparison.md` - best novelty-threat matrix.

The main problem is source-of-truth drift. Newer verified files exist, but older ledgers still show `candidate`, `pending`, and `TBD` rows. The consolidation should promote verified content into canonical files, then archive old scaffolding so a reader does not have to guess which note is current.

## Non-Goals

- Do not write final manuscript prose.
- Do not delete `references/source/*`.
- Do not delete or rewrite raw generated research drops in `research-inbox/*`.
- Do not remove or renumber stable IDs.
- Do not refactor experiment code or benchmark CSVs.
- Do not treat raw venue or literature research as verified facts unless the consolidated doc labels it as such.

## Final Active Surface

Keep these active files:

```text
paper-intelligence/
  README.md
  AGENTS.md
  current-fit-and-publishability.md
  literature-and-nearest-neighbors.md
  claims-gaps-and-risks.md
  results-experiments-and-repro.md
  strategy-and-venues.md
  evidence-and-sources.md
  research-prompts.md
  archive/
    README.md
  references/source/
  research-inbox/
```

Keep `experiments/` only if it gains real `RUN-*.md` notes. Otherwise archive `experiments/README.md` and remove the empty directory.

## Reader Paths

**10 minutes**

1. `README.md`
2. `current-fit-and-publishability.md`
3. Top sections of `claims-gaps-and-risks.md`

**30 minutes**

1. 10-minute path.
2. `results-experiments-and-repro.md`
3. `literature-and-nearest-neighbors.md`
4. `strategy-and-venues.md`

**60 minutes**

1. 30-minute path.
2. Full `GAP-###`, `RR-###`, and `EXP-###` tables.
3. Literature cluster summaries and citation gaps.
4. `evidence-and-sources.md`.
5. `archive/README.md` only for provenance.

## Canonical File Responsibilities

### `README.md`

Front door only. It should say what the folder is, the current verdict, the active file list, the read paths, and the promotion rule.

Use direct wording:

```md
This folder keeps the current paper facts: what AgentC can claim, what evidence exists, which related work is dangerous, and what to do next. It is not a manuscript.
```

Avoid a long inventory of archived or raw files. Move historical mapping to `archive/README.md`.

### `current-fit-and-publishability.md`

One-page current-state memo. It should keep:

- current paper frame;
- strongest alpha;
- what is not ready;
- venue readiness;
- next five actions;
- concise `DEC-###`, top `ANG-###`, and important `IDEA-###` rows when they affect current strategy.

### `literature-and-nearest-neighbors.md`

Canonical related-work packet. It should contain:

- high-level literature positioning;
- cluster summaries;
- verified `LIT-###` blurbs;
- top closest works and novelty threats;
- runnable vs cite-only baseline decisions;
- citation gaps and bibliography readiness;
- safe and forbidden novelty wording.

This file should absorb `literature-verified-blurbs.md`. After promotion, `literature-verified-blurbs.md` becomes provenance in `archive/`, not a parallel source of truth.

### `claims-gaps-and-risks.md`

Canonical claim and reviewer-risk packet. It should contain:

- `CLM-###` claim bank;
- `GAP-###` gap register;
- `RR-###` reviewer risk register;
- `WP-###` ordered work items;
- `QST-###` open questions;
- do-not-say rules.

Risks and weak points should be views over gaps, not independent competing work trackers.

### `results-experiments-and-repro.md`

Canonical results and execution packet. It should contain:

- `RES-###` results ledger;
- `EXP-###` experiment priority board;
- `RUN-###` run log if real runs exist;
- `STAT-###` statistical analysis plan;
- `AE-###` artifact-evaluation notes;
- `NEG-###` negative results;
- result validation checklist;
- reproduction commands.

Commands should move from `TBD` toward runnable where possible, but this consolidation pass does not need to run new experiments.

### `strategy-and-venues.md`

Canonical strategy and venue packet. It should contain:

- current venue ladder;
- verified venue facts vs unverified watchlist;
- `VEN-###` rows;
- `ANG-###` paper angle matrix;
- outline tradeoffs;
- title/abstract constraints;
- manual writing brief and section-brief takeaways.

Venue rows from deep research must be labeled `verified`, `spot-checked`, or `watchlist`.

### `evidence-and-sources.md`

Canonical provenance and source map. It should contain:

- `ART-###` artifact inventory;
- repo source map;
- `DRP-###` raw deep-research index;
- `QTE-###` quote/evidence rows;
- `FIG-###` figure/table ideas;
- pointers to `references/source/` and `research-inbox/`.

Do not move source artifacts or raw research drops into archive.

### `research-prompts.md`

Trimmed prompt library. It should keep only reusable prompts:

- full literature review prompt;
- venue sweep prompt;
- nearest-neighbor / novelty-threat prompt;
- rule-specific literature prompt;
- stochastic-evaluation prompt;
- digest-to-repo prompt.

Archive historical prompt variants after extracting the useful templates.

### `AGENTS.md`

Short operating rules only:

- preserve IDs;
- keep raw research raw;
- do not draft final manuscript prose by default;
- update the right consolidated file;
- keep active docs free of stale archive references.

## Merge And Archive Map

| Current file or group | Target |
|---|---|
| `README.md` | keep and rewrite as short front door |
| `AGENTS.md` | keep and shorten |
| `current-fit-and-publishability.md` | keep and lightly expand with current strategy |
| `literature-verified-blurbs.md` | merge into `literature-and-nearest-neighbors.md`; archive original |
| `literature-ledger.md` | merge into `literature-and-nearest-neighbors.md`; archive original |
| `literature-blurb-todo.md` | archive after preserving any useful candidate-pass context |
| `bibliography-ledger.md` | merge into `literature-and-nearest-neighbors.md`; archive original |
| `citation-gap-list.md` | merge into `literature-and-nearest-neighbors.md`; archive original |
| `literature-review-section-plan.md` | merge into `literature-and-nearest-neighbors.md`; archive original |
| `related-work-map.md` | merge into `literature-and-nearest-neighbors.md`; archive original |
| `literature-ingestion-workflow.md` | summarize in `literature-and-nearest-neighbors.md` or `AGENTS.md`; archive original |
| `citation-style-and-hygiene.md` | merge rules into `literature-and-nearest-neighbors.md` and `AGENTS.md`; archive original |
| `nearest-neighbor-comparison.md` | merge into `literature-and-nearest-neighbors.md`; archive original |
| `positioning-taxonomy.md` | merge into `literature-and-nearest-neighbors.md`; archive original |
| `style-guide.md` | split into `literature-and-nearest-neighbors.md`, `claims-gaps-and-risks.md`, and `AGENTS.md`; archive original |
| `claim-bank.md` | merge into `claims-gaps-and-risks.md`; archive original |
| `paper-gap-register.md` | merge into `claims-gaps-and-risks.md`; archive original |
| `reviewer-risk-register.md` | merge into `claims-gaps-and-risks.md`; archive original |
| `weak-point-resolution-plan.md` | merge into `claims-gaps-and-risks.md`; archive original |
| `question-backlog.md` | merge into `claims-gaps-and-risks.md`; archive original |
| `red-team-review-prompts.md` | merge into `claims-gaps-and-risks.md`; archive original |
| `manual-writing-brief.md` | merge current-state pieces into `current-fit-and-publishability.md`; claim rules into `claims-gaps-and-risks.md`; archive original |
| `results-ledger.md` | merge into `results-experiments-and-repro.md`; archive original |
| `experiment-priority-board.md` | merge into `results-experiments-and-repro.md`; archive original |
| `experiment-run-log.md` | merge template or runs into `results-experiments-and-repro.md`; archive original if no real runs |
| `result-validation-checklist.md` | merge into `results-experiments-and-repro.md`; archive original |
| `statistical-analysis-plan.md` | merge into `results-experiments-and-repro.md`; archive original |
| `reproduction-commands.md` | merge into `results-experiments-and-repro.md`; archive original |
| `artifact-evaluation-plan.md` | merge into `results-experiments-and-repro.md`; archive original |
| `negative-results-ledger.md` | merge into `results-experiments-and-repro.md`; archive original |
| `experiments/README.md` | merge into `results-experiments-and-repro.md`; archive original |
| `venue-positioning-matrix.md` | merge into `strategy-and-venues.md`; archive original |
| `paper-angle-matrix.md` | merge into `strategy-and-venues.md` and current-state memo; archive original |
| `outline-options.md` | merge into `strategy-and-venues.md`; archive original |
| `title-and-abstract-idea-bank.md` | merge constraints into `strategy-and-venues.md` and claims file; archive original |
| `idea-bank.md` | merge important ideas into `current-fit-and-publishability.md`, `strategy-and-venues.md`, or `evidence-and-sources.md`; archive original |
| `deep-research-prompt-templates.md` | extract to `research-prompts.md`; archive original |
| `deep-research-inbox.md` | merge index into `evidence-and-sources.md`; archive original |
| `artifact-inventory.md` | merge into `evidence-and-sources.md`; archive original |
| `repo-source-map.md` | merge into `evidence-and-sources.md`; archive original |
| `quote-and-evidence-bank.md` | merge into `evidence-and-sources.md`; archive original |
| `figure-idea-bank.md` | merge into `evidence-and-sources.md`; archive original |
| `metadata-schemas.md` | merge compact schema into `AGENTS.md` and consolidated docs; archive original |
| `decision-log.md` | merge current `DEC-###` summary into `current-fit-and-publishability.md`; archive original |
| `agentc-paper-intelligence-workplan.md` | archive after extracting any still-useful north-star text |
| `pizza-import-plan.md` | archive as historical process context |
| `idea-generation-protocol.md` | archive after extracting any useful recurrence rule |
| `section-briefs/*` | merge into relevant consolidated files; archive originals under `archive/section-briefs/` |

## Raw And Provenance Policy

Do not archive or rewrite:

- `paper-intelligence/references/source/*`
- `paper-intelligence/research-inbox/*`

Those are original inputs or raw generated research. They stay in place and are indexed from `evidence-and-sources.md`.

## IDs To Preserve

Do not lose or renumber:

- `ART`, `AE`
- `RES`, `EXP`, `RUN`, `STAT`, `NEG`
- `CLM`, `GAP`, `RR`, `WP`, `QST`
- `LIT`, `CIT`
- `VEN`, `ANG`, `TTL`, `IDEA`
- `FIG`, `QTE`, `DRP`, `DEC`

`AE`, `STAT`, and `NEG` are currently used but missing from `metadata-schemas.md`; define them in the consolidated schema or document them in `AGENTS.md` before archiving `metadata-schemas.md`.

## Execution Phases

### Phase 0: Safety Snapshot

Run before editing:

```sh
git status --short --untracked-files=all
git ls-files --others --exclude-standard paper-intelligence

ID_RE='(AE|ANG|ART|CIT|CLM|DEC|DRP|EXP|FIG|GAP|IDEA|LIT|NEG|QST|QTE|RES|RR|STAT|TTL|VEN|WP)-[0-9]{3}|RUN-[0-9]{8}-[0-9]{3}'
rg -o --no-filename "\\b($ID_RE)\\b" paper-intelligence | sort -u > /tmp/pi.ids.before
find paper-intelligence -type f -print0 | sort -z | xargs -0 shasum -a 256 > /tmp/pi.sha256.before
find paper-intelligence/references/source paper-intelligence/research-inbox -type f -print0 | sort -z | xargs -0 shasum -a 256 > /tmp/pi.provenance.before
```

Protect untracked current-state and literature files:

```sh
git add paper-intelligence/current-fit-and-publishability.md \
  paper-intelligence/literature-blurb-todo.md \
  paper-intelligence/literature-verified-blurbs.md
```

Do not run `git clean`. Do not archive anything before this phase is complete.

### Phase 1: Create New Active Files

Create new consolidated files while leaving originals in place:

- `literature-and-nearest-neighbors.md`
- `claims-gaps-and-risks.md`
- `results-experiments-and-repro.md`
- `strategy-and-venues.md`
- `evidence-and-sources.md`
- `research-prompts.md`

Keep and edit:

- `README.md`
- `AGENTS.md`
- `current-fit-and-publishability.md`

### Phase 2: Promote Content

For each consolidated file:

1. Put the human summary first.
2. Preserve tables with stable IDs.
3. Mark unresolved or raw facts explicitly.
4. Link back to raw/provenance files where needed.
5. Remove duplicate process prose.

Priority order:

1. `literature-and-nearest-neighbors.md`
2. `claims-gaps-and-risks.md`
3. `results-experiments-and-repro.md`
4. `strategy-and-venues.md`
5. `evidence-and-sources.md`
6. `research-prompts.md`
7. `README.md`, `AGENTS.md`, and `current-fit-and-publishability.md`

### Phase 3: Validate Before Archive

Run:

```sh
rg -o --no-filename "\\b($ID_RE)\\b" paper-intelligence | sort -u > /tmp/pi.ids.after_promote
comm -23 /tmp/pi.ids.before /tmp/pi.ids.after_promote
```

The `comm` output must be empty before archiving.

### Phase 4: Archive Superseded Files

Create:

```text
paper-intelligence/archive/README.md
```

Move superseded files under `archive/`, preserving subpaths:

- `section-briefs/foo.md` -> `archive/section-briefs/foo.md`
- `experiments/README.md` -> `archive/experiments/README.md`

`archive/README.md` must map every archived path to the consolidated file that replaced it.

### Phase 5: Link Cleanup

Active docs should not point to superseded paths except through `archive/README.md`.

Search for stale references:

```sh
rg -n 'literature-blurb-todo|literature-verified-blurbs|literature-ledger|claim-bank|paper-gap-register|reviewer-risk-register|section-briefs/|experiments/README|handoff\\.md' \
  README.md CLAUDE.md paper-intelligence \
  --glob '!paper-intelligence/archive/**'
```

Expected allowed hits:

- none for `handoff.md`;
- references to archived files only inside `archive/README.md`;
- references to raw files only when intentionally pointing to provenance.

### Phase 6: Final Validation

Run:

```sh
rg -o --no-filename "\\b($ID_RE)\\b" paper-intelligence | sort -u > /tmp/pi.ids.after
comm -23 /tmp/pi.ids.before /tmp/pi.ids.after

rg -o --no-filename "\\b($ID_RE)\\b" paper-intelligence --glob '!archive/**' | sort -u > /tmp/pi.ids.active
comm -23 /tmp/pi.ids.before /tmp/pi.ids.active

find paper-intelligence/references/source paper-intelligence/research-inbox -type f -print0 | sort -z | xargs -0 shasum -a 256 > /tmp/pi.provenance.after
diff -u /tmp/pi.provenance.before /tmp/pi.provenance.after

find paper-intelligence -type f -size 0 -o -type d -empty
git status --short --untracked-files=all
```

The first `comm` must be empty. The active-surface `comm` may only contain IDs intentionally preserved as archive-only history, and those exceptions must be listed in `archive/README.md`.

## Success Criteria

- Avery can understand current paper state in 10 minutes.
- Active docs have no stale `pending/TBD` tables unless explicitly labeled as future work.
- `literature-verified-blurbs.md` has been promoted and archived, not left as a parallel source of truth.
- Current results, risks, claims, venues, and evidence each have one canonical home.
- Raw source artifacts and raw research drops remain unchanged.
- All stable IDs still exist somewhere after consolidation.
- The active file count drops to roughly 8-10 files plus raw/provenance folders.

## Risks And Mitigations

| Risk | Mitigation |
|---|---|
| Losing local-only literature/current-state files | Add or checksum them before moving anything. |
| Losing stable IDs during merge | Run ID manifest checks before and after every archive phase. |
| Creating unreadable mega-files | Keep summaries first, detailed ID tables below. |
| Treating raw venue research as verified | Label venue facts as `verified`, `spot-checked`, or `watchlist`. |
| Hiding useful history | Add `archive/README.md` with old-path to new-path mapping. |
| Reintroducing `handoff.md` confusion | Keep `README.md` and `current-fit-and-publishability.md` as the only entry/current-state docs. |
