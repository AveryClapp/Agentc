//! `OutputBudget` — cap output tokens at ceil(p99 × 1.2) on hot call sites.
//!
//! Compiler analog: strength reduction. Replace an unconstrained generation
//! call with a bounded one. The empirical p99 output distribution provides
//! a tight safe ceiling — 99% of real responses fit within it.
//!
//! Cost driver: OutputTokens — orthogonal to ContextCompress (InputTokens)
//! and ModelDowngrade (ModelPrice). Safe to compose with both.
//!
//! Safety: the proposed cap must be ≥ MIN_OUTPUT_CAP (64 tokens) and must
//! not undercut the existing cap if one is already tighter than p99.

use crate::cost_model::CallSiteProfile;
use crate::dag::Call;
use crate::planner::{Plan, Proposal, RewriteRule};

pub const DEFAULT_ACCURACY_BUDGET: f32 = 0.01;
pub const P99_SAFETY_MULTIPLE: f32 = 1.2;
pub const MIN_OUTPUT_CAP: u32 = 64;
/// Assumed token budget when the call has no explicit max_output_tokens set.
const ASSUMED_UNCAPPED_TOKENS: u32 = 2048;

pub struct OutputBudgetRule {
    accuracy_budget: f32,
}

impl Default for OutputBudgetRule {
    fn default() -> Self {
        Self { accuracy_budget: DEFAULT_ACCURACY_BUDGET }
    }
}

impl RewriteRule for OutputBudgetRule {
    fn name(&self) -> &'static str {
        "OutputBudget"
    }

    fn applies(&self, call: &Call, profile: &CallSiteProfile) -> bool {
        let p99 = profile.output_token_p99;
        if p99 <= 0.0 {
            return false;
        }
        let proposed_cap =
            ((p99 * P99_SAFETY_MULTIPLE).ceil() as u32).max(MIN_OUTPUT_CAP);
        match call.parameters.max_output_tokens {
            Some(current) => {
                if current < p99 as u32 {
                    return false; // existing cap already cuts into p99 — unsafe
                }
                current > proposed_cap // worthwhile only if we'd tighten it
            }
            None => true, // unbounded call — always worthwhile to cap
        }
    }

    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal> {
        let p99 = profile.output_token_p99;
        if p99 <= 0.0 {
            return None;
        }
        let cap = ((p99 * P99_SAFETY_MULTIPLE).ceil() as u32).max(MIN_OUTPUT_CAP);
        let baseline = call.parameters.max_output_tokens.unwrap_or(ASSUMED_UNCAPPED_TOKENS);
        if baseline <= cap {
            return None;
        }
        // Output tokens are roughly half the call cost. We scale down by 0.5
        // to avoid double-counting with input-side rules.
        let output_fraction = baseline.saturating_sub(cap) as f32 / baseline.max(1) as f32;
        let projected = profile.cost_usd.mean as f32 * output_fraction * 0.5;

        let mut rewritten = call.clone();
        rewritten.parameters.max_output_tokens = Some(cap);
        Some(Proposal {
            rewritten: Plan::Rewritten {
                rule: self.name().to_string(),
                call: rewritten,
                projected_savings_usd: projected,
            },
            projected_savings_usd: projected,
            safety_check: Box::new(move |c| {
                // Must not have been externally capped below our proposed floor.
                c.parameters
                    .max_output_tokens
                    .map(|m| m >= MIN_OUTPUT_CAP)
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

    fn hot_profile_with_p99(p99: f32) -> CallSiteProfile {
        let mut p = CallSiteProfile::new("site");
        p.n_observations = 20;
        p.cost_usd = WelfordStats::from_persisted(20, 0.01, 0.0);
        p.output_token_p99 = p99;
        p
    }

    fn unbounded_call() -> Call {
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

    fn capped_call(cap: u32) -> Call {
        let mut c = unbounded_call();
        c.parameters.max_output_tokens = Some(cap);
        c
    }

    #[test]
    fn fires_when_unbounded_and_p99_is_positive() {
        assert!(OutputBudgetRule::default().applies(&unbounded_call(), &hot_profile_with_p99(100.0)));
    }

    #[test]
    fn does_not_fire_when_p99_is_zero() {
        assert!(!OutputBudgetRule::default().applies(&unbounded_call(), &hot_profile_with_p99(0.0)));
    }

    #[test]
    fn does_not_fire_when_existing_cap_below_p99() {
        // cap=50, p99=100 → existing cap already cuts into the p99 range → unsafe.
        assert!(!OutputBudgetRule::default().applies(&capped_call(50), &hot_profile_with_p99(100.0)));
    }

    #[test]
    fn does_not_fire_when_existing_cap_already_tight() {
        // proposed = ceil(100 * 1.2) = 120; cap=120 → no improvement.
        assert!(!OutputBudgetRule::default().applies(&capped_call(120), &hot_profile_with_p99(100.0)));
    }

    #[test]
    fn fires_when_existing_cap_is_loose() {
        // proposed = 120; cap=500 → 500 > 120 → fires.
        assert!(OutputBudgetRule::default().applies(&capped_call(500), &hot_profile_with_p99(100.0)));
    }

    #[test]
    fn proposal_sets_cap_near_ceil_p99_times_1_2() {
        let prop = OutputBudgetRule::default()
            .propose(&unbounded_call(), &hot_profile_with_p99(200.0))
            .expect("must propose");
        match &prop.rewritten {
            Plan::Rewritten { call, .. } => {
                let cap = call.parameters.max_output_tokens.unwrap();
                // f32 arithmetic: 200 * 1.2 may land at 240 or 241 depending on
                // the platform's rounding for 1.2_f32 (= 1.2000000476...).
                assert!(cap >= 240 && cap <= 242, "cap={cap}, expected ~240");
            }
            _ => panic!("expected Rewritten"),
        }
    }

    #[test]
    fn proposal_floor_is_min_output_cap() {
        // p99=1 → ceil(1.2)=2, but MIN_OUTPUT_CAP=64.
        let prop = OutputBudgetRule::default()
            .propose(&unbounded_call(), &hot_profile_with_p99(1.0))
            .expect("must propose");
        match &prop.rewritten {
            Plan::Rewritten { call, .. } => {
                assert!(call.parameters.max_output_tokens.unwrap() >= MIN_OUTPUT_CAP);
            }
            _ => panic!(),
        }
    }

    #[test]
    fn safety_check_rejects_call_already_capped_below_floor() {
        let prop = OutputBudgetRule::default()
            .propose(&unbounded_call(), &hot_profile_with_p99(200.0))
            .expect("must propose");
        // Simulate a race: external code dropped the cap to 10 (below MIN_OUTPUT_CAP).
        let mut bad_call = unbounded_call();
        bad_call.parameters.max_output_tokens = Some(10);
        assert!(!(prop.safety_check)(&bad_call));
    }

    #[test]
    fn projected_savings_is_positive() {
        let prop = OutputBudgetRule::default()
            .propose(&unbounded_call(), &hot_profile_with_p99(200.0))
            .expect("must propose");
        assert!(prop.projected_savings_usd > 0.0);
    }
}
