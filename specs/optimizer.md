---
title: Optimizer
status: active
last-updated: 2026-04-16
---

# Optimizer

The JIT runtime that intercepts LLM calls, lifts them into a typed DAG IR, applies cost-ranked rewrite rules subject to a per-rule accuracy budget, and executes the rewritten plan. Optimization fires on hot call sites only — cold paths run unmodified so that first-call latency stays clean and the cost model has real data before it makes decisions. The optimizer depends on the profiler for execution traces (its training corpus) and the memoization layer for the `Cache` trait (one of its rewrite rules).

---

## Overview

Agent code is mostly untuned. Developers write straightforward `llm.chat(...)` sequences; they rarely hand-tune context windows, parallelize independent tool calls, or route simple subtasks to cheaper models. Agentc's optimizer does that tuning automatically, at runtime, without requiring the application to expose a static execution plan.

The optimizer operates at the **call boundary**. On every intercepted LLM call it asks:

1. Have we seen this call site N times? (If no → execute unmodified, profile it.)
2. What does the empirical cost model say about this call site's cost, latency, and accuracy distribution?
3. Which rewrite rules apply, and what is the projected cost delta of each?
4. Does the rewrite stay within the accuracy budget for this rule?
5. Execute the rewritten plan; record outcome for the cost model.

Five rewrite rules ship in the initial implementation:

| Rule | Trigger | Effect |
|---|---|---|
| `CacheHit` | Call site has a hot memoization cache | Serve cached output via the `Cache` trait instead of calling the model. |
| `ContextCompress` | Prompt > 8 KB and >30% of tokens have zero downstream attention score | Drop or summarize low-salience context chunks. |
| `ParallelBranch` | ≥2 consecutive calls with disjoint input dependencies | Dispatch the independent calls concurrently. |
| `ModelDowngrade` | Call site's outputs are consistently simple (short, structured, or high-confidence-classifiable) | Route to a cheaper model with the same interface. |
| `StateDrop` | Prompt contains agent state fields that no subsequent call in the window reads | Drop the unused fields before dispatch. |

Each rule declares its own safety check — a cheap predicate that must be satisfied before the rewrite commits. Rules fire in a cost-ranked order (largest projected savings first); the first rule to pass its safety check wins and the others are skipped for that call.

This is a **JIT** optimizer in the literal compiler sense: cold code runs interpreted (pass-through), hot code gets compiled (rewritten) once the profile is statistically meaningful. It is not a whole-program optimizer — no global plan is required, no agent code needs to be annotated, no static analysis is performed.

**What the optimizer does not do:**

- Does not rewrite tool implementations, prompt templates, or agent code.
- Does not speculate across call sites that have not yet executed.
- Does not apply rewrites on the first N invocations of a call site; those are always pass-through.
- Does not learn rewrite rules. The rule set is fixed; the cost model under each rule is learned.
- Does not operate without the profiler — a cold-start workspace with no trace data always runs pass-through.

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
max_overhead_ms = 5                 # Abort optimization if budget exceeded.
shadow_rate = 0.02                  # 2% of optimized calls run shadow execution.

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
| `AGENTC_OPTIMIZE_SHADOW=0.1` | Overrides `shadow_rate`. |

### Rust API

```rust
// crates/agentc-optimizer/src/lib.rs

pub struct Optimizer {
    profile: Arc<dyn Profile>,          // Read-only view over traces.db
    cache: Arc<dyn Cache>,              // From agentc-memo
    cost_model: CostModel,
    rules: Vec<Box<dyn RewriteRule>>,
    config: OptimizerConfig,
}

impl Optimizer {
    pub fn new(profile: Arc<dyn Profile>, cache: Arc<dyn Cache>, config: OptimizerConfig) -> Self { ... }

    /// Entry point called by the SDK on every intercepted LLM call.
    /// Returns either a rewritten plan to execute, or `Plan::PassThrough`
    /// if the call is cold or no rule fires.
    pub fn plan(&self, call: &Call) -> Plan { ... }

    /// Record the actual outcome of a plan for the cost model.
    pub fn observe(&self, plan: &Plan, outcome: &Outcome);
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

### FFI surface

Two new functions on `agentc._native`:

```python
# python/agentc/_native.pyi
def optimize_plan(call_json: str) -> str:
    """
    Input: JSON-serialized Call (call_site_id, model, messages, parameters, tools).
    Output: JSON-serialized Plan. "pass_through" for cold or no-fire cases.
    """

