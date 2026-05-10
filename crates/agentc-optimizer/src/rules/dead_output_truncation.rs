//! `DeadOutputTruncation` — cap generation on call sites whose prior outputs
//! are historically unreferenced (dead store elimination).
//!
//! Compiler analog: dead store elimination. A call site whose chain-of-thought
//! output never appears in any downstream input is a dead store; we cap its
//! generation at MAX_DEAD_OUTPUT_TOKENS rather than letting it run free.
//!
//! Cost driver: OutputTokens (orthogonal to InputTokens rules and ModelPrice).
//! Safe to compose with ContextCompress, StateDrop, PromptDedup,
//! StructuredTruncation (all InputTokens), and ModelDowngrade (ModelPrice).
//!
//! Safety: only fires when the TraceOptimizer has explicitly set
//! `parameters.extra.output_is_dead_branch = true` on this call. The first
//! invocation of any call site is always pass-through (cold-site contract).
//! Never tightens a cap already below MAX_DEAD_OUTPUT_TOKENS.

use crate::cost_model::CallSiteProfile;
use crate::dag::Call;
use crate::planner::{CostDriver, Plan, Proposal, RewriteRule};

pub const DEFAULT_ACCURACY_BUDGET: f32 = 0.05;
/// Hard cap applied to dead-output call sites (tokens).
pub const MAX_DEAD_OUTPUT_TOKENS: u32 = 150;

pub struct DeadOutputTruncationRule {
    accuracy_budget: f32,
}

impl Default for DeadOutputTruncationRule {
    fn default() -> Self {
        Self { accuracy_budget: DEFAULT_ACCURACY_BUDGET }
    }
}

fn is_dead_branch(call: &Call) -> bool {
    call.parameters
        .extra
        .as_object()
        .and_then(|o| o.get("output_is_dead_branch"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

impl RewriteRule for DeadOutputTruncationRule {
    fn name(&self) -> &'static str {
        "DeadOutputTruncation"
    }

    fn applies(&self, call: &Call, _profile: &CallSiteProfile) -> bool {
        if !is_dead_branch(call) {
            return false;
        }
        // Worthwhile only if the current cap is looser than our target.
        match call.parameters.max_output_tokens {
            Some(current) => current > MAX_DEAD_OUTPUT_TOKENS,
            None => true,
        }
    }

    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal> {
        if !is_dead_branch(call) {
            return None;
        }
        let baseline = call.parameters.max_output_tokens.unwrap_or(2048);
        if baseline <= MAX_DEAD_OUTPUT_TOKENS {
            return None;
        }

        let output_fraction =
            baseline.saturating_sub(MAX_DEAD_OUTPUT_TOKENS) as f32 / baseline.max(1) as f32;
        let projected = profile.cost_usd.mean as f32 * output_fraction * 0.5;

        let mut rewritten = call.clone();
        rewritten.parameters.max_output_tokens = Some(MAX_DEAD_OUTPUT_TOKENS);
        Some(Proposal {
            rewritten: Plan::Rewritten {
                rule: self.name().to_string(),
                call: rewritten,
                projected_savings_usd: projected,
            },
            projected_savings_usd: projected,
            cost_driver: CostDriver::OutputTokens,
            safety_check: Box::new(|c| {
                c.parameters
                    .max_output_tokens
                    .map(|m| m >= MAX_DEAD_OUTPUT_TOKENS)
                    .unwrap_or(true)
            }),
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
    use serde_json::json;

    fn hot_profile() -> CallSiteProfile {
        let mut p = CallSiteProfile::new("site");
        p.n_observations = 20;
        p.cost_usd = WelfordStats::from_persisted(20, 0.01, 0.0);
        p
    }

    fn dead_call() -> Call {
        let mut c = Call {
            call_site_id: "site".into(),
            trace_id: [0u8; 16],
            span_id: [0u8; 8],
            model: "gpt-4o".into(),
            messages: vec![],
            parameters: Parameters::default(),
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        };
        c.parameters.extra = json!({"output_is_dead_branch": true});
        c
    }

    fn live_call() -> Call {
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

    fn capped_dead_call(cap: u32) -> Call {
        let mut c = dead_call();
        c.parameters.max_output_tokens = Some(cap);
        c
    }

    #[test]
    fn does_not_fire_without_dead_branch_flag() {
        assert!(!DeadOutputTruncationRule::default().applies(&live_call(), &hot_profile()));
    }

    #[test]
    fn fires_on_dead_branch_unbounded() {
        assert!(DeadOutputTruncationRule::default().applies(&dead_call(), &hot_profile()));
    }

    #[test]
    fn fires_when_existing_cap_is_loose() {
        assert!(
            DeadOutputTruncationRule::default().applies(&capped_dead_call(500), &hot_profile())
        );
    }

    #[test]
    fn does_not_fire_when_cap_already_tight() {
        // cap = MAX_DEAD_OUTPUT_TOKENS — no improvement.
        assert!(!DeadOutputTruncationRule::default()
            .applies(&capped_dead_call(MAX_DEAD_OUTPUT_TOKENS), &hot_profile()));
    }

    #[test]
    fn proposal_sets_cap_to_max_dead_output_tokens() {
        let prop = DeadOutputTruncationRule::default()
            .propose(&dead_call(), &hot_profile())
            .expect("must propose");
        match &prop.rewritten {
            Plan::Rewritten { call, .. } => {
                assert_eq!(call.parameters.max_output_tokens, Some(MAX_DEAD_OUTPUT_TOKENS));
            }
            _ => panic!("expected Rewritten"),
        }
    }

    #[test]
    fn projected_savings_is_positive() {
        let prop = DeadOutputTruncationRule::default()
            .propose(&dead_call(), &hot_profile())
            .expect("must propose");
        assert!(prop.projected_savings_usd > 0.0);
    }

    #[test]
    fn safety_check_rejects_cap_below_floor() {
        let prop = DeadOutputTruncationRule::default()
            .propose(&dead_call(), &hot_profile())
            .expect("must propose");
        let mut bad = dead_call();
        bad.parameters.max_output_tokens = Some(10);
        assert!(!(prop.safety_check)(&bad));
    }

    #[test]
    fn live_call_returns_no_proposal() {
        assert!(DeadOutputTruncationRule::default().propose(&live_call(), &hot_profile()).is_none());
    }
}
