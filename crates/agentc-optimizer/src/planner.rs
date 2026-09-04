//! Rule proposal and bounded candidate generation for intercepted LLM calls.
//!
//! [`Optimizer::candidate_plans`] is the production joint-planning front end:
//! it materializes compatible semantic rewrite sets, crosses them with the
//! versioned model catalog, and always returns the immutable reference first.
//! Exact profile lookup and constrained selection live at the vendor-free FFI
//! boundary, where observation identities are also constructed.
//!
//! [`Optimizer::plan`] retains the projected-savings baseline contract:
//! 1. If the optimizer is disabled, return [`Plan::PassThrough`].
//! 2. Look up the `CallSiteProfile`. If the retained window has fewer than
//!    `hot_threshold` observations,
//!    return [`Plan::PassThrough`].
//! 3. Ask every enabled rule if it applies. Collect proposals.
//! 4. Sort by `projected_savings_usd` descending.
//! 5. Run each proposal's safety check in order. First pass wins.
//! 6. If plan evaluation's wall clock exceeds `max_overhead_ms`, the
//!    overhead kill switch returns [`Plan::PassThrough`].
//!
//! The default live path calls [`Optimizer::candidate_plans`] and selects from
//! exact complete-plan evidence. [`Optimizer::plan`] remains directly callable
//! by evaluation code for the historical composition and first-match baselines.

use std::sync::Arc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::budget::Budget;
use crate::config::OptimizerConfig;
use crate::cost_model::{CallSiteProfile, CostModel};
use crate::dag::Call;
use crate::model_catalog::{ModelCatalog, ModelTarget, RequestRequirements};

/// Frozen bound from the joint execution-planning contract. Model routing is
/// an additional target choice and does not consume semantic rewrite depth.
pub const MAX_JOINT_REWRITE_DEPTH: usize = 3;

/// Per-rule attribution inside a `Plan::Composed`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuleApplication {
    pub rule: String,
    pub projected_savings_usd: f32,
    pub cost_driver: CostDriver,
}

/// The Optimizer's output. Python's executor dispatches each variant:
/// `Cached` returns directly, `Rewritten` dispatches the mutated call,
/// `Parallel` issues `asyncio.gather`, `Composed` dispatches the multi-rule
/// mutated call, `PassThrough` runs the original.
///
/// `serde`-tagged so the FFI boundary is readable (no positional indices).
///
/// The variant size is intentionally uneven: `PassThrough` is the hot
/// outcome, and boxing `call`/`calls` just to shrink the rarer rewrite
/// variants would add an allocation on every rule fire.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[allow(clippy::large_enum_variant)]
pub enum Plan {
    PassThrough,
    Cached {
        /// Opaque cached response payload. The Python executor knows how
        /// to decode — we carry it through unchanged.
        value: serde_json::Value,
    },
    Rewritten {
        rule: String,
        call: Call,
        projected_savings_usd: f32,
    },
    Parallel {
        rule: String,
        calls: Vec<Call>,
        projected_savings_usd: f32,
    },
    /// V2: two or more orthogonal rules applied in one pass.
    Composed {
        rules: Vec<RuleApplication>,
        call: Call,
        net_savings_usd: f32,
    },
}

impl Plan {
    pub fn is_pass_through(&self) -> bool {
        matches!(self, Plan::PassThrough)
    }

    /// The first (highest-savings) rule that contributed to this plan.
    pub fn rule(&self) -> Option<&str> {
        match self {
            Plan::PassThrough | Plan::Cached { .. } => None,
            Plan::Rewritten { rule, .. } | Plan::Parallel { rule, .. } => Some(rule.as_str()),
            Plan::Composed { rules, .. } => rules.first().map(|r| r.rule.as_str()),
        }
    }

    /// Stable rule names represented by this executable plan.
    pub fn rule_names(&self) -> Vec<String> {
        match self {
            Plan::Rewritten { rule, .. } | Plan::Parallel { rule, .. } => vec![rule.clone()],
            Plan::Composed { rules, .. } => {
                rules.iter().map(|application| application.rule.clone()).collect()
            }
            Plan::Cached { .. } => vec!["CacheHit".to_string()],
            Plan::PassThrough => Vec::new(),
        }
    }
}

/// Primary cost dimension targeted by a rule. Used by the `CompositionPlanner`
/// to classify rules as orthogonal (different drivers → safe to compose) or
/// overlapping (same driver, same `messages` mutation → unsafe unless
/// explicitly allowlisted).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CostDriver {
    InputTokens,
    OutputTokens,
    ModelPrice,
    CallElimination,
    Structural,
}

