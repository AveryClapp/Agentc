# Prospective input rules × model routing study

This is a development experiment, not a confirmed joint-system advantage or a
full-rule MLSys ablation. The previous contaminated workflow is permanently
aborted; none of its controller state or scheduled questions is reused here.

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
