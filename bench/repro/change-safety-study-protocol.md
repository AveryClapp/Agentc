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

### 1.2 Excluded claims

- Any claim that Agentc lowers cost, tokens, or latency. Cost is recorded as
  telemetry only.
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
responses never enter the agent's history. This is the existing exploration
path in [exploration.rs](../../crates/agentc-optimizer/src/exploration.rs),
which already keeps counterfactual answers out of application history and
charges every leased call to a rolling cap, run here with a study-mode
assignment.

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

## 6. Signals and detectors

### 6.1 Label-free call-level signals

| ID | Signal | Status |
|---|---|---|
| S1 | Lexical Jaccard divergence (`divergence_metric_version` `1`) | Exists: `_text_divergence` in [_optimizer_glue.py](../../python/agentc/_patches/_optimizer_glue.py) |
| S2 | Normalized Jaccard divergence (`normalized-jaccard-v2`) | Exists |
| S3 | Embedding cosine distance (`embedding-cosine-normalized-jaccard-v2`) | Exists |
| S4 | Action agreement: same response type (text or tool call), same tool names, and per-field argument agreement after JSON canonicalization | New. Today structured outputs score only 0 or 1. |
| S5 | Termination and length: stop reason, end-of-conversation or transfer markers, output-token ratio | New |

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

## 7. Analysis

### 7.1 Stages and information flow

| Stage | Content | Spend |
|---|---|---|
| R | Retrospective development analysis of the September OpenRouter protected study (63 tasks, 130 probe pairs, whole-task outcomes for a fixed arm), on branch `exp/protected-native-learning`. Exercises the analysis code end to end. | $0. Development evidence only, never cited as a test result. |
| E0 | Mocked-provider runs of fork assignment, change adapters, and signal computation. | $0 |
| E1 | At most 3 calibration tasks per (workload, model) cell to prove request preservation, change activation, fork isolation, and cost joins. | Small, recorded |
| P1 | A/A noise-floor pilot on tau2 calibration tasks: baseline plus C0 forks only. Measures per-task cost and the H1 floor. | Under $5 |
| C | Calibration: all changes on calibration tasks. Fixes `R`, the C3 and C4 parameters, detector thresholds, and the harmful-cell margin. | Forecast from P1 |
| T | Test: frozen schedule on test tasks. Sole source for H2 to H5. | Forecast from C |

P1 is a feasibility gate. If the A/A action-disagreement rate is so high that no
change could exceed it, S4 is redesigned before Stage C, and the redesign is
recorded as a new revision.

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

## 8. Reuse map

| Need | Existing source |
|---|---|
| Workload pins, admissible boundary, seeds | Predecessor Sections 2.1, 3, 4 |
| tau2 and SWE-agent drivers | `bench/tau_task_worker.py`, `bench/swe_agent_task_worker.py` and their bindings |
| Off-path counterfactual calls with a spend cap | `crates/agentc-optimizer/src/exploration.rs` |
| Divergence metrics S1 to S3 | `python/agentc/_patches/_optimizer_glue.py` |
| Exposure guard and persisted divergence state | `crates/agentc-optimizer/src/budget.rs`, `plan_profile.rs` |
| Billing, receipts, spend journal | `bench/campaign_accounting.py` |
| Damage contract, retry and stopping rules, artifact contract | Predecessor Sections 11, 12, 13 |

## 9. Known gaps before freeze

- `bench/tau_task_worker.py` accepts only `openai/` models. C2 to C4 on tau2
  need an Anthropic route through the same LiteLLM transport.
- No adapter exists for C3, C4, X1 to X3, or B1 and B2. Each needs a request
  transform at the interception boundary plus a pass-through conformance check.
- S4 and S5 do not exist.
- Exploration currently leases at most a fixed number of calls per site per
  24 hours; study mode needs a fork-every-eligible-call setting confined to
  experiment storage.
- Python tooling hangs on this machine while the repository sits in the
  iCloud-synced `~/Documents` folder, so no Stage E0 or later run is possible
  until it moves.

## 10. Threats to validity

- **Provider nondeterminism and drift.** Arms interleave by the predecessor's
  deterministic arm order. Each cell finishes inside a short window, and a
  catalog change starts a new cohort.
- **Few cells.** Two workloads by two to three models by about ten changes
  gives tens of cells, which bounds the precision of H2. The per-task analysis
  is reported alongside the cell-level result.
- **Prompt caching.** C3 and C4 change cache behavior and therefore cost.
  Cost is telemetry only, so this affects interpretation, not the hypotheses.
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