/// A rule's bid to rewrite a call, produced by `RewriteRule::propose`.
///
/// The safety check is a separate closure because we want to evaluate it
/// only against the *winning* proposal — running every check up front
/// burns the overhead budget on a hot path.
pub struct Proposal {
    pub rewritten: Plan,
    pub projected_savings_usd: f32,
    pub cost_driver: CostDriver,
    pub safety_check: Box<dyn Fn(&Call) -> bool + Send + Sync>,
}

/// Trait implemented by each of the five rewrite rules (`CacheHit`,
/// `ContextCompress`, `ParallelBranch`, `ModelDowngrade`, `StateDrop`).
///
/// O2 ships the trait and planner plumbing; the concrete rules are filed
/// under separate beads (O5 in particular).
pub trait RewriteRule: Send + Sync {
    /// Stable name — used in audit rows and the `agentc optimize inspect`
    /// readout. Must never change once a rule ships.
    fn name(&self) -> &'static str;

    /// Cheap predicate: does this rule even look at this call?
    fn applies(&self, call: &Call, profile: &CallSiteProfile) -> bool;

    /// Construct a concrete proposal. Returning `None` is equivalent to
    /// `applies` returning false; rules may short-circuit here when the
    /// projection math produces a non-positive savings number.
    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal>;

    /// Maximum tolerated shadow-mode divergence. Consulted by the
    /// accuracy-budget machinery (bead O4).
    fn accuracy_budget(&self) -> f32;

    /// Whether this rule is safe when the provider adapter says the native
    /// message shape cannot be represented losslessly by [`Call::messages`].
    ///
    /// The conservative default is false. A rule may opt in only when it does
    /// not inspect, hash, remove, replace, or reorder messages; model- and
    /// parameter-only rewrites are the intended examples.
    fn preserves_native_messages(&self) -> bool {
        false
    }
}

/// Top-level optimizer. Constructed once per process; `plan()` is safe to
/// call concurrently.
pub struct Optimizer {
    cost_model: Arc<CostModel>,
    rules: Vec<Box<dyn RewriteRule>>,
    config: OptimizerConfig,
    budget: Arc<Budget>,
}

impl Optimizer {
    pub fn new(
        cost_model: Arc<CostModel>,
        rules: Vec<Box<dyn RewriteRule>>,
        config: OptimizerConfig,
    ) -> Self {
        Self::with_budget(cost_model, rules, config, Arc::new(Budget::new()))
    }

    /// Construct an optimizer with an explicit shared `Budget`. Production
    /// builds use this so the budget warmed from `cost_model.db` is
    /// consulted on every plan; tests stick with [`Optimizer::new`] which
    /// supplies a fresh budget.
    pub fn with_budget(
        cost_model: Arc<CostModel>,
        rules: Vec<Box<dyn RewriteRule>>,
        config: OptimizerConfig,
        budget: Arc<Budget>,
    ) -> Self {
        Self { cost_model, rules, config, budget }
    }

    /// Construct an optimizer with no rules (fail-open pass-through for
    /// every hot call). Used for O2 integration tests and as the stub the
    /// FFI surface falls back to before O5 lands.
    pub fn empty(cost_model: Arc<CostModel>, config: OptimizerConfig) -> Self {
        Self::new(cost_model, Vec::new(), config)
    }

    pub fn config(&self) -> &OptimizerConfig {
        &self.config
    }

    /// Read-only handle to the shared accuracy budget. The FFI layer uses
    /// this to fold shadow-mode divergence samples in without holding a
    /// separate `Arc<Budget>`.
    pub fn budget(&self) -> &Arc<Budget> {
        &self.budget
    }

    /// Per-rule tolerated divergence, looked up by rule name. The FFI
    /// `optimize_record_divergence` path uses this to gate a shadow sample
    /// against the firing rule's own budget. Returns `None` for an unknown
    /// rule name so the caller can fall back to a conservative default.
    pub fn accuracy_budget_for(&self, rule_name: &str) -> Option<f32> {
        self.rules
            .iter()
            .find(|r| r.name() == rule_name)
            .map(|r| r.accuracy_budget())
    }

    /// Resolve the complete plan's divergence threshold once, before its
    /// identity and profile key are constructed. An explicit calibrated
    /// configuration value wins; otherwise the strictest constituent rule
    /// budget applies to this complete plan only.
    pub fn divergence_threshold_for_plan(&self, plan: &Plan) -> f64 {
        self.config
            .global_divergence_threshold
            .or_else(|| {
                plan.rule_names()
                    .iter()
                    .filter_map(|rule| self.accuracy_budget_for(rule))
                    .filter(|value| value.is_finite() && (0.0..=1.0).contains(value))
                    .map(f64::from)
                    .min_by(f64::total_cmp)
            })
            .unwrap_or(0.05)
    }

