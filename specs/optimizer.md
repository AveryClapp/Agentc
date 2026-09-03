---
title: Optimizer
status: draft
last-updated: 2026-09-03
---

# Optimizer

The application-side runtime that intercepts LLM calls, lifts them into a typed
DAG IR, and jointly chooses a target model plus compatible semantic rewrites.
Selection is constrained by request compatibility, complete-plan evidence,
output-divergence, freshness, exploration, and exposure budgets. Cold or
inadmissible alternatives execute the exact original request. The optimizer
depends on the profiler for execution traces and the memoization layer for the
`Cache` trait.

---

## Overview

Agent code is mostly untuned. Developers write straightforward `llm.chat(...)` sequences; they rarely hand-tune context windows, parallelize independent tool calls, or route simple subtasks to cheaper models. Agentc's optimizer does that tuning automatically, at runtime, without requiring the application to expose a static execution plan.

The optimizer operates at the **call boundary**. On every intercepted LLM call it asks:

1. Is this versioned semantic call site hot? If not, execute the reference call.
2. Which model targets and rewrite sequences preserve the native request shape?
3. What has happened when each complete plan ran at this call site?
4. Which plans satisfy the evidence, divergence, freshness, exposure, and
   operator constraints?
5. Select the cheapest or fastest admissible plan with deterministic
   tie-breaking, or fall back to the reference call.
6. Record the outcome against the complete plan that actually executed.

Nine rewrite rules supply transformations to the candidate generator. The registered set is
authoritative in `crates/agentc-optimizer/src/wiring.rs`; this table must match it.

