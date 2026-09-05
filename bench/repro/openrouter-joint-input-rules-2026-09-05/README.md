# Prospective input rules × model routing study

This is a development experiment, not a confirmed joint-system advantage or a
full-rule MLSys ablation. The previous contaminated workflow is permanently
aborted; none of its controller state or scheduled questions is reused here.

## Final status: stopped at the admission gate

**STOPPED_FAILED_ADMISSION — this manifest is closed.** All scheduled calibration,
warmup, and training workflows completed. The frozen, label-free training gate
returned `proceed_to_heldout=false`: no joint rewrite-plus-routing plan was
actually selected with the required 20 exact source-paired observations. The
48 heldout questions were not dispatched. Do not resume this acquisition or
reinterpret its training questions as an untouched evaluation set.

This is a negative observation about admission under this workload and these
unchanged controls, not an estimate of the joint system's treatment effect.
There is no heldout quality comparison, demonstrated joint efficiency advantage,
two-percentage-point safety guarantee, or full-rule ablation result here.
Further work is tracked separately in `bd-323l.6.13.8.6`; this archive is not an
authorization to extend or tune the closed study.

| Completed phase | Questions | Workflows | Primary calls |
| --- | ---: | ---: | ---: |
| Global-router calibration | 16 | 32 (two models) | 96 |
| Warmup | 3 | 18 (six arms) | 54 |
| Training | 64 | 384 (six arms) | 1,152 |
| Heldout | 0 of 48 | 0 | 0 |

The final journals contain 1,302 decisions, 1,302 intents, and 1,372 provider
results: 1,302 primary/calibration calls, 69 exploratory comparisons, and one
shadow comparison. Every result ended with `finish_reason=stop`; no failed or
truncated provider outcome entered the controller. The final financial snapshot
has no missing artifact charges, unincorporated results, pending attempts, or
new failed-attempt allowances.

### What the controller did

The guarded-rules, routing-only, sequential, and joint arms all retained the
original request as their primary throughout warmup and training. The historical
greedy arm applied ContextCompress at all 64 training filter calls and StateDrop
at all 64 training answer calls. These activations are not evidence that the
rewrites preserve task quality.

Joint training accumulated the following exact-plan comparisons:

| Call site | Candidate | Observed pairs | Required pairs |
| --- | --- | ---: | ---: |
| Filter | ContextCompress | 3 | 20 |
| Filter | ModelDowngrade | 2 | 20 |
| Filter | ContextCompress + ModelDowngrade | 3 | 20 |
| Synthesize | ModelDowngrade | 5 | 20 |
| Answer | StateDrop | 16 | 20 |
| Answer | ModelDowngrade | 7 | 20 |
| Answer | StateDrop + ModelDowngrade | 2 | 20 |

No joint candidate reached the admission floor. The diagnostic report preserves
the actual lexical comparisons and exact identities. Lexical disagreement is
not task damage, and its aggregate positive excess is not a reconstruction of
native rolling-window disable state. More scheduled training arrivals did not
by themselves produce more admissible evidence in this run.

Independent global calibration selected Sonnet, not Haiku. On those 16
calibration questions, mean final-answer F1 was 0.90417 for Sonnet and 0.72411 for
Haiku; Haiku was outside the frozen 0.02 margin. These are calibration figures,
not heldout estimates. Thus the sequential control used the source model plus
its own guarded-rule history; it did not test a cheaper global model downstream.

### Final cost accounting

| Arm | Calls charged | Billed USD | Nominal uncached USD |
| --- | ---: | ---: | ---: |
| Original | 201 | 1.146300 | 1.146300 |
| Historical rules | 202 | 0.805965 | 0.805965 |
| Guarded rules | 209 | 1.183173 | 1.183173 |
| Routing only | 213 | 1.157035 | 1.157035 |
| Sequential, including both calibration models | 308 | 1.549931 | 1.549931 |
| Joint | 239 | 1.205944 | 1.205944 |
| **Total** | **1,372** | **7.048348** | **7.048348** |

