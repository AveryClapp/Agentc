---
name: agentc-maintenance-audit
description: Produce a read-only, evidence-backed census of Agentc repository health — paper-claim traceability, evidence integrity, stale docs, structural debt, broken repro paths, placeholder/fake-work risk, and cleanup candidates. Use when performing repository hygiene audits, pre-submission gap analysis, artifact-evaluation readiness checks, maintenance risk classification, or cleanup planning. Do not use for architecture exploration alone, implementation review, feature work, or any request to fix, move, delete, stage, or create issues during the audit pass.
---

# Agentc Maintenance Audit

Evaluate repository health without changing the state being evaluated. Build a census
before recommending action, preserve uncertainty, and keep cleanup authorization
separate from audit evidence.

Agentc is a paper repository under submission pressure. Its committed results back
published claims. Treat evidence integrity as the primary audit axis, not tidiness.

## Preserve The Read-Only Boundary

- Do not edit repository files, result CSVs, figures, the manuscript, or configuration.
- Do not create, update, close, or sync `bd` issues. Draft proposed issues as report text only.
- Do not stage, commit, push, install tooling, or change machine configuration.
- Return the report in the response by default. Write a durable report only when an exact
  output path is authorized; that authority covers the report file, not any audited surface.
- Treat every failure path as read-only. A severe finding is not repair authority.
- Never re-run a paid experiment "to check" during an audit. Reading a CSV is evidence;
  regenerating one mutates the evidence being audited.

Use read-only commands: `rg`, `git status`, `git diff`, `git log`, `git ls-files`,
`cargo check`, `cargo clippy`, `bd list`, `bd show`. Never run a command whose documented
behavior can mutate state. `cargo fix`, `clippy --fix`, and any `bench/run_*.py` are
mutations.

## Route The Request

Select this skill for a bounded repository-health audit, cleanup-candidate census,
pre-submission gap analysis, or placeholder/fake-work completeness review.

Use a different workflow when the primary request is:

- mapping how the runtime works: use exploration;
- judging a diff against acceptance criteria: use code review;
- repairing classified findings: use `agentc-cleanup-workflow` after its preconditions hold;
- implementing a feature or running an experiment: use that work's own path.

For a mixed "audit and fix" request, produce the read-only audit first and stop before
mutation. Do not silently convert the audit into cleanup.

## Establish The Audit Frame

Record before collecting findings:

1. audit question and the decision the evidence must support;
2. repository root, paths, and scope;
3. authoritative surfaces: `CLAUDE.md`, `PRESUBMIT.md`, `specs/`, `bench/paper_results/DATA_MANIFEST.txt`, `main.tex`;
4. excluded paths, protected surfaces, and the initial do-not-touch set;
5. snapshot identity: Git revision, dirty paths, untracked files;
6. available and unavailable capabilities (missing TeX toolchain, unreachable `bd` server, absent API keys).

Resolve claims in this order:

1. the manuscript (`main.tex`) — it is the artifact under submission and outranks the README;
2. committed evidence in `bench/paper_results/` and the generators that produced it;
3. project policy: `CLAUDE.md`, `specs/CLAUDE.md`, `PRESUBMIT.md`;
4. `bd` issues for ownership and work state;
5. repo docs (`README.md`, `specs/`) — these drift fastest and are the weakest authority.

When the README and the paper disagree, the paper is presumed correct and the README is
the finding. State the presumption; do not silently pick a side.

## Build A Census Before Recommendations

Inspect every in-scope surface against these lenses. Record healthy surfaces too, so that
absence of findings is not mistaken for coverage.

- **Paper-claim traceability:** every numeric claim in `main.tex` (abstract, intro, tables,
  captions, conclusion) resolves to a committed CSV, produced by a script that exists in the
  repo, at the stated n / model / config. A claim whose driver or CSV is absent is a
  verified defect regardless of whether the number is true.
- **Evidence integrity:** result CSVs, summary txts, and figures. Every published figure has
  a generator that writes to the directory the manuscript reads. Retracted and superseded
  runs are labeled and quarantined, never left adjacent to canonical data under a
  confusable name.