    /// Add a rule post-construction (primarily for tests that want to
    /// inject a mock rule into an otherwise stock optimizer).
    pub fn push_rule(&mut self, rule: Box<dyn RewriteRule>) {
        self.rules.push(rule);
    }

    /// Enumerate the bounded complete-plan search space used by the empirical
    /// selector. The immutable reference is always element zero. Rule-local
    /// projections only produce concrete mutations; they never rank the
    /// returned plans.
    pub fn candidate_plans(&self, call: &Call, catalog: &ModelCatalog) -> Vec<Plan> {
        let reference = || vec![Plan::PassThrough];
        if !self.config.enabled {
            return reference();
        }

        let deadline = Instant::now();
        let max_overhead_us = (self.config.max_overhead_ms * 1000.0) as u128;
        let profile = self
            .cost_model
            .get(&call.call_site_id)
            .unwrap_or_else(|| CallSiteProfile::new(call.call_site_id.clone()));
        if profile.window_observations < self.config.hot_threshold {
            return reference();
        }

        let now_us = now_us();
        let routing_enabled = self
            .rules
            .iter()
            .any(|rule| rule.name() == "ModelDowngrade");
        let mut proposals = Vec::with_capacity(self.rules.len());
        for rule in &self.rules {
            // The joint path obtains model choices directly from the catalog.
            // ModelDowngrade remains registered for the current-greedy baseline.
            if rule.name() == "ModelDowngrade" {
                continue;
            }
            if self.budget.is_disabled(&call.call_site_id, rule.name(), now_us) {
                continue;
            }
            if call.has_opaque_native_messages() && !rule.preserves_native_messages() {
                continue;
            }
            if rule.applies(call, &profile) {
                if let Some(proposal) = rule.propose(call, &profile) {
                    if proposal.projected_savings_usd >= 0.0 {
                        proposals.push((rule.name().to_string(), proposal));
                    }
                }
            }
            if deadline.elapsed().as_micros() > max_overhead_us {
                return reference();
            }
        }

        let mut semantic_plans = vec![Plan::PassThrough];
        semantic_plans.extend(crate::composition::enumerate_compatible_plans(
            proposals,
            call,
            self.config.max_rewrite_depth,
        ));

        let requirements = RequestRequirements::from_call(call);
        let source = requirements.as_ref().and_then(|requirements| {
            catalog.resolve(
                &requirements.provider_protocol,
                &requirements.provider_namespace,
                &call.model,
            )
        });
        let mut plans = Vec::new();
        for semantic_plan in semantic_plans {
            plans.push(semantic_plan.clone());
            if !routing_enabled {
                continue;
            }
            let (Some(source), Some(candidate_call)) =
                (source, single_call(call, &semantic_plan))
            else {
                continue;
            };
            let mut bounded_call = candidate_call.clone();
            RequestRequirements::apply_transformed_input_bound(call, &mut bounded_call);
            for target in catalog.compatible_targets(&bounded_call) {
                if target.model_id == source.model_id {
                    continue;
                }
                let projected_savings = projected_routing_savings(&profile, source, target);
                if let Some(routed) = route_semantic_plan(
                    &semantic_plan,
                    &bounded_call,
                    target,
                    catalog,
                    projected_savings,
                ) {
                    plans.push(routed);
                }
                if deadline.elapsed().as_micros() > max_overhead_us {
                    return reference();
                }
            }
        }

        if deadline.elapsed().as_micros() > max_overhead_us {
            reference()
        } else {
            plans
        }
    }

