# Agentc MLSys 2027 staged evaluation protocol

- Protocol: `agentc-mlsys2027-v1`
- Revision: 7
- Initial freeze: 2026-09-03
- Revision 2 freeze: 2026-09-03
- Revision 3 freeze: 2026-09-03
- Revision 4 freeze: 2026-09-03
- Revision 5 freeze: 2026-09-03
- Revision 6 freeze: 2026-09-03
- Revision 7 freeze: 2026-09-03
- Revision 2 record: [mlsys-2027-protocol-revision-2.json](mlsys-2027-protocol-revision-2.json)
- Revision 3 record: [mlsys-2027-protocol-revision-3.json](mlsys-2027-protocol-revision-3.json)
- Revision 4 record: [mlsys-2027-protocol-revision-4.json](mlsys-2027-protocol-revision-4.json)
- Revision 5 record: [mlsys-2027-protocol-revision-5.json](mlsys-2027-protocol-revision-5.json)
- Revision 6 record: [mlsys-2027-protocol-revision-6.json](mlsys-2027-protocol-revision-6.json)
- Revision 7 record: [mlsys-2027-protocol-revision-7.json](mlsys-2027-protocol-revision-7.json)
- Runtime baseline before this protocol: `4250a01`
- Revision 2 runtime baseline: `27f896f`
- Revision 3 runtime baseline: `6339d79`
- Revision 4 runtime baseline: `f352f78`
- Revision 5 runtime baseline: `91e864c`
- Revision 6 runtime baseline: `f9a8f04`
- Revision 7 runtime baseline: `d920357`
- Target decision date: 2026-10-01
- Target venue deadline: 2026-10-30 20:00 UTC

This document is the prospective protocol for deciding whether Agentc has an
MLSys 2027 main-track result. It is not a description of experiments already
run and it is not a promise that the campaign will pass. The protocol is
deliberately allowed to return a negative result.

The committed mocked-provider activation screen and live HotpotQA `n=8` smoke
predate this freeze. They are engineering diagnostics only and cannot enter a
paper table, select a test task, or support a quality, latency, or savings
claim.

Revision 2 was issued after request-shape and output-quantile defects were found
during Stage E0, before any Stage C, P, or T execution or outcome inspection.

Revision 3 was issued after actor-scope, LiteLLM-boundary, and storage-isolation
defects were found during Stage E0, before any Stage E1, C, P, or T execution or
outcome inspection.

Revision 4 was issued after the storage-isolation defect was repaired and
tested in the same process during Stage E0, before any Stage E1, C, P, or T
execution or outcome inspection.

Revision 5 was issued after the persisted cost model was changed from
all-history summaries to an exact bounded sample window and verified during
Stage E0, before any Stage E1, C, P, or T execution or outcome inspection.

Revision 6 was issued after per-rule divergence statistics and the consecutive-
breach controller state were made restart-persistent and verified during Stage
E0, before any Stage E1, C, P, or T execution or outcome inspection.

Revision 7 was issued after the native guard boundary was hardened against
non-finite and out-of-range divergence samples and threshold overrides during
Stage E0, before any Stage E1, C, P, or T execution or outcome inspection.
Revisions 2 through 7 do not change the `agentc-mlsys2027-v1` split namespace,
task membership, workloads, models, arms, outcomes, margins, inference,
stopping rules, or gates. Their machine-readable revision records enumerate
every runtime-contract change and the engineering evidence that triggered it.

## 1. Change control and blinding

The following are frozen by this protocol: workload admission, upstream input
versions, split construction, model pool, arm definitions, primary outcomes,
quality margins, statistical tests, safety and value gates, retry/exclusion
rules, and the artifact record. Implementation work may make the frozen
contract executable, but may not change it silently.

- Any change before pilot outcomes are inspected produces a numbered protocol
  revision and a machine-readable diff. Calibration data from the old revision
  is then tagged non-confirmatory.
- Once pilot execution begins, pilot tasks are used only for the stated go/no-go
  decision. They are never moved into calibration or confirmatory splits.
- Once any confirmatory outcome is inspected, no model, threshold, rule,
  workload, task, arm, metric, margin, or analysis may change for this protocol.
  A necessary change creates a new cohort and protocol version; results are not
  pooled across versions.
- Confirmatory results are revealed only after every scheduled arm for a
  workload/model cell has either finished or acquired a predeclared terminal
  failure code.
- All attempted tasks and cells remain in the ledger, including zero-activation,
  failed-integration, and harmful-result cells.

## 2. Claim under test

The admissible main-track claim is narrow:

> Agentc is an application-side control plane that observes opaque provider
> calls, recovers enough repeated-call structure to apply multiple conservative
> semantic rewrites, abstains when its preconditions are absent, and limits
> cumulative quality damage with sampled counterfactual execution—without
> modifying workload source code or controlling the model server.

The protocol does not test or permit a broad "first JIT optimizer for agents"
claim. It also does not claim serving-layer scheduling, KV-cache management, or
semantic preservation by construction.

### 2.1 Admissible integration boundary

A workload is admissible only when all of these hold:

1. The upstream workload and agent source are unchanged. A launcher,
   `sitecustomize`, SDK/LiteLLM adapter, or HTTP gateway may install Agentc, but
   task prompts, tools, state representation, and agent control flow may not be
   edited for Agentc.
2. Baseline and optimized arms use identical workload, agent, model, tool,
   environment, retry, and cache configuration. The optimizer switch is the
   only intended difference.
3. The interceptor observes every eligible model call and preserves the native
   request shape, including tool calls, tool-result identifiers, multimodal
   blocks, cache-control markers, and streaming flags.
4. Calls outside the evaluated agent—graders, user simulators, embedding
   services, judges, and environment services—are traced separately and are
   ineligible for rewrites. Eligibility comes from an explicit stable actor or
   subsystem boundary, never a prompt, task, model, output, or ad hoc call-name
   heuristic; low-cardinality eligible and excluded counts enter the manifest.
5. Every call receives a stable semantic call-site identity. A shared helper is
   not sufficient when it collapses distinct agent stages into one profile.
6. A pass-through conformance run demonstrates request equivalence at the
   provider boundary and identical task outcomes up to residual provider
   nondeterminism.
7. `AGENTC_STORAGE_PATH` is set before Agentc is imported and names the same
   fresh per-arm store passed to any programmatic initialization. The manifest
   records the resolved Python and native store paths, and validation rejects a
   mismatch or evidence of prior-arm warm state.

Failure of any item blocks that workload. It does not authorize prompt
engineering, a substitute in-repository proxy, or selecting a different
workload after seeing outcomes.

## 3. Workloads and immutable upstream inputs

Three unengineered workload families are required. The fixed public task
universe is the source file at the stated revision and digest.