| Rule | Cost driver | Trigger | Effect |
|---|---|---|---|
| `CacheHit` | CallElimination | Call site has a hot memoization cache | Serve cached output via the `Cache` trait instead of calling the model. |
| `ContextCompress` | InputTokens | Prompt > 8 KB and >30% of tokens have zero downstream attention score | Drop or summarize low-salience context chunks. |
| `ParallelBranch` | Structural | ≥2 consecutive calls with disjoint input dependencies | Emit `Plan::Parallel` (observability only; the latency win comes from the caller's dispatcher). |
| `ModelDowngrade` | ModelPrice | Call site's outputs are consistently simple | Route to a cheaper model with the same interface. |
| `StateDrop` | InputTokens | Prompt contains agent state fields that no subsequent call in the window reads | Drop the unused fields before dispatch. |
| `PromptDedup` | InputTokens | Near-duplicate message segments (Jaccard ≥ 0.92) | Keep the highest-IDF copy, drop the rest. |
| `OutputBudget` | OutputTokens | Call site has a stable output-length distribution | Cap `max_output_tokens` at p99 to prevent runaway generation. |
| `StructuredTruncation` | InputTokens | Tool-output messages with unreferenced JSON fields | Project out fields no downstream call reads. |
| `DeadOutputTruncation` | OutputTokens | Output feeds a branch that is never read | Cap output length on the dead branch. |

The candidate generator may combine compatible rules and every configured model
target. Orthogonal cost drivers prune obviously conflicting combinations but do
not establish joint safety. A combined plan becomes user-visible only from its
own `(call-site version, execution-plan ID)` profile; solo rule and solo model
observations never stand in for interaction evidence.

Each rule declares a cheap structural precondition and a mutation. Complete
plans, rather than individual proposals, compete under the constrained selector.
The existing cost-driver composer remains the
`independent_route_then_rewrite` evaluation baseline.

This is a **JIT** optimizer in the literal compiler sense: cold code runs interpreted (pass-through), hot code gets compiled (rewritten) once the profile is statistically meaningful. It is not a whole-program optimizer — no global plan is required, no agent code needs to be annotated, no static analysis is performed.

**What the optimizer does not do:**

- Does not rewrite tool implementations, prompt templates, or agent code.
- Does not speculate across call sites that have not yet executed.
- Does not apply rewrites on the first N invocations of a call site; those are always pass-through.
- Does not learn rewrite rules. The rule set is fixed; complete-plan behavior is observed online.
- Does not operate without the profiler — a cold-start workspace with no trace data always runs pass-through.
- Does not call output divergence task quality or promise semantic equivalence.
- Does not infer the safety of a target-plus-rewrite combination from its parts.

The frozen thesis, title, deployment envelope, thresholds, and analysis contract
are in
[`working/joint-execution-planning-contract.md`](working/joint-execution-planning-contract.md).

---

## Interface

### Python API

The optimizer is invisible by default once `agentc.init()` runs. No user code changes.

```python
import agentc

agentc.init()                       # Profiler + optimizer both activate.
# All subsequent llm.chat(...) calls pass through the optimizer.
```

Opt-outs:

```python
agentc.init(optimizer=False)        # Profiler still runs; no rewrites.

# Per-call opt-out (passes through extra_headers).
response = openai_client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    extra_headers={"agentc-optimize": "false"},
)

# Decorator-level opt-out.
@agentc.no_optimize
def critical_path():
    ...

# Per-rule opt-out (still applies the others).
@agentc.no_optimize(rules=["ModelDowngrade"])
def deterministic_step():
    ...
```

Inspection:

```python
# Returns the last rewrite plan for debugging.
plan = agentc.last_plan()
print(plan.call_site_id)
print(plan.rules_fired)             # [("CacheHit", "skipped: miss"), ("ModelDowngrade", "fired: gpt-4o → gpt-4o-mini")]
print(plan.projected_savings_usd)
print(plan.measured_savings_usd)
```

Fan-out:

```python
# Replaces a serial list comprehension with a thread-pool dispatch.
# Each item is auto-tagged with a fresh UserInput DepSource, and a
# parallel_peer descriptor is staged on a per-thread local so the
# optimizer's ParallelBranch rule fires and writes a Plan::Parallel
# audit row. Concurrent dispatch is performed in Python; the rule's
# role is to verify disjointness and record the optimization.
summaries = agentc.parallel_map(_summarize_chunk, chunks)
```

### CLI

```
$ agentc optimize report
Optimizer report (last 24h)
─────────────────────────────────────────────────────────
Calls intercepted:       18,402
Cold (profiling):         2,104    (11.4%)
Hot, pass-through:        3,291    (17.9%)    # no rule fired
Hot, optimized:          13,007    (70.7%)
Overhead per call:        0.4ms    p99 1.2ms

Rule firings:             applied   skipped   savings
  CacheHit                  5,211     7,796   $62.19
  ContextCompress           3,402       809   $28.44
  ParallelBranch              517       201   $0.00 (latency −38%)
  ModelDowngrade            3,018     1,482   $41.07
  StateDrop                   859       441   $6.93

Savings (24h):            $138.63  (24.7% of baseline spend)
Accuracy divergence:       0.4%    (shadow-mode sample)

$ agentc optimize inspect app.agents.planner:plan_next_step
Call site: app.agents.planner:plan_next_step
  Total invocations:       1,847
  Cost model confidence:   0.92   (adequate sample size)
  Baseline cost:           $0.0241 per call
  Observed cost:           $0.0097 per call
  Savings:                 59.8%

  Rule firings:
    CacheHit            fires 58% of the time
    ModelDowngrade      fires 31% of the time (to gpt-4o-mini)
    (others)            pass-through

  Accuracy:
    Shadow divergence     0.3%
    Budget remaining      0.7%
    Status                healthy

$ agentc optimize disable --rule ModelDowngrade --call-site "app.agents.planner:*"
Disabled ModelDowngrade on 2 call sites matching the pattern.

$ agentc optimize bench --agent bench/agents/swebench_planner.py
Running baseline (optimizer disabled)...
Running optimized...
─────────────────────────────────────────────────────────
Baseline:     $14.82   avg 42.3s per task
Optimized:    $ 8.91   avg 31.7s per task
Savings:      39.9%    (latency −25.1%)
Accuracy:     baseline 82.0% → optimized 81.2% (within budget)
```

### Configuration

`agentc.toml`:

```toml
[optimizer]
enabled = true
hot_threshold = 3                   # Invocations before a call site is eligible.
cost_model_window = 50              # Rolling window for cost model fitting.
divergence_window = 50              # Retained shadow samples per site/rule.
plan_profile_window = 50             # Retained outcomes per site-version/plan.
min_plan_evidence = 20               # Paired samples before admission.
plan_profile_freshness_hours = 24
max_overhead_ms = 5                 # Abort optimization if budget exceeded.
shadow_rate = 0.02                  # 2% of optimized calls run shadow execution.

[optimizer.selection]
objective = "cost"                   # "cost" or "latency".
max_rewrite_depth = 3
exploration_calls_per_site_24h = 20
max_concurrent_counterfactuals = 1
divergence_exposure_budget = 1.0

[optimizer.accuracy_budget]
# Maximum allowed shadow-mode divergence per rule, as a fraction.
# Optimizer auto-disables a rule on a call site if observed divergence exceeds budget.
CacheHit            = 0.01
ContextCompress     = 0.02
ParallelBranch      = 0.00          # pure reordering; divergence is a bug.
ModelDowngrade      = 0.03
StateDrop           = 0.01

[optimizer.rules]
# Individual rule enable/disable and rule-specific tuning.
CacheHit.enabled            = true
ContextCompress.enabled     = true
ContextCompress.min_prompt_bytes = 8192
ParallelBranch.enabled      = true
ParallelBranch.max_fanout   = 4
ModelDowngrade.enabled      = true
ModelDowngrade.route = [
  { from = "gpt-4o",           to = "gpt-4o-mini",          max_output_tokens = 512 },
  { from = "claude-opus-4-7",  to = "claude-haiku-4-5",     max_output_tokens = 1024 },
]
StateDrop.enabled           = true
```

Environment overrides:

| Variable | Effect |
|---|---|
| `AGENTC_OPTIMIZE=0` | Disables the optimizer. Profiling still runs. |
| `AGENTC_OPTIMIZE_HOT_THRESHOLD=10` | Overrides `hot_threshold`. |
| `AGENTC_OPTIMIZE_COST_MODEL_WINDOW=50` | Sets the exact retained sample count per cost profile. |
| `AGENTC_OPTIMIZE_DIVERGENCE_WINDOW=50` | Sets the exact retained divergence samples per call-site/rule pair. |
| `AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW=50` | Sets the exact retained outcomes per call-site-version/plan pair. |
| `AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE=20` | Sets the paired evidence floor for a non-reference plan. |
| `AGENTC_OPTIMIZE_OBJECTIVE=cost` | Selects `cost` or `latency` minimization. |
| `AGENTC_OPTIMIZE_MAX_OVERHEAD_MS=5` | Sets the plan kill-switch budget. |
| `AGENTC_OPTIMIZE_SHADOW=0.1` | Overrides `shadow_rate`. |
| `AGENTC_COMPOSE=0` | Selects the independent first-match compatibility baseline; it is not a joint-planner mode. |

### Rust API

```rust
// crates/agentc-optimizer/src/lib.rs

pub struct Optimizer {
    cost_model: Arc<CostModel>,         // Reference-call reporting profile
    plan_profiles: Arc<PlanProfiles>,   // Site-version × complete-plan evidence
    model_catalog: ModelCatalog,
    rules: Vec<Box<dyn RewriteRule>>,
    config: OptimizerConfig,
}

impl Optimizer {
    pub fn new(profile: Arc<dyn Profile>, cache: Arc<dyn Cache>, config: OptimizerConfig) -> Self { ... }

    /// Entry point called by the SDK on every intercepted LLM call.
    /// Returns the selected executable plan, or `Plan::PassThrough` when no
    /// non-reference candidate is admissible.
    pub fn plan(&self, call: &Call) -> Plan { ... }

    /// Record the actual outcome against the plan that really dispatched.
    pub fn observe(&self, plan: &Plan, outcome: &Outcome);
}

pub struct CandidatePlan {
    pub id: ExecutionPlanId,
    pub call: Call,
    pub target: ModelTarget,
    pub rewrites: Vec<RewriteApplication>,
    pub validation: ValidationPolicy,
    pub estimate: Option<PlanEstimate>,
}

pub struct PlanEstimate {
    pub paired_observations: u32,
    pub expected_cost_usd: f64,
    pub expected_latency_ms: f64,
    pub divergence_upper_p95: f64,
    pub expected_net_savings_usd: f64,
    pub fresh: bool,
}

pub enum Plan {
    PassThrough,
    Cached { value: CacheValue },
    Rewritten {
        rule: &'static str,
        call: Call,                   // Possibly mutated prompt/params/model
        projected_savings_usd: f32,
    },
    Parallel { calls: Vec<Call>, rule: &'static str },
}

pub trait RewriteRule: Send + Sync {
    fn name(&self) -> &'static str;
    fn applies(&self, call: &Call, profile: &CallSiteProfile) -> bool;
    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal>;
    fn accuracy_budget(&self) -> f32;
}

pub struct Proposal {
    pub rewritten: Plan,
    pub projected_savings_usd: f32,
    pub safety_check: Box<dyn Fn(&Call) -> bool + Send + Sync>,
}
```

`ExecutionPlanId` hashes the provider protocol, requested and target model IDs,
ordered rewrite names, implementation versions and parameters, cache policy,
output budget, and validation policy. Price metadata is observation metadata and
does not change plan identity. The selector sees only `CandidatePlan` values and
returns one decision; provider adapters and profile storage remain hidden behind
the optimizer.

### FFI surface

The native extension exposes seven optimizer functions:

```python
# python/agentc/_native.pyi
def optimize_configure(storage_path: str) -> str:
    """Flush prior state, build at storage_path, and return the owned path."""

def optimize_storage_path() -> str:
    """Return the path owned by the active native optimizer."""

def optimize_reset() -> None:
    """Flush and drop native optimizer state."""

def optimize_plan(call_json: str) -> str:
    """
    Input: JSON-serialized Call (call_site_id, model, messages, parameters, tools).
    Output: JSON-serialized Plan. "pass_through" for cold or no-fire cases.
    """

def optimize_observe(plan_json: str, outcome_json: str) -> None:
    """
    Feeds the cost model with the measured outcome of a plan.
    """

def optimize_record_divergence(
    call_site_version: str, execution_plan_id: str, divergence: float
) -> None:
    """Feed one sampled counterfactual into the complete-plan guard."""

def optimize_flush() -> None:
    """Flush buffered cost-profile and guard-divergence state."""
```

All plan execution happens in Python — the SDK receives the `Plan` back from Rust and dispatches the (possibly rewritten) LLM call(s) itself. Rust never calls out to the user's LLM provider.

---

## Architecture

### Layered flow

```
    ┌─────────────────────────────────┐
    │ User code: llm.chat(...)        │
    └────────────┬────────────────────┘
                 ▼
    ┌─────────────────────────────────┐
    │ Python SDK interceptor          │  attribute the call to a call_site_id
    │ (agentc._intercept)             │
    └────────────┬────────────────────┘
                 ▼
    ┌─────────────────────────────────┐
    │ Plan dispatch                   │
    │   if optimizer disabled → pass  │
    │   else → optimize_plan(call)    │
    └────────────┬────────────────────┘
                 ▼
    ┌─────────────────────────────────┐
    │ Rust: Optimizer::plan           │
    │   1. version semantic call site │
    │   2. enumerate model + rewrites │
    │   3. load complete-plan profiles│
    │   4. constrain + select         │
    │   5. fallback or return Plan    │
    └────────────┬────────────────────┘
                 ▼
    ┌─────────────────────────────────┐
    │ Python executor                 │
    │   Cached   → return CacheValue  │
    │   Rewritten → dispatch modified │
    │   Parallel → asyncio.gather     │
    │   PassThrough → original call   │
    └────────────┬────────────────────┘
                 ▼
    ┌─────────────────────────────────┐
    │ optimize_observe(plan, outcome) │
    │ → profiler emits span           │
    │ → exact plan profile updates    │
    └─────────────────────────────────┘
```

### DAG IR

Each LLM call enters the optimizer as a `Call`; a sequence of consecutive calls within a single trace forms the rolling DAG that the optimizer reasons about.

```rust
pub struct Call {
    pub call_site_id: String,            // "module.function:line"
    pub trace_id: [u8; 16],
    pub span_id: [u8; 8],
    pub model: String,
    pub messages: Vec<Message>,
    pub parameters: Parameters,
    pub tools: Vec<Tool>,
    pub input_deps: Vec<DepSource>,      // where each message's content came from
    pub occurrence_ix: u32,              // how many times this call_site has been seen this trace
}

pub enum DepSource {
    Literal,                             // hardcoded in user code
    UserInput { span_id: [u8; 8] },      // came from the trace's root input
    ToolOutput { span_id: [u8; 8] },     // came from a prior tool call
    LlmOutput { span_id: [u8; 8] },      // came from a prior LLM call
    State { key: String },               // came from agent state (StateDrop needs this)
}
```

`DepSource` annotations come from the SDK interceptor; it tracks which objects flow into `messages` using a lightweight provenance tagger (`python/agentc/_provenance.py`). For framework-native agents (LangGraph, CrewAI, Autogen) the tagger hooks into the framework's state-passing primitives; for raw SDK usage it falls back to `DepSource::Literal` everywhere, which disables the rules that need provenance (`ParallelBranch`, `StateDrop`) while still allowing the rest to fire.

The rolling DAG itself isn't materialized as a graph structure on the hot path. Instead, the optimizer queries the profiler for the last `N` spans in the current trace and treats those as the "recent nodes" when applying DAG-shape rules (`ParallelBranch`, `StateDrop`):

```sql
SELECT span_id, call_site_id, start_time, end_time, input_content_hash, output_content_hash
FROM spans
WHERE trace_id = ?
ORDER BY start_time DESC
LIMIT 16;
```

### Complete-plan profiles

The existing per-`call_site_id` cost model remains the reference-call and
operator-reporting summary. It does not drive joint selection because it pools
outcomes from different target models and transformations. For each call site it
tracks:

```rust
pub struct CallSiteProfile {
    pub call_site_id: String,
    pub n_observations: u32,           // lifetime count for operator reporting
    pub window_observations: u32,      // retained count, capped at cost_model_window

    // Cost distribution (last cost_model_window observations).
    pub input_tokens:  WelfordStats,   // mean, variance
    pub output_tokens: WelfordStats,
    pub latency_ms:    WelfordStats,
    pub cost_usd:      WelfordStats,

    // Output shape features — inform ModelDowngrade and friends.
    pub output_token_p95: f32,
    pub output_token_p99: f32,
    pub output_is_structured: f32,     // fraction of outputs that parse as JSON
    pub output_is_short: f32,          // fraction with output_tokens <= 128
}
```

Decision-critical evidence lives in a separate bounded profile keyed by
`(call_site_version, execution_plan_id)`:

```rust
pub struct PlanProfile {
    pub call_site_version: CallSiteVersion,
    pub execution_plan_id: ExecutionPlanId,
    pub n_observations: u64,
    pub window_observations: u32,
    pub paired_observations: u32,
    pub input_tokens: WelfordStats,
    pub output_tokens: WelfordStats,
    pub latency_ms: WelfordStats,
    pub cost_usd: WelfordStats,
    pub divergence_samples: VecDeque<f32>,
    pub provider_protocol: String,
    pub target_model_id: String,
    pub target_model_version: String,
    pub updated_at_us: i64,
}
```

`WelfordStats` is the numerically stable mean/variance summary. Both stores
retain exact newest-N samples and recompute each summary
from that bounded set, including nearest-rank p95 and p99. `CostModel::get`
clones the summary while sharing the retained samples through `Arc`; planning
therefore does not copy the window. The profiles are **empirical, not
predictive**: an observation updates only the complete plan that actually ran.
A routing-only profile and rewrite-only profile cannot authorize their
combination.

The cost model and plan profiles persist in `cost_model.db` (sibling of `traces.db`) with an
in-memory cache warmed at optimizer start. `optimize_observe` updates the
matching in-memory plan window synchronously after the provider response. The native runtime
flushes dirty profiles every 16 observations, on explicit `optimize_flush`, and
before lifecycle reset. Each SQLite transaction writes the summary and its
exact retained samples together.

Rule-level projections remain candidate-generation hints:

| Rule | Projection |
|---|---|
| `CacheHit` | `cost_usd.mean` (we skip the call entirely) |
| `ContextCompress` | `cost_usd.mean * dropped_input_fraction` |
| `ParallelBranch` | `0` cost, `(n - 1) * latency_ms.mean / n` latency |
| `ModelDowngrade` | `cost_usd.mean * (1 - target_model_price_ratio)` |
| `StateDrop` | `cost_usd.mean * dropped_state_fraction` |

The constrained selector ranks admitted plans by observed complete-plan cost or
latency. Its net estimate subtracts optimizer work, sampled counterfactual cost,
and observed fallback/retry cost. Rule projections never override a complete-
plan observation.

### Hot threshold and cold path

A call site is **cold** when `window_observations < hot_threshold`. Cold calls
return `Plan::PassThrough` immediately — no rules are evaluated and no overhead
beyond the profile lookup is incurred. The lifetime `n_observations` counter
does not keep a migrated or emptied window hot. This matters because:

1. Rules that depend on output-shape features (`ModelDowngrade`) need observations to fire correctly; firing on observation #1 would be a random bet.
2. The cost-model confidence below `hot_threshold` is 0; projected savings can't be ranked reliably.
3. Users observe that "the first few calls of a new agent run at full cost" — this is intentional and documented.

The default `hot_threshold = 3` is chosen so that a call site is optimizable after a warm-up that's short enough to matter for interactive workloads (most agents run ≥ 10 calls per session) but long enough to filter literal one-off calls.

### Rule engine

On a hot call, the optimizer:

1. Gathers the recent DAG context (last 16 spans in the trace).
2. Calls `rule.applies(&call, &profile)` and `rule.propose(...)` for each
   enabled rule to obtain structurally valid mutations.
3. Crosses those mutations with allowlisted model targets, preserving operation
   order and rejecting incompatible request shapes.
4. Computes a canonical identity for every complete candidate and loads its
   exact plan profile.
5. Rejects candidates with inadequate evidence, stale/non-finite estimates,
   excessive divergence, non-positive net benefit, active disable state, or an
   exhausted exploration/exposure budget.
6. Minimizes expected billed cost or latency among admitted plans. Ties prefer
   more evidence, lower divergence, fewer mutations, then the lexicographically
   smaller plan ID.
7. Returns the immutable reference call when no alternative qualifies.

Cost-driver orthogonality is a compatibility filter and an explicit
`independent_route_then_rewrite` baseline. It is not evidence that a composed
plan is safe or near-additive on a different model.

### Rule specifications

#### `CacheHit`

- **Applies when:** The `Cache` trait returns `Some(CacheHit)` for the canonical form of the call.
- **Safety check:** Cache age is within `ttl_seconds`. Source-specific: for `Exact` hits, always pass; for `Lsh` hits, require `similarity >= 0.95` (tighter than the memoization default — the optimizer's budget is stricter than opt-in memoize's).
- **Rewrite:** `Plan::Cached { value }`.
- **Observation feedback:** Divergence measured in shadow mode; high divergence auto-disables the rule on that call site.

#### `ContextCompress`

- **Applies when:** `prompt_bytes > min_prompt_bytes` (default 8 KB) AND at least 30% of the prompt's tokens have zero downstream attention score (per the profiler's attention-slice detector).
- **Safety check:** The compressed prompt still contains every token that appears in `DepSource::UserInput`, every token that any subsequent span read (via span input-hash overlap), and at least one token from each distinct role in the original messages list.
- **Rewrite:** `Plan::Rewritten { call: call_with_compressed_messages, ... }`. Compression is **extractive** — drop low-salience message segments. It does not summarize or rewrite content; summary-based compression requires a secondary LLM call that blows the overhead budget.
- **Projection:** `cost_usd.mean * fraction_dropped`.