def optimize_observe(plan_json: str, outcome_json: str) -> None:
    """
    Feeds the cost model with the measured outcome of a plan.
    """
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
    │   1. profile.lookup(call_site)  │
    │   2. if cold → PassThrough      │
    │   3. rank & apply rules         │
    │   4. safety checks              │
    │   5. return Plan                │
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
    │ → cost model updates            │
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

### Cost model

The cost model is a per-`call_site_id` rolling estimator fitted from the profiler's `spans` table. For each call site it tracks:

```rust
pub struct CallSiteProfile {
    pub call_site_id: String,
    pub n_observations: u32,
    pub confidence: f32,               // 0..1, saturates at cost_model_window samples

    // Cost distribution (last cost_model_window observations).
    pub input_tokens:  WelfordStats,   // mean, variance
    pub output_tokens: WelfordStats,
    pub latency_ms:    WelfordStats,
    pub cost_usd:      WelfordStats,

    // Accuracy proxies — per-rule, rolling.
    pub shadow_divergence_by_rule: HashMap<&'static str, WelfordStats>,

    // Output shape features — inform ModelDowngrade and friends.
    pub output_token_p95: f32,
    pub output_is_structured: f32,     // fraction of outputs that parse as JSON
    pub output_is_short: f32,          // fraction with output_tokens <= 128
}
```

`WelfordStats` is the numerically stable online mean/variance estimator (already used in the profiler for span stats). The cost model is **empirical, not predictive** — it summarizes what has been observed under each (call_site, rule) combination, and it trusts that distribution to extrapolate. No learned model, no neural network. A per-rule lookup is O(1).

The cost model persists in `cost_model.db` (sibling of `traces.db`) with an in-memory cache warmed at optimizer start. `optimize_observe` updates both the in-memory stats and the persistent store asynchronously via the writer thread.

**Projected savings per rule** come from direct arithmetic on the cost model:

| Rule | Projection |
|---|---|
| `CacheHit` | `cost_usd.mean` (we skip the call entirely) |
| `ContextCompress` | `cost_usd.mean * dropped_input_fraction` |
| `ParallelBranch` | `0` cost, `(n - 1) * latency_ms.mean / n` latency |
| `ModelDowngrade` | `cost_usd.mean * (1 - target_model_price_ratio)` |
| `StateDrop` | `cost_usd.mean * dropped_state_fraction` |

All projections ignore the optimizer's own overhead, which is tracked separately and subtracted from reported savings in `agentc optimize report`.

### Hot threshold and cold path

A call site is **cold** when `n_observations < hot_threshold`. Cold calls return `Plan::PassThrough` immediately — no rules evaluated, no overhead beyond the profile lookup. This matters because:

1. Rules that depend on output-shape features (`ModelDowngrade`) need observations to fire correctly; firing on observation #1 would be a random bet.
2. The cost-model confidence below `hot_threshold` is 0; projected savings can't be ranked reliably.
3. Users observe that "the first few calls of a new agent run at full cost" — this is intentional and documented.

The default `hot_threshold = 3` is chosen so that a call site is optimizable after a warm-up that's short enough to matter for interactive workloads (most agents run ≥ 10 calls per session) but long enough to filter literal one-off calls.

### Rule engine

On a hot call, the optimizer:

1. Gathers the recent DAG context (last 16 spans in the trace).
2. Calls `rule.applies(&call, &profile)` for each enabled rule. Filters to applicable rules.
3. Calls `rule.propose(&call, &profile)` for each applicable rule → `Vec<Proposal>`.
4. Sorts proposals by `projected_savings_usd` descending.
5. For each proposal in order, runs `proposal.safety_check(&call)`. The first to pass wins.
6. Returns `proposal.rewritten`.

Rules never compose in a single plan — the first passing rule wins. Composition increases the accuracy blast radius (a rewrite that fails under compression AND downgrade is hard to debug). **Rejected: greedy composition with cumulative budget.** See Design Decisions.

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

