# Agentc change-safety study protocol

- Protocol: `agentc-change-safety-v1`
- Status: **draft, not frozen**. Nothing below is admissible evidence until a
  freeze record (`change-safety-protocol-revision-1.json`) binds this file's
  digest, the runtime commit, and the workload pins.
- Opened: 2026-11-09
- Predecessor: [mlsys-2027-evaluation-protocol.md](mlsys-2027-evaluation-protocol.md)
  (revision 12). That protocol asked whether Agentc's plan selection saves money
  at acceptable quality. Its October 1 decision date went by without a GO
  record, and its savings question is closed. This protocol reuses its workload pins, split and
  seed derivation, damage contract, and artifact contract, and states each
  reuse explicitly.

## 1. Question

Teams now change the calls a deployed agent sends all the time: they move to a
cheaper model tier, migrate to a newer model version, turn on provider-side
context editing or compaction, or install a client-side rewriter. Each change
is usually judged by an offline benchmark run before rollout, or not at all.
Agentc already contains the machinery for a different check: issue the changed
request off-path next to the real one, compare the two responses, and disable
the change when sampled evidence turns bad.

This study asks whether that check works:

> For changes applied to the model calls of an existing LLM agent, how well do
> cheap, label-free, call-level signals predict task-level damage, and how much
> sampled counterfactual traffic does a detector need to catch a damaging change
> before it exceeds a declared damage budget?

The study is a measurement. Every hypothesis below can come out negative, and a
negative answer is a reportable result: it would mean that call-level guards,
including Agentc's, provider rollouts, and router canaries, cannot see a class
of damage.

### 1.1 Admissible claims

- Predictive validity of each signal for task-level damage, per workload and
  change family, with intervals.
- Operating characteristics of each detector (catch rate within budget, calls
  and dollars to detect, false disables) at each sampling rate.
- Which change families produce damage that call-level signals miss.
- The billed cost of each change once provider prompt caching is counted,
  including when a change that removes tokens raises cost because it
  invalidates a cached prefix (Section 7.4).

### 1.2 Excluded claims

- Any claim that Agentc lowers cost, tokens, or latency. Cost is measured as a
  property of each change, never as a result of Agentc.
- That output divergence is task quality, or that a low signal certifies a
  change as safe.
- That a result on these workloads generalizes to agents with other structure.

## 2. Hypotheses

Each hypothesis is fixed before Stage C and tested on the test split.

| ID | Hypothesis | Test |
|---|---|---|
| H1 | The A/A floor is non-trivial: two independent samples of the same request disagree on the next action at a measurable rate on tool-using workloads, even at `temperature=0`. | Descriptive: A/A action-disagreement rate and text-divergence distribution per workload and model, with intervals. |
| H2 | Excess call-level signal over the A/A floor is positively associated with task-level damage across (workload, model, change) cells. | Spearman rank correlation over cells with a task-cluster bootstrap interval; AUROC for classifying cells as harmful (Section 7.2). |
| H3 | On tool-using workloads, action-level agreement predicts damage better than text-level divergence. | Paired bootstrap difference in AUROC between signal S4 and each of S1 to S3. |
| H4 | Changes that remove context (tool-result clearing, compaction, ContextCompress) produce less call-level signal per unit of task damage than model-tier swaps, because their damage appears later in the trajectory. | Ratio of excess signal to damage by change family, with intervals; per-step divergence profiles along the trajectory. |
| H5 | At 2% sampling, a detector calibrated on the A/A floor catches at least 90% of harmful cells within the damage budget with at most 5% false disables on A/A and benign cells. | Trace replay over recorded streams (Section 7.3), for each detector in Section 6.2. |

## 3. Workloads

Workload source, agent code, prompts, tools, and control flow are unchanged, as
required by Section 2.1 of the predecessor protocol, which applies here in full.

