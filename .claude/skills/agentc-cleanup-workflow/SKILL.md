---
name: agentc-cleanup-workflow
description: Execute an authorized Agentc maintenance cleanup in small, reversible, validated batches. Use when executing a named cleanup issue, and only after an accepted current audit classification, exact writable paths, consumer scan, generator-owner scan, shared-WIP clearance, rollback plan, and validation plan exist. Do not use for read-only audits, architecture exploration, ordinary feature work, experiment runs, or cleanup requests missing any prerequisite.
---

# Agentc Cleanup Workflow

Apply already-classified maintenance findings without widening authority. Prefer
stabilization, archive, redirects, and reversible consolidation over destructive cleanup.

Agentc is under submission pressure and its committed results back published claims. The
default bias is **preserve**: a wrong deletion can silently invalidate a paper table, and the
loss may not surface until an artifact reviewer runs the repro script.

## Fail Closed Before Mutation

Verify every precondition against current repository state:

1. **Named issue:** an in-progress `bd` issue (or explicit user authorization) names the exact
   paths and operation classes in the batch.
2. **Accepted audit:** a current entry in `specs/working/maintenance-audit-findings.md`
   identifies every target, its lifecycle state, disposition, uncertainty, and do-not-touch status.
3. **Consumer scan:** references, imports, commands, manifests, docs, symlinks, fixtures,
   `main.tex` `\input`/`\includegraphics`/`\ref`, and repro scripts have been checked for each target.
4. **Generator owner:** generated files (figures, fixtures, result CSVs) have an identified
   generator and regeneration command. Generated output is never hand-edited.
5. **Shared-WIP clearance:** `git status`, diffs, and live issues show no overlapping unowned
   work on target paths or their consumers. Check whether another pane or agent holds the file.
6. **Rollback:** every operation has a path-specific reversal using captured content or archive
   evidence. Rollback never assumes a clean worktree and never uses destructive Git.
7. **Validation:** per-batch behavior, link, consumer, and manuscript checks are named before execution.
8. **Protected authority:** any protected surface has explicit authorization.

If any item is absent, stale, contradictory, or unverifiable, do not mutate. Return a
remediation plan, missing-evidence list, and blocked paths.

Repository writes do not imply Git or `bd` authority. Do not stage, commit, amend, rebase, or
push unless separately authorized; push always requires its own authority.

## Protect Evidence And Ownership

- **Never rewrite or delete a result CSV in `bench/paper_results/` that backs a published
  claim.** These are immutable score-bearing evidence. Correct the claim, not the evidence.
- **Never delete a `retracted` or `contaminated` artifact.** It documents a correction and is
  required for traceability. Quarantine it, rename it so it cannot be confused with canonical
  data, and record the supersession — do not remove it.
- **Never re-run a paid experiment as part of a cleanup batch.** Regenerating evidence is an
  experiment, not a cleanup; it needs its own authorization and its own cost budget.
- Archive historical evidence before removing a current path. Preserve an index, provenance,
  and any required redirect.
- Change generated output through its owner generator, then verify deterministic regeneration.
  Never disguise a hand edit as regeneration.
- A figure the manuscript includes may not be deleted or renamed until `main.tex` and
  `main_trimmed.tex` are updated in the same batch. Both files.
- Treat `unknown`, active shared WIP, unclear consumers, and unclear provenance as
  do-not-touch conditions.
- Do not treat severity, age, duplication count, or a dead-reference scan as deletion authority.

If an immutable artifact creates a stale public path, place the redirect or explanatory
metadata in the authorized mutable owner rather than altering the evidence artifact.

## Prepare The Dry-Run Batch Manifest

Before changing a file, define the smallest independently reversible batch. For every move,
archive, redirect, consolidation, generator action, or deletion, record:

- finding ID, lifecycle classification, and authorizing issue;
- exact source and destination paths;
- operation type and reason;
- before-state content hash; generated or immutable status;
- known consumers and the query used to find them;
- owning generator or compatibility contract;
- rollback action and rollback validation;
- behavior, tests, links, docs, and manuscript checks;
- paths explicitly excluded from the batch.

Do not begin when the manifest contains a wildcard disposition, an unexplained delete, an
unknown consumer, a missing before-hash, or an operation outside the issue's scope.

## Execute One Reversible Batch

1. Revalidate audit evidence, issue state, dirty paths, consumers, and generator owners
   immediately before the batch.
2. Stabilize correct behavior and regression coverage before moving or removing its current
   implementation.
3. Archive historical evidence and establish redirects before retiring a path.
4. Consolidate duplicate behavior into its declared owner, update consumers, prove equivalence,
   then remove the old path.
5. Perform only the operations in the manifest. Preserve unrelated dirty work.
6. Run the predefined validation immediately after the batch.
7. On regression or unexpected diff, stop and apply the rollback. Revalidate the restored state
   before reporting.
8. Continue to another batch only after the current one is independently sound and recorded.

Delay naming, comment, and documentation polish until structural ownership and paths are
stable. Then update authoritative documentation, replace duplicate claims with pointers, and
add the smallest guardrail that would catch recurrence.

## Validation Per Batch Type

| Batch type | Required validation |
|---|---|
| Doc/README number correction | The corrected number matches the canonical CSV and the manuscript. Cite both. |
| Figure pipeline change | Regenerate the figure; confirm byte-or-visual equivalence; confirm `main.tex` still resolves it. |
| Dependency declaration | `uv lock --check`; a clean-venv `pytest --collect-only` succeeds. |
| Repro-script fix | Run the script's first command from a clean checkout, or state precisely why not. |
| Rust change | `cargo check --workspace`; `cargo clippy --workspace --exclude agentc-profiler`; `cargo test --workspace --exclude agentc-profiler`. |
| Manuscript edit | The claim traces to a committed CSV; both `main.tex` and `main_trimmed.tex` updated. |
| Archive/move | Every consumer updated; no broken link; index entry recorded. |

## Stop Conditions

Stop before or during mutation when:

- the authorizing issue, classification, exact scope, or protected owner is unclear;
- audit evidence drifted after the snapshot;
- a consumer or current use cannot be resolved;
- the generator owner or regeneration path is ambiguous;
- shared dirty work or another agent overlaps the target;
- rollback cannot be executed and verified independently;
- a result CSV backing a published claim would need to be edited, moved, or deleted;
- a cleanup would require re-running a paid experiment;
- validation regresses or produces unexplained output;
- a requested operation falls outside the manifest.

Do not improvise around a stop. Record the affected paths, evidence, and the exact decision
needed to resume.

## Degraded Behavior And Routing

If Git, `bd`, validation dependencies, generators, or a TeX toolchain are unavailable, remain
read-only. Produce a blocked-path list and a precondition-completion plan; do not claim the
cleanup ran.

Use `agentc-maintenance-audit` when findings still need census or classification. Do not select
cleanup for a read-only request.

## Closeout Record

Record for each completed batch, appending to `specs/working/maintenance-audit-findings.md`:

1. authorizing issue, audit finding IDs, snapshot, and exact manifest;
2. before and after hashes;
3. moves, archives, redirects, generator actions, and deliberately retained paths;
4. consumer and shared-WIP scan results;
5. validation commands and outcomes;
6. rollback method and verification result;
7. documentation and relapse guardrails updated after paths stabilized;
8. remaining findings, blocked paths, and follow-on batches.

Report staging, commit, and push as `not authorized` unless separate authority was given.
Completion of file cleanup alone does not authorize Git publication or issue closure.
