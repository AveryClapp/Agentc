# Agentc — How It Works and How We Evaluate It

A plain-language companion to the code. This explains **what the rewrite rules do, how they're
implemented, and how the benchmarks are actually run** — the parts that are easy to lose track of
because they live across the Rust crates, the Python SDK, and the bench harness.

> This doc deliberately avoids quoting result numbers, which go stale. For canonical, warmup-corrected
> results see `bench/paper_results/DATA_MANIFEST.txt`; for the evidence ledger see
> `paper-intelligence/results-experiments-and-repro.md`; for the manuscript see `main.tex`.

---

## The core idea

Agentc is a **just-in-time optimizer for LLM agent workloads** — think of it as a compiler pass that
sits between an agent's code and the model provider, intercepts each call, and rewrites it to cost
less, without the application changing a line.

The decisions are made by **deterministic code, not by an LLM.** No rule reads the prompt and "decides"
with a language model. Each rule fires on a numeric threshold, a lookup table, or a measured statistic.
The **only** model anywhere in the decision path is a small **embedding model** (text → a vector of
numbers) used purely for similarity — never to generate text, never to judge.

Why no LLM in the loop: **cost** (an LLM on every call would cost more than it saves), **speed** (a
rule decision takes microseconds vs. an LLM's hundreds of milliseconds), and **determinism** (same
input → same decision, so it's testable). The bet is that *cheap-and-blind, backed by a safety net*
beats *smart-but-expensive*.

---

## The rewrite rules

Nine rules ship. They group by **what cost they attack** ("cost driver"). Three are empirically
validated in the paper; the other six are implemented and unit-tested but not yet backed by a
standalone experiment (see "What's validated vs. shipped" below).

| Rule | What it does | Saves | Status |
|---|---|---|---|
| **ContextCompress** | Drops context messages scored as unimportant | input tokens | validated |
| **ModelDowngrade** | Routes a call to a cheaper model | model price | validated |
| **StateDrop** | Drops stale scratchpad state nothing reads | input tokens | validated |
| **CacheHit** | Serves a cached answer instead of calling the model | the whole call | shipped, unvalidated |
| **PromptDedup** | Removes near-duplicate messages | input tokens | shipped, unvalidated |
| **StructuredTruncation** | Drops unused JSON fields from tool outputs | input tokens | shipped, unvalidated |
| **OutputBudget** | Caps how many tokens the model may generate | output tokens | shipped, unvalidated |
| **DeadOutputTruncation** | Caps generation on reasoning branches nothing reads | output tokens | shipped, unvalidated |
| **ParallelBranch** | Runs independent calls concurrently | latency (not cost) | shipped, unvalidated |

Each rule implements a common `RewriteRule` trait: `applies()` is a cheap gate, `propose()` builds
the rewrite plus a `safety_check` closure re-verified at commit time, and `accuracy_budget()` sets how
much output drift the safety guard tolerates before disabling the rule. Source:
`crates/agentc-optimizer/src/rules/`.

### The input-token rules (prune the prompt)

- **ContextCompress** (`context_compress.rs`) — the interceptor attaches a per-message importance score
  ("attention score") on `parameters.extra.attention_scores`. The rule drops messages whose score is
  essentially zero, while always keeping the user's input, follow-on tokens, and at least one message
  per role. Pure arithmetic over pre-supplied scores; no model reads the text.
- **StateDrop** (`state_drop.rs`) — each message is tagged with what produced it (`message_deps`). A
  message tagged as scratchpad *state* is dropped only if its key appears in **none** of the recent
  spans' reads (`window_state_reads`). Guardrails: never drops the system prompt, must retain ≥50% of
  messages, and refuses if a dropped key also tags a system message. It's dependency bookkeeping —
  "delete state nothing downstream reads."
- **PromptDedup** (`prompt_dedup.rs`) — finds near-duplicate messages (token overlap ≥ 0.92), keeps the
  single most distinctive copy (highest IDF), never drops user input, keeps ≥2 messages. Compiler
  analog: common-subexpression elimination.
- **StructuredTruncation** (`structured_truncation.rs`) — when a tool returns a JSON object, drops the
  top-level keys the next ("consumer") message doesn't reference. Compiler analog: projection pushdown.
  At least one key must survive; system messages are never touched.

### The model-price rule (route cheaper)

- **ModelDowngrade** (`model_downgrade.rs`) — a static route table says which cheaper model substitutes
  for which expensive one. It only fires after a call site is "hot," passes a probation sampling gate,
  and — critically — only if the **measured output divergence** between the two models stays inside the
  accuracy budget. Decided by measurement, not by reasoning about the prompt.

### The output-token rules (cap generation)

- **OutputBudget** (`output_budget.rs`) — caps `max_output_tokens` at `ceil(p99 × 1.2)` of the call
  site's observed output lengths: 99% of real answers fit, with headroom. Won't tighten a cap already
  below the p99, and floors the cap at 64 tokens.
- **DeadOutputTruncation** (`dead_output_truncation.rs`) — when the trace optimizer flags a reasoning
  branch as "dead" (its output is never read downstream), caps that generation at 150 tokens. Compiler
  analog: dead-store elimination.

### The call-elimination rule (don't call at all)

- **CacheHit** (`cache_hit.rs`) — serves a previously computed answer. An **exact** cache hit always
  qualifies; a **near** hit qualifies only when embedding cosine similarity ≥ 0.95 (stricter than the
  opt-in memoize default). This is the one rule where the embedding model matters: text → vector →
  nearest-neighbor lookup → similarity threshold.

### The structural rule (reorder)

- **ParallelBranch** (`parallel_branch.rs`) — dispatches two consecutive calls concurrently **only**
  when their input dependencies are provably disjoint. Its accuracy budget is **0.0** — outputs must be
  identical, because it's pure reordering. It saves wall-clock latency, not tokens.

---

## The one model in the loop

The **embedding model** (`embed_text_bytes`, 256-dim vectors) turns text into numbers so code can
measure similarity. It appears in exactly two places: **CacheHit** (is this prompt close enough to a
cached one?) and the safety guard's optional **embedding divergence mode** (did the rewrite change the
meaning of the output?). It never generates text and never makes a decision — it emits numbers that
deterministic code thresholds.