| Family | Workload | Role | Pins |
|---|---|---|---|
| Stateful tool use | `tau2-bench` `banking_knowledge`, official `llm_agent` and `user_simulator` | Primary. Short trajectories, tool calls, deterministic reward. | Predecessor Section 3, unchanged. |
| Long-horizon coding | SWE-agent `v1.1.0` on SWE-bench Verified | Secondary. Long contexts where clearing and compaction actually trigger. | Predecessor Section 3, unchanged, with its USD 2.00 per-instance ceiling. |

OSWorld is out of scope for v1 because of its cost and multimodal request
shapes. A second tau2 domain (`retail`) is an optional replication cell if the
budget allows.

The tau2 user simulator stays fixed at `openai/gpt-4.1-mini-2025-04-14` and is
never changed. Its own nondeterminism is part of the trajectory-level A/A floor.

### 3.1 Splits

Splits use the predecessor's derivation (Section 4) with the namespace string
`agentc-change-safety-v1` in place of `agentc-mlsys2027-v1`, so the new split
is independent of any earlier assignment.

| Workload | Calibration | Test | Not selected |
|---|---:|---:|---:|
| tau2 banking knowledge (97) | first 30 | final 67 | 0 |
| SWE-bench Verified (500) | first 20 | next 40 | remaining 440 |

The tau2 ordered-ID digest under this namespace (newline-terminated IDs, as in
the predecessor) is
`d3fe713f676fdc1a3431aad09d99747e74bccc7d1f9410f10e8a0d642f0d439e`. The
implementation reproduces the predecessor's published tau2 digest
`915b4717…c417b` when given its namespace.

**Open decision (SWE-bench).** The frozen SWE environment pipeline admits only
the predecessor's 150-task cohort; task images were provisioned and verified
for those tasks alone. Drawing 20 and 40 tasks from all 500 under the new
namespace would select tasks with no admitted environment. Proposed resolution:
apply the new namespace within the 150 admitted tasks (calibration first 20,
test next 40, remaining 90 not selected). This stays outcome-blind and reuses
verified environments. To be decided before freeze.

Any task already viewed in an earlier E0 or E1 screen is listed in an exposure
ledger before Stage C. It stays in its assigned split; it is never replaced.

## 4. Change catalog

A change is a deterministic function from the evaluated agent's request to a
new request (or a new model ID). It applies only to eligible calls of the
evaluated agent, never to the user simulator, graders, or judges. Parameters
are fixed before Stage C.

| ID | Change | Family | Mechanism |
|---|---|---|---|
| C0 | Identity (independent resample) | A/A control | Same request, sent again. Required for every model. |
| C1 | Cheaper tier, same family and generation | Model swap | For example `claude-sonnet-4-5-20250929` to `claude-haiku-4-5-20251001`; `gpt-5.4-2026-03-05` to `gpt-5.4-mini-2026-03-17`. |
| C2 | Newer version, same tier | Model migration | For example `claude-sonnet-4-5-20250929` to a current Sonnet snapshot. Exact IDs are pinned from provider catalogs at freeze. |
| C3 | Provider tool-result clearing | Context removal | Anthropic `context_management` with `clear_tool_uses_20250919` (beta `context-management-2025-06-27`). `keep` and `trigger` are fixed at freeze from calibration input-length quantiles, so the edit actually fires. |
| C4 | Provider compaction | Context removal | Anthropic `compact_20260112` where the pinned model supports it. OpenAI Responses compaction is admissible only if the agent's existing transport already uses the Responses API; otherwise C4 is excluded for OpenAI and recorded as such. |
| C5 | Agentc `ContextCompress` at its default gate | Context removal | Client-side rewrite through the existing rule. |
| C6 | Agentc `OutputBudget` at its default quantile | Output cap | Client-side rewrite through the existing rule. |

Implementation: [changes.py](../../python/agentc/change_safety/changes.py).
C3 and C4 shapes were checked against the provider documentation on
2026-11-24. C4 requires a trigger of at least 50,000 input tokens, and the
documented model list excludes `claude-sonnet-4-5-20250929`, for which C4 is
recorded as ineligible. C5 and C6 ask the existing Rust rule for its proposal
against the live call-site profile (`optimize_propose_rule`), without the
hot-call threshold, admission or guard, and without recording a plan. Two
consequences are part of the design, not defects:

- C5 measures what the rule would do, not what Agentc ships. Production
  abstains from ContextCompress on tau2-style messages whose native shape it
  cannot represent losslessly; the study skips that abstention, or C5 could
  never fire on tau2.
- C5 rarely fires on tau2 even so. The rule keeps every exchange containing an
  assistant tool-call message with no text, and most tau2 tool calls have none.
  Its activation rate is reported per workload.

Context-removal changes record characters removed and an estimate of tokens
removed (`ceil(chars/4)`; the repository has no tokenizer-based estimator),
which the analysis uses for damage against information removed.

### 4.1 Seeded harmful controls

These give known-harm cases with a range of visibility. They are fixed before
any outcome is seen and are reported separately from C1 to C6.

| ID | Seeded change | Expected visibility |
|---|---|---|
| X1 | Replace the model with the smallest model in the same provider catalog | Loud: immediate action changes |
| X2 | Truncate every tool result to its first 200 characters | Quiet: the next action often still looks plausible |
| X3 | Remove the system prompt | Mixed: tone and policy compliance shift later |

### 4.2 Benign controls

| ID | Change | Purpose |
|---|---|---|
| B1 | Reorder keys inside JSON tool results | Meaning-preserving formatting change |
| B2 | Normalize whitespace in message text | Meaning-preserving formatting change |

Benign and harmful status used in detector scoring comes from measured
trajectory-level outcomes (Section 7.2), not from these expectations. The
expectations only document why each control exists.

## 5. Measurement design

Each (workload, model, change) cell is measured in two coupled ways.

### 5.1 Call-level forks

Run the baseline trajectory unchanged. At each eligible call, also send one
**fork**: either the identical request (C0) or the changed request. Fork
responses never enter the agent's history.

Forks are sent by the study dispatcher
([dispatcher.py](../../bench/change_safety/dispatcher.py)) through the
SDK's [`ShadowRunner`](../../python/agentc/change_safety/shadow.py), not by the
optimizer's exploration leases in `exploration.rs`. Most changes in Section 4
(provider context edits, seeded harms, formatting controls) are not optimizer
plans, and exploration's per-site caps would stop forking long before every
eligible call. Agentc stays active as the instrument: with the optimizer's
master switch off, every reference call is planned as a pass-through and
observed, which builds the call-site profile C5 and C6 read, and nothing is
rewritten. Forks bypass Agentc's interception so they never enter that
profile. The dispatcher refuses to start unless the optimizer, its
exploration and its shadow sampling are all off, because their extra provider
calls would bypass the spend ledger.

The fork arm for each call is assigned by a deterministic hash of
`(protocol, workload, task, repetition, call index)`. Each eligible call gets
exactly one fork, so fork cost is one extra call per eligible call regardless
of the number of changes. C0 receives a fixed share of forks (initially 25%) so
the A/A floor is measured on the same trajectories as every change.

### 5.2 Trajectory-level rollouts

Run complete trajectories with the change applied at every eligible call, and
the same number of baseline trajectories. Repetitions per task are `R=3` in
calibration and are fixed for the test stage from calibration variance.
Baseline repetitions also give the trajectory-level A/A floor.

Damage for a cell is the paired mean task-outcome difference, baseline minus
changed, over tasks. Secondary damage measures are added model calls and added
tool calls per trajectory, because a change can raise interaction cost while
completion is unchanged. The detector question is whether forks sampled from a few
calls predict what happens when the change is deployed on every call, which is
the decision a rollout faces.

### 5.3 Telemetry

Every call records the fields in the predecessor's Section 13, plus the fork
arm, the change ID and its parameters, provider-reported applied edits
(`context_management.applied_edits` for C3 and C4), and cached-input token
counts. C3 and C4 invalidate the provider prompt cache when they fire, so
cached and uncached input are kept separate.

