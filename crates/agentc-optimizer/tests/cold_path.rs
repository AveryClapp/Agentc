//! Integration: a cold call site (`n_observations < hot_threshold`) must
//! always return `Plan::PassThrough`. This is the spec's exit criterion
//! for O2 item #1.

use std::sync::Arc;

use agentc_optimizer::{
    cost_model::{CostModel, CostModelUpdate},
    dag::Call,
    planner::{CostDriver, Optimizer, Plan, Proposal, RewriteRule},
    CallSiteProfile, OptimizerConfig,
};

fn sample_call(site: &str) -> Call {
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

fn observe(cost_model: &CostModel, site: &str, n: u32) {
    for _ in 0..n {
        cost_model.observe(CostModelUpdate {
            call_site_id: site.into(),
            input_tokens: 100,
            output_tokens: 20,
            latency_ms: 50.0,
            cost_usd: 0.0005,
            output_is_structured: false,
            output_is_short: true,
            now_us: Some(0),
        });
    }
}

struct AlwaysFires;
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
                rule: "AlwaysFires".into(),
                call: call.clone(),
                projected_savings_usd: 1.0,
            },
            projected_savings_usd: 1.0,
            cost_driver: CostDriver::InputTokens,
            safety_check: Box::new(|_| true),
        })
    }
    fn accuracy_budget(&self) -> f32 {
        0.01
    }
}

#[test]
fn zero_observations_is_pass_through() {
    let cm = Arc::new(CostModel::new());
    let opt = Optimizer::new(
        cm,
        vec![Box::new(AlwaysFires)],
        OptimizerConfig::default(),
    );
    assert!(matches!(opt.plan(&sample_call("new-site")), Plan::PassThrough));
}

#[test]
fn observations_below_threshold_is_pass_through() {
    let cm = Arc::new(CostModel::new());
    observe(&cm, "warming", 2); // default hot_threshold = 3
    let opt = Optimizer::new(
        cm,
        vec![Box::new(AlwaysFires)],
        OptimizerConfig::default(),
    );
    assert!(matches!(opt.plan(&sample_call("warming")), Plan::PassThrough));
}

#[test]
fn observations_at_threshold_is_eligible() {
    let cm = Arc::new(CostModel::new());
    observe(&cm, "hot", 3); // exactly at threshold
    let opt = Optimizer::new(
        cm,
        vec![Box::new(AlwaysFires)],
        OptimizerConfig::default(),
    );
    match opt.plan(&sample_call("hot")) {
        Plan::Rewritten { rule, .. } => assert_eq!(rule, "AlwaysFires"),
        other => panic!("expected Rewritten, got {other:?}"),
    }
}

#[test]
fn higher_threshold_keeps_calls_cold() {
    let cm = Arc::new(CostModel::new());
    observe(&cm, "mostly-hot", 5);
    let opt = Optimizer::new(
        cm,
        vec![Box::new(AlwaysFires)],
        OptimizerConfig { hot_threshold: 10, ..Default::default() },
    );
    assert!(matches!(opt.plan(&sample_call("mostly-hot")), Plan::PassThrough));
}