---

## How a call flows

1. A workload runs under `agentc record`, which injects a startup hook that calls `agentc.init()` and
   monkey-patches the OpenAI and Anthropic SDKs (`python/agentc/_lifecycle.py`).
2. Every patched `create()` funnels through `intercept()` (`python/agentc/_intercept.py`): build a
   `Call` → ask the Rust optimizer for a `Plan` → dispatch → record the outcome (tokens, latency, cost)
   back into the cost model for future decisions.
3. The optimizer (`crates/agentc-optimizer`) ranks each firing rule's proposal by projected savings and
   returns the plan: pass-through, a rewritten call, a cached value, or a parallel dispatch.
4. Everything is **fail-open**: any panic, deserialization error, or misconfiguration returns
   pass-through — "just run the original call." Optimization is never allowed to break the app.

A call site is left untouched until it's been seen a few times (the "hot threshold"), so the optimizer
**observes before it acts**.

---

## The safety guard (shadow mode)

The guard is what lets the rules decide "blind" without silently degrading answers.

- **How it samples:** on roughly 2% of eligible calls, the interceptor also runs the **original,
  un-rewritten** call — a second, real, billed model call — and compares the two outputs.
- **How it scores drift:** output "divergence" is measured one of three ways (set by
  `AGENTC_SHADOW_DIVERGENCE_MODE`): **lexical** (1 − word overlap), **normalized** (ignores formatting
  and filler), or **embedding** (cosine distance on vectors).
- **How it disables a rule:** state lives in a per-process `Budget` (`crates/agentc-optimizer/src/budget.rs`).
  If a rule's divergence exceeds its budget on **5 consecutive** samples, the guard disables that rule
  at that call site for 24 hours, and the planner skips it. A within-budget sample resets the streak.

**What's demonstrated:** a guard sweep deliberately breaks StateDrop; with the guard off, accuracy
craters; with it on, the guard catches the rule, disables it, and cuts the damage sharply (see the
`bench/paper_results/gsweep_claude_*` CSVs and `bench/repro/guard_frontier.sh`).

