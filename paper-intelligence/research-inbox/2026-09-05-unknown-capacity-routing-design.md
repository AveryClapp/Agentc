# Unknown-capacity routing: minimum contract and the earlier opportunity test

2026-09-05; design Bead `bd-323l.6.13.8.6.20.5.1`; inspected source `8c1b473`.
Design only: no inference, ledger access, runtime edits, or native builds. This
note proposes experiments and interfaces; it reports no joint-system result.

## Recommendation

**After the newly reviewed 54-call typed feasibility screen passes, run a small
fixed model × rules factorial before implementing unknown-capacity routing.** It
can test whether useful model-dependent rewrite opportunities exist. If every arm
is correct and Haiku plus rules is uniformly cheapest, the result favors a fixed
configuration—not an adaptive joint planner or an MLSys contribution claim.

Keep current native model-downgrade abstention. The
[context-fit investigation](2026-09-05-typed-context-fit.md) found no documented
OpenRouter full-input certificate, and Anthropic still describes its counter as
an estimate. Neither client bytes nor a measured overhead margin can populate a
field whose contract is a genuine input upper bound.
[Anthropic token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting).

## Earlier experiment: opportunity, not planner admission

Freeze these four cells, with one complete own-history workflow per cell/task:

| Fixed model | Original messages | Frozen input rewrites |
| --- | --- | --- |
| Sonnet 4.5 | S0 | SR |
| Haiku 4.5 | H0 | HR |