The record schema is [records.py](../../python/agentc/change_safety/records.py):
one `ForkRecord` per eligible call in a baseline run and one
`TrajectoryRecord` per trajectory. Every provider call, including the user
simulator and forks, is billed through
[spend.py](../../bench/change_safety/spend.py), which reserves each call's
worst-case cost before dispatch and stops the stage before the cap can be
crossed. A model without a known price is refused.

Retries are disabled at every actor (`num_retries=0`), so no call is billed
outside the ledger.

## 6. Signals and detectors

### 6.1 Label-free call-level signals

| ID | Signal | Status |
|---|---|---|
| S1 | Lexical Jaccard divergence (`divergence_metric_version` `1`) | Exists: `_text_divergence` in [_optimizer_glue.py](../../python/agentc/_patches/_optimizer_glue.py) |
| S2 | Normalized Jaccard divergence (`normalized-jaccard-v2`) | Exists |
| S3 | Embedding cosine distance (`embedding-cosine-normalized-jaccard-v2`) | Exists |
| S4 | Action agreement: same response type (text or tool call), same tool names, and per-field argument agreement after JSON canonicalization | `action-agreement-v1` |
| S5 | Termination and length: stop reason, end-of-conversation or transfer markers, output-token ratio | `termination-length-v1` |

Implementation: [signals.py](../../python/agentc/change_safety/signals.py).
Every signal is a divergence in `[0, 1]`; a signal that cannot be computed
(provider error, missing embedding model) is null, never zero, and S3 never
falls back to another metric.

- **S1 to S3** match the production guard's metrics. Like the guard, they
  score a response containing tool calls only as identical (0) or different (1).
- **S4.** Two text replies agree; their wording is S1 to S3's job. A text reply
  and a tool call disagree completely. Two tool-call responses disagree
  completely unless they call the same multiset of tools. When they do, S4 is 1
  minus the mean fraction of top-level argument fields that match, pairing
  calls to the same tool in emitted order.
- **S5** is 1 when exactly one response ends the conversation or exactly one
  was cut off at the output limit. Otherwise it is
  `min(1, |log2((o_f + 1) / (o_b + 1))| / 2)` over output tokens. tau2's end
  markers are `transfer_to_human_agents`, `done` and `###STOP###`; SWE-agent's
  is `submit`.

Stage R found that S1 scores any difference as maximal on compact JSON
replies, which contain no whitespace. S1 is reported as specified, but this
caveat applies wherever replies are compact JSON.

The **excess** of a signal is its fork value minus the A/A distribution for the
same workload and model, summarized as the shift in mean and as the rate of
exceeding the A/A 95th percentile.

### 6.2 Detectors compared in trace replay

| Detector | Source |
|---|---|
| Complete-plan exposure guard (`E_t >= 1.0` in 24 hours) | Existing, predecessor Section 11 |
| Five-consecutive-breach controller | Existing compatibility controller |
| One-sided CUSUM on excess S4 or S1, threshold calibrated on A/A | New, calibration split only |
| Sequential probability ratio test on action-disagreement rate against the A/A rate | New, calibration split only |
| Anytime-valid confidence sequence on the mean signal; disable when its lower bound exceeds the A/A mean plus a margin | New. Its false-disable guarantee holds at any stopping time, so it needs no threshold tuning beyond the margin. |

Detectors are in [detectors.py](../../python/agentc/change_safety/detectors.py),
which ships in the SDK; replay is in [replay.py](../../bench/change_safety/replay.py).

## 7. Analysis

### 7.1 Stages and information flow

| Stage | Content | Spend |
|---|---|---|
| R | Retrospective development analysis of the September OpenRouter protected study (63 tasks, 130 probe pairs, whole-task outcomes for a fixed arm), archived as tag `archive/exp/protected-native-learning`. Exercises the analysis code end to end. Converter: [stage_r.py](../../bench/change_safety/stage_r.py). | $0. Development evidence only, never cited as a test result. |
| E0 | Mocked-provider runs of fork assignment, change adapters, signal computation, rollouts, analysis and replay: `python -m bench.change_safety.run e0`. The mock is served through LiteLLM itself, so calls take the same path, Agentc interception included, as live ones. | $0 |
| E1 | At most 3 calibration tasks per (workload, model) cell to prove request preservation, change activation, fork isolation, and cost joins. | Small, recorded |
| P1 | A/A noise-floor pilot on tau2 calibration tasks: baseline plus C0 forks only. Measures per-task cost and the H1 floor. | Under $5 |
| C | Calibration: all changes on calibration tasks. Fixes `R`, the C3 and C4 parameters, detector thresholds, and the harmful-cell margin. | Forecast from P1 |
| T | Test: frozen schedule on test tasks. Sole source for H2 to H5. | Forecast from C |