**Honest gaps (as of this writing):** the guard's **false-positive rate** — how often it wrongly
disables a *good* rule — is unmeasured (the `bench/audit_guard_fp.py` script is a non-functional stub,
and at least one benign rule was auto-disabled in the committed sweep data). And rolling divergence
state lives only in memory, so it's lost on restart (only the resulting disable rows persist). These
are tracked as follow-up work.

---

## How we evaluate

**The harness.** The real accuracy/cost path is `agentc record` → patched SDK → a SQLite `traces.db`.
Cost, tokens, and latency are read from `traces.db`; **accuracy is scored from `PASS/FAIL <task_id>`
lines the agent prints to stdout**. (Note: `bench/harness.py`, despite the name, is a *mock*
`time.sleep` pipeline used only for overhead measurement — not the accuracy path.)

**The ablation matrix.** For each workload the harness runs, per rule: `all-on` (reference),
`<rule>-off` (everything except this rule — its marginal contribution), and `<rule>-only` (just this
rule — its standalone contribution). Rules are disabled by shelling out to
`agentc optimize disable --rule <name> --call-site '*'` into a **per-config isolated** cost-model DB,
so configs don't leak into each other. The optimizer itself is toggled by the `AGENTC_OPTIMIZE` env var.

**Pairing and significance.** Baseline (optimizer off) and optimized (optimizer on) run the **same
agent on the same tasks**, so results pair per task. Significance uses **McNemar's exact test** on the
per-task right/wrong flips (does turning a rule on flip more answers the wrong way than chance?), with
bootstrap confidence intervals on headline accuracy claims.

**Warmup correction.** Because the optimizer learns costs and only fires once warm, each config runs
~30 warmup tasks to populate the cost model, then resets the traces/audit but **keeps the cost model
warm**, then runs the N measurement tasks. This measures steady-state behavior, not a cold start. It's
also why some headline numbers dropped after correction — the pre-correction figures were inflated by
cold-start effects.

**Environment.** OpenAI Python SDK; default model `gpt-4o-mini`; `gaia_router` uses `gpt-4o` as the
base so ModelDowngrade has a price gap to exploit; one generalization run uses Together's LLaMA-3.3-70B.

**Known methodology caveats (be honest about these):**
- No random seed is set, and the two headline agents (`long_context_qa`, `gaia_router`) run at
  provider-default temperature (~1.0) while most others pin `temperature=0` — so the flagship numbers
  are the least reproducible in the suite.
- Sample sizes are small (n≈30–300), mostly single-model, single-seed.
- Several fixtures are purpose-built; headline ContextCompress savings are much larger on the
  long-prompt fixture than on real HotpotQA, where the precondition rarely fires (a correct-abstention
  story, not a failure — but worth stating plainly).
- The LLaMA/Together model isn't in the frozen `data/pricing.json`, so those runs logged $0 cost.

---

## What's validated vs. shipped

Nine rules ship; **three** are backed by a standalone experiment: **ContextCompress**,
**ModelDowngrade**, **StateDrop**. The other six are implemented and unit-tested but not yet
independently validated — notably, the entire **output-token** savings axis (OutputBudget,
DeadOutputTruncation) has no dedicated experiment, and CacheHit appears only as a fire-rate statistic.

The paper's stance is deliberately narrow: **claim the three, note the six as implemented-but-unevaluated.**
Keep the code, scope the claims. The runtime's real contribution is orthogonal to rule count — the
JIT interception layer, the observe-before-acting gate, the composition planner, the safety guard, and
sub-millisecond overhead all stand regardless of how many rules are turned on.

---

## Pointers

- **Canonical results:** `bench/paper_results/DATA_MANIFEST.txt`
- **Evidence ledger:** `paper-intelligence/results-experiments-and-repro.md`
- **Rule implementations:** `crates/agentc-optimizer/src/rules/`
- **Safety guard:** `crates/agentc-optimizer/src/budget.rs`, `python/agentc/_patches/_optimizer_glue.py`
- **Interception:** `python/agentc/_intercept.py`, `python/agentc/_lifecycle.py`
- **Specs (authoritative technical detail):** `specs/`
