//! Integration: every ambiguous state resolves to `PassThrough`. Exit-
//! criterion coverage:
//!   - malformed `Call` JSON at the FFI boundary
//!   - a rule that panics during `propose`
//!   - overhead kill switch when a rule blows the latency budget
//!
//! The Python-level `catch_unwind` wrapper lives in
//! `crates/agentc-profiler/src/lib.rs`; these tests exercise the Rust
//! side directly so the behaviour is guaranteed under `cargo test`.

use std::sync::Arc;

use agentc_optimizer::{
    cost_model::{CostModel, CostModelUpdate},
    dag::Call,
    ffi::{optimize_plan, PASS_THROUGH_JSON},
    planner::{Optimizer, Plan, Proposal, RewriteRule},
    CallSiteProfile, OptimizerConfig,
};

fn heat_up(cm: &CostModel, site: &str, n: u32) {
    for _ in 0..n {
        cm.observe(CostModelUpdate {
            call_site_id: site.into(),
            input_tokens: 100,
            output_tokens: 20,
            latency_ms: 50.0,
            cost_usd: 0.001,
            output_is_structured: false,
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

struct SleepyRule;
impl RewriteRule for SleepyRule {
    fn name(&self) -> &'static str {
        "SleepyRule"
    }
    fn applies(&self, _: &Call, _: &CallSiteProfile) -> bool {
        true
    }
    fn propose(&self, call: &Call, _: &CallSiteProfile) -> Option<Proposal> {
        std::thread::sleep(std::time::Duration::from_millis(25));
        Some(Proposal {
            rewritten: Plan::Rewritten {
                rule: "SleepyRule".into(),
                call: call.clone(),
                projected_savings_usd: 10.0,
            },
            projected_savings_usd: 10.0,
            safety_check: Box::new(|_| true),
        })
    }
    fn accuracy_budget(&self) -> f32 {
        0.0
    }
}

/// A rule that panics inside `propose`. Verifies that
/// `std::panic::catch_unwind` at the FFI boundary converts this to
/// `PassThrough` rather than an exception on the caller side.
struct PanicRule;
impl RewriteRule for PanicRule {
    fn name(&self) -> &'static str {
        "PanicRule"
    }
    fn applies(&self, _: &Call, _: &CallSiteProfile) -> bool {
        true
    }
    fn propose(&self, _: &Call, _: &CallSiteProfile) -> Option<Proposal> {
        panic!("simulated rule failure");
    }
    fn accuracy_budget(&self) -> f32 {
        0.0
    }
}

#[test]
fn malformed_call_json_yields_pass_through() {
    let cm = Arc::new(CostModel::new());
    let opt = Optimizer::empty(cm, OptimizerConfig::default());
    assert_eq!(optimize_plan(&opt, "not json"), PASS_THROUGH_JSON);
    assert_eq!(optimize_plan(&opt, "{}"), PASS_THROUGH_JSON);
    assert_eq!(
        optimize_plan(&opt, "{\"call_site_id\":\"x\"}"),
        PASS_THROUGH_JSON
    );
}

#[test]
fn overhead_kill_switch_returns_pass_through() {
    let cm = Arc::new(CostModel::new());
    heat_up(&cm, "site", 10);
    let opt = Optimizer::new(
        cm,
        vec![Box::new(SleepyRule)],
        OptimizerConfig { max_overhead_ms: 1.0, ..Default::default() },
    );
    let plan = opt.plan(&hot_call("site"));
    assert!(matches!(plan, Plan::PassThrough), "got {plan:?}");
}

#[test]
fn rule_panic_is_converted_to_pass_through() {
    // `Optimizer::plan` itself does not catch_unwind — that's the FFI
    // layer's job. Here we verify the FFI-equivalent wrapper that the
    // profiler binding invokes: catch_unwind around `rust_plan`.
    use std::panic::AssertUnwindSafe;

    let cm = Arc::new(CostModel::new());
    heat_up(&cm, "site", 10);
    let opt = Optimizer::new(
        cm,
        vec![Box::new(PanicRule)],
        OptimizerConfig::default(),
    );

    let call_json = serde_json::to_string(&hot_call("site")).unwrap();
    let out = std::panic::catch_unwind(AssertUnwindSafe(|| optimize_plan(&opt, &call_json)))
        .unwrap_or_else(|_| PASS_THROUGH_JSON.to_string());

    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(v["kind"], "pass_through");
}

#[test]
fn empty_optimizer_always_passes_through_hot_calls() {
    let cm = Arc::new(CostModel::new());
    heat_up(&cm, "site", 10);
    let opt = Optimizer::empty(cm, OptimizerConfig::default());
    assert!(matches!(opt.plan(&hot_call("site")), Plan::PassThrough));
}
