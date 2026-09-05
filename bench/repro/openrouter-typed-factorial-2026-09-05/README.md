# Fixed model-by-rewrite opportunity ablation

Prospective source `7f65733`; canonical manifest SHA-256:
`4e30338f35205059a2b3d5b32fe41ee2b771b4f35eeea18bc194b5730b2ac148`.
No acquisition before independent integrated-code and manifest review.
This experiment measures fixed configurations, **not adaptive native admission,
joint-planner efficacy, a safety certificate or MLSys main-track readiness**.

The [set-valued feasibility pilot](../openrouter-typed-set-pilot-2026-09-05/README.md)
completed 54/54 calls, all 18 workflows valid, source/repeat 6/6 exact each and
target 5/6. Its complete journals, schema, scoring and ledger evidence are bound
into this manifest. Earlier failed pilots and all their charges remain unchanged.

## Allocation and treatment

32 new generated development tasks (seed `2026090503`), four cells, three stages:
384 provider calls. Cell order is deterministically randomized within each task.
Every cell uses its own filter and synthesis history; none is copied across cells.

| Cell | Fixed model | Input treatment |
| --- | --- | --- |
| S0 | Sonnet 4.5 | Original messages |
| SR | Sonnet 4.5 | ContextCompress at filter, StateDrop at answer |
| H0 | Haiku 4.5 | Original messages |
| HR | Haiku 4.5 | Same rewrites as SR |

Synthesis is never rewritten. The same prompts, schemas, output cap and parsed
application semantics apply in all cells. Filter IDs are sets of three distinct
domain IDs; synthesis and final answers remain exact typed values. Wrong but valid
answers are not repaired or used to choose a model. Invalid application outputs
count as failures and continue only through their own canonical null history.
This differs prospectively from the feasibility screen's stop-on-invalid policy;
no invalid cell is discarded to create a complete-case comparison.

The Rust bridge invokes the actual rules' applicability, proposal and safety
checks, without an optimizer, routing controller, observations or invented input
bound. It preserves original message bytes and exact non-message JSON tokens;
the driver remaps aligned annotations using unambiguous retained indices.
Ambiguous equal-text provenance preserves the full original call and explicitly
records `ambiguous_provenance`. Other no-ops record their reasons. This is forced
rule materialization, not hotness gating, learned selection, full legacy-rule
coverage, or a native risk-policy test. Only CC and SD are in scope.

Frozen bridge SHA-256:
`b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122`.
A read-only executable copy lives under this worktree's
`target/native-snapshots/<sha>/typed_rule_materializer`, outside rebuild outputs.
The manifest also fingerprints workspace Cargo files and transitive native source.

## Budget and failure handling

The original USD50 campaign begins at USD18.47998347 known charges plus
USD6.259659 retained uncertainty: USD24.73964247 committed, USD25.26035753
remaining. No additional budget is created. The hard stage ceiling is USD9.50,
including holds and the next full context reservation. Stop initiating calls once
known stage charges reach USD3.00; this is a progress stop, not a USD3 final-cost
guarantee. Sonnet/Haiku reserve USD6.015360/USD1.215360 per request at capped prices.
Every prior allowance remains retained, and every dispatch rechecks the campaign.

Provider failure stops the whole matrix with no retry or resume. A permanent
exclusive claim beside the resolved original ledger binds the campaign and stage,
so copied output folders cannot race into a second run. A local claim is also
retained. Partial matrices are archived without paired opportunity conclusions.
Missing generation identity, unknown pipeline changes, altered evidence or an
unreviewed provider endpoint halt. There are no fallback providers, tools, cache
controls, request fees or extra source calls hidden from accounting.

## Analysis and limits

Reconstruct all intents, selected deletions, exact requests, raw receipts, own
histories and bills before analysis. Report each complete cell's exact quality,
typed validity, paid and nominal uncached costs, token/cache counts and
observational latency. Keep all task-level paired values. The quality interaction
is `(Q_HR - Q_H0) - (Q_SR - Q_S0)`. For strictly positive workflow costs report
`log(C_HR/C_H0) - log(C_SR/C_S0)`; exclude and list zero-cost cases without adding
an arbitrary offset. Absolute dollar interaction is reported separately: cheaper
token prices multiplied by identical token reduction are not learned synergy.

Intervals are descriptive whole-task bootstraps, not a damage guarantee. These
are 32 development tasks from the same synthetic generator and a single sample
per cell, not public-workload generalization or confirmatory heldout. The original
48 protected QA questions remain unused. If a fixed Haiku-plus-rules configuration
wins uniformly, that supports the fixed configuration, not an adaptive planner.
Any learned policy requires separate training and fresh evaluation later.
