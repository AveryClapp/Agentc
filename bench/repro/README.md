# Reproduction: accuracy guard and cross-model selectivity

Committed drivers for the shadow-divergence accuracy-guard results. Each writes
`gsweep_*.csv` summary files into `bench/paper_results/` (plus `*.per_task.csv`
pass/fail vectors). See the top-level `Reproducibility` section / `tab:repro` for
the rule-validation tables; this directory covers the guard and cross-model results.

The prospective main-track campaign is governed by the frozen
[MLSys 2027 staged evaluation protocol](mlsys-2027-evaluation-protocol.md). Its
engineering, calibration, pilot, and confirmatory stages must not be mixed with
the historical guard results described below.

## Joint-campaign engineering check (no API key)

From the repository root, with Python 3.12 or newer:

```bash
python -m bench.joint_campaign bench/repro/joint-campaign-fixture-e0.json \
  --output /tmp/agentc-joint-fixture-e0 --validate-only
python -m bench.joint_campaign bench/repro/joint-campaign-fixture-e0.json \
  --output /tmp/agentc-joint-fixture-e0
```

Validation prints the frozen protocol and schedule digests without creating the
output directory or starting workers. The run needs only Python's standard
library and schedules 160 subprocesses:
two synthetic fixture families, four tasks each, two repetitions, and all ten
primary arms. Network calls and billed spend are zero. Quality, token, latency,
and cost values in this fixture are **simulated**; it does not invoke the native
planner or measure task-solving ability.

Use a new or empty output directory. The runner writes `campaign.json`,
`run-context.json`, `raw-records.jsonl`, `analysis.json`, `report.md`, and a
`manifest.json` with artifact hashes, completion counts, and separate recorded
versus billed costs. It freezes task membership, deterministic arm order and
seeds, the protocol digest, and the Git source state. After an interrupted run,
repeat the command with `--resume`; changed inputs, source state, or invalid
ledger records are rejected. Keep generated outputs outside tracked result
archives.

The analysis pairs arms within workload/task/repetition and bootstraps tasks
before repetitions. Repeated trials do not receive a pooled exact McNemar
p-value; separate per-repetition values are diagnostics. Intervals remain
exploratory and unadjusted. The resampled best-control diagnostic uses point
quality differences to select controls and does not establish non-inferiority,
validate a calibration-selected policy, or decide the protocol GO gate.

### Upstream adapter check

`joint-campaign-e0.json` instead uses the pinned tau2 banking-knowledge and
SWE-agent call paths with LiteLLM mock responses and a socket guard. Its 40 runs
check adapter dispatch and artifact conformance, not full tasks or official
scores. Configure these existing upstream installations before executing it:

| Variable | Required input |
|---|---|
| `AGENTC_TAU2_ROOT` | tau2 checkout at `fc0055dc4e0a316c3f83133267fbd6faaa770992` |
| `AGENTC_TAU2_PYTHON` | That checkout's Python environment with Agentc installed |
| `AGENTC_SWEAGENT_ROOT` | SWE-agent checkout at `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9` |
| `AGENTC_SWEAGENT_PYTHON` | That checkout's Python environment with Agentc installed |
| `AGENTC_SWEBENCH_PARQUET` | SWE-bench Verified task file matching the frozen digest |

Run the same command with `bench/repro/joint-campaign-e0.json` and a separate
output directory. The worker verifies the reported native planner mode:
`current_greedy` runs the predecessor projected-savings composer with the
complete-plan guard; `joint_guarded` runs the profiled selector with its paired
evidence gate. The evaluation mode is frozen at optimizer configuration, and
invalid modes pass through. The greedy control excludes joint exploration.

The runner, fixture worker, adapter worker, and adapter parameter support were
adopted from `d0190167db57bec6245fd7c89c82969c17ac88e3`, then hardened here for
workload cost isolation, held-out locks, repeated-trial inference, and truthful
reporting. Raw historical experiments were not imported or changed.

Live calibration and held-out workers still require admission and full
agent/user/judge accounting. P/T inputs require at least five repetitions and a
calibration lock whose SHA-256 binds its canonical JSON `selections`. The
runner's after-worker 125% spend stop is an experiment diagnostic, not a hard
financial reservation or permission to spend. Paid execution must use the
authorized cumulative campaign ledger before any provider dispatch.

### Original campaign journal audit (no credentials or provider calls)

```bash
python -m bench.campaign_accounting \
  --ledger .git/openrouter-pilot-budget.jsonl \
  --output /tmp/agentc-campaign-accounting-audit.json
```

The reader opens the existing original journal under a shared lock, validates
the complete JSONL stream and credential/path bindings, and replays its
historical attempt and source/candidate-group accounting. It preserves all
failed and abandoned allowances, previously billed partial outputs, and
unclaimed group capacity. Missing files, truncated records, duplicate JSON
keys, non-finite amounts, unknown events, conflicting origins, and changes
during the audit fail validation. The command cannot create or overwrite the
journal; an explicit audit output also requires a new file.

`--expected-sha256` can bind the audit to an enrolled immutable prefix, and
`--expected-key-id` can verify a known credential hash without loading a key.
Output includes the byte digest, event counts, known costs, retained uncertainty,
and pending reservations. `billed_result_records` counts billing records,
including legacy partial outputs; it is not a successful-inference or task
count. Ledger-only remaining funds do not include unrecorded account spending.
The offline audit sets `live_account_reconciled=false` and
`dispatch_authorized=false`; paid transport still requires live account
reconciliation, a fresh enrollment, and the authorized cumulative ceiling.

### Fresh structured dispatch (local validation)

`bench.campaign_transport.CampaignTransport` adds the pre-dispatch producer.
It requires the existing original journal, its expected SHA-256, the actual key,
a previously unused stage and its explicit cap, and frozen `ChatContract`
records. It appends an enrollment bound to the original path, key and complete
prefix. It cannot create a journal, resume an old stage, replay a cached call,
retry an attempt, or release an allowance. Each call requires a fresh identity
and explicit actor (`agent`, `user`, `judge`), kind (`primary`, `exploration`,
`shadow`, `fallback`), task, arm and turn attribution. The pinned Tau text binding
below connects real upstream actors; the campaign mock workers remain E0 controls.

Before reserving, it queries live cumulative account usage and the unique
serving endpoint, checks capabilities and all price tiers, and includes all
original charges, retained holds, pending calls and external account spend.
Reservations cover the entire pinned serving context plus the requested output
cap, including provider-side tool/schema overhead. Live key balance also covers
charges not yet reflected in its account observation. An unknown price,
account reset, unsupported feature, or unresolved foreign call stops dispatch.

The stdlib HTTP implementation disables response caching, redirects, provider
fallbacks and implicit retries. Requests retain real function calls, tool-result
identities, schemas and sampling parameters. Receipts preserve the raw HTTP body
before checking billing, endpoint identity and unique generation IDs. Refusals,
length stops and malformed argument strings reach the application unchanged;
their billing receipt does not certify task success. Errors and whole-operation
timeouts retain the full reserved allowance, even if no body returns.

File locks cover journal transactions, rather than HTTP waits. Each network
operation runs in an owned child process with a total deadline, allowing a
background probe and foreground call to overlap without taking the parent's
signal timer. Terminating a child does not establish provider cancellation.
Only this dispatcher's live process handles can justify its own pending calls;
an unknown or lost reservation blocks further dispatch. `CampaignStopped` is a
fatal exception outside ordinary SDK fallback handlers, and the dispatcher
also keeps a sticky stop state. Workers must drain background work and call
`assert_complete()` before reporting completion, because an SDK probe worker
can swallow its exception. This check does not cancel work or release funds.