These totals charge every calibration, warmup, training, exploration, and shadow
call exactly once. Sequential includes all 96 global-router calibration calls
($0.358832). All spending belongs to setup/training; heldout spending is zero.
The cost differences above are descriptive incurred spending, not quality-matched
efficiency effects or causal cache/latency results. The $9 training ceiling and
$15 stage ceiling were not reached; the admission gate caused the stop.

At the archived ledger snapshot, campaign known charges were $16.94093847 with
$0.147306 in retained historical uncertainty and no pending upper bounds:
$17.08824447 conservatively committed. This stage added no uncertainty. The
historical allowances remain held; no failure was declared free or settled.
These amounts describe the captured snapshot, not a later account balance.

### Archive validation

[`final-analysis.json`](final-analysis.json) was generated with the independently
reviewed read-only analyzer and a consistent shared-ledger snapshot. It verifies
the frozen source and fixture bindings, exact own-arm request histories,
generation and endpoint attribution, journal prefixes, reconstructed gate,
static selection, and complete stage accounting. Its `paired_questions` and
`comparisons` arrays are empty, and `comparison_available=false`.

The report's generic suppression labels include failed/missing gate,
incomplete scheduled workflows, and an incomplete completion summary. Here the
gate file **exactly matches** its reconstruction, and the summary hashes
**match** the final journals: `schedule_complete=false` correctly records the
intentional stop before heldout, not damaged or missing provenance.

Canonical JSON SHA256 values:

- Manifest: `dcb703739e460810825f0966c06371d2848a4d17346afbfea4e77c52a0548ace`
- Calls: `9717b3a67da932155321f65621e6f5a118d7e7c87ab4a62b2e2c27a976c5af4a`
- Decisions: `916b672c1e322f97aacd6fbcd3da64230f1e017e0fe1efacde9b67e711fb3f3b`
- Intents: `2e1b147da367e5d5d9a8064979d26380c1a5d64400df8e623918e44ee175335f`
- Final analysis: `0d89c89d8b10b5e5a6437f286e834fa47afa386e0b5d52bda4df037900e16bc1`

The private ledger's captured **byte** SHA256 is
`fdca8ea8e992d50494a28c5ee5b14f0de92fff6198cc47c4d225bf3720678d9b`.
Its raw provider bodies and key bindings remain outside the repository. The
frozen acquisition source files, manifest, and native binary are unchanged.

## Question and controls

Does jointly selecting an input rewrite and a model improve a three-call QA
workflow over selecting either alone? Each arm consumes its own filter,
synthesis, and answer outputs. The source is Claude Sonnet 4.5 and the routing
target is Claude Haiku 4.5, both pinned to Anthropic through OpenRouter with
provider fallback disabled. Endpoint capabilities and prices are checked live
when the immutable manifest is created.

| Arm | Decision policy |
| --- | --- |
| Original | Source model, original requests |
| Historical rules | Existing greedy planner, restricted input-rule menu |
| Guarded rules | Learned input rewrites, source model |
| Routing only | Learned model selection, no input rewrite |
| Sequential | Independent calibration chooses one global model, then guarded input rewrites |
| Joint | Joint selection of input rewrite and model |

The menu is prospective and structural: ContextCompress only at filter,
StateDrop only at answer, and ModelDowngrade at all three sites. Downstream
attention annotations are omitted, so large downstream outputs cannot silently
enable additional compression candidates. The output cap stays 512. This tests
two existing input rules plus routing, not OutputBudget or the entire engine.
The actual planner has at most three candidate identities at filter and answer
and one at synthesis, assuming each rule produces its single current proposal.

## Allocation and evaluation

The manifest fixes 131 campaign-fresh questions before inference: 16 independent
global-router calibration questions, 3 warmup questions, 64 training questions,
and 48 untouched heldout questions. All six arms share the same non-calibration
questions with deterministic interleaved order; their outputs and stores remain
separate. This requires 2,166 primary calls if completed, plus training probes,
sampled shadows, and any bounded retries.

Native minimum evidence remains 20 exact-plan comparisons. The native rule
thresholds, rolling windows, exposure budget, and safe source fallback are not
relaxed. The new incumbent-exploration fix allows a cold alternative to finish
learning after another plan is admitted: only an acquired bounded lease replaces
the incumbent primary with the original reference for that comparison. This
does not compare two different rewrites under a source-reference contract.