#### `ParallelBranch`

- **Applies when:** The current call carries a `parallel_peer` descriptor on `parameters.extra` (staged by `agentc.parallel_map` via a thread-local), AND both the call and the peer have at least one non-`Literal` `DepSource`, AND the deps are disjoint (no span's output feeds another's input).
- **Safety check:** The disjointness proof must hold on the exact `DepSource` annotations; no heuristic overlap.
- **Rewrite:** `Plan::Parallel { calls, rule: "ParallelBranch", projected_savings_usd }`. Concurrent dispatch happens in `agentc.parallel_map` (driver pattern) — the executor's `Plan::Parallel` branch is observe-only, falling through to the original call so a serial-mode environment doesn't double-dispatch. The rule's contribution is the audit row plus the disjointness proof.
- **Projection:** `0` on cost; `(n - 1) * latency / n` on wall clock.

#### `ModelDowngrade`

- **Candidate source:** The model catalog enumerates every allowlisted target
  compatible with the provider protocol, tools, modalities, context length, and
  output cap. `ModelDowngrade` remains the name of the routing-only baseline.
- **Admission:** A target alone or in combination with rewrites needs its own
  complete-plan evidence. Output length and shape are candidate filters, not a
  substitute for observed behavior on the target.
- **Rewrite:** Set `call.model` to the selected target while preserving the
  provider-native request.
