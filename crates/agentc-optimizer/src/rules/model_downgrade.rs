//! `ModelDowngrade` — route a hot call site to a cheaper model when the
//! call-site's outputs are reliably short or structured.
//!
//! Spec § Rule specifications > ModelDowngrade:
//! - Applies when `call.model` is in the route table AND
//!   `output_token_p95 <= route.max_output_tokens` AND
//!   `output_is_short + output_is_structured >= 0.80`.
//! - Safety: projected shadow divergence ≤ the rule's accuracy budget.
//!   On the first-ever downgrade attempt for a call site, divergence
//!   is unknown → fire probabilistically at 30% for the first 20
//!   shadow samples, then commit fully if the observed divergence is
//!   within budget.
//! - Projection: `cost_usd.mean * (1 - price_ratio)`.

use std::sync::Arc;

use crate::budget::Budget;
use crate::cost_model::CallSiteProfile;
use crate::dag::Call;
use crate::planner::{Plan, Proposal, RewriteRule};
use crate::shadow::ShadowSampler;

/// Default accuracy budget from spec § Accuracy budget.
pub const DEFAULT_ACCURACY_BUDGET: f32 = 0.03;
/// Minimum short+structured fraction.
pub const MIN_SHORT_OR_STRUCTURED_FRACTION: f32 = 0.80;
/// Required shadow observations before a call site leaves the
/// probabilistic-fire warmup.
pub const PROBATION_OBSERVATIONS: u64 = 20;
/// Probability of firing during the probation window.
pub const PROBATION_FIRE_RATE: f32 = 0.30;

/// One `from → to` routing entry.
#[derive(Debug, Clone)]
pub struct ModelDowngradeRoute {
    pub from: String,
    pub to: String,
    /// Ratio of to-price over from-price (0.1 means 10× cheaper).
    pub price_ratio: f32,
    /// p95 ceiling on the call site's output tokens for the route to apply.
    pub max_output_tokens: u32,
}

pub struct ModelDowngradeRule {
    routes: Vec<ModelDowngradeRoute>,
    budget: Arc<Budget>,
    probation_sampler: ShadowSampler,
    accuracy_budget: f32,
}

impl ModelDowngradeRule {
    pub fn new(routes: Vec<ModelDowngradeRoute>, budget: Arc<Budget>) -> Self {
        Self {
            routes,
            budget,
            probation_sampler: ShadowSampler::new(PROBATION_FIRE_RATE),
            accuracy_budget: DEFAULT_ACCURACY_BUDGET,
        }
    }

    pub fn with_probation_seed(mut self, seed: u64) -> Self {
        self.probation_sampler = ShadowSampler::with_seed(PROBATION_FIRE_RATE, seed);
        self
    }

    pub fn with_accuracy_budget(mut self, budget: f32) -> Self {
        self.accuracy_budget = budget;
        self
    }

    fn route_for<'a>(&'a self, model: &str) -> Option<&'a ModelDowngradeRoute> {
        self.routes.iter().find(|r| r.from == model)
    }
}