    /// Entry point. Never panics (rule panics and any downstream panic is
    /// caught at the FFI boundary in `agentc-profiler`; internally we just
    /// return `PassThrough` on any ambiguous state).
    pub fn plan(&self, call: &Call) -> Plan {
        // Step 1 — master switch.
        if !self.config.enabled {
            return Plan::PassThrough;
        }

        let deadline = Instant::now();
        let max_overhead_us = (self.config.max_overhead_ms * 1000.0) as u128;

        // Step 2 — cold-path early return.
        let profile = self
            .cost_model
            .get(&call.call_site_id)
            .unwrap_or_else(|| CallSiteProfile::new(call.call_site_id.clone()));
        if profile.window_observations < self.config.hot_threshold {
            return Plan::PassThrough;
        }

        // Overhead kill-switch (pre-rule): if we're already over budget
        // just reading the profile, don't risk rule work. This is cheap
        // because `Instant::elapsed` is just a CLOCK_MONOTONIC read.
        if deadline.elapsed().as_micros() > max_overhead_us {
            return Plan::PassThrough;
        }

        // Step 3 — gather proposals. `applies` is the cheap filter;
        // `propose` does the potentially-expensive projection math.
        // Each rule is gated by `Budget::is_disabled` so that operator
        // overrides and auto-disables (post-cooldown re-enable) take
        // effect without touching this loop.
        let now_us = now_us();
        let mut proposals: Vec<(String, Proposal)> = Vec::with_capacity(self.rules.len());
        for rule in &self.rules {
            if self.budget.is_disabled(&call.call_site_id, rule.name(), now_us) {
                continue;
            }
            if call.has_opaque_native_messages() && !rule.preserves_native_messages() {
                continue;
            }
            if !rule.applies(call, &profile) {
                continue;
            }
            if let Some(p) = rule.propose(call, &profile) {
                if p.projected_savings_usd >= 0.0 {
                    proposals.push((rule.name().to_string(), p));
                }
            }
            // Re-check the kill switch between rules — a single runaway
            // `propose` shouldn't starve the remaining rules of signal,
            // but once we're over budget there's no point continuing.
            if deadline.elapsed().as_micros() > max_overhead_us {
                return Plan::PassThrough;
            }
        }

        // Step 4 — rank by projected savings descending (highest-value rules
        // are considered first in the composition selection loop).
        proposals.sort_by(|a, b| {
            b.1.projected_savings_usd
                .partial_cmp(&a.1.projected_savings_usd)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Step 5 — either V2 composition or V1 first-match-wins.
        // `AGENTC_COMPOSE=0` disables composition for baseline comparisons.
        
        if self.config.compose {
            let result = crate::composition::compose_proposals(proposals, call);
            if deadline.elapsed().as_micros() > max_overhead_us {
                return Plan::PassThrough;
            }
            result.plan
        } else {
            // V1 first-safety-check-wins: proposals already sorted by savings desc.
            proposals
                .into_iter()
                .find_map(|(_name, prop)| {
                    if (prop.safety_check)(call) {
                        Some(prop.rewritten)
                    } else {
                        None
                    }
                })
                .unwrap_or(Plan::PassThrough)
        }
    }
}

fn single_call<'a>(original: &'a Call, plan: &'a Plan) -> Option<&'a Call> {
    match plan {
        Plan::PassThrough => Some(original),
        Plan::Rewritten { call, .. } | Plan::Composed { call, .. } => Some(call),
        Plan::Cached { .. } | Plan::Parallel { .. } => None,
    }
}

fn projected_routing_savings(
    profile: &CallSiteProfile,
    source: &ModelTarget,
    target: &ModelTarget,
) -> f32 {
    let source_price = profile.input_tokens.mean
        * source.price.input_per_million_tokens_usd
        + profile.output_tokens.mean * source.price.output_per_million_tokens_usd;
    let target_price = profile.input_tokens.mean
        * target.price.input_per_million_tokens_usd
        + profile.output_tokens.mean * target.price.output_per_million_tokens_usd;
    if source_price <= 0.0 || !source_price.is_finite() || !target_price.is_finite() {
        return 0.0;
    }
    (profile.cost_usd.mean * (1.0 - target_price / source_price)) as f32
}

fn route_semantic_plan(
    semantic_plan: &Plan,
    candidate_call: &Call,
    target: &ModelTarget,
    catalog: &ModelCatalog,
    projected_savings_usd: f32,
) -> Option<Plan> {
    let metadata = catalog.routed_target(candidate_call, target).ok()?;
    let mut routed_call = candidate_call.clone();
    routed_call.model = target.model_id.clone();
    metadata.annotate_call(&mut routed_call).ok()?;
    let routing_application = RuleApplication {
        rule: "ModelDowngrade".to_string(),
        projected_savings_usd,
        cost_driver: CostDriver::ModelPrice,
    };

    match semantic_plan {
        Plan::PassThrough => Some(Plan::Rewritten {
            rule: routing_application.rule,
            call: routed_call,
            projected_savings_usd,
        }),
        Plan::Rewritten {
            rule,
            projected_savings_usd: rewrite_savings,
            ..
        } => Some(Plan::Composed {
            rules: vec![
                RuleApplication {
                    rule: rule.clone(),
                    projected_savings_usd: *rewrite_savings,
                    cost_driver: cost_driver_for_rule(rule),
                },
                routing_application,
            ],
            call: routed_call,
            net_savings_usd: *rewrite_savings + projected_savings_usd,
        }),
        Plan::Composed {
            rules,
            net_savings_usd,
            ..
        } => {
            let mut routed_rules = rules.clone();
            routed_rules.push(routing_application);
            Some(Plan::Composed {
                rules: routed_rules,
                call: routed_call,
                net_savings_usd: *net_savings_usd + projected_savings_usd,
            })
        }
        Plan::Cached { .. } | Plan::Parallel { .. } => None,
    }
}

fn cost_driver_for_rule(rule: &str) -> CostDriver {
    match rule {
        "OutputBudget" | "DeadOutputTruncation" => CostDriver::OutputTokens,
        "ModelDowngrade" => CostDriver::ModelPrice,
        "CacheHit" => CostDriver::CallElimination,
        "ParallelBranch" => CostDriver::Structural,
        _ => CostDriver::InputTokens,
    }
}

fn now_us() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_micros() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost_model::CostModelUpdate;
    use crate::dag::Message;
    use crate::model_catalog::{
        default_model_catalog, OPENAI_CHAT_COMPLETIONS_PROTOCOL, ROUTE_CONTEXT_KEY,
    };

    fn sample_call(site: &str) -> Call {
        Call {
            call_site_id: site.to_string(),
            trace_id: [0u8; 16],
            span_id: [0u8; 8],
            model: "gpt-4o".into(),
            messages: vec![],
            parameters: Default::default(),
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        }
    }

    fn observe(cost_model: &CostModel, site: &str, n: u32) {
        for _ in 0..n {
            cost_model.observe(CostModelUpdate {
                call_site_id: site.to_string(),
                input_tokens: 100,
                output_tokens: 50,
                latency_ms: 100.0,
                cost_usd: 0.001,
                output_is_structured: false,
                output_is_short: true,
                now_us: Some(0),
            });
        }
    }

    struct AlwaysFires {
        savings: f32,
    }
    impl RewriteRule for AlwaysFires {
        fn name(&self) -> &'static str {
            "AlwaysFires"
        }
        fn applies(&self, _: &Call, _: &CallSiteProfile) -> bool {
            true
        }
        fn propose(&self, call: &Call, _: &CallSiteProfile) -> Option<Proposal> {
            let mut rewritten = call.clone();
            rewritten.parameters.max_output_tokens = Some(64);
            Some(Proposal {
                rewritten: Plan::Rewritten {
                    rule: self.name().to_string(),
                    call: rewritten,
                    projected_savings_usd: self.savings,
                },
                projected_savings_usd: self.savings,
                cost_driver: CostDriver::OutputTokens,
                safety_check: Box::new(|_| true),
            })
        }
        fn accuracy_budget(&self) -> f32 {
            0.05
        }
    }

