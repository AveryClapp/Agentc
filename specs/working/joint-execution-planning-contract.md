---
title: Joint Execution Planning Contract
status: active
last-updated: 2026-09-03
---

# Joint Execution Planning Contract

This document freezes the MLSys thesis and the runtime contract that implements
it. It is the decision record for `bd-323l.2`; the canonical component details
remain in `specs/optimizer.md`, and the prospective evidence rules remain in
`bench/repro/mlsys-2027-evaluation-protocol.md`.

## Thesis

Agentc is an application-side execution planner for repeated LLM call sites. It
enumerates combinations of model target and semantic request rewrites, learns
their observed cost, latency, and output-divergence behavior, and chooses the
least expensive or fastest admissible combination. It operates above opaque
provider APIs, requires no workload-source rewrite, and returns the original
request when evidence or safety constraints are insufficient.

The contribution is **joint constrained plan selection**, not model routing or
any individual rewrite. The planner must measure a transformed request on its
actual target model because model and rewrite effects need not be additive.

### Frozen title

> **Agentc: Constrained Joint Routing and Semantic Rewriting for API-Based LLM Agents**

### Frozen abstract spine

> API-based LLM agents repeatedly issue calls whose cheapest adequate execution
> depends on both the model and the request presented to that model. Existing
> routers choose a model for an otherwise fixed request, while point optimizers
> compress, cache, or truncate requests for a fixed model. Treating those
> decisions independently misses interactions: a smaller model may tolerate the
> original context but fail after compression, while a transformed request may
> make that same model adequate. We present Agentc, an application-side runtime
> that intercepts opaque provider calls and jointly selects a target model and a
> compatible set of semantic rewrites at each repeated call site. Agentc builds
> bounded, versioned profiles for complete execution plans, admits a plan only
> when its observed cost or latency benefit clears explicit evidence and
> divergence constraints, and otherwise executes the original request. Sampled
> counterfactuals detect drift and drive a persistent fallback controller. We
> evaluate Agentc against fixed-model, routing-only, rewrite-only, and full-system
> baselines on unchanged agent workloads, reporting task quality, billed cost,
> latency, exploration expense, abstention, and damage before fallback.

The submitted abstract appends only results produced by the frozen confirmatory
protocol. It does not add a firstness claim, call output divergence “quality,”
or generalize beyond measured adapters and workloads.

## Contributions

1. **A joint execution-plan abstraction.** One canonical identity covers the
   target provider/model, ordered semantic rewrites and their parameters, cache
   behavior, output budget, and validation policy. Profiles and guards attach to
   this complete identity rather than to a rule or model in isolation.
2. **Online per-call-site capability frontiers.** Agentc retains a bounded
   history for every observed `(call-site version, execution plan)` and selects
   from the empirical cost--latency--divergence frontier without provider logits,
   model weights, or workload labels.
3. **Interaction-aware, constrained selection.** A plan competes only after its
   complete combination has evidence. The selector minimizes the configured
   objective subject to request compatibility, evidence, divergence, freshness,
   and operational budgets; it does not infer joint safety by adding solo-rule
   estimates.
4. **Persistent adaptation and explicit fallback.** Exploration and ongoing
   counterfactual checks are bounded, state survives restart, stale model or
   prompt versions become cold, and failure returns the exact original request.
5. **A selection-valid evaluation.** Fixed-strong, fixed-cheap, routing-only,
   rewrite-only, independently composed, and joint-planner arms use the same
   tasks, model pool, calibration budget, and provider accounting.

## Deployment and threat model

### In scope

- Python applications reached through the demonstrated OpenAI, Anthropic, and
  LiteLLM non-streaming adapters.
- Hosted or self-hosted models invoked through opaque request/response APIs.
- Repeated semantic call sites for which a bounded online profile can form.
- Text calls and native structured calls for which a candidate explicitly
  preserves all fields it cannot represent.
- Provider errors, transient outages, process restart, prompt changes, model
  version changes, and ordinary workload drift.

### Outside the guarantee

- Adversarial providers, compromised SDKs, or malicious model output.
- Semantic equivalence by construction. Agentc observes a divergence proxy and,
  where the workload exposes it, task quality; neither proves equivalence.
- Irreversible external actions without an application-provided transactional or
  validation boundary.
- Unobserved calls, unsupported streaming paths, cross-language/subprocess calls,
  or framework-neutral interception until the compatibility study measures them.
