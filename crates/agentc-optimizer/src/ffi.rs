//! Vendor-free FFI surface.
//!
//! Pure-Rust `optimize_plan`/`optimize_observe` adapters that the PyO3
//! binding in `agentc-profiler::_native` re-exports. The adapters accept
//! JSON strings and — crucially — never panic on malformed input or
//! internal errors: every failure falls through to `{"kind":"pass_through"}`
//! so the caller always receives a valid [`crate::Plan`].
//!
//! Panic trapping is the PyO3 layer's responsibility
//! (`std::panic::catch_unwind`), because that's the only boundary the
//! Python interpreter actually observes.

use std::sync::Arc;

use crate::cost_model::{CostModel, CostModelUpdate};
use crate::dag::{Call, Outcome};
use crate::planner::{Optimizer, Plan};

/// Canonical PassThrough JSON, returned whenever anything goes sideways.
pub const PASS_THROUGH_JSON: &str = "{\"kind\":\"pass_through\"}";

/// Plan a call. Any deserialization or internal failure yields
/// `PASS_THROUGH_JSON`.
pub fn optimize_plan(opt: &Optimizer, call_json: &str) -> String {
    let call: Call = match serde_json::from_str(call_json) {
        Ok(c) => c,
        Err(_) => return PASS_THROUGH_JSON.to_string(),
    };
    let plan = opt.plan(&call);
    serde_json::to_string(&plan).unwrap_or_else(|_| PASS_THROUGH_JSON.to_string())
}

/// Fold the outcome of a dispatched plan into the cost model. Failures
/// are swallowed — the user's call already returned, so there's no way to
/// surface the error anyway.
pub fn optimize_observe(
    cost_model: &Arc<CostModel>,
    plan_json: &str,
    outcome_json: &str,
) -> Result<(), String> {
    let plan: Plan = serde_json::from_str(plan_json).map_err(|e| e.to_string())?;
    let outcome: Outcome = serde_json::from_str(outcome_json).map_err(|e| e.to_string())?;

    // Only Rewritten/Parallel/PassThrough actually carry a call worth
    // attributing; Cached is served from memoization's cache stats, not
    // the optimizer's cost model.
    let call_site_id = match &plan {
        Plan::Rewritten { call, .. } => call.call_site_id.clone(),
        Plan::Parallel { calls, .. } => calls
            .first()
            .map(|c| c.call_site_id.clone())
            .unwrap_or_default(),
        Plan::PassThrough | Plan::Cached { .. } => {
            // PassThrough still feeds the cost model — but the caller
            // must supply the site via a separate path; we can't recover
            // it from a Plan::PassThrough. Treat as a no-op here.
            return Ok(());
        }
    };
    if call_site_id.is_empty() {
        return Ok(());
    }
    cost_model.observe(CostModelUpdate {
        call_site_id,
        input_tokens: outcome.input_tokens,
        output_tokens: outcome.output_tokens,
        latency_ms: outcome.latency_ms,
        cost_usd: outcome.cost_usd,
        output_is_structured: outcome.output_is_structured,
        output_is_short: outcome.output_is_short,
        now_us: None,
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::OptimizerConfig;

    fn empty_optimizer() -> Optimizer {
        Optimizer::empty(Arc::new(CostModel::new()), OptimizerConfig::default())
    }

    #[test]
    fn malformed_call_json_yields_pass_through() {
        let s = optimize_plan(&empty_optimizer(), "not json");
        assert_eq!(s, PASS_THROUGH_JSON);
    }

    #[test]
    fn valid_call_cold_site_yields_pass_through() {
        let call = serde_json::json!({
            "call_site_id": "site-x",
            "trace_id": "00".repeat(16),
            "span_id": "00".repeat(8),
            "model": "gpt-4o",
            "messages": [],
        });
        let s = optimize_plan(&empty_optimizer(), &call.to_string());
        // Valid round-trip, but cold ⇒ still pass_through.
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["kind"], "pass_through");
    }

    #[test]
    fn observe_on_pass_through_is_noop() {
        let cm = Arc::new(CostModel::new());
        let plan = Plan::PassThrough;
        let outcome = Outcome {
            input_tokens: 1,
            output_tokens: 1,
            latency_ms: 1.0,
            cost_usd: 0.001,
            output_is_structured: false,
            output_is_short: true,
        };
        let ok = optimize_observe(
            &cm,
            &serde_json::to_string(&plan).unwrap(),
            &serde_json::to_string(&outcome).unwrap(),
        );
        assert!(ok.is_ok());
        assert!(cm.get("anything").is_none());
    }
}