- **Projection:** Price metadata supplies an initial arithmetic hint; observed
  complete-plan billed cost ranks admitted candidates.

#### `StateDrop`

- **Applies when:** The call's `messages` contain content tagged with `DepSource::State { key }` for one or more keys, AND none of the last `N` spans in the trace read any of those keys (via tagged downstream `input_deps`).
- **Safety check:** The dropped keys are not present in the system prompt (which might encode invariants); and the post-drop prompt retains ≥ 50% of the original `messages` list (otherwise a larger rewrite is too risky).
- **Rewrite:** `Plan::Rewritten` with the identified state fields removed from `messages`.
- **Projection:** `cost_usd.mean * dropped_state_fraction`.

### Plan risk, exploration, and fallback

Every non-reference plan has a plan-level divergence threshold selected on the
frozen calibration split. The optimizer retains the exact newest 50 paired
divergence samples per `(call-site version, execution-plan ID)`. A plan needs at
least 20 paired observations and a one-sided conformal upper 95th-percentile at
or below its threshold before it becomes user-visible.

Initial exploration returns the reference result and executes at most one
candidate counterfactual in the background. It is capped at 20 candidate calls
per call site in 24 hours and one concurrent counterfactual. After admission,
the selected result is returned and the reference is sampled at `shadow_rate`
(default 2%) for drift detection.

The controller tracks sampled divergence exposure:

```text
E_t = sum(max(0, divergence_i - threshold)).
```

Crossing `E_t = 1.0` in 24 hours durably disables that complete plan. A
provider/model version, prompt-shape version, tool schema, or rewrite
implementation change starts a cold profile immediately. The 24-hour cooldown
ends in cold re-admission, not automatic full-rate reuse.