P1 is a feasibility gate. If the A/A action-disagreement rate is so high that no
change could exceed it, S4 is redesigned before Stage C, and the redesign is
recorded as a new revision.

**Stage R limitation.** The September study has no A/A control: every probe is
a non-identity plan and each task ran once. Stage R therefore exercises the
damage, replay and conversion paths but cannot estimate H1, and without an A/A
floor it cannot compute excess signal for H2 or H3. Its one change with task
outcomes (fixed Haiku plus ContextCompress and StateDrop) has damage −4.8 pp,
95% interval [−12.7, +1.6]: indeterminate. P1 is the first stage that can
measure the A/A floor.

### 7.2 Predictive validity (H2 to H4)

A cell is **harmful** when the lower 95% bound of its damage exceeds a margin
fixed in Stage C (initial proposal: 5 percentage points of task success). Cells
whose damage interval lies inside the margin are **benign**; others are
**indeterminate** and are reported but excluded from AUROC.

Intervals use a task-cluster bootstrap with 10,000 replicates and a seed fixed
at freeze. Rank correlations and AUROCs are reported per workload and pooled,
with Holm correction across the five signals.

### 7.3 Detection (H5)

Recorded fork streams from Stage T are replayed through each detector at
sampling rates `{0.01, 0.02, 0.05, 0.10, 1.0}`, with at least 10,000 permuted
call orders per rate. Damage accrues under the predecessor's Section 11
contract, `D_t = sum max(0, q_baseline - q_changed)` with `D_max = 5.0`
task-equivalent losses, using each cell's measured damage rate. Report catch
rate within budget, calls, dollars, and time to disable, p50/p95/p99 damage at
disable, and false disables on A/A and benign cells.

### 7.4 Cost under prompt caching

Each trajectory records input, cached-input, cache-creation and output tokens
alongside billed USD. [cache_cost.py](../../bench/change_safety/cache_cost.py)
computes billed cost from those fields and the closed-form break-even of a
token-removing change. An agent's context is append-mostly, so removing or
editing tokens at position `k` invalidates the cached prefix after `k`; the
removal saves money only when the tokens saved outweigh re-writing the
invalidated suffix at the cache-write price. The report gives each change's
measured cost per trajectory against baseline, and where the break-even
predicts a cost increase for a token-removing change, whether the measurement
agrees. This describes the changes; it is not a claim about Agentc.

## 8. Implementation map

| Need | Source |
|---|---|
| Workload pins, admissible boundary, seeds | Predecessor Sections 2.1, 3, 4 |
| Splits, seeds, arm order, fork assignment | [assignment.py](../../bench/change_safety/assignment.py) |
| Change catalog and conformance checks | [changes.py](../../python/agentc/change_safety/changes.py) |
| Signals S1 to S5 | [signals.py](../../python/agentc/change_safety/signals.py) |
| Off-path forks and the deployable monitor | [shadow.py](../../python/agentc/change_safety/shadow.py) |
| Routing, rollouts, forks and per-trajectory accounting | [dispatcher.py](../../bench/change_safety/dispatcher.py) |
| tau2 driver (any LiteLLM provider) | [tau2_driver.py](../../bench/change_safety/tau2_driver.py), tau2 at `fc0055dc4e0a316c3f83133267fbd6faaa770992` |
| SWE-agent driver | [swe_driver.py](../../bench/change_safety/swe_driver.py) over the frozen `run_task` and `grade_task` |
| Spend cap | [spend.py](../../bench/change_safety/spend.py) |
| Mock provider for E0 | [mock_provider.py](../../bench/change_safety/mock_provider.py) |
| Analysis, replay, cache cost | [analysis.py](../../bench/change_safety/analysis.py), [replay.py](../../bench/change_safety/replay.py), [cache_cost.py](../../bench/change_safety/cache_cost.py) |
| Command line | [run.py](../../bench/change_safety/run.py) |
| Damage contract, retry and stopping rules, artifact contract | Predecessor Sections 11, 12, 13 |