- **Repro-path liveness:** every command in the manuscript's reproducibility appendix,
  `PRESUBMIT.md`, `bench/repro/`, and the README runs from a clean checkout — including
  fixture bootstrap. A command that assumes a gitignored artifact already exists is broken.
- **Documentation truth:** canonical authority, duplicate claims, links, path redirects, and
  current-versus-historical labeling. Docs that publish numbers the paper has corrected or
  disavowed are verified defects, not staleness.
- **Completeness:** placeholders, stubs, fake success paths, dead entry points, and
  documentation claiming behavior absent from code. Verify by reading the implementation and
  tracing the logic — never from a grep hit or a function name.
- **Code ownership:** rule registration, planner dispatch reachability, dead dependencies,
  duplicate behavior, import direction.
- **Tests and guardrails:** contract coverage, stale expectations, skipped checks.
- **Environment:** repository-relative paths, declared dependencies vs actual imports,
  machine-specific assumptions, credentials.
- **Work state:** `bd` issues, handoffs, shared dirty paths, abandoned or superseded work,
  and issue IDs cited in docs that do not exist in the tracker.

Completeness is an audit lens, not permission to remove suspect material.

## Classify Lifecycle And Evidence

Assign exactly one lifecycle state to each item:

- `active`: current behavior or authority with known consumers;
- `generated`: owned by a generator or reproducible process;
- `historical`: retained evidence or context, not current behavior;
- `superseded`: replaced by an identified current owner;
- `retracted`: explicitly invalidated while retained for traceability;
- `compatibility`: retained to preserve an old path or interface;
- `protected`: mutation requires authority beyond the audit;
- `unknown`: current use, provenance, or ownership is unresolved.

`bench/paper_results/*.csv` backing a published claim is `protected`. Contaminated or
pre-warmup-correction runs are `retracted`, not deletable — they document a correction and
must survive for traceability.

Put `unknown`, protected, generated-with-unclear-owner, shared-WIP, and consumer-uncertain
items in `do not touch yet`. Do not infer that old, duplicated, or unreferenced material is
deletable.

For every finding record:

- stable finding ID (`MNT-NNN`) and audit lens;
- lifecycle state;
- exact evidence: paths, line numbers, or read-only command output;
- evidence state: `verified defect`, `observation`, or `inferred risk`;
- severity and confidence, each justified independently;
- blast radius: does this affect the PAPER, the ARTIFACT REVIEW, the CODE, or internal docs only?
- root cause and affected consumers;
- proposed disposition, dependencies, and validation needed;
- do-not-touch status and the evidence needed to clear it.

Use severity to describe impact, not to authorize disposition. Use confidence to describe
evidence quality, not importance. A finding may be high-severity and low-confidence; report
both.

## Verify Before Asserting

Every existence claim must be verified by reading the file and tracing the logic. A grep hit
is a hypothesis, not a finding. Mark each claim:

- `verified defect` — read the file, traced the code, confirmed the defect;
- `observation` — read the file, reporting what is there without asserting a defect;
- `inferred risk` — pattern-level signal, not yet confirmed by reading.

Never report an unavailable check as clean. If the TeX toolchain, `bd` server, API keys, or
a generator are unavailable, label the affected checks `not observed` and reduce confidence.

## Synthesize Without Hiding Disagreement

Deduplicate findings sharing a root cause while preserving distinct evidence and conflicting
interpretations. Rank by blast radius first (paper > artifact > code > internal docs), then
impact, confidence, dependency order, reversibility, and validation cost. Do not collapse the
result into a score that implies automatic deletion.

Propose the smallest independently testable cleanup batches. Draft each proposed `bd` issue
with scope, rationale, exact paths, dependencies, risks, rollback expectation, validation,
and success criteria. Do not run `bd create`.

## Report Contract

Return:

1. audit frame, snapshot, exclusions, capability limits;
2. census coverage by lens, including surfaces checked with no finding;
3. findings using the required fields, ranked by blast radius;
4. do-not-touch-yet set with reasons and unblock evidence;
5. deduplicated priorities and dependency order;
6. proposed reversible batches and draft issues;
7. validation needs, unresolved disagreements, and blind spots.

Append durable findings to `specs/working/maintenance-audit-findings.md` when authorized.
That file is the living census; never overwrite prior findings, only append and update status.