Each comparison updates one complete-plan history. A composed result is never
copied into every constituent rule as if it were causal evidence. Solo-rule
guard rows remain available for the routing-only and rewrite-only baselines.

Divergence and thresholds are finite fractions in `[0,1]`; invalid values do not
create or mutate state. Text divergence uses the calibration-selected metric.
Tool calls compare tool identity and schema-valid arguments. The runtime calls
this quantity divergence, not quality. Workload-level task damage is measured
only by the evaluation harness where labels or official scores exist.

### Overhead budget

The optimizer targets < 1 ms p99 per intercepted call. The `plan` path's work:

| Step | Target | Notes |
|---|---|---|
| FFI boundary (JSON in) | 100 μs | 2-5 KB payload |
| Profile lookup | 50 μs | in-memory HashMap |
| DAG context fetch (last 16 spans) | 300 μs | SQLite read, cached per-trace |
| Rule applies + propose | 200 μs | 5 rules × 40 μs |
| Ranking + safety checks | 100 μs | |
| FFI boundary (JSON out) | 100 μs | |
| **p99 total** | **≤ 1 ms** | |

`max_overhead_ms` (default 5 ms) is the kill switch: if the measured plan time exceeds it, the optimizer returns `Plan::PassThrough` and logs. This protects against pathological cases (huge prompts, slow SQLite pages) while keeping the runtime honest.

### Persistent storage

Two new DBs alongside `traces.db`:

- **`cost_model.db`** — per-call-site rolling stats. Schema below.
- **`optimizer_audit.db`** — a ring buffer of the last 10,000 plans (for `agentc optimize inspect`). Schema below.

```sql
-- cost_model.db
CREATE TABLE IF NOT EXISTS call_site_profile (
    call_site_id          TEXT PRIMARY KEY NOT NULL,
    n_observations        INTEGER NOT NULL,
    window_observations   INTEGER NOT NULL,
    input_tokens_mean     REAL NOT NULL,
    input_tokens_var      REAL NOT NULL,
    output_tokens_mean    REAL NOT NULL,
    output_tokens_var     REAL NOT NULL,
    latency_ms_mean       REAL NOT NULL,
    latency_ms_var        REAL NOT NULL,
    cost_usd_mean         REAL NOT NULL,
    cost_usd_var          REAL NOT NULL,
    output_token_p95      REAL NOT NULL,
    output_token_p99      REAL NOT NULL,
    output_is_structured  REAL NOT NULL,
    output_is_short       REAL NOT NULL,
    updated_at            INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS call_site_observation (
    call_site_id          TEXT NOT NULL,
    sample_sequence       INTEGER NOT NULL,
    input_tokens          INTEGER NOT NULL,
    output_tokens         INTEGER NOT NULL,
    latency_ms            REAL NOT NULL,
    cost_usd              REAL NOT NULL,
    output_is_structured  INTEGER NOT NULL CHECK (output_is_structured IN (0, 1)),
    output_is_short       INTEGER NOT NULL CHECK (output_is_short IN (0, 1)),
    observed_at           INTEGER NOT NULL,
    PRIMARY KEY (call_site_id, sample_sequence)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_profile (
    call_site_version       TEXT NOT NULL,
    execution_plan_id       TEXT NOT NULL,
    n_observations          INTEGER NOT NULL,
    window_observations     INTEGER NOT NULL,
    paired_observations     INTEGER NOT NULL,
    input_tokens_mean       REAL NOT NULL,
    output_tokens_mean      REAL NOT NULL,
    latency_ms_mean         REAL NOT NULL,
    cost_usd_mean           REAL NOT NULL,
    divergence_upper_p95    REAL,
    provider_protocol       TEXT NOT NULL,
    target_model_id         TEXT NOT NULL,
    target_model_version    TEXT NOT NULL,
    updated_at              INTEGER NOT NULL,
    PRIMARY KEY (call_site_version, execution_plan_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_observation (
    call_site_version       TEXT NOT NULL,
    execution_plan_id       TEXT NOT NULL,
    sample_sequence         INTEGER NOT NULL,
    input_tokens            INTEGER NOT NULL,
    output_tokens           INTEGER NOT NULL,
    latency_ms              REAL NOT NULL,
    cost_usd                REAL NOT NULL,
    divergence              REAL CHECK (
        divergence IS NULL OR (divergence >= 0.0 AND divergence <= 1.0)
    ),
    dispatch_fallback       INTEGER NOT NULL CHECK (dispatch_fallback IN (0, 1)),
    provider_protocol       TEXT NOT NULL,
    target_model_id         TEXT NOT NULL,
    target_model_version    TEXT NOT NULL,
    price_table_version     TEXT NOT NULL,
    observed_at             INTEGER NOT NULL,
    PRIMARY KEY (call_site_version, execution_plan_id, sample_sequence)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_disabled (
    call_site_version       TEXT NOT NULL,
    execution_plan_id       TEXT NOT NULL,
    reason                  TEXT NOT NULL,
    exposure                REAL NOT NULL CHECK (exposure >= 0.0),
    disabled_at             INTEGER NOT NULL,
    reenable_at             INTEGER NOT NULL,
    PRIMARY KEY (call_site_version, execution_plan_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rule_divergence (
    call_site_id          TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    n_samples             INTEGER NOT NULL,
    window_samples        INTEGER NOT NULL,
    divergence_mean       REAL NOT NULL,
    divergence_var        REAL NOT NULL,
    consecutive_breaches  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (call_site_id, rule)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rule_divergence_observation (
    call_site_id          TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    sample_sequence       INTEGER NOT NULL,
    divergence            REAL NOT NULL CHECK (divergence >= 0.0 AND divergence <= 1.0),
    observed_at           INTEGER NOT NULL,
    PRIMARY KEY (call_site_id, rule, sample_sequence)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS optimizer_disabled (
    call_site_id          TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    reason                TEXT NOT NULL,
    disabled_at           INTEGER NOT NULL,
    reenable_at           INTEGER NOT NULL,
    PRIMARY KEY (call_site_id, rule)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rule_set_stats (
    call_site_id          TEXT NOT NULL,
    rule_set              TEXT NOT NULL,
    n                     INTEGER NOT NULL,
    mean                  REAL NOT NULL,
    m2                    REAL NOT NULL,
    updated_at            INTEGER NOT NULL,
    PRIMARY KEY (call_site_id, rule_set)
) STRICT, WITHOUT ROWID;

-- optimizer_audit.db
CREATE TABLE IF NOT EXISTS plan_audit (
    audit_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_us                 INTEGER NOT NULL,
    call_site_id          TEXT NOT NULL,
    span_id               BLOB NOT NULL,
    plan_kind             TEXT NOT NULL,
    rule                  TEXT,
    projected_savings_usd REAL,
    measured_savings_usd  REAL,
    overhead_us           INTEGER NOT NULL,
    shadow_sampled        INTEGER NOT NULL DEFAULT 0,
    shadow_divergence     REAL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_audit_call_site ON plan_audit(call_site_id, ts_us DESC);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON plan_audit(ts_us);
```