impl RewriteRule for ModelDowngradeRule {
    fn name(&self) -> &'static str {
        "ModelDowngrade"
    }

    fn applies(&self, call: &Call, profile: &CallSiteProfile) -> bool {
        let Some(route) = self.route_for(&call.model) else { return false; };
        if profile.output_token_p95 > route.max_output_tokens as f32 {
            return false;
        }
        let shape_score = profile.output_is_short + profile.output_is_structured;
        if shape_score < MIN_SHORT_OR_STRUCTURED_FRACTION {
            return false;
        }
        // If the budget has auto-disabled this rule for this call site,
        // refuse. The planner would also drop disabled rules at a
        // higher layer, but refusing here is cheap insurance.
        if self.budget.is_disabled(&call.call_site_id, self.name(), 0) {
            return false;
        }
        true
    }

    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal> {
        let route = self.route_for(&call.model)?.clone();

        // Probation gate: during warmup, fire probabilistically.
        let entry = self.budget.get_entry(&call.call_site_id, self.name());
        let observations = entry.as_ref().map(|e| e.stats.n).unwrap_or(0);
        if observations < PROBATION_OBSERVATIONS && !self.probation_sampler.should_sample() {
            return None;
        }

        // After probation, require observed mean divergence within
        // budget. During probation we fire optimistically (the above
        // rate-limits it).
        if observations >= PROBATION_OBSERVATIONS {
            if let Some(e) = &entry {
                if (e.stats.mean as f32) > self.accuracy_budget {
                    return None;
                }
            }
        }

        let projected_savings =
            profile.cost_usd.mean as f32 * (1.0 - route.price_ratio).max(0.0);

        let rewritten = Call {
            model: route.to.clone(),
            ..call.clone()
        };

        let rule_name = self.name().to_string();
        Some(Proposal {
            rewritten: Plan::Rewritten {
                rule: rule_name.clone(),
                call: rewritten,
                projected_savings_usd: projected_savings,
            },
            projected_savings_usd: projected_savings,
            safety_check: Box::new(move |_| true),
        })
    }

    fn accuracy_budget(&self) -> f32 {
        self.accuracy_budget
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost_model::WelfordStats;
    use crate::dag::Parameters;

    fn routes() -> Vec<ModelDowngradeRoute> {
        vec![ModelDowngradeRoute {
            from: "gpt-4o".into(),
            to: "gpt-4o-mini".into(),
            price_ratio: 0.1,
            max_output_tokens: 128,
        }]
    }

    fn hot_profile(n: u32) -> CallSiteProfile {
        let mut p = CallSiteProfile::new("site");
        p.n_observations = n;
        p.cost_usd = WelfordStats::from_persisted(n as u64, 0.01, 0.0);
        p.output_is_short = 1.0;
        p.output_is_structured = 0.0;
        p.output_token_p95 = 64.0;
        p
    }

    fn call() -> Call {
        Call {
            call_site_id: "site".into(),
            trace_id: [0u8; 16],
            span_id: [0u8; 8],
            model: "gpt-4o".into(),
            messages: vec![],
            parameters: Parameters::default(),
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        }
    }

    #[test]
    fn does_not_fire_for_unknown_model() {
        let b = Arc::new(Budget::new());
        let rule = ModelDowngradeRule::new(routes(), b);
        let mut c = call();
        c.model = "some-other".into();
        assert!(!rule.applies(&c, &hot_profile(20)));
    }

    #[test]
    fn does_not_fire_when_p95_exceeds_route_cap() {
        let b = Arc::new(Budget::new());
        let rule = ModelDowngradeRule::new(routes(), b);
        let mut p = hot_profile(20);
        p.output_token_p95 = 256.0;
        assert!(!rule.applies(&call(), &p));
    }

    #[test]
    fn does_not_fire_when_short_plus_structured_low() {
        let b = Arc::new(Budget::new());
        let rule = ModelDowngradeRule::new(routes(), b);
        let mut p = hot_profile(20);
        p.output_is_short = 0.5;
        p.output_is_structured = 0.2; // 0.7 < 0.8
        assert!(!rule.applies(&call(), &p));
    }

    #[test]
    fn does_not_fire_when_budget_disables_site() {
        let b = Arc::new(Budget::new());
        for _ in 0..crate::budget::BREACH_STREAK {
            b.record_sample("site", "ModelDowngrade", 0.99, 0.03, 0);
        }
        let rule = ModelDowngradeRule::new(routes(), b);
        assert!(!rule.applies(&call(), &hot_profile(20)));
    }

    #[test]
    fn probation_fires_probabilistically() {
        let b = Arc::new(Budget::new());
        // Seed the probation sampler so the outcome is deterministic.
        let rule = ModelDowngradeRule::new(routes(), b).with_probation_seed(1);
        let p = hot_profile(20);
        // With 0 observations, we're in probation. Over many attempts,
        // about 30% should propose.
        let mut fires = 0;
        for _ in 0..1000 {
            if rule.propose(&call(), &p).is_some() {
                fires += 1;
            }
        }
        let rate = fires as f32 / 1000.0;
        assert!((rate - 0.30).abs() < 0.05, "probation fire rate {rate}");
    }

    #[test]
    fn post_probation_rejects_over_budget_sites() {
        let b = Arc::new(Budget::new());
        // Seed 25 samples with high divergence → mean > budget.
        for _ in 0..25 {
            b.record_sample("site", "ModelDowngrade", 0.05, 1.0, 0);
        }
        let rule = ModelDowngradeRule::new(routes(), b);
        assert!(rule.propose(&call(), &hot_profile(20)).is_none());
    }

    #[test]
    fn post_probation_fires_when_mean_within_budget() {
        let b = Arc::new(Budget::new());
        for _ in 0..25 {
            b.record_sample("site", "ModelDowngrade", 0.01, 1.0, 0);
        }
        let rule = ModelDowngradeRule::new(routes(), b);
        let prop = rule
            .propose(&call(), &hot_profile(20))
            .expect("within budget must fire");
        match prop.rewritten {
            Plan::Rewritten { call: c, .. } => assert_eq!(c.model, "gpt-4o-mini"),
            _ => panic!("expected Rewritten"),
        }
    }

    #[test]
    fn projected_savings_uses_price_ratio() {
        let b = Arc::new(Budget::new());
        for _ in 0..25 {
            b.record_sample("site", "ModelDowngrade", 0.01, 1.0, 0);
        }
        let rule = ModelDowngradeRule::new(routes(), b);
        let prop = rule.propose(&call(), &hot_profile(20)).unwrap();
        // cost.mean = 0.01, price_ratio = 0.1 → savings = 0.009.
        assert!((prop.projected_savings_usd - 0.009).abs() < 1e-4);
    }
}
