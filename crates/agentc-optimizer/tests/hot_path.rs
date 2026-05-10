//! Integration: a hot call site exercises the full rule-ranking pipeline.
//! Exit-criterion coverage:
//!   - hot call with no firing rule → `PassThrough`
//!   - hot call with one firing rule → `Rewritten`
//!   - proposals ranked by projected savings descending

use std::sync::Arc;

use agentc_optimizer::{
    cost_model::{CostModel, CostModelUpdate},
    dag::Call,
    planner::{CostDriver, Optimizer, Plan, Proposal, RewriteRule},
    CallSiteProfile, OptimizerConfig,
};

fn heat_up(cm: &CostModel, site: &str, n: u32) {
    for _ in 0..n {
        cm.observe(CostModelUpdate {
            call_site_id: site.into(),
            input_tokens: 100,
            output_tokens: 40,
            latency_ms: 80.0,
            cost_usd: 0.002,
            output_is_structured: true,
            output_is_short: true,
            now_us: Some(0),
        });
    }
}

fn hot_call(site: &str) -> Call {
    Call {
        call_site_id: site.into(),
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

struct Fires {
    name: &'static str,
    savings: f32,
}
impl RewriteRule for Fires {
    fn name(&self) -> &'static str {
        self.name
    }
    fn applies(&self, _: &Call, _: &CallSiteProfile) -> bool {
        true
    }
    fn propose(&self, call: &Call, _: &CallSiteProfile) -> Option<Proposal> {
        Some(Proposal {
            rewritten: Plan::Rewritten {
                rule: self.name.into(),
                call: call.clone(),
                projected_savings_usd: self.savings,
            },
            projected_savings_usd: self.savings,
            cost_driver: CostDriver::InputTokens,
            safety_check: Box::new(|_| true),
        })
    }
    fn accuracy_budget(&self) -> f32 {
        0.02
    }
}

struct SkipsEverything;
impl RewriteRule for SkipsEverything {
    fn name(&self) -> &'static str {
        "SkipsEverything"
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

#[test]
fn hot_call_no_firing_rule_returns_pass_through() {
    let cm = Arc::new(CostModel::new());
    heat_up(&cm, "site", 10);
    let opt = Optimizer::new(
        cm,
        vec![Box::new(SkipsEverything), Box::new(SkipsEverything)],
        OptimizerConfig::default(),
    );
    assert!(matches!(opt.plan(&hot_call("site")), Plan::PassThrough));
}

#[test]
fn hot_call_picks_highest_projected_savings() {
    let cm = Arc::new(CostModel::new());
    heat_up(&cm, "site", 10);
    let opt = Optimizer::new(
        cm,
        vec![
            Box::new(Fires { name: "small", savings: 0.01 }),
            Box::new(Fires { name: "big", savings: 0.10 }),
            Box::new(Fires { name: "medium", savings: 0.05 }),
        ],
        OptimizerConfig::default(),
    );
    match opt.plan(&hot_call("site")) {
        Plan::Rewritten { rule, .. } => assert_eq!(rule, "big"),
        other => panic!("expected big, got {other:?}"),
    }
}

#[test]
fn rules_that_do_not_apply_never_contribute() {
    let cm = Arc::new(CostModel::new());
    heat_up(&cm, "site", 10);
    let opt = Optimizer::new(
        cm,
        vec![
            Box::new(SkipsEverything),
            Box::new(Fires { name: "only", savings: 0.02 }),
            Box::new(SkipsEverything),
        ],
        OptimizerConfig::default(),
    );
    match opt.plan(&hot_call("site")) {
        Plan::Rewritten { rule, .. } => assert_eq!(rule, "only"),
        other => panic!("expected only, got {other:?}"),
    }
}