Schema migration adds `window_observations`, both retained-sample tables,
`rule_divergence.window_samples`, and
`rule_divergence.consecutive_breaches` to existing databases. A divergence row
from before breach persistence receives a zero streak. An unbounded legacy
cost or divergence summary cannot be reconstructed into an exact window:
migration preserves its lifetime count, zeros its window statistics, and
retains any already-persisted breach streak. Changing either configured window
to a smaller value truncates and persists the newest samples at startup.

The joint planner adds the three `execution_plan_*` tables above without
promoting legacy `call_site_profile`, `rule_divergence`, or `rule_set_stats`
rows into plan evidence. Those rows remain reporting and baseline state. Every
new complete plan starts cold because the old schema cannot identify the target,
ordered transformations, or validation policy that produced an observation.

`plan_audit` supports pruning to a 10,000-row cap through `audit::prune`.
Pruning is an explicit maintenance operation; the runtime does not silently
delete audit rows in the background.

### Repo layout

```
crates/
├── agentc-optimizer/                # New crate.
│   ├── Cargo.toml
│   ├── src/
│   │   ├── lib.rs
│   │   ├── planner.rs               # Optimizer::plan entry point
│   │   ├── cost_model.rs            # CallSiteProfile, WelfordStats
│   │   ├── dag.rs                   # Call, DepSource, DAG context queries
│   │   ├── budget.rs                # Accuracy budget enforcement
│   │   ├── shadow.rs                # Shadow-mode sampling + divergence
│   │   ├── rules/
│   │   │   ├── mod.rs
│   │   │   ├── cache_hit.rs
│   │   │   ├── context_compress.rs
│   │   │   ├── parallel_branch.rs
│   │   │   ├── model_downgrade.rs
│   │   │   └── state_drop.rs
│   │   ├── schema.rs                # DDL for cost_model.db and optimizer_audit.db
│   │   └── ffi.rs                   # PyO3 bindings re-exported via agentc-profiler
│   └── tests/
│       ├── cold_path.rs
│       ├── hot_path.rs
│       ├── rule_ranking.rs
│       ├── accuracy_budget.rs
│       └── rules/
│           ├── cache_hit.rs
│           ├── context_compress.rs
│           ├── parallel_branch.rs
│           ├── model_downgrade.rs
│           └── state_drop.rs
├── agentc-profiler/                 # Extended: exposes read-only Profile trait.
└── agentc-memo/                     # Existing: exposes Cache trait (consumed by CacheHit rule).

python/agentc/
├── _intercept.py                    # SDK-level call capture
├── _provenance.py                   # DepSource tagging helpers
├── _optimizer.py                    # optimize_plan/optimize_observe shim, executor
├── _executor.py                     # dispatches Plan variants
├── _shadow.py                       # shadow-mode double-execution
└── _native.pyi                      # extended with optimize_* stubs
```

### Python ↔ Rust boundary

| Responsibility | Python | Rust |
|---|---|---|
| Provider SDK interception | ✓ | |
| Provenance tagging | ✓ | |
| `Call` assembly + JSON serialization | ✓ | |
| FFI `optimize_plan` dispatch | | ✓ |
| Profile lookup | | ✓ |
| Rule evaluation | | ✓ |
| Safety checks | | ✓ |
| Projected savings ranking | | ✓ |
| `Plan` serialization | | ✓ |
| Plan execution (cached → return; rewritten → dispatch; parallel → asyncio.gather) | ✓ | |
| Shadow-mode double-execution | ✓ | |
| Divergence measurement | ✓ | ✓ (confirms) |
| `optimize_observe` | | ✓ |
| Cost model updates | | ✓ |
| Audit trail writes | | ✓ |
| CLI `agentc optimize …` | | ✓ |

### Error handling and fail-open

Every optimizer path is wrapped to fail open:

1. **`optimize_plan` FFI raises** → SDK treats as `PassThrough`, logs at debug.
2. **Rule panics** (PyO3 `PanicException`) → the optimizer treats that rule as inapplicable for the call, logs at warn, and falls back to the next rule or `PassThrough`.
3. **Cost-model DB corruption** → in-memory cache continues; persistence is disabled until restart.
4. **Plan dispatch fails** (e.g., downgraded model is unavailable) → executor catches, retries the original call once, logs at warn.
5. **Shadow-mode execution fails** → the primary result still returns; divergence is not recorded for that call.

A user's LLM call never fails because the optimizer failed.

### Concurrency model

- **Plan evaluation is read-only** for shared state. A `DashMap` shard lock is
  held only long enough to clone a profile summary; the retained sample window
  is shared through `Arc` and is not copied. Multiple threads then evaluate
  plans independently.
- **Cost model updates lock one call-site entry.** An update mutates that
  profile's copy-on-write sample window, recomputes its statistics, and records
  a monotonically increasing dirty generation. An older flush clears a dirty
  marker only when its captured generation is still current.
- **SQLite cost-model flushes are serialized** by the native profiler's cost-DB
  mutex. Each transaction replaces the exact retained samples and summary for
  every captured dirty site.
- **Guard updates lock one site/rule entry.** Each shadow sample mutates a
  copy-on-write retained window, recomputes its statistics, updates the raw
  breach streak, records a dirty generation, and then uses the native cost-DB
  mutex to persist the summary and exact samples atomically. A flush clears
  only the captured generation, so a concurrent post-snapshot sample remains
  dirty.
- **Audit writes are synchronous and serialized** by a separate audit-DB mutex.
- **Shadow execution runs in a background `asyncio.Task`** or thread (depending on the SDK), so it never blocks the primary return. If the shadow task doesn't complete within 2× the primary latency, it's dropped.

---

## Dependencies

### Sibling components

- **agentc-profiler** — supplies `traces.db` and the `Profile` trait. The optimizer is a downstream reader; it never writes to profiler-owned tables.
- **agentc-memo** — supplies the `Cache` trait for the `CacheHit` rule. Memoization's decorator-based opt-in is independent of the optimizer's automatic activation; the optimizer only consumes the trait, not the decorator.
- **agentc-core** — SQLite infrastructure, canonical path resolution, merge coordination (applied to `cost_model.db` as well as `traces.db`).

### Rust crates