After complete training, a label-free gate requires an actually selected joint
rewrite-plus-routing plan with at least 20 comparisons for its exact identity.
If the gate fails, acquisition stops before heldout. That is a negative result
about learning feasibility, not an efficacy comparison. No heldout answer is
used to tune the menu, thresholds, router, or gate.

If the gate passes, heldout planning uses training profiles: no native outcome
or divergence feedback, exploration, or shadow calls are added. Report all 48
matched questions, unchanged normalized raw-answer EM/F1, and paired differences
against every control. Question-paired bootstrap intervals are descriptive;
48 questions cannot certify a two-percentage-point harm bound. A budget-stopped
or provider-stopped prefix is not promoted to a favorable efficacy result.

## Costs and recovery

Training, including calibration and warmup, has a $9 hard ceiling. The entire
new stage has a $15 ceiling and shares the existing $50 campaign ledger. Every
dispatch reserves its conservative upper bound first. A logical call has at
most two exact-payload attempts with persisted backoff. Failed attempts never
become optimizer observations and retain their full financial bound, split
between any known charge and unresolved allowance.

Before this run, the ledger records $9.89259047 in known charges and $0.147306
in retained historical allowances. The interrupted old call was explicitly
abandoned after independent review; its $0.019698 allowance was not released.
Read-only account usage was $9.89396747, so account-level checks remain active
in addition to ledger accounting. No claim of zero billing for failures is made.

Report both billed cost and nominal uncached repricing. Separate primary costs
from calibration, warmup, training, probe, shadow, and failed-attempt costs;
do not present training as free. The old probe-heavy rate projected about $14
for the original allocation, not a guaranteed cost bound. Frozen-feedback
heldout disables those evaluation probes. The hard ceilings can still stop the
study, and all such costs remain in the record.

Resume reconstructs fresh isolated stores from the immutable paid journals and
rejects changed decisions. The reconstruction window is eight hours, below the
24-hour profile freshness setting. This is not evidence of native crash
recovery, provider determinism, or steady-state latency performance.

## Reproduction

Use Python 3.13 on this host and `PYTHONHASHSEED=0`. The native build is from
`4052d6d3da3ed0b36e7b2a87e04f0b5efcd4f0ce`, with binary SHA256
`84f9ecb542b5cb42c075a5ad8c536461dd48e3e45c9be25bc386ba7ce5e28501`.
The manifest separately binds the native source tree, native binary, Python
acquisition sources, fixture, exact schedule, policies, prompts, and endpoints.

The driver is `python -m bench.openrouter_joint_study prepare` followed by
`python -m bench.openrouter_joint_study run`, using explicit `--env-file`,
`--ledger`, `--fixture`, `--native`, and `--output` paths. Preparation additionally
requires the reviewed `--runtime-commit` and `--native-sha256` above. The ledger
must be the shared authoritative campaign ledger, never an empty replacement.
Credentials and the private raw provider ledger do not belong in Git.

## Read-only analysis

`python -m bench.openrouter_joint_analysis` is separate from acquisition. Use
`PYTHONHASHSEED=0`, the frozen fixture, a stable artifact snapshot, and the
externally recorded manifest hash:
`dcb703739e460810825f0966c06371d2848a4d17346afbfea4e77c52a0548ace`.
Arguments are `--artifacts`, `--fixture`, `--manifest-sha256`, and a new immutable
`--output` path. Supply `--ledger` for financial completeness; this opens the
existing ledger read-only with a nonblocking shared lock, never creates one,
and needs no API credential. A busy writer causes a refusal, not a wait.

The analyzer checks source bindings, own-arm histories, actual provider
attribution, complete question pairing, chronological exact-plan evidence, and
known costs plus retained failed-attempt bounds. It provides descriptive paired
bootstrap differences only after complete, valid heldout acquisition with a
consistent financial snapshot. No automatic efficacy claim is made.

`prefix-173-analysis.json` analyzes the immutable 173-call/165-decision prefix
from acquisition commit `4174aa7`. It verifies the frozen calibration choice
and seven exact feedback groups. It contains **no paired quality comparisons**:
training and heldout are incomplete and no financial snapshot was supplied.
This is a production-format validation artifact, not the experiment's result.