The rewrite treatment is exactly ContextCompress at `filter`, no rewrite at
`synthesize`, and StateDrop at `answer`, using public provenance. Keep schemas,
prompts, output caps, parser, provider pinning, and failure policy identical across
cells. Randomize cell order within each task using a frozen seed. Never copy
another arm's selected IDs or synthesis into an arm's history. A factorial varies
the factors jointly; its interaction contrast is separate from its main effects.
[NIST factorial designs](https://www.itl.nist.gov/div898/handbook/pri/section3/pri3331.htm).

**Allocation:** 32 fresh development tasks, four cells, three stages: 384 calls.
Proposed generator seed `2026090503` must pass an ID-overlap check against every
exposed fixture and all protected heldout IDs before freezing. These 32 tasks
are entirely development, not train/test partitions or confirmatory heldout.
Leave the original 48 heldout questions unopened. Do not tune the policy on this
matrix and then claim evaluation on the same matrix. An optional full source-repeat
cell would add 96 calls; omit it initially to conserve exposure, and label the
single-realization matrix's stochastic uncertainty explicitly. Existing repeats
on different exposed tasks are not matched repeats for these 32 tasks.

**Budget proposal:** a new USD9.50 hard stage ceiling, including all its charges,
holds, and the next reservation; stop starting new calls once known stage charges
reach USD3.00. The latter is a prospective progress stop, **not** a guarantee that
the last call cannot exceed USD3.00. At the inspected contract, a single Sonnet
reservation is USD6.015360, so a call begun below the progress stop fits below
USD9.015360 before other stage commitments. Provider failure stops the whole matrix
without retry; report a partial matrix, not a complete-case efficacy contrast.
Unlike the feasibility pilot, prospectively count application-invalid completions
as failures and continue their own histories with the canonical null envelope;
never discard those arms or substitute another arm's state. The archive reports
USD25.87364953 campaign availability at its
snapshot, but acquisition must recheck the actual shared ledger after the current
pilot and retain every historical hold. A second large allowance loss is material.
[Archive and accounting basis](../../bench/repro/openrouter-typed-schema-v2-pilot-2026-09-05/README.md),
[`upper_cost` and `Ledger.call`](../../bench/openrouter_pilot.py).

### Rewrite fidelity is not native policy execution

The production typed adapter deliberately omits a numeric bound. Native
[`ffi::describe_plan` and `reserve_profiled_exploration`](../../crates/agentc-optimizer/src/ffi.rs)
also depend on that bound parser for identity and profiling. Merely turning off
MD does not establish a functioning complete-plan path. The existing
[native mechanics test](../../tests/test_typed_relational_workload.py) explicitly
injects a hypothetical bound and cannot justify doing so in paid experiments.

Use a small deterministic benchmark materializer, with deletion positions and
provenance persisted and independently reconstructed. Check parity directly
against the existing
[`ContextCompressRule`](../../crates/agentc-optimizer/src/rules/context_compress.rs)
and [`StateDropRule`](../../crates/agentc-optimizer/src/rules/state_drop.rs)
proposal/safety functions in a no-network Rust harness: neither rule needs a
provider token bound to compute its deletion. Check all stage inputs, schema
preservation, protected messages, unread-state handling, and label blindness.
The bridge returns the raw native proposal plus unambiguous retained indices;
the driver remaps aligned metadata from the original, checking proposal-message
equality and unchanged non-message request fields. Ambiguous duplicate-message
mapping fails closed. Synthetic projection statistics used only by this parity
harness are not online observations. Reuse [`restrict_call`](../../bench/openrouter_joint_study.py) for
stage annotation scope. If parity is unavailable, name the treatment a
benchmark-only deletion policy; do not present it as verified native CC/SD.
Either path measures fixed rewrite execution, not hotness gates, plan selection,
learning, or guard admission.

### Analysis that distinguishes interaction from price multiplication

Report every cell's task exactness, typed validity, billed cost, token counts,
cache usage, failures, and observational latency. For task-level quality use
`(Q_HR − Q_H0) − (Q_SR − Q_S0)`. For positive complete-workflow billed costs also
report `log(C_HR/C_H0) − log(C_SR/C_S0)`; show zero-cost cases separately rather
than inventing a log offset. Pair contrasts by task and show all 32 values;
any interval is a small-development-sample description, not a damage certificate.

Cheaper token prices multiplied by identical token reduction naturally produce
combined dollar savings, and can even produce an absolute-dollar interaction.
That alone is not learned synergy. Inspect model-specific quality losses,
relative token/cost effects, and whether useful rankings change across tasks or
call sites. A workflow factorial captures propagated history effects; it does not
isolate a stage-level causal mechanism. An after-the-fact per-task oracle is only
an optimistic opportunity diagnostic, never an executable policy or feedback
oracle. Positive opportunity would motivate a later frozen learner versus the
best fixed cell on new questions; this matrix cannot establish that comparison.

## Minimum future opt-in contract

Call the proposed policy `opaque-provider-attempt-v1`; do not change existing
`PROVEN_BOUND` defaults or numerical semantic/evidence/exposure guards.

1. Separate protocol/capability facts from `PROVEN_BOUND(n)` versus `UNKNOWN`.
   UNKNOWN is never a numeric surrogate. Preserve provider/credential namespace,
   routing group, structured-output, tools/streaming, and output-limit checks.
   Bind a small client-size envelope as exposure scope, explicitly not proof of
   provider context fit. Unknown policies, schemas, or versions abstain.
2. Bind capacity policy, exact original-source fallback, schema/parser/adapter
   versions, and endpoint cohort into call-site and execution-plan identity.
   Existing `ValidationPolicy.parameters` can carry the policy without repurposing
   the divergence threshold. Old and new profile windows must not pool.
3. Initially allow only protected source-first counterfactuals. A later,
   separately reviewed transition may allow user-visible candidates that meet
   all existing semantic-admission requirements and the new availability gate.
   A successful small pilot does not prove future context fit.
4. Before either candidate-first dispatch or a protected source/candidate pair,
   atomically commit their combined allowance in the original campaign ledger.
   At today's 512-cap reservation constants, Haiku plus Sonnet requires
   USD7.230720 headroom. This is liability arithmetic, not a fit certificate.
   Transfer each authorized slot into its actual attempt without double counting;
   release only a provably never-dispatched unused slot. Failed-attempt allowances
   remain retained. A pending or ambiguous attempt never authorizes another call.
5. Only a verified capacity failure can authorize at most one exact original-source
   fallback, with no partial candidate output exposed. Record a separate durable
   availability event; the first verified overflow disables that exact cohort
   until review. Refusals, generic length stops, unknown 400s, timeout/cancellation,
   and ambiguous errors do not authorize fallback. Source failure remains failure.
   Never decide fallback using gold answers or repair a valid-but-wrong answer.
6. Do not manufacture a semantic pair from a provider failure. A normal completion
   with invalid application JSON remains an application failure under the existing
   metric, not a capacity event. Keep failed-attempt cost and latency in total
   economics; successful fallback does not erase either. Persistent availability
   state is separate from the existing semantic exposure controller.

Anthropic documents both input-too-long 400s and accepted requests ending with
`model_context_window_exceeded`. The current HTTP handler preserves only sanitized
status details, so HTTP400 alone cannot qualify. Initially recognize a verified
native context-exhaustion receipt; broader HTTP classification requires a reviewed,
bounded, private-body classifier. This restriction is intentional.
[Overflow behavior](https://platform.claude.com/docs/en/build-with-claude/context-windows),
[`request_json`](../../bench/openrouter_pilot.py).

## Exact implementation seams and acceptance checks

| Seam | Required change and no-network checks |
| --- | --- |
| [`model_catalog.rs`](../../crates/agentc-optimizer/src/model_catalog.rs), [`planner.rs`](../../crates/agentc-optimizer/src/planner.rs), [`ffi.rs`](../../crates/agentc-optimizer/src/ffi.rs) | Parse request facts without inventing capacity; separate eligibility from explicit UNKNOWN attempt permission. Test unchanged default abstention, capability mismatch, malformed policies, and distinct policy-bound identities. |
| [`execution_plan.rs`](../../crates/agentc-optimizer/src/execution_plan.rs), [`plan_profile.rs`](../../crates/agentc-optimizer/src/plan_profile.rs), [`exploration.rs`](../../crates/agentc-optimizer/src/exploration.rs) | Persist exact-cohort availability separately. Existing exploration failure counts a failed lease but carries no billed failure outcome. Test first-overflow disable/restart, drift invalidation, idempotence, and zero fabricated semantic observations. |
| [`openrouter_pilot.py`](../../bench/openrouter_pilot.py), [`openrouter_attempts.py`](../../bench/openrouter_attempts.py), [`openrouter_joint_analysis.py`](../../bench/openrouter_joint_analysis.py) | Add explicit group allowance/outcome reconstruction. Current per-call global pending/failure blockers must not be bypassed. Test crashes at every group/attempt transition, wrong ledger/key, concurrent claims, original holds, exact fallback payload, and no duplicate dispatch. |
| [`_optimizer_glue.py`](../../python/agentc/_patches/_optimizer_glue.py), [`_executor.py`](../../python/agentc/_executor.py) | Existing generic exception fallback is not this narrower policy. Add a distinct opt-in coordinator; test sync/async refusal, cancellation, ambiguous error, verified overflow, and no extra shadow after fallback. Do not alter legacy behavior incidentally. |

Tracked implementation: `bd-323l.6.13.8.6.20.11` owns the fixed-factorial driver and
`.20.11.1` its bounded native materialization bridge; the earlier planning child
`.20.5.1.1` is consolidated into `.20.11` before implementation.
`.20.5.1.2` is deferred group accounting/outcome classification; `.20.5.1.3` is
deferred native identity/eligibility/availability work dependent on `.20.5.1.2`.
Build the diagnostic first. No paid rollout, native enablement, or main-track claim
follows from this design note.