Already in the workspace:
- `rusqlite` (bundled)
- `sha2`, `serde`, `serde_json`
- `zstd`
- `pyo3`, `pyo3-log`
- `parking_lot` (for the in-memory cost model's `RwLock`)
- `dashmap` (for the call-site profile cache)

New workspace additions:
- None.

### Python packages

- `asyncio` (stdlib) — required for `ParallelBranch`.
- No new third-party dependencies.

### Framework integrations (optional)

Provenance tagging has adapters for:

- `langgraph` (hooks `StateGraph.node` decorators)
- `crewai` (hooks `Task.execute`)
- `autogen` (hooks `ConversableAgent.generate_reply`)

Missing adapter → `DepSource::Literal` everywhere; `ParallelBranch` and `StateDrop` no-op, the other three rules work normally.

---

## Evaluation

### Correctness

| Check | Test fixture |
|---|---|
| Cold call returns `PassThrough` on call #1..hot_threshold | `tests/cold_path.rs` |
| Call #hot_threshold+1 evaluates rules | `tests/hot_path.rs` |
| Rules rank by projected savings descending | `tests/rule_ranking.rs` |
| Safety check failure skips to next proposal | `tests/rule_ranking.rs` |
| No rule fires → returns `PassThrough` | `tests/hot_path.rs` |
| `CacheHit` on Exact source always passes | `tests/rules/cache_hit.rs` |
| `CacheHit` on Lsh < 0.95 is skipped | `tests/rules/cache_hit.rs` |
| `ContextCompress` retains `DepSource::UserInput` tokens | `tests/rules/context_compress.rs` |
| `ParallelBranch` requires disjoint deps | `tests/rules/parallel_branch.rs` |
| `ModelDowngrade` waits for ≥ 20 shadow samples before committing | `tests/rules/model_downgrade.rs` |
| `StateDrop` preserves system prompt | `tests/rules/state_drop.rs` |
| Budget-exceeded rule auto-disables | `tests/accuracy_budget.rs` |
| Auto-disabled rule re-enables after 24h | `tests/accuracy_budget.rs` |
| Optimizer FFI panic yields PassThrough | `tests/fail_open.rs` |
| Overhead kill switch activates above `max_overhead_ms` | `tests/fail_open.rs` |
| All cost and shape statistics retain exactly the last configured N samples | `cost_model::tests::rolling_window_recomputes_every_stat_after_distribution_shift` |
| Exact retained window survives restart and continues eviction | `cost_model::tests::retained_window_survives_restart_and_continues_eviction` |
| Smaller runtime window is persisted on restart | `cost_model::tests::smaller_window_is_applied_and_persisted_on_restart` |
| Legacy unbounded profiles restart cold while preserving lifetime count | `schema::tests::legacy_unbounded_profile_is_cold_started_without_losing_lifetime_count` |
| Concurrent post-snapshot update remains dirty and survives restart | `cost_model::tests::flush_keeps_dirty_marker_for_post_snapshot_observation` |
| Divergence mean and breach streak survive restart | `budget::tests::divergence_and_breach_streak_survive_restart` |
| Divergence statistics retain exactly the newest configured N samples | `budget::tests::retained_window_ages_out_old_divergence_distribution` |
| Exact divergence window survives restart and continues eviction | `budget::tests::retained_window_survives_restart_and_continues_eviction` |
| Smaller divergence window is persisted on restart | `budget::tests::smaller_window_is_applied_and_persisted_on_restart` |
| Legacy cumulative divergence stats restart cold while preserving lifetime count and streak | `schema::tests::legacy_divergence_rows_cold_start_window_and_gain_zero_breach_streak` |
| Inspect does not report a cold-started zero-sample row as measured zero divergence | `reporting::tests::inspect_ignores_cold_started_divergence_without_retained_samples` |
| A within-budget sample resets a hydrated streak | `budget::tests::within_budget_sample_resets_restarted_streak` |
| A concurrent post-snapshot divergence remains dirty | `budget::tests::flush_keeps_post_snapshot_sample_dirty` |
| The fifth breach after restart disables durably | `tests/test_lifecycle.py::TestInit::test_guard_breach_streak_survives_reinit` |
| Canonical plan identity includes target, ordered rewrites, versions, parameters, and validation policy | `execution_plan::tests::canonical_identity_*` |
| Cost and latency objectives select the best admissible complete plan | `execution_plan::tests::selects_*` |
| Missing, non-finite, stale, under-evidenced, or non-positive candidates abstain | `execution_plan::tests::rejects_*` |
| Routing-only and rewrite-only evidence cannot admit their joint plan | `plan_profile::tests::complete_plan_evidence_is_not_synthesized` |
| A harmful composed interaction updates only its complete-plan guard | `plan_guard::tests::composed_sample_has_single_causal_identity` |
| A failed selected target retries the exact original request once | `tests/test_optimizer_glue.py::test_routed_failure_replays_exact_original` |

`bench/guard_persistence_preflight.py` is the deterministic Stage E0 replay for
the restart boundary. It records four breaches, restarts, records the fifth,
and verifies both immediate SQLite state and a second-restart pass-through. It
uses no provider calls and is permanently labeled `paper_evidence=false`.

`crates/agentc-optimizer/examples/divergence_window_preflight.rs` is the
deterministic Stage E0 replay for estimator drift and restart equivalence. It
ages a 50-sample old distribution completely out, reloads the exact retained
rows, persists a smaller configured window, and exercises conservative legacy
migration without provider calls. It is permanently labeled
`paper_evidence=false`.

### Performance targets

Plan benchmarks live in `bench/optimizer_bench.py`. The release-mode bounded
window diagnostic lives in
`crates/agentc-optimizer/examples/cost_model_window_preflight.rs` and remains
Stage E0 engineering evidence rather than a paper benchmark.

| Metric | Target | Measurement |
|---|---|---|
| p50 plan overhead (hot call) | < 0.5 ms | 5-rule optimizer, 100k-entry cache |
| p99 plan overhead (hot call) | < 1.2 ms | Same |
| p50 plan overhead (cold call) | < 100 μs | Profile lookup + early return |
| p99 plan overhead (cold call) | < 300 μs | Same |
| Shadow-mode sample rate | 2% ± 0.3% | Bernoulli(0.02) over 10k calls |
| Cost model write throughput | > 1000 observations/s | In-memory update; persistence measured separately |

### Savings / accuracy (reference agents)

| Agent | Baseline cost | Target savings | Accuracy baseline | Accuracy floor |
|---|---|---|---|---|
| `bench/agents/swebench_planner.py` | $14.82 / 50 tasks | ≥ 30% | 82.0% resolve rate | ≥ 80.0% |
| `bench/agents/gaia_router.py` | $8.44 / 80 questions | ≥ 35% | 71.2% correct | ≥ 69.0% |
| `bench/agents/rag_summarizer.py` | $4.21 / 200 docs | ≥ 40% | 0.84 ROUGE-L | ≥ 0.82 |
| `bench/agents/multiagent_research.py` | $22.18 / 30 tasks | ≥ 25% | 7.4/10 quality | ≥ 7.1/10 |

Accuracy floor is the hard fail gate — no release passes if a reference agent drops below it.

### Plan-selection ablations

The confirmatory harness runs fixed-strong, fixed-cheap, routing-only,
rewrite-only on the strong model, independently routed-then-rewritten, and joint
guarded policies on identical tasks and model pools. Secondary arms remove each
model and rewrite family, run each rewrite on each compatible fixed model, and
disable complete-plan interaction profiles.

This produces a `(call-site, model, rewrite set)` capability frontier and tests
whether joint selection adds value beyond the strongest router or rewrite
system. It also exposes negative interactions and abstention-dominant workloads.

### Counterfactual divergence bounds

For each `(call-site version, execution plan)` pair, calibration selects a
threshold from `{0.05, 0.10, 0.15, 0.20, 0.30, 0.50}`. Admission requires at
least 20 paired samples and a one-sided conformal upper 95th-percentile no
greater than that threshold. The held-out report includes the full distribution,
false disables, missed harmful plans, time and calls to fallback, divergence
exposure, task-equivalent damage where labels exist, and net savings after
counterfactual cost.

### Acceptance criteria (ship gate)

The optimizer crate reaches `status: active` when:

- All correctness tests pass.
- Joint planning beats routing-only and rewrite-only in at least two frozen
  workload/model cells without crossing the predeclared quality margin.
- Joint planning beats independently routed-then-rewritten execution on at
  least one primary efficiency outcome in those cells.
- p99 plan overhead is within 1.2 ms on the reference hardware.
- The plan-level guard meets the frozen 2% damage and false-disable gate.
- Fail-open paths are exercised by fault-injection tests (`tests/fail_open.rs`).

---

## Design Decisions

### Hot-path JIT, not eager optimization

Cold calls are pass-through; optimization kicks in after `hot_threshold` observations. The profiler already produces the empirical data the cost model needs, and first-call latency stays clean (no optimizer overhead before we have profile data to act on). **Rejected: eager rewriting on every call.** Pays optimizer cost on the first invocation when the cost model has zero confidence — the rewrite is a guess, not a decision. **Rejected: waste-triggered only.** Only fires on call sites the profiler's 5 detectors flag; misses wins outside those detector categories.

### Joint complete-plan selection, not independent routing and rewriting

Enumerate target-plus-rewrite candidates and compare complete-plan profiles.
This captures model--rewrite interactions directly and gives every observed
outcome one causal plan identity. **Rejected: route first and then run the
orthogonality composer.** It assumes that a router's fixed-request estimate and a
rewrite's fixed-model estimate transfer to their combination. Keep this path as
an evaluation baseline. **Rejected: infer joint safety from solo-rule budgets.**
A compressed-and-routed request can fail even when each decision is benign alone.

### Empirical complete-plan profiles, not a learned predictor

Retain the exact newest observations per call-site version and complete
execution plan, and derive cost, latency, output-shape, and divergence summaries
from that set. This makes drift response deterministic across process restarts
while keeping the default state bounded to 50 samples per plan. The call-site
aggregate remains reporting-only. **Rejected: unbounded Welford aggregates.**
They cannot age out provider or workload drift or recover exact quantiles.
**Rejected: mergeable exponential decay.** It makes the configured horizon
approximate and complicates replay. **Rejected: a learned predictor before an
empirical baseline.** It adds a training pipeline and opaque failure modes
before the project has demonstrated that the joint opportunity exists.

### Complete-plan divergence contract

Attach evidence and disable state to `(call-site version, execution-plan ID)`.
Initial exploration returns the reference result; ongoing sampled comparisons
track drift after admission. Persist the exact window and exposure state so a
restart cannot erase evidence immediately before fallback. **Rejected: charging
a composed comparison to every constituent rule.** It fabricates causal evidence
and can disable an innocent transformation. **Rejected: a global budget.** It
muddles which plan consumed exposure. **Rejected: calling the divergence meter
an accuracy oracle.** Task damage is available only in labeled evaluation.

### `CacheHit` as a rewrite rule, not a bypass

Memoization is first-class as a rewrite rule so the optimizer's ranking, budget, and audit trail apply uniformly. This also lets `ModelDowngrade` propose cost wins on calls where `CacheHit` didn't fire — projected savings are compared apples-to-apples. **Rejected: memoization short-circuits before the optimizer.** Two side-effect paths, two audit trails, two budget systems. One plan pipeline is easier to reason about and benchmark.

### Python drives plan execution; Rust plans but never dispatches

The Rust optimizer computes `Plan`s and emits them as JSON. Python's executor is responsible for the actual LLM calls (cached return, rewritten dispatch, parallel fan-out). This keeps Rust free of vendor SDKs, HTTP clients, and credential handling. **Rejected: Rust executes directly.** Requires linking a Rust HTTP client, handling provider SDKs' auth conventions, and re-implementing streaming across every provider — a full second SDK surface.

### Provenance tagging depends on framework adapters

`ParallelBranch` and `StateDrop` need `DepSource` annotations that can't be reliably inferred from raw `messages`. Framework adapters supply them; framework-free users get the other three rules (`CacheHit`, `ContextCompress`, `ModelDowngrade`). This trades universal coverage for correctness — a `ParallelBranch` that fires on non-disjoint deps is a race condition, not a savings. **Rejected: heuristic provenance from message content overlap.** False positives on shared boilerplate (system prompts); false negatives on renamed fields. Requires tuning per-framework anyway.

### Production counterfactual sampling at 2%

Use 2% as the frozen production operating point and measure its actual detection
delay, retained savings, synchronous latency, false disables, and task damage.
It is a cost setting, not an assertion that every session yields enough samples.
Initial candidate calibration uses bounded reference-visible exploration rather
than exposing an unadmitted result. **Rejected: extrapolating from 100%
sampling.** Detection time changes by orders of magnitude. **Rejected: never
sampling.** The runtime then has no direct signal for provider, model, or
workload drift.

---

## Open Questions

> **OPEN (avery, 2026-05-15):** Decide how to handle streaming LLM responses under the optimizer. `CacheHit` is trivial (replay the cached stream chunk-by-chunk). `ModelDowngrade` on a streaming call requires the downgrade target also supports streaming at the same chunking granularity. `ParallelBranch` interleaves streams from parallel calls, which the user's UI may or may not expect. Initial implementation disables the optimizer for streaming calls (`extra_headers={"agentc-optimize": "false"}` equivalent, applied automatically when `stream=True`); revisit when we have a streaming reference agent.

> **OPEN (avery, 2026-05-15):** Resolve how `ContextCompress` interacts with vendor-side prompt caching (OpenAI's prefix caching, Anthropic's `cache_control`). Dropping tokens from a cached prefix invalidates the vendor cache and can cost more than it saves. Provisional behavior: `ContextCompress` is disabled when the `messages` list contains any `cache_control` marker; this is conservative and may leak savings. Needs a proper cost model term for "cached prefix length gained/lost per compression."