    struct NeverFires;
    impl RewriteRule for NeverFires {
        fn name(&self) -> &'static str {
            "NeverFires"
        }
        fn applies(&self, _: &Call, _: &CallSiteProfile) -> bool {
            false
        }
        fn propose(&self, _: &Call, _: &CallSiteProfile) -> Option<Proposal> {
            None
        }
        fn accuracy_budget(&self) -> f32 {
            0.0
        }
    }

    struct NativeMessagePreservingRule;
    impl RewriteRule for NativeMessagePreservingRule {
        fn name(&self) -> &'static str {
            "NativeMessagePreservingRule"
        }
        fn applies(&self, _: &Call, _: &CallSiteProfile) -> bool {
            true
        }
        fn propose(&self, call: &Call, _: &CallSiteProfile) -> Option<Proposal> {
            let mut rewritten = call.clone();
            rewritten.parameters.max_output_tokens = Some(64);
            Some(Proposal {
                rewritten: Plan::Rewritten {
                    rule: self.name().to_string(),
                    call: rewritten,
                    projected_savings_usd: 0.5,
                },
                projected_savings_usd: 0.5,
                cost_driver: CostDriver::OutputTokens,
                safety_check: Box::new(|_| true),
            })
        }
        fn accuracy_budget(&self) -> f32 {
            0.01
        }
        fn preserves_native_messages(&self) -> bool {
            true
        }
    }

    struct DropFirstMessage;
    impl RewriteRule for DropFirstMessage {
        fn name(&self) -> &'static str {
            "DropFirstMessage"
        }
        fn applies(&self, call: &Call, _: &CallSiteProfile) -> bool {
            call.messages.len() >= 2
        }
        fn propose(&self, call: &Call, _: &CallSiteProfile) -> Option<Proposal> {
            let mut rewritten = call.clone();
            rewritten.messages.remove(0);
            Some(Proposal {
                rewritten: Plan::Rewritten {
                    rule: self.name().to_string(),
                    call: rewritten,
                    projected_savings_usd: 0.1,
                },
                projected_savings_usd: 0.1,
                cost_driver: CostDriver::InputTokens,
                safety_check: Box::new(|_| true),
            })
        }
        fn accuracy_budget(&self) -> f32 {
            0.01
        }
    }

    struct RoutingEnabled;
    impl RewriteRule for RoutingEnabled {
        fn name(&self) -> &'static str {
            "ModelDowngrade"
        }
        fn applies(&self, _: &Call, _: &CallSiteProfile) -> bool {
            false
        }
        fn propose(&self, _: &Call, _: &CallSiteProfile) -> Option<Proposal> {
            None
        }
        fn accuracy_budget(&self) -> f32 {
            0.05
        }
        fn preserves_native_messages(&self) -> bool {
            true
        }
    }

    struct UnsafeRule;
    impl RewriteRule for UnsafeRule {
        fn name(&self) -> &'static str {
            "UnsafeRule"
        }
        fn applies(&self, _: &Call, _: &CallSiteProfile) -> bool {
            true
        }
        fn propose(&self, call: &Call, _: &CallSiteProfile) -> Option<Proposal> {
            let mut rewritten = call.clone();
            rewritten.parameters.max_output_tokens = Some(32);
            Some(Proposal {
                rewritten: Plan::Rewritten {
                    rule: self.name().to_string(),
                    call: rewritten,
                    projected_savings_usd: 999.0, // ranks first, but fails safety
                },
                projected_savings_usd: 999.0,
                cost_driver: CostDriver::OutputTokens,
                safety_check: Box::new(|_| false),
            })
        }
        fn accuracy_budget(&self) -> f32 {
            0.0
        }
    }

    struct SlowRule;
    impl RewriteRule for SlowRule {
        fn name(&self) -> &'static str {
            "SlowRule"
        }
        fn applies(&self, _: &Call, _: &CallSiteProfile) -> bool {
            true
        }
        fn propose(&self, _: &Call, _: &CallSiteProfile) -> Option<Proposal> {
            std::thread::sleep(std::time::Duration::from_millis(20));
            None
        }
        fn accuracy_budget(&self) -> f32 {
            0.0
        }
    }

    #[test]
    fn disabled_optimizer_short_circuits() {
        let cm = Arc::new(CostModel::new());
        observe(&cm, "site", 50);
        let opt = Optimizer::empty(cm, OptimizerConfig { enabled: false, ..Default::default() });
        assert!(matches!(opt.plan(&sample_call("site")), Plan::PassThrough));
    }

    #[test]
    fn cold_call_returns_pass_through() {
        let cm = Arc::new(CostModel::new());
        observe(&cm, "site", 2); // < default hot_threshold (3)
        let opt = Optimizer::new(
            cm,
            vec![Box::new(AlwaysFires { savings: 1.0 })],
            OptimizerConfig::default(),
        );
        assert!(matches!(opt.plan(&sample_call("site")), Plan::PassThrough));
    }

    #[test]
    fn budget_disabled_rule_is_gated_off() {
        // Regression (bd-inc): the planner's `budget.is_disabled(..) continue`
        // is the ONLY thing that disables 8 of the 9 rules. Deleting it must
        // fail a test. AlwaysFires fires on a hot call; once the accuracy guard
        // auto-disables it (BREACH_STREAK consecutive over-budget samples), the
        // same call must pass through.
        let cm = Arc::new(CostModel::new());
        observe(&cm, "site", 10);
        let budget = Arc::new(Budget::new());
        let opt = Optimizer::with_budget(
            cm,
            vec![Box::new(AlwaysFires { savings: 1.0 })],
            OptimizerConfig::default(),
            budget.clone(),
        );

        // Baseline: the rule fires.
        assert!(
            matches!(opt.plan(&sample_call("site")), Plan::Rewritten { .. }),
            "rule should fire before it is disabled"
        );

        // Drive the guard to auto-disable AlwaysFires for this call site. Use
        // the planner's own clock so the cooldown window covers the next plan().
        let now = now_us();
        for _ in 0..crate::budget::BREACH_STREAK {
            budget.record_sample("site", "AlwaysFires", 1.0, 0.05, now);
        }
        assert!(budget.is_disabled("site", "AlwaysFires", now), "guard should have disabled it");

        // The gate must now suppress the rule → pass-through.
        assert!(
            matches!(opt.plan(&sample_call("site")), Plan::PassThrough),
            "a disabled rule must be gated off by the planner (planner.rs disable gate)"
        );
    }

    #[test]
    fn hot_call_with_no_applicable_rule_returns_pass_through() {
        let cm = Arc::new(CostModel::new());
        observe(&cm, "site", 10);
        let opt = Optimizer::new(cm, vec![Box::new(NeverFires)], OptimizerConfig::default());
        assert!(matches!(opt.plan(&sample_call("site")), Plan::PassThrough));
    }

    #[test]
    fn hot_call_with_firing_rule_returns_rewritten() {
        let cm = Arc::new(CostModel::new());
        observe(&cm, "site", 10);
        let opt = Optimizer::new(
            cm,
            vec![Box::new(AlwaysFires { savings: 0.5 })],
            OptimizerConfig::default(),
        );
        let plan = opt.plan(&sample_call("site"));
        match plan {
            Plan::Rewritten { rule, projected_savings_usd, .. } => {
                assert_eq!(rule, "AlwaysFires");
                assert!((projected_savings_usd - 0.5).abs() < 1e-6);
            }
            _ => panic!("expected Rewritten, got {:?}", plan),
        }
    }

    #[test]
    fn opaque_native_messages_gate_message_unsafe_rules() {
        let cm = Arc::new(CostModel::new());
        observe(&cm, "site", 10);
        let opt = Optimizer::new(
            cm,
            vec![Box::new(AlwaysFires { savings: 0.5 })],
            OptimizerConfig::default(),
        );
        let mut call = sample_call("site");
        call.parameters.extra = serde_json::json!({
            crate::dag::NATIVE_MESSAGES_OPAQUE_KEY: true,
        });

        assert!(matches!(opt.plan(&call), Plan::PassThrough));
    }

    #[test]
    fn opaque_native_messages_allow_parameter_only_rules() {
        let cm = Arc::new(CostModel::new());
        observe(&cm, "site", 10);
        let opt = Optimizer::new(
            cm,
            vec![Box::new(NativeMessagePreservingRule)],
            OptimizerConfig::default(),
        );
        let mut call = sample_call("site");
        call.parameters.extra = serde_json::json!({
            crate::dag::NATIVE_MESSAGES_OPAQUE_KEY: true,
        });

        match opt.plan(&call) {
            Plan::Rewritten { call, .. } => {
                assert_eq!(call.parameters.max_output_tokens, Some(64));
                assert!(call.has_opaque_native_messages());
            }
            other => panic!("expected parameter-only rewrite, got {other:?}"),
        }
    }

    #[test]
    fn configured_rewrite_depth_bounds_joint_candidate_enumeration() {
        let cm = Arc::new(CostModel::new());
        observe(&cm, "depth-site", 10);
        let optimizer = Optimizer::new(
            cm,
            vec![
                Box::new(DropFirstMessage),
                Box::new(AlwaysFires { savings: 0.5 }),
            ],
            OptimizerConfig {
                max_rewrite_depth: 1,
                ..OptimizerConfig::default()
            },
        );
        let catalog = default_model_catalog().unwrap();
        let mut call = sample_call("depth-site");
        call.messages.push(Message {
            role: "user".to_string(),
            content: "second".to_string(),
        });

        let plans = optimizer.candidate_plans(&call, &catalog);
        assert!(plans
            .iter()
            .all(|plan| !matches!(plan, Plan::Composed { rules, .. } if rules.len() > 1)));
        assert!(plans
            .iter()
            .any(|plan| matches!(plan, Plan::Rewritten { .. })));
    }

    #[test]
    fn configured_global_divergence_threshold_overrides_rule_budget() {
        let optimizer = Optimizer::new(
            Arc::new(CostModel::new()),
            vec![Box::new(AlwaysFires { savings: 0.5 })],
            OptimizerConfig {
                global_divergence_threshold: Some(0.25),
                ..OptimizerConfig::default()
            },
        );
        let plan = Plan::Rewritten {
            rule: "AlwaysFires".to_string(),
            call: sample_call("threshold-site"),
            projected_savings_usd: 0.5,
        };
        assert_eq!(optimizer.divergence_threshold_for_plan(&plan), 0.25);
    }

    #[test]
    fn transformed_request_size_can_unlock_a_joint_route_only() {
        let cm = Arc::new(CostModel::new());
        observe(&cm, "transformed-route-site", 10);
        let opt = Optimizer::new(
            cm,
            vec![Box::new(DropFirstMessage), Box::new(RoutingEnabled)],
            OptimizerConfig::default(),
        );
        let mut catalog = default_model_catalog().unwrap();
        for target in &mut catalog.targets {
            if target.adapter_protocol != OPENAI_CHAT_COMPLETIONS_PROTOCOL {
                continue;
            }
            match target.model_id.as_str() {
                "gpt-4o-2024-11-20" => {
                    target.context_window_tokens = 400;
                    target.max_output_tokens = 100;
                }
                "gpt-4o-mini-2024-07-18" => {
                    target.context_window_tokens = 180;
                    target.max_output_tokens = 100;
                }
                _ => {}
            }
        }
        let mut call = sample_call("transformed-route-site");
        call.model = "gpt-4o".to_string();
        call.messages = vec![
            Message {
                role: "system".to_string(),
                content: "x".repeat(150),
            },
            Message {
                role: "user".to_string(),
                content: "y".repeat(100),
            },
        ];
        call.parameters.max_output_tokens = Some(20);
        call.parameters.extra = serde_json::json!({
            ROUTE_CONTEXT_KEY: {
                "provider_protocol": OPENAI_CHAT_COMPLETIONS_PROTOCOL,
                "provider_namespace": "openai",
                "input_tokens_upper_bound": 300,
                "input_tokens_upper_bound_basis": "json_utf8_bytes_v1",
                "image_input": false,
                "tool_calling": false,
                "structured_outputs": false,
                "streaming": false
            }
        });

        let plans = opt.candidate_plans(&call, &catalog);
        assert!(
            !plans.iter().any(|plan| matches!(
                plan,
                Plan::Rewritten { rule, call, .. }
                    if rule == "ModelDowngrade"
                        && call.model == "gpt-4o-mini-2024-07-18"
            )),
            "the original 300+20 token request must not fit the 180-token target"
        );
        let joint_call = plans.iter().find_map(|plan| match plan {
            Plan::Composed { rules, call, .. }
                if rules.iter().any(|rule| rule.rule == "DropFirstMessage")
                    && rules.iter().any(|rule| rule.rule == "ModelDowngrade")
                    && call.model == "gpt-4o-mini-2024-07-18" =>
            {
                Some(call)
            }
            _ => None,
        });
        let joint_call =
            joint_call.expect("dropping 150 known content bytes should unlock the joint route");
        assert_eq!(
            RequestRequirements::from_call(joint_call)
                .unwrap()
                .input_tokens_upper_bound,
            150
        );
    }

    #[test]
    fn first_safety_check_pass_wins_over_higher_ranked_failure() {
        let cm = Arc::new(CostModel::new());
        observe(&cm, "site", 10);
        // UnsafeRule projects 999 savings but fails safety; AlwaysFires
        // projects 0.5 but passes. The planner must pick AlwaysFires.
        let opt = Optimizer::new(
            cm,
            vec![
                Box::new(UnsafeRule),
                Box::new(AlwaysFires { savings: 0.5 }),
            ],
            OptimizerConfig::default(),
        );
        match opt.plan(&sample_call("site")) {
            Plan::Rewritten { rule, .. } => assert_eq!(rule, "AlwaysFires"),
            other => panic!("expected AlwaysFires, got {:?}", other),
        }
    }

    #[test]
    fn overhead_kill_switch_forces_pass_through() {
        let cm = Arc::new(CostModel::new());
        observe(&cm, "site", 10);
        let opt = Optimizer::new(
            cm,
            vec![Box::new(SlowRule), Box::new(AlwaysFires { savings: 1.0 })],
            OptimizerConfig { max_overhead_ms: 1.0, ..Default::default() },
        );
        // SlowRule sleeps 20ms which exceeds the 1ms budget.
        let plan = opt.plan(&sample_call("site"));
        assert!(
            matches!(plan, Plan::PassThrough),
            "expected kill-switch PassThrough, got {:?}",
            plan
        );
    }

    #[test]
    fn plan_serializes_with_tag() {
        let p = Plan::PassThrough;
        let s = serde_json::to_string(&p).unwrap();
        assert_eq!(s, "{\"kind\":\"pass_through\"}");
    }

    #[test]
    fn plan_composed_serializes_with_tag() {
        let p = Plan::Composed {
            rules: vec![
                RuleApplication {
                    rule: "ContextCompress".into(),
                    projected_savings_usd: 0.3,
                    cost_driver: CostDriver::InputTokens,
                },
                RuleApplication {
                    rule: "OutputBudget".into(),
                    projected_savings_usd: 0.1,
                    cost_driver: CostDriver::OutputTokens,
                },
            ],
            call: sample_call("site"),
            net_savings_usd: 0.4,
        };
        let s = serde_json::to_string(&p).unwrap();
        assert!(s.contains("\"composed\""), "tag missing: {s}");
        assert!(s.contains("ContextCompress"));
        assert!(s.contains("OutputBudget"));
    }
}