The development environment pins LiteLLM 1.81.11, the version the repository's
LiteLLM path was validated on and within tau2's supported range. LiteLLM is not
a locked project dependency; the freeze record must name the installed version.

Setup from a clean checkout:

```bash
uv sync --extra test                 # Python 3.12 venv, native extension, test deps
bash scripts/download_model.sh       # model2vec weights for S3
git clone https://github.com/sierra-research/tau2-bench ../tau2-bench
git -C ../tau2-bench checkout fc0055dc4e0a316c3f83133267fbd6faaa770992
uv pip install -e "../tau2-bench[knowledge]" "litellm==1.81.11"
uv run python -m bench.change_safety.run e0 --out /tmp/e0   # $0
uv run python -m bench.change_safety.stage_r --out /tmp/stage_r   # $0
```

## 9. Known gaps before freeze

- **SWE split.** Decide the open SWE-bench split question in Section 3.1.
- **Model pins.** Pin C1, C2 and X1 model IDs from provider catalogs, and fix
  C3 and C4 parameters from calibration input-length quantiles.
- **SWE environments.** A SWE-bench trajectory needs Docker and the admitted
  task images. The binding and grading integration is tested without Docker;
  an end-to-end SWE trajectory has not yet run on this machine.
- **Freeze record.** `change-safety-protocol-revision-1.json` binds this file's
  digest, the runtime commit, the tau2 and SWE-agent pins, the LiteLLM version
  and the split digests.

## 10. Threats to validity

- **Provider nondeterminism and drift.** Arms interleave by the predecessor's
  deterministic arm order. Each cell finishes inside a short window, and a
  catalog change starts a new cohort.
- **Few cells.** Two workloads by two to three models by about ten changes
  gives tens of cells, which bounds the precision of H2. The per-task analysis
  is reported alongside the cell-level result.
- **Prompt caching.** C3 and C4 change cache behavior and therefore cost.
  Section 7.4 measures this directly; it does not affect H1 to H5.
- **Interception effects.** The pass-through conformance run from predecessor
  Section 2.1 item 6 must pass before any fork is trusted.

## 11. Related work this study answers to

| Work | Relevance |
|---|---|
| [vCache](https://proceedings.iclr.cc/paper_files/paper/2026/hash/9559cd2116de7a8f5672eac3fcd232cc-Abstract-Conference.html) (ICLR 2026) | Online, error-bounded decisions with explicit exploration; the standard for a stated risk budget. |
| [When Machine Learning Isn't Sure](https://proceedings.mlsys.org/paper_files/paper/2026/hash/96aca14d6c4dcd3adf54bc2c5ad7f138-Abstract-Conference.html) (MLSys 2026) | Runtime rejection and fallback accepted as a systems contribution. |
| [Opportunity Is Not Realizability](https://arxiv.org/abs/2608.08265) (preprint) | Selection-valid separation of oracle and deployable gains; sets the statistical bar. |
| [What Does Context Compression Cost an Agent?](https://arxiv.org/abs/2608.16370) (preprint, August 2026) | Finds compression raised retrieval calls in all six model-regime comparisons without a significant completion change; closest framing to H4 and the reason for the secondary damage measures. |
| [ACON](https://arxiv.org/abs/2510.00615) (preprint) | Learned context compression for long-horizon agents; a change this study's detectors should be able to evaluate. |
| [Anthropic context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing), [OpenAI compaction](https://developers.openai.com/api/docs/guides/compaction) | Provider-side changes C3 and C4. |