Receipt `latency_ms` measures the HTTP operation including child startup and
IPC, and `pre_dispatch_ms` measures validation, account/endpoint preflight and
reservation. These are transport diagnostics; end-to-end task timing must also
include durable finalization and worker overhead. The offline auditor validates
new enrollments, requests, raw bodies and billing transitions while retaining
the original historical accounting rules.

Run the local provider and temporary-journal controls without credentials:

```bash
python -m pytest tests/test_campaign_transport.py tests/test_campaign_accounting.py
```

These tests use synthetic journals and loopback providers. They exercise
tool-aware agent/user/judge requests, concurrency, price/funding gates, failed
allowances, cancellation, process loss, torn writes and altered receipts. They
do not dispatch paid calls or establish workload efficacy.

### Pinned Tau text binding and local controls

`bench.tau_campaign_binding.TauCampaignBinding` binds the agent, user simulator
and NL judge at Tau revision `672227c6b6676edc20d57ea53b7000262aae77b9`.
Use that checkout's frozen environment on Python 3.12: its audio dependency
does not import on Python 3.13. Install the current Agentc wheel and its normal
dependencies into this environment. The binding checks the source revision,
tracked changes, import location and data directory before installing.

Install the binding before `agentc.init()`, enter a task/arm scope, and drain
with `assert_complete()` before publishing results. Context exit drains and
shuts down the runtime. The actual upstream serializers, message histories,
tool environment and evaluation logic still run. Frozen actor caps bound the
judge's otherwise uncapped output; aliases only qualify the same OpenAI model.
The wire serializer follows pinned LiteLLM's null-envelope-field omission while
preserving tool names, IDs, argument strings, text and message order.
Retries, cache use and unexpected client features stop before dispatch. Only
the evaluated assistant can be optimized. Simulator and judge calls retain
their exclusion scopes and are billed to the same transport.

The SDK carries actor/task context into real background threads. At dispatch,
the binding records the native serialized plan without interpreting it.
Observation handles are issued after completion, so a first primary request
does not invent one; exploration and shadow calls carry their actual parent
observation handles, and exploration also carries its native lease. Actual
provider charges feed both Tau's cost field and Agentc's cost profile.

The `tau-nl-complete-coverage-v1` grading adapter requires an exact assertion
inventory and raw boolean verdicts before the official parser runs. It rejects
missing, duplicate, mismatched and coerced results; valid true/false results
and reward rules remain unchanged. This is an explicit guard around the pinned
evaluator, not unmodified leaderboard grading. Do not replace DB+NL tasks with
a DB-only subset.

Run these commands with the pinned environment's Python, with the repository
root on `PYTHONPATH`, and a new output directory for each invocation:

```bash
python -m bench.tau_campaign_preflight --source /path/to/tau2 --output /tmp/tau-control --optimize-agent
python -m bench.tau_campaign_preflight --source /path/to/tau2 --output /tmp/tau-invalid-judge --optimize-agent --invalid-judge
python -m bench.tau_exploration_preflight --source /path/to/tau2 --output /tmp/tau-learning-control
python -m bench.tau_exploration_preflight --source /path/to/tau2 --output /tmp/tau-failed-probe --fail-probe
python -m bench.tau_exploration_preflight --source /path/to/tau2 --output /tmp/tau-tool-control --tool-mode match
python -m bench.tau_exploration_preflight --source /path/to/tau2 --output /tmp/tau-tool-difference --tool-mode different
python -m bench.tau_exploration_preflight --source /path/to/tau2 --output /tmp/tau-tool-malformed-json --tool-mode malformed-json
python -m bench.tau_exploration_preflight --source /path/to/tau2 --output /tmp/tau-tool-invalid-envelope --tool-mode invalid-envelope
```

These E0 controls use hand-authored tasks, scripted loopback providers and new
synthetic journals. The orchestration control runs the real retail tools and
DB+NL evaluation. The mechanism controls exercise native leased learning,
foreground/background HTTP overlap, restart admission and shadow attribution;
its failure variant requires a swallowed probe error to stop worker completion
and retain the full allowance. They make no paid calls, access no original
campaign journal and provide no benchmark efficacy or provider latency result.
The complete live campaign still needs calibrated arm policies and a frozen
configuration; these commands are not a paid campaign launcher.
The tool variants use the actual Tau tool schema/history serializer and return
typed tool-call outputs. Matching actions collect the ordinary 20 native pairs,
survive restart and trigger one seeded shadow at the unchanged 2% rate. Changed
actions and malformed argument JSON accumulate real adverse feedback and disable
the candidate without changing the visible reference. An invalid envelope must
stop completion and retain its billing allowance. A tool execution counter stays
zero throughout, including while a blocked probe overlaps another foreground
task. The native tool gate requires an explicit comparator/settings declaration
and rejects legacy or unsupported seams. These are synthetic mechanism controls;
natural-workload eligibility and efficacy still need prospective real trials.

`bench.tau_task_worker.TauTaskWorker` runs an ordered, explicitly enrolled retail
cohort using Tau's official task loader and `build_text_orchestrator`. Supply an
already enrolled `CampaignTransport`, a fresh output directory, `TauTaskConfig`
with the task-file digest and exact model IDs, and the frozen `TauCell` sequence.
The same transport and cumulative stage cap cover every cell; the worker never
opens another financial session. Native stores are isolated by arm/repetition
and persist across tasks within that arm. The binding restricts agent dispatch
to the enrolled strong/cheap pair, including background candidates. Enroll the
catalog's dated target ID explicitly; qualifying a bare alias does not convert
it to a dated snapshot. User and judge models remain fixed across arms.

Each cell saves the unaltered upstream simulation and reward, every actual HTTP
receipt and billed result, opaque native plans, and measured `optimizer.plan_call`
time. Cost attribution includes exploration, shadows and fallbacks. Step-limit
outcomes remain scored and incomplete; invalid grading or accounting produces an
unscored failure artifact and stops the cohort. A completion artifact is written
only after the full cohort and accounting gate pass. Source, data, SDK binary,
Python code and installed package versions are captured in the enrollment.

The worker enables `AGENTC_OPTIMIZE_SELECTION_AUDIT` and retains the joint
selector's actual alternatives, estimates, admission state, constraints and
rejection reasons. Candidate counts are attributed once to the foreground
decision, rather than repeated for its probes or shadows. Cold and interrupted
searches are distinguished from completed enumeration; unavailable metadata
keeps its count null. The greedy comparator explicitly reports that it does
not enumerate complete plans. These snapshots preserve canonical plan IDs and
the immutable primary forced by exploration. A missing audit from an older
native build stops acquisition before dispatch rather than publishing a guessed
count. Unsupported models still run their actual pass-through request with
explicitly unavailable identity/selection metadata.

The acquisition artifacts retain full upstream and provider data. The
`bench.tau_campaign_worker` adapter projects them into the joint runner's
normalized records. Safety metrics remain explicitly unavailable.
The static and sequential arms are rejected until their calibration policies
exist. The local controls exercise supported arms, warm learning across real
tasks, official negative scores, step limits, invalid judging and a cumulative
budget stop without opening the original journal:

```bash
python -m bench.tau_task_preflight --source /path/to/tau2 --output /tmp/tau-task-arms --control arms
python -m bench.tau_task_preflight --source /path/to/tau2 --output /tmp/tau-task-warm --control warm
python -m bench.tau_task_preflight --source /path/to/tau2 --output /tmp/tau-task-limit --control step-limit
python -m bench.tau_task_preflight --source /path/to/tau2 --output /tmp/tau-task-judge --control invalid-judge
python -m bench.tau_task_preflight --source /path/to/tau2 --output /tmp/tau-task-budget --control stage-cap
```

Their scripted responses and artificial bills are E0 integration evidence only;
using an official task does not turn a fixture response into a quality result.

### Owned Tau scheduling and normalized records

Run these controls from the repository root in the pinned Tau Python 3.12
environment with the current Agentc extension installed:

```bash
python -m bench.tau_campaign_schedule_preflight --source /path/to/tau2 --output /tmp/tau-schedule-success
python -m bench.tau_campaign_schedule_preflight --source /path/to/tau2 --output /tmp/tau-schedule-judge --control invalid-judge
python -m bench.tau_campaign_schedule_preflight --source /path/to/tau2 --output /tmp/tau-schedule-budget --control stage-cap
```

The successful control schedules 42 runs: two official retail tasks, two
repetitions and seven implemented arms, plus a no-network fixture sibling.
One owned transport covers every agent, user and judge invocation. The local
provider produces 98 physical HTTP requests with synthetic bills. The two
failure controls must stop without sealing a successful campaign or cohort.
Each command requires a fresh output directory and forbids external network.

`run_campaign(..., worker_sessions={workload_id: session})` enters each owned
session once, keeps it alive across scheduled cells, and closes it before
analysis or a success manifest. An `owned:` worker command requires an explicit
session; owned sessions cannot resume. The runner rejects a session that
suppresses another session's failure. `TauCampaignWorkerSession` binds the
scheduled task, seed, ordinal, model pair, network policy and arm store to the
already enrolled `TauTaskWorker`. Supply its campaign state root through
`storage_root`, and configure `task_timeout_seconds` within the worker's frozen
timeout. Tau checks its framework deadline between turns, and the transport
enforces each provider deadline. The runner also rejects an elapsed cell bound
after return; it does not cancel an unsettled financial transaction.

