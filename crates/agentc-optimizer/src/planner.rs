//! `Optimizer::plan` — the entry point called by the Python interceptor on
//! every LLM call.
//!
//! Contract (from `specs/optimizer.md` § Architecture > Layered flow):
//! 1. If the optimizer is disabled, return [`Plan::PassThrough`].
//! 2. Look up the `CallSiteProfile`. If `n_observations < hot_threshold`,
//!    return [`Plan::PassThrough`].
//! 3. Ask every enabled rule if it applies. Collect proposals.
//! 4. Sort by `projected_savings_usd` descending.
//! 5. Run each proposal's safety check in order. First pass wins.
//! 6. If plan evaluation's wall clock exceeds `max_overhead_ms`, the
//!    overhead kill switch returns [`Plan::PassThrough`].
//!
//! Rules never compose in a single plan (see Design Decisions §
//! "First-match wins, no rule composition").

use std::sync::Arc;
use std::time::Instant;

use serde::{Deserialize, Serialize};

use crate::config::OptimizerConfig;
use crate::cost_model::{CallSiteProfile, CostModel};
use crate::dag::Call;

/// The Optimizer's output. Python's executor dispatches each variant:
/// `Cached` returns directly, `Rewritten` dispatches the mutated call,
/// `Parallel` issues `asyncio.gather`, `PassThrough` runs the original.
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
}

impl Plan {
    pub fn is_pass_through(&self) -> bool {
        matches!(self, Plan::PassThrough)
    }

    pub fn rule(&self) -> Option<&str> {
        match self {
            Plan::PassThrough | Plan::Cached { .. } => None,
            Plan::Rewritten { rule, .. } | Plan::Parallel { rule, .. } => Some(rule.as_str()),
        }
    }
}

/// A rule's bid to rewrite a call, produced by `RewriteRule::propose`.
///
/// The safety check is a separate closure because we want to evaluate it
/// only against the *winning* proposal — running every check up front
/// burns the overhead budget on a hot path.
pub struct Proposal {
    pub rewritten: Plan,
    pub projected_savings_usd: f32,
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
}

/// Top-level optimizer. Constructed once per process; `plan()` is safe to
/// call concurrently.
pub struct Optimizer {
    cost_model: Arc<CostModel>,
    rules: Vec<Box<dyn RewriteRule>>,
    config: OptimizerConfig,
}

impl Optimizer {
    pub fn new(
        cost_model: Arc<CostModel>,
        rules: Vec<Box<dyn RewriteRule>>,
        config: OptimizerConfig,
    ) -> Self {
        Self { cost_model, rules, config }
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

    /// Add a rule post-construction (primarily for tests that want to
    /// inject a mock rule into an otherwise stock optimizer).
    pub fn push_rule(&mut self, rule: Box<dyn RewriteRule>) {
        self.rules.push(rule);
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
        if profile.n_observations < self.config.hot_threshold {
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
        let mut proposals: Vec<(String, Proposal)> = Vec::with_capacity(self.rules.len());
        for rule in &self.rules {
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

        // Step 4 — rank by projected savings descending.
        proposals.sort_by(|a, b| {
            b.1.projected_savings_usd
                .partial_cmp(&a.1.projected_savings_usd)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Step 5 — first safety-check pass wins.
        for (_name, prop) in proposals {
            if (prop.safety_check)(call) {
                // Final kill-switch check: if evaluation already blew
                // past the budget, do not commit the rewrite. The caller
                // must never see a Plan that took longer than
                // `max_overhead_ms` to compute.
                if deadline.elapsed().as_micros() > max_overhead_us {
                    return Plan::PassThrough;
                }
                return prop.rewritten;
            }
        }

        Plan::PassThrough
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost_model::CostModelUpdate;

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
            Some(Proposal {
                rewritten: Plan::Rewritten {
                    rule: self.name().to_string(),
                    call: call.clone(),
                    projected_savings_usd: self.savings,
                },
                projected_savings_usd: self.savings,
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

    struct UnsafeRule;
    impl RewriteRule for UnsafeRule {
        fn name(&self) -> &'static str {
            "UnsafeRule"
        }
        fn applies(&self, _: &Call, _: &CallSiteProfile) -> bool {
            true
        }
        fn propose(&self, call: &Call, _: &CallSiteProfile) -> Option<Proposal> {
            Some(Proposal {
                rewritten: Plan::Rewritten {
                    rule: self.name().to_string(),
                    call: call.clone(),
                    projected_savings_usd: 999.0, // ranks first, but fails safety
                },
                projected_savings_usd: 999.0,
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
}