- A guarantee that an eligible cheaper plan exists. Abstention is a valid and
  reportable outcome.

The original request is the reference plan. Agentc never weakens provider-side
authentication, content policy, tool schema, or application opt-outs. A selected
plan that fails to dispatch retries the exact original request once under the
upstream retry policy and records the fallback.

## Execution-plan contract

### Identity

`ExecutionPlanId` is the SHA-256 digest of a canonical serialization containing:

```text
schema_version
provider_protocol
requested_model_id
target_model_id
ordered_rewrites[] = {stable_name, implementation_version, parameters}
cache_policy
output_budget
validation_policy
```

Order remains significant. Two plans with the same rules in a different order
have different identities unless the candidate generator proves that the
operations commute and emits one canonical order. Price is metadata, not part of
identity; a price-table version belongs to the observation and analysis record.

`CallSiteVersion` combines the stable semantic call-site ID with a hash of the
request template shape, provider protocol, tool schema, and relevant application
configuration. Raw user content and credentials never enter the durable key. A
changed version starts cold rather than inheriting evidence from an incompatible
request.

### Candidate

Every candidate contains:

```rust
pub struct CandidatePlan {
    pub id: ExecutionPlanId,
    pub call: Call,
    pub target: ModelTarget,
    pub rewrites: Vec<RewriteApplication>,
    pub validation: ValidationPolicy,
    pub estimate: Option<PlanEstimate>,
}
```

The candidate generator always includes the immutable reference plan. Rewrite
rules propose transformations; the model catalog proposes compatible targets;
the generator produces only combinations whose request fields can be represented
losslessly and whose declared preconditions hold. It bounds the search with the
configured target allowlist and maximum plan depth. It never sends credentials or
provider traffic from Rust.

### Profile

The decision key is `(CallSiteVersion, ExecutionPlanId)`. Each key has two
independent newest-50 default windows. The execution window records input and
output tokens, billed cost, latency, dispatch/fallback state, output shape,
runtime version, and observation time. The paired window records complete-plan
counterfactual divergence, its correlated execution sequence, runtime version,
and observation time. Sparse shadow sampling therefore cannot be aged out by
ordinary unpaired executions before it reaches the admission floor. Lifetime
execution and paired counts exist only for reporting. Every decision statistic
derives from its exact retained window, and both windows survive restart.

Solo observations do not populate a joint profile. A routing-only observation of
model `M` and a rewrite-only observation of rule `R` provide no direct evidence
for plan `(M, R)`.

### Admission

A non-reference plan is admissible only when all conditions hold:

1. The adapter declares the target and every mutation representable for the
   native request shape.
2. The exact plan profile has at least 20 paired counterfactual observations in
   its retained window.
3. Its one-sided conformal upper 95th-percentile divergence is at or below the
   threshold selected from `{0.05, 0.10, 0.15, 0.20, 0.30, 0.50}` on the frozen
   calibration split for that provider/model/rule family.
4. The profile matches the current call-site, prompt-shape, provider-protocol,
   target-model, and implementation versions and has been observed within the
   24-hour freshness horizon.
5. Expected gross benefit is positive and expected net benefit remains positive
   after counterfactual inference, retry, and optimizer overhead.
6. No plan-level disable, hard request invariant, operator deny rule, exploration
   cap, or divergence-exposure budget is active.

The selector minimizes expected billed USD by default. In latency mode it
minimizes expected end-to-end request latency. It uses stable tie-breaking:
larger evidence count, then smaller observed divergence, then fewer mutations,
then lexicographic `ExecutionPlanId`. Missing, stale, or non-finite estimates are
inadmissible. If no non-reference plan qualifies, it selects the reference plan.

### Exploration and drift

An under-observed candidate acquires evidence only through explicit exploration.
During initial calibration, Agentc returns the reference result and may execute
one candidate as a bounded background counterfactual; this spends money but does
not expose the candidate result to the application. The per-call-site exploration
cap defaults to 20 candidate calls per 24 hours and one concurrent counterfactual.

After admission, the selected result is returned and the reference is sampled at
the configured rate. A plan-level controller consumes complete-plan divergence;
it does not charge the same composed result independently to every constituent
rule. Provider/model or prompt-shape version changes invalidate admission
immediately. The count-bounded windows track recent behavior; the separate
24-hour freshness horizon rejects a profile when no current evidence has arrived.