Normalized records preserve official scores, every exact billed receipt, actual
requested/selected models, and native execution identities. Uninstrumented
calls have a separately labeled fixed-dispatch identity. Original shadow and
fallback requests retain the native reference identity. Token totals use
inclusive prompt plus completion counts; cached and reasoning details are
subsets, and omitted details remain null. See the
[OpenRouter usage contract](https://openrouter.ai/docs/api_reference/overview).
Unknown safety, candidate counts and token details carry explicit reasons.
Cold or interrupted enumeration cannot establish zero opportunity. Private
output directories and record files use modes 0700 and 0600 respectively.

The optional `diagnostic_arms` subset is allowed only in non-paper Stage E0.
Its manifest reports `diagnostic_complete`, and its analysis emits no
competitive comparisons or protocol GO decision. The full ten-arm contract
remains required for the primary campaign. Add `--all-policies` to the schedule
control to exercise all ten arms: 40 Tau cells and 20 sibling fixture cells.
The three additional policies use an explicitly declared E0 mechanism lock.
Measured safety, provider calibration and held-out efficacy are still required.
Synthetic recorded costs remain distinct from actual provider spend.

### Tau policy-safety observation (no paid calls)

Pass `safety_config=TauSafetyConfig()` to `TauTaskWorker` to observe the actual
retail tool calls and database changes, then request a separate policy review.
The observer runs each tool once, preserves its response and sparse state
changes, and verifies that those changes reconstruct the final database. It
records whether initial history came from the task; Tau's generated greeting
does not establish prior authentication. Invalid schemas, unauthenticated
private reads and cross-user changes can establish failures mechanically.
Business errors and the official task reward remain separate from safety.

The semantic review receives the pinned policy, actual messages, tool schemas
and relevant state evidence, without the arm or official reward. Its response
must cover all message/event IDs and all five frozen safety categories, with
valid citations. Incomplete reviews preserve unknown verdicts; they cannot
erase mechanically established failures. Review receipts use actor `judge`
and carry their purpose and evidence hash. Task cost and latency include the
review, and the safety configuration is frozen at enrollment.

```bash
python -m bench.tau_safety_preflight \
  --source /path/to/pristine-pinned-tau --output /fresh/path/safety-e0 \
  --control unsafe-read
```

Other controls are `safe`, `invalid-tool-schema` and `malformed-review`. They
exercise two original Tau tasks with loopback provider responses and synthetic
billing. These controls validate observation and accounting, not the semantic
reviewer's accuracy on real provider trajectories. C/P/T remains gated until
sensor accuracy and guard admission have measured evidence. The original paid
journal is never opened by this preflight.

### Complete-plan paired evidence (no paid calls)

`bench.guard_evidence.collect_checkpoint` reads a closed native checkpoint in
a separate SQLite process and joins every supplied request, response and bill
to the owned journal. It replays output comparisons in a separate process with
the exact enrolled SDK files. It supports lexical/normalized text, typed tool
actions and the declared mini-SWE bash contract. Embedding comparisons require
additional enrolled model evidence and are currently rejected.

The audit preserves the mode, configuration, call-site version, execution plan
and provider/model/price versions. It reconstructs the conformal upper p95 from
the retained paired samples, checks lifetime counts against physical receipt
coverage, and reports missing, stale, divergent or durably disabled windows.
Unpaired outcome eviction cannot evict paired evidence. All actor costs and
shadow charges remain in the totals; historical uncertainty in the cumulative
journal remains intact. The audit retains the exact journal prefix hash and
byte count used for each measurement.

```bash
python -m bench.guard_evidence_preflight \
  --source /path/to/pristine-pinned-tau --output /fresh/path/paired-e0 \
  --observations 20
```

Use fresh directories with `--observations 19` to check the evidence floor and
`--observations 60` to check the native 50-sample retained window. The separate
`--control disable-restart --observations 2` injects divergent candidate replies
and checks that each exact-plan disable survives restart. These engineering
controls deliberately reset the RNG before each primary call: configured
sampling is 2%, but observed sampling is dense and reported explicitly.
They establish neither production detection delay nor task-damage, savings,
sensor-accuracy or drift outcomes. A qualifying paired window never issues a
calibration admission; those further measurements remain required.

### Frozen static and sequential execution mechanisms (no paid calls)

The controller frontier can also be exercised without provider calls:

```bash
cargo build --release -p agentc-optimizer --example guard_frontier_replay
python -m bench.guard_frontier_preflight \
  --binary target/release/examples/guard_frontier_replay \
  --output /fresh/path/controller-frontier --divergence-mode normalized
```

Use an environment with the installed Agentc extension. The default replay
predeclares 20 harmful and 20 benign arithmetic scenarios for each of the three
frozen policy families, including candidate-output changes and missing reference
feedback. It runs 84 repetitions over the six frozen rates and six thresholds:
10,080 sequences per operating point, 362,880 in total. `--repetitions 1` is a
smaller engineering smoke and does not meet that sequence floor.

The Rust example calls the production `PlanGuard::record_sample` with fresh
tokens issued by `PlanProfiles`; quality labels never reach the controller.
The separate analyzer scores predeclared answer bytes, independently verifies
the random sampling and native exposure/disable timeline, and reports damage,
missed harm, benign disables, censored delays and modeled costs. Learning costs
and failed-reference costs remain explicit. Complete-plan identities include
the metric, rate and threshold. These traces exercise the controller primitive;
they do not replay full planner admission or prove disk durability. Their time
and prices are declared models, not provider latency or invoices. The resulting
frontier cannot become provider calibration admission or paper efficacy.

Review a closed replay without rerunning or resampling it:

```bash
python -m bench.guard_admission_preflight \
  --acquisition /path/to/closed/controller-frontier \
  --output /fresh/path/guard-threshold-decision
```

This pass recomputes the raw sequences, evaluates the frozen criteria separately
for each family and selects no threshold when the frontier fails. Qualifying
engineering points use maximum net savings, then lower cumulative damage and
lower threshold; they still cannot authorize provider admission. The review also
reports the sampling rate needed for 90% detection and the maximum rate retaining
80% of gross savings under the declared prices. This necessary bound assumes
perfect immediate feedback, so a failed bound cannot be repaired by lowering the
divergence threshold alone. Provider C policy sealing/loading rejects the old
summary-only guard admissions until raw provider/durability verification exists.

The native evaluation modes `best_static_joint`, `route_then_rewrite` and
`rewrite_then_route` execute an explicit immutable request policy using
`agentc._evaluation_policy.FrozenEvaluationPolicy`. The static mode holds its
target and ordered recipe fixed. The sequential modes use a frozen threshold
router whose feature is message text in UTF-8 bytes: routing first reads the
original request and applies the selected target's recipe; rewriting first
applies the requested strong target's recipe and routes the transformed request.
None of these paths consults live complete-plan interaction profiles or starts
counterfactual exploration. Catalog capability checks, native execution
identities, 2% shadowing and the persisted complete-plan guard still apply.

The declaration binds an exact configuration string/hash and calibration-lock
hash to the native cohort. Malformed, missing or mismatched declarations fall
back to the reference. Unknown cost estimates and enumeration counts stay
explicitly unavailable. Input rewrites retain aligned provenance and attention
arrays using their actual retained message indices, including duplicate text;
their implementation versions prevent reuse of older evidence.

Run the mechanism control in the pinned Tau Python environment with the current
Agentc extension and repository root on `PYTHONPATH`:

```bash
python -m bench.frozen_policy_preflight \
  --source /path/to/pristine-pinned-tau \
  --output /tmp/agentc-frozen-policy-control
```

It uses real upstream model generation, synthetic histories and one loopback
budget session. Actual original/transformed router inputs and dispatched model,
cap, shadow and charge receipts are recorded. Top-level null message fields
are omitted before both planning and dispatch; non-null fields and argument
strings remain intact. The sample configurations are declared Stage E0 fixtures
with `trained_policies=false`. The execution API is complemented by
`bench.policy_calibration`, which fits and seals choices from owned acquisition
records. Its contract binds the workload, model pool, output cap, quality margin,
guard threshold, protocol hash, task-universe hash and disjoint training,
validation and held-out task inventories. A corpus must cover every enrolled
task in every registered trial; provider calibration requires repetitions 0--4.

Each rollout has exactly `trial_id`, `task` and `calls` fields. The task is a
normalized owned-worker record and calls are its physical receipt records.
The trial registry maps trial IDs to roles and modes. Training roles are
`reference`, `cheap_original`, `static_candidate` and `fixed_recipe`; fixed
trials declare their exact native configuration. Validation contains one strong
reference and one `validation_policy` trial for each of the three modes.
Reference trajectories must come from unmodified fixed-target arms. Candidate
configuration hashes must match native execution, and receipt reuse, missing
costs, incomplete outcomes and split overlap are rejected.

```bash
python -m bench.policy_calibration fit \
  --contract /path/to/contract.json --registry /path/to/training-registry.json \
  --rollouts /path/to/training-rollouts.json --output /fresh/path/fit.json
python -m bench.policy_calibration seal \
  --contract /path/to/contract.json --registry /path/to/validation-registry.json \
  --rollouts /path/to/validation-rollouts.json --fitted /path/to/fit.json \
  --held-out-tasks /path/to/held-out-ids.json \
  --guard-admission /path/to/measured-guard-admission.json \
  --output /fresh/path/policy-lock.json
```

Both commands are offline and write exclusive private artifacts. Router fitting
uses original strong/cheap trajectories; its estimated trajectory-cost objective
is not a measured conditional-policy benefit. Rewrite recipes are selected
independently at each fixed target. Sealing requires complete, disjoint scored
validation of the exact static policy and both sequential orders. E0 sealing
remains available. Provider C sealing and loading are currently rejected:
summary-only counts and flags cannot establish measured guard admission, and
the provider frontier/durability integration is unfinished. A future admission
must preserve mode and configuration identity; opposite orders cannot share
evidence merely because their parameters match.

Pass `FrozenPolicyBundle.from_lock(lock)` as `policy_bundle` to the owned worker
and put the same lock in the campaign specification. Before dispatch, both
workers verify the enrolled lock, models, tasks, cap, margin and protocol.
Fixture locks remain restricted to non-paper E0 loopback acquisition. The
workers still reject C/P/T while provider-grade task safety is unvalidated;
the selector's unit fixtures do not establish real calibration, safety or efficacy.

`CalibrationTrialBundle.create(contract, registry, partition="training")`
authorizes acquisition before a deployment lock exists. Validation requires
the unchanged fitted artifact and a registry covering all three exact policies.
The bundle enrolls a counterbalanced trial/task/repetition schedule. Different
candidates may share an arm name, but retain separate trial state stores.
Its unsealed authorization cannot enter P/T or become a `FrozenPolicyBundle`.

`bench.policy_calibration_acquisition.acquire_trials` runs that schedule through
one owned worker session and an already enrolled transport. Each agent, user,
judge and counterfactual receipt carries the trial ID and authorization hash;
the fitter rejects relabeled receipts or mixed authorizations. The collector
retains an incremental private prefix on failure and writes a complete corpus
only after validating every enrolled cell and the cumulative financial join.
It does not authorize spend or resume interrupted acquisition. Tau retains its
verified receipt hash, physical call count and total cost in normalized tasks.

Run the complete E0 acquisition/fit/validation/seal control with the pinned Tau
environment and current Agentc extension:

```bash
python -m bench.policy_calibration_preflight \
  --source /path/to/pristine-pinned-tau --output /fresh/path/calibration-e0
```

It acquires six registered training trials on one official task, fits the
policies, and acquires the strong reference plus all three fitted policies on
a different official task. One transport covers both phases. The resulting
lock is trained on synthetic E0 responses and charges and remains restricted
to non-paper loopback use. Repeat in fresh directories with `--control stage-cap`
and `--control invalid-judge` to verify incomplete acquisition cannot seal a
policy lock. Actual provider calibration and guard measurements remain required.

### Frozen SWE-bench cohort prerequisites (no provider calls)

Bind the protocol's exact public task universe and probe the frozen evaluator
before preparing an owned acquisition. Run this in an environment with the
grader dependencies and `pyarrow`, supplying the pristine source archives at
the protocol's pinned revisions:

```bash
python -m bench.swebench_cohort \
  --dataset /path/to/verified.parquet \
  --agent /path/to/SWE-agent-0f3acafacabc0def8cc76b4e48acb4b6cf302cb9 \
  --evaluator /path/to/SWE-bench-02e7a74ffd0b707aab73d203fe87bdc7c76afc8e \
  --task-assets /path/to/swe-bench-tasks-3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d \
  --pin-images \
  --output /fresh/path/swebench-prerequisites
```

The private output includes a public-only cohort with the fixed 30 calibration,
30 pilot and 90 confirmatory tasks, repository distributions for selected and
unselected tasks, source closures, and one evaluator-input check per assigned
task. Gold patches, test patches and reference-test identities stay outside the
public cohort. Failed prerequisites retain every assigned task and no score.
The command starts no task environments and makes no provider calls.

The frozen evaluator cannot consume the frozen legacy Parquet rows directly:
they lack `image`, `eval_script`, `log_parser` and `eval_type`. The compatibility
bridge verifies all 13 original fields for each selected task before adding the
pinned task assets' evaluator metadata. It preserves the original 500-row
universe, the 30/30/90 split, and public agent inputs. Omitting `--task-assets`
diagnoses the original schema mismatch and exits 2.
`--pin-images` reads public registry metadata, verifies the exact linux/amd64
manifest bytes, and writes immutable image references without downloading
layers. Registry failures retain every assigned task and return exit 2.
For an offline recheck, replace `--pin-images` with
`--image-pins /path/to/prior-audit/image-pins.json`; receipts must match the
same cohort and bridge. Each invocation requires a fresh output directory.
Supplying both options reuses verified receipts and queries only unresolved
images, preserving the earlier capture in its original directory.
These checks do not admit task environments; unchanged and gold-patch controls
are still required before acquisition. The older Requests development grader
below does not satisfy the frozen evaluator pin.

Run those controls for the complete selected cohort using verified images
already present in the local Docker daemon:

```bash
python -m bench.swebench_environment \
  --dataset /path/to/verified.parquet \
  --agent /path/to/SWE-agent-0f3acafacabc0def8cc76b4e48acb4b6cf302cb9 \
  --evaluator /path/to/SWE-bench-02e7a74ffd0b707aab73d203fe87bdc7c76afc8e \
  --task-assets /path/to/swe-bench-tasks-3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d \
  --image-pins /path/to/prior-audit/image-pins.json \
  --output /fresh/path/swebench-environment-check
```

Each control uses a fresh container and the unchanged frozen grading script.
The checker verifies a clean repository tree equal to the frozen base; the
image may contain the official build's additional setup commit. Admission
requires complete reference-test coverage, preserved passing tests, an
unresolved unchanged control, and a resolved original gold-patch control.
Missing image pins, images absent locally, and failed controls stay in the
150-task report. The command cannot pull images implicitly and exits 2 until
all task environments pass. It makes no model calls and preserves raw grading
artifacts in its private output directory. A worker can use
`verify_environment_report` from `bench.swebench_environment` to replay saved
admissions against their exact inputs and checker version before execution.

### Requests grading fixture (no provider calls)

`bench.requests_fixture_preflight` validates the original development task
`psf__requests-1766` using the original pinned SWE-bench image and dataset.
Run it in the SWE-bench grader environment at
`b524f150d5d76f188c741d75669025f718c89c2e`, with `pyarrow` installed. The
dataset must match revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` and
SHA-256 `a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd`.
The driver rejects changes to the installed official harness, evaluation script,
image identities, and dependency wheels.

Prepare the pinned images and legacy pytest wheels outside the repository:

```bash
docker pull --platform linux/amd64 swebench/sweb.eval.x86_64.psf_1776_requests-1766@sha256:0618949b48408350dfae74d27e47ff9df8192fbf03248aa4f10650e0a36f52eb
docker pull --platform linux/amd64 kennethreitz/httpbin@sha256:599fe5e5073102dbb0ee3dbb65f049dab44fa9fc251f6835c9990f8fb196a72b
python - <<'PY' > /tmp/agentc-requests-pytest-requirements.txt
from bench.requests_fixture_preflight import PYTEST_WHEELS
for filename, digest in sorted(PYTEST_WHEELS.items()):
    name, version, _ = filename.split('-', 2)
    print(f'{name}=={version} --hash=sha256:{digest}')
PY
python -m pip download --require-hashes --no-deps --only-binary=:all: \
  --python-version 39 -r /tmp/agentc-requests-pytest-requirements.txt \
  --dest /tmp/agentc-requests-pytest-wheels
python -m bench.requests_fixture_preflight --dataset /path/to/verified.parquet \
  --pytest-wheels /tmp/agentc-requests-pytest-wheels \
  --output /tmp/agentc-requests-fixture-control
```

The driver runs the unchanged checkout and the official gold patch in separate
clean containers against HTTP and HTTPS on an internal Docker network. It adds
the fixture's CA to the original public trust bundle, retaining certificate
verification. Pytest 4.0.2 preserves the tests' legacy string-expression API;
plain assertions avoid its incompatible Python 3.9 assertion rewriter. A
reporting plugin emits actual call, setup, and teardown outcomes for the
unchanged official parser. No tests, task IDs, or score rules are altered or
filtered. Every dependency is installed from verified local wheels without
network access in the test containers.

`summary.json` records admission, all test results, raw-log and source hashes,
image pins, and dependency versions. Admission requires every official
reference test to be observed, all preservation tests to pass, an expected
regression failure on the unchanged checkout, and a resolved gold control.
Use a fresh output directory for each diagnostic. The owned containers and
network are removed afterward. This is environment validation only; the
original paid pilot stays unscored and its histories, journal, and holds are
unchanged. A future trajectory study must freeze this environment before
dispatch and retain the original task denominators.

### Coding submission round trip (no provider calls)

The pinned mini-SWE-agent v1 at
`a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d` reports submissions by
[joining split output lines](https://github.com/SWE-agent/mini-swe-agent/blob/a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d/src/microsweagent/agents/default.py#L123).
That loses the terminal newline needed by a Git diff. The current
`bench.coding_submission` capture policy preserves the actual tool-output body
only when the upstream line-splitting transformation reproduces the reported
submission exactly. It checks the latest successful submission command and
retains the original returned text, tool receipts, exact UTF-8 patch hash and
serialization change. It never appends an unsupported newline or collects a
new diff after a task has stopped.

Use the isolated pinned SWE-bench grader environment, a pristine checkout of
the mini-SWE-agent revision above, the original dataset and the locked pytest
wheels from the Requests fixture. Mini-SWE-agent's imports also require Jinja2,
MarkupSafe, rich, platformdirs, python-dotenv and PyYAML; the verified combined
environment uses Jinja2 3.1.6 and MarkupSafe 3.0.3. These are upstream benchmark
dependencies, separate from Agentc's runtime environment.

```bash
python -m bench.coding_submission_preflight \
  --source /path/to/pinned-mini-swe-agent \
  --dataset /path/to/verified.parquet \
  --pytest-wheels /path/to/locked-pytest-wheels \
  --output /tmp/agentc-coding-submission-control
```

The command executes the real upstream agent and Docker environment on the
original Requests task with three scripted E0 controls. The gold control must
survive submission capture and resolve under the official evaluator. Its diff
also changes trust data, verifying that patch application precedes fixture CA
setup. An inapplicable submission must retain a strict application failure.
A run that edits the checkout but reaches its step limit before submitting
must receive no credit for those unsubmitted edits. Every acquisition uses a
fresh clean container, private durable transcripts and a fresh output path;
containers have no external network or host mounts. Cleanup is synchronous.

`bench.coding_task_worker.run_task` accepts only the public task field allowlist
and a caller-supplied model, preserving actual requests, responses, actions,
trajectory and submission receipts. Provider accounting must come from that
model's owned campaign transport; this helper is not a paid campaign launcher.
To grade newly captured receipts separately, repeat `--submission PATH` on
`bench.requests_fixture_preflight`. The grader verifies task/base/image/dataset
identity and receipt consistency, then requires fresh unchanged and gold
environment controls before grading any candidate. Missing reference tests,
inapplicable patches and unsubmitted runs remain visible. The grader's
`provider_calls=0` describes grading only. These scripted controls establish
serialization and grading behavior, not agent efficacy or compression savings;
the original paid pilot remains untouched and unscored.

### Owned coding queries and scheduling (no paid provider calls)

`bench.coding_campaign_binding.CodingCampaignBinding` installs before Agentc
initialization and serves the real upstream model's `query` interface through
one already-enrolled `CampaignTransport`. Every foreground, exploration,
shadow and fallback invocation carries its actual cell and turn attribution.
Model cost includes all physical invocations, including billed replies that
fail validation. Retries, response caching, unfrozen models and output caps
are rejected. Background replies never reach the agent's tool executor.

The `mini-swe-bash-v1` contract uses the pinned upstream parser: extract exactly
one bash fence and strip only its outer command whitespace. Agreement requires
the resulting command to match exactly. Different prose around that command
does not change its action; changed or malformed commands cannot supply
agreement. Parser and execution settings enter the native cohort identity, so
ordinary prose-comparison observations cannot admit coding plans. An older
native build lacking this contract stops before provider dispatch.

Use the pinned combined mini-SWE/grader environment above with the current
Agentc extension and LiteLLM 1.81.11/OpenAI 2.20.0 installed. Export a JSON task
with only `instance_id`, `repo`, `base_commit`, `problem_statement`, and `version`
from the original dataset; never export gold patches or evaluator tests to the
agent's task file. Put the repository root on `PYTHONPATH`:

```bash
python -m bench.coding_campaign_preflight \
  --source /path/to/pinned-mini-swe-agent \
  --public-task /path/to/public-task.json \
  --mode matching --output /tmp/agentc-coding-owned-control

python -m bench.coding_campaign_schedule_preflight \
  --source /path/to/pinned-mini-swe-agent \
  --dataset /path/to/verified.parquet \
  --pytest-wheels /path/to/locked-pytest-wheels \
  --public-task /path/to/public-task.json \
  --output /tmp/agentc-coding-schedule-control
```

The binding control's other modes are `changed`, `malformed` and
`failed-response`. It combines real agent execution with explicit repeated
query mechanism controls, exercises native paired admission and shadowing,
and joins every loopback POST to its reservation and exact synthetic charge.
These repeated queries are not natural agent trajectories.

The scheduler uses `CodingCampaignWorkerSession`, seven arms by default,
two repetitions and a sibling engineering fixture. Arm/repetition stores are
fresh and separate; the same transport and model binding stay enrolled across
cells. `RequestsGradingSession` establishes unchanged/gold environment controls
before acquisition, keeps the internal HTTP fixture alive and grades each new
receipt in a fresh official container. Both Tau and coding use
`bench.campaign_records` for billing joins and normalized call measurements.
Scores bind the captured submission and unchanged official evaluation script;
incomplete grades stop the campaign. Agent latency includes initialization,
tools, provider draining and cleanup, with grading latency recorded separately.

Repeat the scheduler command in fresh directories with `--control stage-cap`
and `--control failed-response` to verify stops retain actual charges and emit
no completion manifest. Only the pinned Requests development task is currently
admitted by this grader adapter. Add `--all-policies` to run all ten arms with
declared E0 policies: 20 coding cells and 20 sibling fixture cells. Safety
remains unknown, so paper-stage cells are rejected. Measured calibration,
a broader fresh task cohort and held-out provider trajectories remain required. The diagnostic
manifest cannot become efficacy or savings evidence. Neither command accesses
the original paid journal or makes paid provider calls.

## Building the paper sources (no API key)

The main source uses the official MLSys 2025 style required by the
[2027 CFP](https://mlsys.org/Conferences/2027/CallForResearchPapers), in anonymous
review mode. `appendix.tex` builds separately and imports the main paper's
cross-references. With Tectonic installed:

```bash
python bench/repro/build_submission.py --output /tmp/agentc-paper-build
```

Alternatively, select an installed `latexmk` with `--engine latexmk`. Use a new
or empty output directory. The helper copies the source and figures into
temporary staging, builds the main paper before the appendix, and preserves
the PDFs, compiler logs, labels, bibliography output, and input/artifact hashes
in the output directory. It leaves compiler intermediates outside the checkout.
Tectonic may download TeX packages on its first run; there are no LLM calls.

The style and bibliography-style files are copied unchanged from the official
[`mlsys2025style.zip`](https://media.mlsys.org/Conferences/MLSYS2025/mlsys2025style.zip),
SHA-256 `04e77090038f78c985154c71f7a57b7fbb2553ddd3e4e62f431ed420ba3768ef`.
When Poppler's `pdfinfo`, `pdffonts`, and `pdftotext` are installed, the helper
also records page counts, reference-start page, PDF author metadata, embedded
font checks, and compiler overflow/undefined-reference counts in `build.json`.
The manuscript flushes body floats and starts references on a new page. An
explicit auxiliary-file boundary must agree with the PDF heading before the
helper reports an exact `body_page_count`; otherwise it reports conservative
bounds that allow body text to share the first reference page. Missing inspection
tools are reported explicitly; a successful
build alone does not certify format or evidence, and `submission_ready` remains
false. The revised body centers the implemented complete-plan selector and
distinguishes historical rule evidence from the missing held-out results.

The active diagram describes exact-plan admission, evidence leases, dispatch,
and profile feedback. Regenerate it without provider calls:

```bash
python bench/paper_figures/fig_joint_architecture.py
```

This command writes both the figure-script PDF and its included copy under
`figures/`. The immediately preceding rule-centered manuscript is in git history
(`paper-intelligence/archive/2026-10-10-pre-joint-rewrite-main.tex` at commit `c0a32cd`).

The Graphviz figure scripts use explicit Times New Roman / Courier New
families and the Quartz PDF backend on macOS; Cairo there encodes spaces as
Type 3 fonts. Other platforms use Graphviz's default PDF backend. The concurrency
plot uses TrueType embedding. The earlier figures remain available for the
historical draft. Regenerate them without
new provider calls:

```bash
python bench/paper_figures/fig1_architecture.py
python bench/paper_figures/fig2_twogatepipeline.py
python bench/paper_figures/fig_concurrency.py --out figures/fig8_throughput.pdf
cp bench/paper_figures/fig1_architecture.pdf figures/fig1_architecture.pdf
cp bench/paper_figures/fig2_twogatepipeline.pdf figures/fig2_twogatepipeline.pdf
```

These commands require the existing `viz` extra and a Graphviz `dot` executable.
The plot reads the historical concurrency CSVs; this rendering repair does not
turn those results into evidence for the current joint planner.

### Autogen historical accuracy source

Both `ContextCompress-only` and `all-on` are in the combined committed sidecar;
the separately named `autogen_bridge-n300-ALLON.per_task.csv` is empty. Recompute
the two 300-task paired comparisons without provider calls:

```bash
python -m bench.paired_analysis \
  bench/paper_results/autogen_bridge-n300-warmup.per_task.csv
```

The combined file's SHA-256 is
`ac57798ec689e8999896236dcdb0227c4bfe497a62d55d1378b0b22d4599db20`.
Both groups have the same 229/300 baseline. CC-only solves 225/300 (BF=6, FB=2,
McNemar p=0.2891); all-on solves 226/300 (BF=7, FB=4, p=0.5488).
The unadjusted paired bootstrap intervals are [-3.33, +0.33] and [-3.33, +1.00]
pp. These descriptive comparisons do not establish non-inferiority.
`autogen_bridge_warmup_n300.csv` records captured-token reductions of 26.0% and
14.0%; original span-count denominators are missing, so they do not validate
per-call savings or a complete-workload cost reduction. The manuscript withdraws
the unsupported 23.5% per-call figure. Historical CSVs remain unchanged.

## Prerequisites

- Python env: `.venv` with the project installed (`PYTHONPATH=python`).
- `OPENAI_API_KEY` for the gpt-4o-mini runs.
- For cross-model runs, an OpenAI-compatible endpoint + key (see below). No code
  changes are needed: routing is via `BENCH_OPENAI_BASE_URL` + `BENCH_BASELINE_MODEL`.

## Script -> paper artifact map

| Script | Reproduces | Notes |
|---|---|---|
| `guard_frontier.sh` | `tab:guard`, `fig:metric-tradeoff` | gpt-4o-mini, n=200, budget frontier tau in {0.10,0.20,0.30,0.50} |
| `crossmodel_selectivity.sh` | `tab:xmodel` (one row) | n=100; run once per model family (below) |
| `../guard_overhead_bench.py` | fresh complete-plan feedback overhead | `python -m bench.guard_overhead_bench`; structured Stage E0 output, CPU/SQLite only, no API |
| `../../crates/agentc-optimizer/examples/exploration_preflight.rs` | bounded exploration persistence and accounting | `cargo run -p agentc-optimizer --example exploration_preflight --quiet`; structured Stage E0 output, SQLite only, no API |
| `../../crates/agentc-optimizer/examples/joint_planner_preflight.rs` | live exact-profile joint selection and planning overhead | `cargo run --release -p agentc-optimizer --example joint_planner_preflight --quiet`; structured Stage E0 output, no API |
| `../live_exploration_preflight.py` | production-adapter reference-visible exploration and restart admission | `python bench/live_exploration_preflight.py --output /tmp/live-exploration.json`; deterministic fake provider, no network |
| `../paper_figures/fig9_metric_tradeoff.py` | renders `fig:metric-tradeoff` | reads the `gsweep_tradeoff_*` CSVs |

## Reproducing `tab:guard` + `fig:metric-tradeoff`

```bash
OPENAI_API_KEY=... bash bench/repro/guard_frontier.sh
python bench/paper_figures/fig9_metric_tradeoff.py
```

## Reproducing `tab:xmodel` (4 families)

Each family is one invocation of `crossmodel_selectivity.sh`. The operating budget
`TAU` is a per-model hyperparameter (the verbose Qwen3 regime calibrates tighter):

```bash
# gpt-4o-mini (OpenAI), tau=0.20  -- default endpoint
MODEL=gpt-4o-mini-2024-07-18 BASE=https://api.openai.com/v1 TAU=0.20 PREFIX=gpt \
  OPENAI_API_KEY=... bash bench/repro/crossmodel_selectivity.sh

# Llama-3.3-70B (Meta) via Together, tau=0.20
MODEL=meta-llama/Llama-3.3-70B-Instruct-Turbo BASE=https://api.together.xyz/v1 \
  TAU=0.20 PREFIX=xmodel TOGETHER_API_KEY=... bash bench/repro/crossmodel_selectivity.sh

# Claude Haiku 4.5 (Anthropic) via the OpenAI-compat endpoint, tau=0.20
MODEL=claude-haiku-4-5 BASE=https://api.anthropic.com/v1/ \
  TAU=0.20 PREFIX=claude ANTHROPIC_API_KEY=... bash bench/repro/crossmodel_selectivity.sh

# Qwen3-235B (Alibaba) via Together, tau=0.10 (its operating point)
MODEL=Qwen/Qwen3-235B-A22B-Instruct-2507-tput BASE=https://api.together.xyz/v1 \
  TAU=0.10 PREFIX=qwen3 TOGETHER_API_KEY=... bash bench/repro/crossmodel_selectivity.sh
```

## Notes

- `AGENTC_OPTIMIZE_SHADOW=1` (full sampling) is set so the predecessor guard's
  auto-disable triggers within a single run; the runtime default is 0.02. The
  disable timing, damage, fire retention, latency, and net cost are all
  sampling-rate-dependent, so these cells support only metric selectivity under
  dense feedback. They do not validate the 2% operating point. Shadow calls are
  absent from the reported token and cost totals, making guarded savings gross
  main-path quantities rather than net deployment savings.
- Temperature-1 stochasticity gives ~+/-2-3 pp accuracy noise at these n; the
  decision (rule kept vs disabled) is robust, recovery completeness is not.
- Qwen3-235B served via Together throughput tier can occasionally stall a request;
  re-run the affected single cell if a run hangs with no progress.

## Offline optimizer activation preflight

`bench.activation_preflight` screens real benchmark request shapes before any
paid run. It executes the normal SDK patch and native optimizer while replacing
OpenAI dispatch with a deterministic local response. Provider credentials and
compatible-endpoint settings are masked in every worker. The JSON retains only
prompt sizes, content digests, call-site IDs, plans, and rule counts.

This is explicitly **not paper evidence**: model output, quality, token usage,
latency, and cost are synthetic; the optimizer overhead ceiling is also raised
to remove debug-build timing noise. Use it to decide which workload/rule pairs
merit a live paired evaluation, not to claim savings or semantic safety.

```bash
# Requires the native extension, OpenAI SDK, and the desired local fixtures.
maturin develop -m crates/agentc-profiler/Cargo.toml
python -m pip install -e '.[openai]'

PREFLIGHT_STORAGE="$(mktemp -d /tmp/agentc-activation-preflight.XXXXXX)"
python -m bench.activation_preflight \
  --tasks 8 \
  --storage-root "$PREFLIGHT_STORAGE" \
  --output bench/repro/activation-preflight-2026-09-03.json
```

The committed [activation-preflight-2026-09-03.json](activation-preflight-2026-09-03.json)
records the source commit and SHA-256 of every local fixture. The exact run used
generated, gitignored fixtures; `bd-bjs` tracks the still-missing clean-clone
bootstrap path, including the RAG fixture generator.

The initial screen produced the following post-warm-up rule counts. An asterisk
marks output-dependent behavior that the local response can trigger but cannot
validate.

| Workload | Class | ContextCompress gates (size / dead fraction) | Rules |
|---|---|---:|---|
| HotpotQA | natural request | 3/8 / 0/8 | OutputBudget 5* |
| Wikipedia QA | natural request | 12/16 / 0/16 | OutputBudget 10* |
| SWE-bench planner | task-prompt proxy | 0/8 / 8/8 | OutputBudget 5* |
| RAG summarizer | engineered reference | 0/24 / 24/24 | ParallelBranch 14; OutputBudget 7* |
| Long-context QA | purpose-built control | 8/8 / 8/8 | ContextCompress 5; OutputBudget 5* |
| Multi-rule QA | purpose-built control | 24/24 / 24/24 | ContextCompress 11; StateDrop 7; OutputBudget 18* |

The RAG row is diagnostic rather than clean evidence: chunk-summary and combine
calls currently share one optimizer call-site profile. `bd-8uxj` tracks that
benchmark-validity defect.

### Live Hotpot follow-up smoke

The [live n=8 result](live-hotpot-smoke-2026-09-03.json) checked the natural
Hotpot row with pinned `gpt-4o-mini-2024-07-18` calls. It reproduced the offline
plan sequence (three cold pass-throughs, then five OutputBudget rewrites), with
the same 5/8 quality outcome and exactly the same token counts and $0.0023913
cost in both arms. Every answer was already below the proposed 64-token cap, so
the rule activated without realizing savings. The file is labeled engineering
smoke and records the unrandomized order, debug build, disabled shadow sampling,
and other reasons it is not paper evidence.

### OSWorld request-shape preflight

The [OSWorld request preflight](osworld-request-preflight-2026-09-03.json)
replays the native request structure used by the frozen OSWorld V2
`AnthropicAgent` without contacting Anthropic or launching a desktop. It verifies
that Agentc intercepts `client.beta.messages.create`, blocks structural rewrites
on opaque multimodal/tool histories, permits `OutputBudget`, and returns the
original system, message, tool, beta-header, and thinking objects by identity.
It also records the output-quantile overshoot discovered during the screen as
`bd-pbus`. This is Stage E0 integration evidence only, not a benchmark result.

The follow-up [output-quantile preflight](output-quantile-preflight-2026-09-03.json)
replays the triggering constant-output case after the estimator correction.
The persisted p95/p99 now equal the three observed 80-token completions and the
resulting cap is 96 rather than 143. The file also states the conservative
all-history-maximum contract, legacy-profile implications, and the bounded-
window work deferred to `bd-bwgu`.

### Complete-plan guard preflights

Three zero-network Stage E0 checks exercise the complete-plan controller rather
than the retained per-rule compatibility path:

```bash
python -m bench.guard_persistence_preflight \
  --output /tmp/complete-plan-guard-persistence.json
python -m bench.guard_input_validation_preflight \
  --output /tmp/complete-plan-guard-input-validation.json
python -m bench.guard_overhead_bench \
  --output /tmp/complete-plan-guard-overhead.json
```

The persistence check warms a canonical `ContextCompress+OutputBudget` plan,
persists two positive-exposure samples (`E=0.8`), restarts, crosses the plan
budget on the third (`E=1.2`), and verifies the exact-plan disable across a
second restart. The input check rejects invalid divergence values without any
paired or guard state and verifies that invalid configured thresholds resolve
to the composed plan's declared `0.01` minimum. Both require zero legacy rule
rows. The overhead check issues a fresh observation token per sample and times
token validation, exact-plan profile/guard updates, and synchronous SQLite
durability separately from the divergence metric.

To measure concurrent feedback across multiple call sites:

```bash
python -m bench.guard_overhead_bench \
  --fresh-tokens 2000 --workers 4 --sites 16 \
  --output /tmp/complete-plan-guard-contention.json
```

The output includes p50/p95/p99 feedback latency, wall time, and throughput.
Before and after runtime shutdown, separate SQLite processes verify the exact
lifetime and retained feedback counts for every site. Final flushing cannot
repair a lost timed write and make the measurement pass. The harness uses native-proposed
composed plans with synthetic outcomes; it does not demonstrate their admission.
Every measured token receives feedback. The configured shadow rate does not
turn this into a simulation of unsampled production requests.

These checks are deliberately marked `paper_evidence=false`. In particular,
the overhead result is a single-machine local diagnostic that excludes the
shadow provider call, request dispatch, and billed tokens. Concurrent runs
exercise Python threads and the shared native persistence path in one process;
throughput wall time includes thread startup and joining. The
historical `18 us/sample` claim came from replaying one synthetic token through
the legacy idempotence path and is superseded; do not cite it.

Committed outputs from the corrected harnesses are
[persistence](complete-plan-guard-persistence-preflight-2026-09-03.json),
[input validation](complete-plan-guard-input-validation-preflight-2026-09-03.json),
and [overhead](complete-plan-guard-overhead-preflight-2026-09-03.json). On the
recorded development-machine run, 2,000 fresh feedback samples had a 306.2 us
mean and 412.3 us p99, plus a separately measured 5.0 us divergence metric.
Those values characterize this E0 run only.

### Bounded exploration preflight

The zero-network Rust preflight exercises the initial-calibration controller:

```bash
cargo run -p agentc-optimizer --example exploration_preflight --quiet
```

The committed [output](bounded-exploration-preflight-2026-09-03.json) records a
seeded two-plan scenario. Every decision returns the reference result while at
most one candidate holds a counterfactual lease. The first candidate exhausts
its exact-plan divergence-exposure and labeled task-damage budgets and is not
retried; the other candidate consumes the remaining four-call site budget. The
call cap, $0.04 counterfactual cost, divergence exposure, and separately labeled
task damage survive a database close and restart.

This is Stage E0 mechanism evidence only. It issues no provider calls and tests
the controller independently of provider dispatch, so it cannot support a
quality, cost-savings, or safety claim.

### Live profiled joint-planner preflight

The zero-network release preflight exercises the production planning function:

```bash
cargo run --release -p agentc-optimizer --example joint_planner_preflight --quiet
```

It warms a reference call site, enumerates the reference, routing-only,
rewrite-only, and joint candidates, persists exact reference and joint profiles,
reloads them after a SQLite restart, and repeatedly runs the same function used
by the PyO3 interceptor. The committed
[output](joint-planner-preflight-2026-09-04.json) contains three 20,000-call
replications. All 60,000 decisions selected the one evidenced
`OutputBudget + gpt-4o-mini` plan with the expected identity; no unrelated or
reference identity was returned. Median p50 was 127.25 us and the largest p99
was 303.833 us on the recorded arm64 development machine.

The timing includes JSON decode/encode, rewrite proposal and bounded
enumeration, model cross-product, canonical identity hashing, exact-profile and
guard lookup, and constrained selection. It excludes providers, task quality,
counterfactual calls, and concurrent SQLite writes. Accordingly it is Stage E0
mechanism evidence, not support for savings, quality, or main-track efficacy.

### Live reference-visible exploration preflight

The deterministic Python preflight exercises the real OpenAI adapter, native
joint planner, background counterfactual worker, durable lease controller, and
restart warmup without contacting a provider:

```bash
python bench/live_exploration_preflight.py \
  --output /tmp/live-exploration-preflight.json
```

It performs three reference warmups followed by 20 reference-visible
calibration calls. Each calibration call returns the original 1,024-token-cap
request response and runs one 121-token-cap candidate off-path. After shutdown
and restart, the 20 exact paired observations admit that candidate on the next
user-visible call. The committed
[output](live-exploration-preflight-2026-09-04.json) records 23 reference calls,
20 background candidates, one post-restart admitted candidate, and 44 total
fake-provider invocations.

This is Stage E0 mechanism evidence with `network_calls=0` and
`paper_evidence=false`. Its sub-millisecond local timings are not provider
latency results and must not appear as efficacy evidence.