| Family | Frozen workload and agent | Task universe | Immutable input |
|---|---|---:|---|
| Stateful tool use and retrieval | `tau2-bench` `v1.0.1`, `banking_knowledge`, text mode, official `llm_agent`, official `user_simulator`, `retrieval_config=bm25` with default `top_k=10` | 97 | code `fc0055dc4e0a316c3f83133267fbd6faaa770992`; `data/tau2/domains/banking_knowledge/tasks.json` SHA-256 `213c7f3e6dc0420b1184ee271e39e38c6ece3c43edfa362db49a560828ebd543` |
| Software engineering | SWE-agent `v1.1.0` with unmodified `config/default.yaml` on SWE-bench Verified | fixed 150 of 500 | agent `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`; config SHA-256 `188b6c75b632c88ed53c78125642066f0c0836c57d00b5deadd9a6d01857447d`; dataset revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`; Parquet SHA-256 `a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd`; evaluator `02e7a74ffd0b707aab73d203fe87bdc7c76afc8e` |
| Computer use | OSWorld V2 official release `osworld-v2-2026.08.08`, `mm_agents.anthropic.main.AnthropicAgent`, AWS Ubuntu `1920x1080`, `us-east-1` | 108 | code `d578d2d4e0dc82b43e270fdaa7fa89d9708cd154`; release task-hash manifest SHA-256 `42f8f6f8939b8712997d5891456a575f8a2a5f53465e9e3e6747af5d6efd091`; `evaluation_examples/test_v2.json` SHA-256 `7f2c0f531d035dd6f5886c492b96739534385f8e5369e4c764295ad52bf6e7f4` |

The OSWorld release also freezes task, gated-asset, and website tags to
`v2026.08.08`, the AWS image to `ami-01017272139e01feb`, screen size to
`1920x1080`, and website host suffix to `site.hku.icu`. The gated task and asset
hash checks must pass before a run is admissible.

SWE-agent uses a per-instance model-cost ceiling of USD 2.00. Reaching the
ceiling is a task failure, not an exclusion. Tau2's user simulator is fixed to
`openai/gpt-4.1-mini-2025-04-14` and is never optimized. Environment and grader
calls are included in end-to-end latency and monetary totals but broken out
from eligible assistant calls. The tau2 integration assigns the stable scopes
`tau2.evaluated_assistant` and `tau2.user_simulator`; only the former is
eligible. Neither model identity nor request content selects that scope.

The repository's `swebench_planner`, `long_context_qa`, `multirule_qa`,
`rag_summarizer`, and other purpose-built agents remain mechanism tests. None is
a substitute for these three workloads.

## 4. Deterministic task splits and seeds

Canonical task identifiers are the upstream `id` for tau2, `instance_id` for
SWE-bench Verified, and `task_<three-digit-id>` for OSWorld. For each task,
compute:

```text
split_key = SHA256(
  UTF8("agentc-mlsys2027-v1") || 0x00 ||
  UTF8(workload_name)          || 0x00 ||
  UTF8(canonical_task_id)
)
```

Sort lexicographically by the 32 raw digest bytes, with canonical task ID as a
tie-breaker. Assign contiguous ranges as follows:

| Workload | Calibration | Sealed pilot | Sealed confirmatory | Not selected |
|---|---:|---:|---:|---:|
| tau2 banking knowledge (97) | first 20 | next 20 | final 57 | 0 |
| SWE-bench Verified (500) | first 30 | next 30 | next 90 | remaining 350 |
| OSWorld V2 (108) | first 22 | next 22 | final 64 | 0 |

The exact `workload_name` strings and SHA-256 digests of each complete
newline-delimited ordered ID list are:

| `workload_name` | Ordered-ID digest |
|---|---|
| `tau2-banking-knowledge` | `915b4717167618cf6d2da855450fe7cb5143a7c4be710e7baed5d80d898c417b` |
| `swebench-verified` | `5f649dc4518274c7a605cce1a5dcbf451eb1e14cba30a6dc2a032b84731ca9e4` |
| `osworld-v2` | `ec2798cd66968cf6735af9e4b5deafca38c006532df96fa0c3d6d9f08c273556` |

This is an outcome-blind fixed compute sample, not an easy-task filter. For
SWE-bench, repository, patch size, baseline success, and difficulty play no
role in selection. The report must compare selected and non-selected metadata
distributions without using outcomes to alter membership.

For any provider that accepts a numeric seed, derive it as the unsigned
big-endian first eight bytes of
`SHA256("agentc-run-v1\0" || workload || "\0" || task_id || "\0" || arm ||
"\0" || repetition)`, reduced modulo `2^31 - 1`. Providers that do not honor a
seed are labeled `seed_unsupported`; a fabricated seed must not be recorded.

Arm order is also fixed by sorting arm names by the full digest of the same
tuple with prefix `agentc-arm-order-v1`. Each task starts from a fresh workload
environment snapshot. Agentc storage is fresh once per
`(stage, workload, model, arm, repetition)` and then persists through that
split's digest-sorted task order, so the aggregate includes honest cold-start
tasks and later steady state without leaking calibration state into pilot or
test. Arms never share Agentc storage. This deterministically counterbalances
provider drift and warm-cache order without choosing a favorable sequence.

## 5. Stages and information flow

### Stage E0: engineering screens

Mocked-provider runs, trace-shape inspection, one-task provider checks, guard
simulation, and purpose-built agents are unlimited. They may find bugs and
estimate spend. They are permanently labeled `paper_evidence=false`. The
frozen-workload LiteLLM admission bundle is an E0 call-shape screen, despite
invoking real upstream Python call sites; its synthetic usage, cost estimate,
latency, and activation sequence cannot select any Stage C setting.

### Stage E1: admission

For each workload/model cell, run at most three calibration tasks to prove
installation, interceptor coverage, request preservation, scoring, environment
reset, trace/audit joins, and cost attribution. No value or quality claim may
use these calls. A cell that fails admission is blocked until fixed; the task
is not replaced.

### Stage C: calibration

Only calibration tasks may select the best single rule, tune thresholds from
the grids below, estimate variance and spend, and select matched operating
points for point baselines. Calibration results are reported separately.

### Stage P: sealed pilot

Run all primary arms on pilot tasks with the calibration-selected configuration.
The pilot is a binary feasibility gate only. No parameter changes follow a
viewed pilot. A failed pilot stops the `v1` campaign or triggers a new protocol
that never reuses pilot tasks as evidence.

### Stage T: confirmatory

Run the complete frozen schedule on confirmatory tasks. Do not inspect partial
arm-level aggregates. The confirmatory dataset is the only source for the
primary headline result and the October 1 decision.

## 6. Frozen system configuration and rule scope

All measured builds are release builds. Unless an arm states otherwise:

- `hot_threshold=3` observations per semantic call site;
- `cost_model_window=50`;
- `max_overhead_ms=5`;
- composition enabled;
- normalized token-overlap shadow divergence;
- production shadow sampling rate `0.02`;
- no manually authored provenance, support labels, task-specific rule hints,
  or hand-marked safe call sites.

Revision 3 adds a context-local actor eligibility gate. Stable framework
entrypoints may create a named scope with one Boolean rewrite decision; provider
adapters trace every call and record the scope, eligibility, and decision
reason. For the frozen tau2 text workload, Agentc wraps the unmodified imported
`generate` aliases in `tau2.agent.llm_agent` and
`tau2.user.user_simulator`. The evaluated-assistant alias is eligible and the
user-simulator alias is not. An explicit `agentc-optimize: false` request header
may only further opt a call out; it cannot opt an excluded scope in.

Revision 3 also adds a LiteLLM logical-call seam for synchronous and
asynchronous non-streaming `completion` calls. It is installed before provider
SDK adapters, repairs tau2's known by-value completion alias, preserves
unrelated native request objects, applies only supported mutated fields, and
suppresses nested OpenAI or Anthropic adapters so one logical request produces
one plan, observation, and provider span. Patch failure is fail-open and
shutdown restores the exact prior module functions and known aliases.

The frozen tau2 and SWE-agent paths are non-streaming. LiteLLM streaming still
passes through to an available provider SDK stream adapter; a route without
such an adapter lacks route-independent Agentc tracing. This is outside the
required cells but blocks any framework-neutral LiteLLM streaming claim until
`bd-ez1k` closes.

Every Stage E1, C, P, or T launcher must export its fresh
`AGENTC_STORAGE_PATH` before importing Agentc and pass the same path to
`agentc.init` when using programmatic initialization. This defense-in-depth
launcher requirement remains after `bd-hoer` closes.

Revision 4 makes programmatic storage ownership explicit in the runtime:
`agentc.init(storage_path=...)` resolves one absolute path, installs that path
for initialization, configures the native optimizer to the same path, and
restores the caller's prior environment on shutdown. Shutdown and subsequent
initialization reset process-global native optimizer state. Two repeated
same-process E0 runs warmed store A through three observations and one rewrite,
then showed that the first call in fresh store B passed through with no cost
profile rows. Python and native paths matched in both stores and a conflicting
environment path was never created. This closes `bd-hoer` as a Stage C blocker;
it does not admit a workload at Stage E1.

Revision 2 adds a conservative provider-shape gate. When the string-only DAG
cannot round-trip a native message exactly, the adapter marks the message list
opaque and the planner rejects every rule unless it explicitly declares that it
does not inspect, hash, remove, replace, or reorder messages. At this revision,
only `ModelDowngrade`, `OutputBudget`, and `DeadOutputTruncation` make that
declaration. The Python adapter must then return the original native system,
message, tool, beta-header, cache-control, multimodal, and thinking objects; a
text projection may be used for profiling but never for reconstruction.

Revision 5 supersedes revision 2's interim all-history output maximum and
moving p95 proxy. Each semantic call site now retains the exact newest
`cost_model_window=50` observations and recomputes input, output, latency, cost,
output-shape, nearest-rank p95, and nearest-rank p99 statistics from that set.
The summary and retained samples are written in one SQLite transaction and
rehydrated together. `n_observations` remains a lifetime operator count;
`window_observations` drives hot gating, confidence, and rolling statistics.
Changing to a smaller window keeps and persists the newest samples. A legacy
aggregate-only profile cannot be reconstructed exactly, so migration preserves
its lifetime count, clears its statistical window, and requires fresh samples
before rewrites resume.

Five release-mode, zero-network Stage E0 repetitions aged a 50-sample old
distribution entirely out after 50 new observations, persisted exactly 50
rows, and reloaded an identical profile. Deterministic migration, window resize,
copy-on-write snapshot, concurrent observation, and flush-interleaving tests
also pass. This retires `bd-bwgu` as a Stage C blocker without admitting a
workload or supplying paper evidence.

Revision 6 makes every completed shadow comparison immediately persist its
cumulative `(n, mean, variance)` divergence estimate and consecutive-breach
streak. Startup hydrates that state before planning. The fifth consecutive
over-budget sample persists a 24-hour disable, and startup hydrates the disable
before serving subsequent plans. Legacy divergence rows receive a zero streak
because the former schema did not retain it.

Five release-mode, zero-network Stage E0 repetitions recorded four breaches,
restarted, recorded the fifth breach, and observed the durable disable before a
lifecycle flush. A second restart remained pass-through. Frozen tau2 and
SWE-agent no-network reruns preserved plan sequences, response digests, scopes,
and observation counts. This retires only the persistence blocker `bd-shi1.5`;
it does not establish the production 2% guard operating point, damage budget,
drift response, or paper evidence.

Revision 7 constrains every divergence sample and threshold to a finite
fraction in `[0,1]`. The native boundary validates a Python `f64` divergence
before narrowing it to the guard's `f32` storage type and discards an invalid
sample without creating or mutating guard state. The environment threshold is
also parsed and validated as `f64`; an invalid override falls through to the
firing rule's validated accuracy budget and then to `0.05` if needed. This
prevents slightly negative or above-one values from rounding back into range.

Five release-mode, zero-network Stage E0 repetitions rejected non-finite,
ordinary out-of-range, and narrowing-sensitive divergence values, and showed
that the same invalid threshold classes selected `OutputBudget`'s `0.01`
fallback. Frozen tau2 and SWE-agent reruns preserved plan sequences, response
digests, scopes, and observation counts. This retires only `bd-h7k9`; it does
not establish controller calibration, a damage bound, drift response, or paper
evidence.

The main benefit claim is restricted to `ContextCompress`, `ModelDowngrade`,
`OutputBudget`, and exact `CacheHit`. `StateDrop` is a safety-stress mechanism
and becomes a benefit-eligible rule only if structure is inferred by a general
adapter with no workload-specific annotations before Stage C. It otherwise
remains implemented-only. `ParallelBranch` is excluded from benefit and
latency claims unless its plan executes true concurrency on both sync and async
paths before Stage C; plan selection or fire counts alone do not qualify.

Every other implemented rule is reported as exploratory or implemented-only.
All rules still produce eligibility, proposal, application, rejection, and
abstention counts so absence of opportunity remains visible.

Structural thresholds and transformations are exactly those in the code-freeze
commit. A code change that modifies a gate, rewrite, ranking function, route,
or composition result requires a protocol revision before Stage P.

### 6.1 Guard-threshold selection

For each provider/model pair and destructive rule, Stage C evaluates normalized
divergence thresholds `{0.05, 0.10, 0.15, 0.20, 0.30, 0.50}`. Select the
configuration with maximum retained net savings subject to calibration harmful-
site detection of at least 90%, benign disable rate at most 5%, and the
cumulative-damage budget in Section 11. Break ties by lower cumulative damage,
then lower threshold. If no point qualifies, that rule is disabled for the
benefit experiment and the guard gate fails for that pair; no threshold is
chosen from pilot or confirmatory outcomes.

## 7. Frozen model and provider coverage

No floating `latest`, undated OpenAI alias, or silent provider substitution is
admissible.

| Family/provider | Strong model | Cheap model for routing |
|---|---|---|
| OpenAI | `gpt-5.4-2026-03-05` | `gpt-5.4-mini-2026-03-17` |
| Anthropic | `claude-sonnet-4-5-20250929` | `claude-haiku-4-5-20251001` |
| Meta via Together | `meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo` | `meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo` |

The OpenAI and Anthropic IDs were present in the provider account catalogs on
2026-09-03. The Together IDs were present in `GET /v1/models` on that date but
do not expose an immutable served-binary revision. Every Together run therefore
archives the catalog response digest, returned model string, provider request
ID, date, and region. A catalog or behavior change starts a new cohort.

Required cells are:

| Workload | Required model pairs |
|---|---|
| tau2 banking knowledge | OpenAI and Meta/Together |
| SWE-agent / SWE-bench Verified | OpenAI and Anthropic |
| OSWorld V2 | Anthropic |

This covers three model families, three serving routes, two provider protocols,
and three application stacks. The strong model is the baseline model. The cheap
model appears only in fixed-cheap, cascade/router, or `ModelDowngrade` arms.
Exact request parameters are the upstream agent defaults except
`temperature=0` where the provider supports it; unsupported parameters are
omitted and recorded. Five repetitions remain mandatory despite temperature
zero because hosted inference and interactive environments are nondeterministic.

Prices are not read from mutable source code during analysis. Before Stage C,
archive an official price-table snapshot with effective date and compute each
request from provider-reported uncached input, cached input, output, reasoning,
and tool-token categories. A later price change is reported in a sensitivity
analysis and never rewrites the contemporaneous billed-cost result.

## 8. Arms

### 8.1 Primary arms

1. `unmodified`: upstream agent without Agentc; measures absolute instrumentation
   overhead against `trace_only`.
2. `trace_only`: Agentc records calls but the optimizer is disabled. This is the
   paired system baseline for all value comparisons.
3. `all_on_guarded`: the benefit-eligible frozen rule set, composition enabled,
   selected guard threshold, shadow rate 2%.
4. `best_single_guarded`: the one rule selected on Stage C by maximum net cost
   reduction subject to its quality margin.
5. `naive_sequence_guarded`: every individually eligible rule applied in the
   fixed order `CacheHit`, `ContextCompress`, `OutputBudget`,
   `ModelDowngrade`, `StateDrop`, with no composition ranking; ineligible rules
   are logged and skipped.

The main unified-system comparison is `all_on_guarded` against `trace_only`,
`best_single_guarded`, and `naive_sequence_guarded`.

### 8.2 Secondary ablations

- each benefit-eligible rule alone;
- leave-one-rule-out from `all_on_guarded`;
- `all_on_guarded` with shadow rates `0`, `0.01`, `0.05`, `0.10`, and `1.0`
  for the safety experiment, not the primary value estimate;
- cold-start and persisted-warm-state variants;
- composition disabled (first-match planner).

Primary arms use five paired repetitions per task. Secondary ablations use
three paired repetitions unless Stage C power analysis requires more; fewer is
not allowed. If compute prevents a scheduled arm, the cell is incomplete and
cannot pass the main-track gate.

### 8.3 Mechanism-matched baselines

- Compression: random drop, oldest/recency drop, BM25/extractive relevance, and
  LLMLingua-2. Match Agentc's achieved input-token reduction on Stage C, then
  freeze that operating point.
- Routing: fixed strong, fixed cheap, a fixed confidence cascade, and either
  RouteLLM or AgentOpt using the same strong/cheap pool and calibration budget.
- Caching: provider-native prefix caching as deployed, exact application cache,
  and vCache or a calibrated semantic cache when its interface is compatible.
- Full control-plane comparison: AgentOpt at commit
  `08b2d2c7fe370c884d956afbe540a09abc163c27`, or a documented failure matrix if
  its official implementation cannot execute the frozen workload unchanged.

An incompatible baseline is not replaced by a weak strawman. Record the exact
incompatibility and compare capabilities only. Concurrency baselines such as
LLMCompiler or AsyncFC enter only if Agentc's concurrency criterion in Section
6 is met.

## 9. Outcomes and measurement

### 9.1 Primary outcome pair

Each workload has two co-primary outcomes:

| Workload | Quality | Efficiency |
|---|---|---|
| tau2 | official final task reward in `[0,1]` | total billed USD per assigned task |
| SWE-bench Verified | official resolved/not-resolved per instance | total billed USD per assigned instance |
| OSWorld V2 | official normalized task score | end-to-end wall time per assigned task |

Cost is a predeclared secondary efficiency outcome for OSWorld; end-to-end wall
time is a predeclared secondary efficiency outcome for tau2 and SWE-agent. Exact
tau2 full-pass rate (`reward == 1`) is a secondary quality outcome. An efficiency
win counts only when the quality non-inferiority test passes.

### 9.2 Non-inferiority margins

- tau2 mean final reward: optimized minus baseline greater than `-0.03`.
- SWE-bench resolved rate: optimized minus baseline greater than `-0.02`.
- OSWorld mean normalized score: optimized minus baseline greater than `-0.02`.

The lower endpoint of the two-sided 95% interval must exceed the margin. Report
the interval even when the point estimate improves. Any newly induced invalid
tool schema, evaluator bypass, disallowed external action, destructive action,
or task-state leak is a hard safety failure regardless of the aggregate margin.

### 9.3 Required telemetry

Retain per task, arm, and repetition:

- official task score and paired outcome flip;
- provider-billed USD and token categories, eligible and ineligible model calls,
  tool calls, cache hits/misses, retries, shadow calls, and failed calls;
- end-to-end latency, time to first and last useful action where defined, and
  request latency at p50/p95/p99;
- throughput and quality-adjusted goodput at concurrency `1`, `8`, and `32` for
  workloads that safely support those levels;
- Agentc CPU time, RSS delta, persistent-state bytes, embedding time/cost, lock
  wait, cold-start cost, steady-state overhead, and synchronous shadow latency;
- per-rule eligibility, proposal, application, realized mutation, dispatch
  fallback, abstention reason, and natural fire rate;
- chosen composition, rejected alternatives, projected savings, and realized
  marginal savings of each pass.

Aggregate over all assigned tasks (intention to treat), not only warm, eligible,
successful, or rewritten calls. Report cold and warm strata in addition to the
aggregate.

## 10. Statistical analysis

- Primary inference is paired by workload task and repetition.
- Use a hierarchical paired bootstrap: sample tasks with replacement, then
  repetitions within sampled tasks, with 10,000 deterministic resamples. Report
  mean paired effect, median task effect, and percentile 95% interval.
- For binary task flips, additionally report the exact two-sided McNemar test
  and discordant-pair counts. McNemar significance does not replace the
  non-inferiority interval.
- For latency and cost, report paired geometric-mean ratio and arithmetic total
  difference with bootstrap intervals. Winsorization and outlier removal are
  prohibited; show the raw tail and retry contribution.
- The Stage C power analysis uses observed paired variance and the frozen margin.
  If the scheduled confirmatory sample has less than 80% power for the margin or
  value threshold, the campaign is `NO-GO`; do not enlarge the test set after
  looking at pilot/test effects.
- The 15 hypotheses obtained from five workload/model cells times the three
  unified-system contrasts form the confirmatory family. Control family-wise
  error with Holm's method. All other comparisons are labeled secondary with
  Benjamini-Hochberg false-discovery control within each workload.

## 11. Guard experiment and damage contract

The guard is evaluated at shadow rates `{0, 0.01, 0.02, 0.05, 0.10, 1.0}` on:

- naturally harmful rewrites observed without opening confirmatory aggregates;
- at least 20 seeded harmful site-scenarios for each destructive claimed rule;
- at least 20 benign meaning-preserving site-scenarios per destructive rule;
- provider errors, process restart, model-version drift, structured tool calls,
  and multimodal calls.

Injection changes the candidate rewrite or its result, never the task label or
guard. Scenarios and expected harm/benign labels are fixed from calibration or
public fixtures before Stage P. Deterministic trace replay measures controller
operating characteristics over at least 10,000 sequences per rate; a live
stratified subset validates divergence and billed/latency accounting.

For normalized task quality `q` in `[0,1]`, cumulative damage at a call site is

```text
D_t = sum_{i <= t} max(0, q_baseline_i - q_rewritten_i).
```

The operator damage budget is `D_max = 5.0` task-equivalent losses per
`(call_site, rule)` in a 24-hour window. A site is caught in budget only if the
disable decision is durable before `D_t > 5.0`. The production 2% point passes
only if all of the following hold on held-out scenarios:

- at least 90% of harmful sites are caught in budget;
- benign-site disable rate is at most 5%;
- missed-harm rate, calls/time/cost to disable, and p50/p95/p99 cumulative damage
  are all reported;
- net savings after shadow inference is at least 80% of gross savings;
- a restart neither erases accumulated evidence nor silently re-enables a rule;
- sampled counterfactual cost and synchronous request latency are included, not
  described as microsecond bookkeeping overhead.

The current five-consecutive-breach controller receives no exemption. With
Bernoulli 2% sampling, even a rewrite that breaches on every shadow sample has
expected disable time 250 rewritten calls (median 234, p90 398, p95 456).
When a sampled comparison breaches with probability 0.5, the expected wait for
five consecutive sampled breaches is about 3,100 rewritten calls. These are
protocol predictions to test, not acceptable safety results. A failure requires
a controller redesign or removal of the 2% safety claim.

Revision 6 verifies the restart-persistence mechanism required by this section,
not the damage contract. The cumulative estimator is not yet drift-bounded, and
its detection/false-disable frontier and durable-write overhead remain open
before Stage C.

Revision 7 verifies that malformed numeric inputs cannot poison or bypass the
persisted guard state. It does not improve the five-breach controller's
detection delay, false-disable rate, drift behavior, or cumulative damage.

## 12. Retry, exclusion, and stopping rules

- Provider 429/5xx/network failures use the frozen upstream retry policy. Count
  every attempt, latency, and billable token reported by the provider.
- After upstream retries are exhausted, record a terminal infrastructure
  failure and score the assigned task as failed for intention-to-treat analysis.
  Also report a sensitivity analysis excluding provider-wide outages.
- A task may be excluded only for a demonstrably corrupt frozen upstream input
  or environment failure reproduced in the baseline before outcomes are
  unblinded. The task ID and evidence remain in an exclusion ledger. It is never
  replaced.
- Stop a cell immediately for credential leakage, evaluator contamination,
  irreversible unsafe external action, request-shape corruption, or spend above
  125% of the pre-run estimate. Spend stops pause the complete cell for explicit
  budget review; completed favorable tasks are not reported alone.
- Rate-limit or outage pauses resume with the same arm schedule and fresh
  environment. A provider model retirement creates a failed/incomplete frozen
  cell, not an alias substitution.

## 13. Artifact contract

Each run directory contains a manifest with:

- protocol ID and digest, Agentc commit, branch, clean/dirty state and diff
  digest, release/debug profile, OS/architecture, dependency-lock digests;
- workload/agent/dataset commits, source-file digests, canonical task IDs and
  split, environment/image/site/task/asset pins;
- provider, endpoint class, account region, exact requested and returned model,
  SDK version, request ID, date, model-catalog digest, and price-table digest;
- arm, rule set, ordered rule configuration, guard configuration, task/repetition
  seed support, deterministic arm order, warm/cold state, and storage digest;
- resolved Python and native storage paths plus per-scope eligible, excluded,
  opt-out, planner, observation, and span counts;
- raw per-call timing and usage, trace/span/call-site IDs, plan audit, mutations,
  abstentions, retries, errors, task result, and cost calculation;
- hashes of request/response content plus replayable content wherever licenses
  and privacy permit; explicit redaction and licensing fields otherwise;
- expected spend, actual spend, stop reason, and completeness status.

Secrets, credentials, private endpoint URLs, home-directory paths, and raw
OSWorld gated task/evaluator content are forbidden in committed artifacts. A
validator must reject a run whose task count, digest, trace/audit join, pricing,
or arm schedule is incomplete.

The public package has two paths: a no-key `reproduce-lite` that rebuilds all
tables and figures from canonical frozen results, and a paid `reproduce-paper`
that states keys, hardware, expected spend/runtime, rate limits, commands, and
allowable stochastic variation. Every table cell maps to one canonical manifest
and raw-result digest.

## 14. Pilot and October 1 decisions

Stage P proceeds to confirmatory execution only if:

1. all three workloads pass admission with unmodified upstream source;
2. at least two workloads naturally apply two or more benefit-eligible
   mechanisms after warmup;
3. at least two pilot workloads show either at least 10% lower billed cost or
   at least 15% lower end-to-end latency without a point estimate beyond the
   non-inferiority margin;
4. the 2% guard meets its damage contract on calibration and seeded pilot
   scenarios;
5. the projected complete campaign fits the approved budget and finishes before
   2026-10-01.

MLSys 2027 main track is `GO` on 2026-10-01 only if the sealed confirmatory
package satisfies every row:

| Gate | Pass condition |
|---|---|
| Natural usefulness | At least two of three workloads achieve at least 10% lower billed cost or 15% lower end-to-end latency, and their quality intervals clear the frozen margins. |
| Multi-mechanism value | At least two independent rule families contribute held-out gain; `all_on_guarded` beats both `best_single_guarded` and `naive_sequence_guarded` on at least one primary efficiency outcome without a quality trade. |
| Safety at 2% | At least 90% harmful sites caught within `D_max=5.0`; benign disables at most 5%; at least 80% gross savings retained; restart and drift cases pass. |
| Competitive position | At matched quality, Agentc beats a mechanism-matched baseline on a primary outcome in at least two workloads, or demonstrates a measured integration/composition capability that baseline cannot provide. |
| Statistical validity | Frozen tasks, five paired primary repetitions, selection-disjoint calibration, intervals, multiplicity correction, and complete intention-to-treat results. |
| Artifact integrity | Clean-clone replay works and manuscript, task manifests, raw results, prices, tables, and figures agree. |

Any failed row is `NO-GO` for MLSys 2027 main under this protocol. It is not an
invitation to retune the confirmatory set. A negative result may still justify a
narrow guard, applicability, trace-prevalence, artifact, or later-venue paper.

## 15. Admission blockers at freeze

No Stage C, P, or T result is admissible while its relevant blocker is open.

| Bead | Blocked evidence |
|---|---|
| `bd-323l.5` | the broader compatibility/overhead matrix and additive-value experiment above native provider or serving optimizations remain incomplete; this blocks the competitive-position gate, not E1 interception of the frozen tau2/SWE-agent non-streaming paths. |
| `bd-ez1k` | route-independent LiteLLM streaming is incomplete; this does not block the frozen non-streaming cells but blocks any framework-neutral streaming claim. |
| `bd-8uxj` | shared helper call-site identity collapses distinct RAG stages; semantic call-site identity needs a general solution. |
| `bd-3q3l` | the composition proxy destroys its own provenance tags and cannot validate provenance-dependent rewrites. |
| `bd-vdj` | exact current model routes and snapshot pins are absent from runtime wiring. |
| `bd-o2qj` | pricing, dataset pin, and fixture-count integrity defects remain. |
| `bd-jq6c`, `bd-shi1.4` | 2% guard behavior, harmful-site detection, and false disables are not yet established. |
| `bd-nggo` | persisted divergence is cumulative rather than drift-bounded; provider/workload drift can retain stale guard evidence. |
| `bd-rm0w` | per-shadow-sample durable-write overhead and cost-DB mutex contention are not quantified. |
| `bd-z5zj` | same-process Python and bundled-Rust SQLite ownership remains unaudited beyond the separate-process E0 probe boundary. |
| `bd-bjs` | clean-clone fixture/bootstrap path is incomplete. |
| `bd-7s4.1` | CI does not execute the benchmark harness tests. |
| `bd-zqeq` | composed rule-set savings windows are bounded in process but not persisted; this blocks a restart-persistent composition-payoff claim, not the call-site window verified in revision 5. |
| `bd-6bon` | the operator report mixes a retained-sample cost window with a wall-clock audit window; it cannot supply paper aggregates until aligned, while the protocol's raw per-task ledger remains authoritative. |

Closing a blocker requires its own tests and evidence. A closed Bead alone does
not admit a workload; the Stage E1 conformance result does.

Revision 2 retires four Stage E0 blockers without admitting a workload:
`bd-voua` preserves structured native requests and gates lossy rewrites;
`bd-zsvv` intercepts Anthropic stable and beta message resources; `bd-lhte`
preserves the OpenAI output-cap parameter family; and `bd-pbus` removes the
invalid quantile overshoot and tail underreaction for fresh profiles. The
supporting non-paper artifacts are
[osworld-request-preflight-2026-09-03.json](osworld-request-preflight-2026-09-03.json)
and
[output-quantile-preflight-2026-09-03.json](output-quantile-preflight-2026-09-03.json).

Revision 3 retires the frozen non-streaming portions of `bd-6wzv` and
`bd-xs37` at Stage E0 without admitting a workload at Stage E1. In two repeated
no-network runs, frozen tau2 v1.0.1 produced eight eligible assistant calls,
eight excluded simulator calls, eight plans and observations, and 16 successful
scoped spans; frozen SWE-agent v1.1.0 produced one plan, observation, and span
at its real `_single_query` call site. The semantic response digests, plan
sequences, and scope reports repeated exactly across runs. The synthetic tau2
sequence of three warmup pass-throughs followed by five `OutputBudget`
activations is only an integration control. The evidence is
[litellm-admission-preflight-2026-09-03.json](litellm-admission-preflight-2026-09-03.json).

Revision 4 retires `bd-hoer` at Stage E0 without admitting a workload at Stage
E1. The runtime now gives Python lifecycle state and the native optimizer one
explicit storage owner, resets native state across same-process shutdown and
reinitialization, and restores the caller's environment. Two repeated
zero-network runs proved that a warm store A did not make a fresh store B hot;
the normalized results matched exactly. Launchers still use a unique resolved
store per `(stage, workload, model, arm, repetition)` and archive both resolved
paths. The evidence is
[storage-isolation-preflight-2026-09-03.json](storage-isolation-preflight-2026-09-03.json).

Revision 5 retires `bd-bwgu` at Stage E0 without admitting a workload at Stage
E1. Every call-site cost and shape statistic now uses the exact newest bounded
sample set, survives restart, and ages out old distributions deterministically.
Five release repetitions produced identical correctness and persistence
results; frozen tau2 and SWE-agent no-network admission reruns preserved their
previous plan sequences, response digests, scopes, and observation counts.
Rule-set restart persistence and the operator report's mixed time/sample window
remain explicitly scoped by `bd-zqeq` and `bd-6bon`. The evidence is
[cost-model-window-preflight-2026-09-03.json](cost-model-window-preflight-2026-09-03.json).

Revision 6 retires `bd-shi1.5` at Stage E0 without admitting a workload at
Stage E1. The runtime now persists cumulative divergence statistics and the
current consecutive-breach streak after each shadow comparison, hydrates both
on restart, and preserves the resulting 24-hour disable across a second
restart. Five release repetitions produced identical normalized state and plan
sequences; frozen tau2 and SWE-agent reruns preserved their earlier semantics.
The 2% controller frontier, drift-bounded estimation, non-finite input
hardening, and durable-write overhead remain explicit blockers at revision 6.
The evidence is
[guard-persistence-preflight-2026-09-03.json](guard-persistence-preflight-2026-09-03.json).

Revision 7 retires `bd-h7k9` at Stage E0 without admitting a workload at Stage
E1. Invalid divergence samples now leave both fresh and existing guard state
unchanged, and invalid environment thresholds fall back to the rule budget
after validation at `f64` precision. Five release repetitions produced
identical normalized results; frozen tau2 and SWE-agent reruns preserved their
previous plan sequences, response digests, scopes, and observation counts. The
controller frontier, damage contract, drift-bounded estimator, and durable-
write overhead remain explicit blockers. The evidence is
[guard-input-validation-preflight-2026-09-03.json](guard-input-validation-preflight-2026-09-03.json).