- **Applies when:** The last-executed N spans in the trace contain ≥ 2 consecutive `LlmOutput` or `ToolOutput` calls whose `input_deps` are disjoint (no span's output feeds another's input).
- **Safety check:** The disjointness proof must hold on the exact `DepSource` annotations; no heuristic overlap. The SDK must expose an async dispatch primitive for the rule to issue concurrent calls (OpenAI/Anthropic SDKs both do; raw `requests` calls don't qualify).
- **Rewrite:** `Plan::Parallel { calls, rule: "ParallelBranch" }`. Python executor dispatches via `asyncio.gather` or a thread pool depending on the SDK.
- **Projection:** `0` on cost; `(n - 1) * latency / n` on wall clock.

#### `ModelDowngrade`

- **Applies when:** The call's current `model` has an entry in `config.rules.ModelDowngrade.route`, AND `output_token_p95 <= route.max_output_tokens`, AND `output_is_short + output_is_structured >= 0.80` (the call site reliably produces short or structured outputs).
- **Safety check:** Projected shadow-mode divergence under the downgrade ≤ the rule's accuracy budget on this call site. On first-ever downgrade attempt for a call site, divergence is unknown → the rule fires probabilistically (30%) and observes; it commits fully only after ≥ 20 shadow samples confirm the budget.
- **Rewrite:** `Plan::Rewritten` with `call.model = route.to`.
- **Projection:** `cost_usd.mean * (1 - price_ratio(from, to))`.

#### `StateDrop`

- **Applies when:** The call's `messages` contain content tagged with `DepSource::State { key }` for one or more keys, AND none of the last `N` spans in the trace read any of those keys (via tagged downstream `input_deps`).
- **Safety check:** The dropped keys are not present in the system prompt (which might encode invariants); and the post-drop prompt retains ≥ 50% of the original `messages` list (otherwise a larger rewrite is too risky).
- **Rewrite:** `Plan::Rewritten` with the identified state fields removed from `messages`.
- **Projection:** `cost_usd.mean * dropped_state_fraction`.

### Accuracy budget

Every rule declares a per-call-site accuracy budget (e.g., `ModelDowngrade = 0.03` → 3% maximum shadow divergence). The optimizer maintains a rolling divergence estimate per `(call_site, rule)` pair, fed by shadow-mode sampling (`shadow_rate`, default 2% of optimized calls).

Budget enforcement:

- **Pre-fire:** The rule's safety check rejects proposals whose projected divergence (from the cost model) exceeds the budget.
- **Post-fire:** If the rolling observed divergence exceeds the budget for `k` consecutive samples (default `k = 5`), the optimizer auto-disables the rule on that call site. A disabled `(call_site, rule)` row lives in `optimizer_disabled` with the reason; it is re-enabled after a 24-hour cooldown so that transient issues don't poison the cache permanently.

Shadow mode:

- A shadow-sampled call runs **both** the rewritten and the unrewritten plan.
- The user receives the rewritten result (so savings are realized); the unrewritten result is discarded after divergence measurement.
- Divergence = `1 - jaccard(output_tokens(rewritten), output_tokens(unrewritten))` for text outputs; for tool-calls, `1.0` if the tool name differs, else token-level Jaccard on arguments.
- Sampling is per-call (Bernoulli(shadow_rate)), not per-call-site. A high-volume call site is sampled more often in absolute terms, which is what we want for the confidence interval.

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
CREATE TABLE call_site_profile (
    call_site_id          TEXT PRIMARY KEY NOT NULL,
    n_observations        INTEGER NOT NULL,
    input_tokens_mean     REAL NOT NULL,
    input_tokens_var      REAL NOT NULL,
    output_tokens_mean    REAL NOT NULL,
    output_tokens_var     REAL NOT NULL,
    latency_ms_mean       REAL NOT NULL,
    latency_ms_var        REAL NOT NULL,
    cost_usd_mean         REAL NOT NULL,
    cost_usd_var          REAL NOT NULL,
    output_token_p95      REAL NOT NULL,
    output_is_structured  REAL NOT NULL,
    output_is_short       REAL NOT NULL,
    updated_at            INTEGER NOT NULL
) STRICT;

CREATE TABLE rule_divergence (
    call_site_id          TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    n_samples             INTEGER NOT NULL,
    divergence_mean       REAL NOT NULL,
    divergence_var        REAL NOT NULL,
    PRIMARY KEY (call_site_id, rule)
) STRICT, WITHOUT ROWID;

CREATE TABLE optimizer_disabled (
    call_site_id          TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    reason                TEXT NOT NULL,
    disabled_at           INTEGER NOT NULL,
    reenable_at           INTEGER NOT NULL,
    PRIMARY KEY (call_site_id, rule)
) STRICT, WITHOUT ROWID;

-- optimizer_audit.db
CREATE TABLE plan_audit (
    audit_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_us                 INTEGER NOT NULL,
    call_site_id          TEXT NOT NULL,
    span_id               BLOB(8) NOT NULL,
    plan_kind             TEXT NOT NULL,   -- "pass_through" | "cached" | "rewritten" | "parallel"
    rule                  TEXT,            -- null for pass_through
    projected_savings_usd REAL,
    measured_savings_usd  REAL,
    overhead_us           INTEGER NOT NULL,
    shadow_sampled        INTEGER NOT NULL DEFAULT 0,
    shadow_divergence     REAL
) STRICT;

CREATE INDEX idx_audit_call_site ON plan_audit(call_site_id, ts_us DESC);
CREATE INDEX idx_audit_ts ON plan_audit(ts_us);
```

`plan_audit` is a ring: when it exceeds 10,000 rows, `DELETE FROM plan_audit WHERE audit_id < ?` prunes the oldest ones in the background writer thread.

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

- **Plan evaluation is read-only** for shared state: profile and cache are accessed through `Arc<dyn ...>` read handles. Multiple threads can plan concurrently without locks.
- **Cost model updates are serialized** on a single writer thread (the same thread that serves memoization inserts and span writes). `observe` enqueues a `CostModelUpdate` message.
- **Audit writes are serialized** on the same writer thread.
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

### Performance targets

Benchmarks live in `bench/optimizer_bench.py`:

| Metric | Target | Measurement |
|---|---|---|
| p50 plan overhead (hot call) | < 0.5 ms | 5-rule optimizer, 100k-entry cache |
| p99 plan overhead (hot call) | < 1.2 ms | Same |
| p50 plan overhead (cold call) | < 100 μs | Profile lookup + early return |
| p99 plan overhead (cold call) | < 300 μs | Same |
| Shadow-mode sample rate | 2% ± 0.3% | Bernoulli(0.02) over 10k calls |
| Cost model write throughput | > 1000 observations/s | Single writer thread |

### Savings / accuracy (reference agents)

| Agent | Baseline cost | Target savings | Accuracy baseline | Accuracy floor |
|---|---|---|---|---|
| `bench/agents/swebench_planner.py` | $14.82 / 50 tasks | ≥ 30% | 82.0% resolve rate | ≥ 80.0% |
| `bench/agents/gaia_router.py` | $8.44 / 80 questions | ≥ 35% | 71.2% correct | ≥ 69.0% |
| `bench/agents/rag_summarizer.py` | $4.21 / 200 docs | ≥ 40% | 0.84 ROUGE-L | ≥ 0.82 |
| `bench/agents/multiagent_research.py` | $22.18 / 30 tasks | ≥ 25% | 7.4/10 quality | ≥ 7.1/10 |

Accuracy floor is the hard fail gate — no release passes if a reference agent drops below it.

### Per-rule ablations

`bench/optimizer_ablation.py` runs each reference agent with:

1. All rules enabled (baseline savings number).
2. Each rule disabled one at a time.
3. Only one rule enabled at a time.

This produces a (rule × agent) contribution matrix that informs rule-specific budget tuning and identifies rules that hurt on specific workloads.

### Shadow-mode divergence bounds

For each `(rule, agent)` pair, the shadow-mode divergence over 1,000 invocations must stay within the rule's configured budget:

| Rule | Default budget | Measured on reference agents |
|---|---|---|
| `CacheHit` | 1.0% | (filled in by `bench/optimizer_ablation.py`) |
| `ContextCompress` | 2.0% | |
| `ParallelBranch` | 0.0% | must be exactly zero |
| `ModelDowngrade` | 3.0% | |
| `StateDrop` | 1.0% | |

### Acceptance criteria (ship gate)

The optimizer crate reaches `status: active` when:

- All correctness tests pass.
- All four reference agents hit their savings target without dropping below their accuracy floor.
- p99 plan overhead is within 1.2 ms on the reference hardware.
- Shadow-mode divergence stays within budget on every `(rule, agent)` pair.
- Fail-open paths are exercised by fault-injection tests (`tests/fail_open.rs`).

---

## Design Decisions

### Hot-path JIT, not eager optimization

Cold calls are pass-through; optimization kicks in after `hot_threshold` observations. The profiler already produces the empirical data the cost model needs, and first-call latency stays clean (no optimizer overhead before we have profile data to act on). **Rejected: eager rewriting on every call.** Pays optimizer cost on the first invocation when the cost model has zero confidence — the rewrite is a guess, not a decision. **Rejected: waste-triggered only.** Only fires on call sites the profiler's 5 detectors flag; misses wins outside those detector categories.

### First-match wins, no rule composition

Rules are ranked by projected savings; the first proposal to pass its safety check becomes the plan, and remaining rules are skipped for that call. Composition (e.g., apply `ContextCompress` then `ModelDowngrade`) multiplies the accuracy blast radius and makes divergence attribution ambiguous. **Rejected: greedy composition with cumulative budget.** A compressed-and-downgraded call that produces a bad answer is hard to attribute; debugging the accuracy regression requires running counterfactuals per rule. Start with first-match and revisit after savings data says otherwise.

### Empirical cost model, not learned

Per-call-site rolling mean/variance via Welford's algorithm. No neural network, no bandit, no gradient-anything. The cost model summarizes observations; it doesn't extrapolate beyond them. **Rejected: per-call-site bandit (Thompson sampling over rule choice).** Adds per-rule arm state and assumes rule choice is a contextual-bandit problem, which it isn't — `applies(&call, &profile)` is a hard predicate, not a stochastic one. **Rejected: learned cost predictor.** Non-trivial training pipeline, hard to debug regressions, buys accuracy we don't need when the decisions are "cache vs not" and "downgrade vs not."

### Per-rule accuracy budget with shadow mode

Each rule has its own divergence budget (e.g., `ModelDowngrade = 0.03`); the optimizer tracks rolling divergence per `(call_site, rule)` and auto-disables on breach. Shadow-mode sampling at 2% provides the ground truth. **Rejected: global accuracy budget.** A single budget muddles attribution — you can't tell which rule burned the budget. **Rejected: pre-flight validator (cheap LLM re-checks the output).** Doubles latency on every call; contradicts the ≤ 1 ms overhead goal.

### `CacheHit` as a rewrite rule, not a bypass

Memoization is first-class as a rewrite rule so the optimizer's ranking, budget, and audit trail apply uniformly. This also lets `ModelDowngrade` propose cost wins on calls where `CacheHit` didn't fire — projected savings are compared apples-to-apples. **Rejected: memoization short-circuits before the optimizer.** Two side-effect paths, two audit trails, two budget systems. One plan pipeline is easier to reason about and benchmark.

### Python drives plan execution; Rust plans but never dispatches

The Rust optimizer computes `Plan`s and emits them as JSON. Python's executor is responsible for the actual LLM calls (cached return, rewritten dispatch, parallel fan-out). This keeps Rust free of vendor SDKs, HTTP clients, and credential handling. **Rejected: Rust executes directly.** Requires linking a Rust HTTP client, handling provider SDKs' auth conventions, and re-implementing streaming across every provider — a full second SDK surface.

### Provenance tagging depends on framework adapters

`ParallelBranch` and `StateDrop` need `DepSource` annotations that can't be reliably inferred from raw `messages`. Framework adapters supply them; framework-free users get the other three rules (`CacheHit`, `ContextCompress`, `ModelDowngrade`). This trades universal coverage for correctness — a `ParallelBranch` that fires on non-disjoint deps is a race condition, not a savings. **Rejected: heuristic provenance from message content overlap.** False positives on shared boilerplate (system prompts); false negatives on renamed fields. Requires tuning per-framework anyway.

### Shadow-mode at 2%

2% sampling gives usable divergence confidence intervals within a single session for high-traffic call sites, while capping the shadow-execution cost overhead at 2% of total spend. **Rejected: 100% shadow during calibration.** Doubles spend during the window; prevents the savings from being realized. **Rejected: never shadow.** Without ground-truth divergence data, the accuracy budget can't be enforced.

---

## Open Questions

> **OPEN (avery, 2026-05-15):** Decide how to handle streaming LLM responses under the optimizer. `CacheHit` is trivial (replay the cached stream chunk-by-chunk). `ModelDowngrade` on a streaming call requires the downgrade target also supports streaming at the same chunking granularity. `ParallelBranch` interleaves streams from parallel calls, which the user's UI may or may not expect. Initial implementation disables the optimizer for streaming calls (`extra_headers={"agentc-optimize": "false"}` equivalent, applied automatically when `stream=True`); revisit when we have a streaming reference agent.

> **OPEN (avery, 2026-05-15):** Resolve how `ContextCompress` interacts with vendor-side prompt caching (OpenAI's prefix caching, Anthropic's `cache_control`). Dropping tokens from a cached prefix invalidates the vendor cache and can cost more than it saves. Provisional behavior: `ContextCompress` is disabled when the `messages` list contains any `cache_control` marker; this is conservative and may leak savings. Needs a proper cost model term for "cached prefix length gained/lost per compression."

> **OPEN (avery, 2026-05-15):** Decide the eviction policy for `call_site_profile` rows that haven't been touched in ≥ 7 days. Stale profiles can steer rewrites based on outdated behavior (e.g., a prompt template changed but the call_site_id didn't). Options: (1) time-decay the `n_observations` counter, (2) prune stale rows outright, (3) hash the prompt structure into `call_site_id` so template changes create a new site.