The runtime calls its online quantity **divergence exposure**, not task damage:

```text
E_t = sum(sampled i <= t) max(0, divergence_i - threshold)
```

The default exposure budget is `1.0` per `(CallSiteVersion, ExecutionPlanId)` in
24 hours. Crossing it disables the plan durably. The evaluation separately
measures task-equivalent damage where labels exist using the protocol's
`D_max = 5.0`; Agentc does not claim it can observe task damage in an unlabeled
production call.

## Frozen system thresholds

| Setting | Value |
|---|---:|
| Hot call-site observations | 3 |
| Exact plan execution-outcome window | 50 |
| Exact plan paired-divergence window | 50 |
| Minimum paired plan evidence | 20 |
| Profile freshness horizon | 24 h |
| Default objective | billed USD |
| Maximum plan depth | 3 semantic rewrites plus one model target |
| Production counterfactual rate | 2% |
| Calibration divergence grid | 0.05, 0.10, 0.15, 0.20, 0.30, 0.50 |
| Exploration cap | 20 candidate calls/site/24 h |
| Concurrent counterfactuals/site | 1 |
| Divergence-exposure budget | 1.0/site/plan/24 h |
| Plan-time kill switch | 5 ms |
| Durable disable cooldown | 24 h, followed by cold re-admission |

## Frozen analysis plan

The task splits, model versions, non-inferiority margins, retry rules, and
statistical tests in the MLSys protocol remain fixed. The primary policy arms
become:

1. `trace_only_fixed_strong` -- reference request on the strong model;
2. `fixed_cheap` -- reference request on the cheap model;
3. `routing_only` -- model selection with all semantic rewrites disabled;
4. `rewrite_only_fixed_strong` -- Agentc rewrites with model fixed strong;
5. `best_static_joint` -- one target-plus-rewrite configuration selected on
   calibration data and held fixed for the entire test split;
6. `route_then_rewrite` -- a separately trained router runs on the original
   request, followed by an independently calibrated rewrite policy;
7. `rewrite_then_route` -- the rewrite policy runs first and the router sees the
   transformed request;
8. `current_greedy` -- the existing projected-savings and cost-driver
   CompositionPlanner, including its static `ModelDowngrade` proposal;
9. `joint_guarded` -- complete-plan profiling and constrained joint selection.

All arms receive identical task membership, model pool, request budget, and
calibration opportunity. The confirmatory comparison is `joint_guarded` against
the best admissible result among arms 1--8, not only against the unmodified
agent. An efficiency win counts only when the workload-specific 95% quality
interval clears its frozen non-inferiority margin:

- tau2 mean reward: `-0.03`;
- SWE-bench Verified resolve rate: `-0.02`;
- OSWorld normalized score: `-0.02`.

The main interaction claim requires a selection-valid 95% lower bound above zero
for `joint_guarded` versus the best arm among `routing_only`,
`rewrite_only_fixed_strong`, `best_static_joint`, both sequential orders, and
`current_greedy` in two workload/model cells without crossing the quality
margin. At least one such cell must exhibit a held-out model/rewrite rank reversal
or another predeclared material departure from additive isolated effects. Report
every attempted cell, including zero-opportunity and abstention-dominant cells.

## Novelty boundary

The paper does not claim novelty for routing, cascades, compression, caching,
output budgeting, JIT compilation, or application-side interception alone. It
must distinguish the complete mechanism from AgentOpt and routing systems, and
the explicit plan-level interaction model from ApproxMLIR, Cognify, Murakkab,
Agentix, Parrot, and Agent JIT Compilation. Capability differences count only
when demonstrated at the stated deployment boundary; otherwise they remain
source-backed positioning, not empirical wins.

## Implementation sequence

Beads under `bd-323l.6` are the authoritative implementation graph:

- `bd-323l.6.1`: execution-plan identity and constrained selector;
- `bd-323l.6.2`: per-call-site, per-plan persistent profiles;
- `bd-323l.6.3`: bounded exploration and counterfactual feedback;
- `bd-323l.6.4`: model catalog and provider-safe routed dispatch;
- `bd-323l.6.5`: baseline and ablation harness;
- `bd-323l.6.6`: objectives, risk configuration, and diagnostics;
- `bd-323l.6.7`: repair composed-plan divergence attribution.

The implementation replaces the current static `ModelDowngrade` decision path;
it does not add an independent router beside it.
