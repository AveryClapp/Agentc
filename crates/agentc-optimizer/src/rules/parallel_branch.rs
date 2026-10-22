//! `ParallelBranch` — dispatch consecutive independent calls concurrently.
//!
//! Spec § Rule specifications > ParallelBranch:
//! - Fires when the last-executed N spans in the trace contain ≥ 2
//!   consecutive `LlmOutput`/`ToolOutput` calls whose `input_deps` are
//!   disjoint.
//! - Safety: disjointness must hold on exact `DepSource` annotations; no
//!   heuristic overlap.
//! - Accuracy budget: 0.0 — any divergence is a bug.
//!
//! In this milestone the rule reads the current call's `input_deps` and
//! the most recent peer's provenance (passed via the `peer_deps` slot on
//! the proposal). The Python interceptor is responsible for populating
//! `call.input_deps` and, via `DagContextCache`, the peer's. The rule
//! itself only performs the disjointness check + rewrite.
//!
//! The planner doesn't give rules direct access to `DagContextCache`;
//! instead, the executor stages the Parallel plan over the *current*
//! call plus any sibling it already decided belongs with it. For this
//! bead we model that as: `ParallelBranchRule` fires whenever the call's
//! `parameters.extra` carries a `"parallel_peer": {...}` object whose
//! `input_deps` are disjoint from the call's own.
//!
//! This is the minimum spec-aligned shape that unblocks the rule; a
//! richer DAG-level planner can grow from here without touching the
//! safety math.

use serde::Deserialize;

use crate::cost_model::CallSiteProfile;
use crate::dag::{Call, DepSource};
use crate::planner::{CostDriver, Plan, Proposal, RewriteRule};

pub const DEFAULT_ACCURACY_BUDGET: f32 = 0.0;

pub struct ParallelBranchRule {
    max_fanout: usize,
    accuracy_budget: f32,
}

impl Default for ParallelBranchRule {
    fn default() -> Self {
        Self {
            max_fanout: 4,
            accuracy_budget: DEFAULT_ACCURACY_BUDGET,
        }
    }
}

impl ParallelBranchRule {
    pub fn new(max_fanout: usize) -> Self {
        Self {
            max_fanout,
            accuracy_budget: DEFAULT_ACCURACY_BUDGET,
        }
    }
}

impl RewriteRule for ParallelBranchRule {
    fn identity(&self) -> Option<crate::planner::RuleIdentity> {
        crate::planner::RuleIdentity::new(
            "parallel-independence-certificate-v1",
            self.accuracy_budget,
            serde_json::json!({
                "max_fanout": self.max_fanout,
            }),
        )
    }

    fn name(&self) -> &'static str {
        "ParallelBranch"
    }

    fn applies(&self, call: &Call, _profile: &CallSiteProfile) -> bool {
        // Cheap gate: we must have at least one non-literal dep or
        // there's nothing to reason about. And we need a peer entry on
        // `extra`.
        if call.input_deps.is_empty() {
            return false;
        }
        peer_from_call(call).is_some()
    }

    fn propose(&self, call: &Call, _profile: &CallSiteProfile) -> Option<Proposal> {
        let peer = peer_from_call(call)?;
        // Safety demands we can prove disjointness. Two `Literal` deps
        // are trivially disjoint by type, but they carry no provenance
        // at all — firing on an all-literal call is effectively
        // unproven. Require at least one non-literal dep on each side.
        if !has_concrete_dep(&call.input_deps) || !has_concrete_dep(&peer.input_deps) {
            return None;
        }
        if !deps_are_disjoint(&call.input_deps, &peer.input_deps) {
            return None;
        }

        // Build the parallel plan: current call + the peer. The peer was
        // serialized as a `Call` by the interceptor, so we can reuse its
        // shape here.
        let calls = vec![call.clone(), peer.into_call(call)];
        if calls.len() > self.max_fanout {
            return None;
        }

        // Reordering preserves the billed calls, so projected dollar savings
        // are zero. The greedy cost ranking must prefer actual cost reductions;
        // the joint selector can assess latency from complete-plan observations.
        let projected = 0.0;

        Some(Proposal {
            rewritten: Plan::Parallel {
                rule: self.name().to_string(),
                calls,
                projected_savings_usd: projected,
            },
            projected_savings_usd: projected,
            cost_driver: CostDriver::Structural,
            safety_check: Box::new(|call| {
                // Final safety gate: re-verify disjointness + concrete
                // deps at commit time.
                if let Some(peer) = peer_from_call(call) {
                    has_concrete_dep(&call.input_deps)
                        && has_concrete_dep(&peer.input_deps)
                        && deps_are_disjoint(&call.input_deps, &peer.input_deps)
                } else {
                    false
                }
            }),
        })
    }

    fn accuracy_budget(&self) -> f32 {
        self.accuracy_budget
    }
}

/// The peer call shape stashed on `Call.parameters.extra.parallel_peer`
/// by the Python interceptor. Holds only the dep info + model/messages
/// we need to reconstruct a `Call`. Everything else inherits the
/// primary call's values.
#[derive(Debug, Clone, Deserialize)]
struct Peer {
    #[serde(default)]
    call_site_id: Option<String>,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    messages: Vec<crate::dag::Message>,
    #[serde(default)]
    input_deps: Vec<DepSource>,
}

impl Peer {
    fn into_call(self, base: &Call) -> Call {
        Call {
            call_site_id: self
                .call_site_id
                .unwrap_or_else(|| base.call_site_id.clone()),
            trace_id: base.trace_id,
            span_id: base.span_id,
            model: self.model.unwrap_or_else(|| base.model.clone()),
            messages: if self.messages.is_empty() {
                base.messages.clone()
            } else {
                self.messages
            },
            parameters: base.parameters.clone(),
            tools: base.tools.clone(),
            input_deps: self.input_deps,
            occurrence_ix: base.occurrence_ix.saturating_add(1),
        }
    }
}

fn peer_from_call(call: &Call) -> Option<Peer> {
    let extra = &call.parameters.extra;
    if extra.is_null() {
        return None;
    }
    let obj = extra.as_object()?;
    let peer = obj.get("parallel_peer")?.clone();
    serde_json::from_value::<Peer>(peer).ok()
}

/// Disjoint ⇔ no span-level overlap. `Literal` never overlaps (no
/// upstream node to share); `State` overlaps iff keys match; everything
/// else overlaps iff `span_id` matches.
fn deps_are_disjoint(a: &[DepSource], b: &[DepSource]) -> bool {
    for x in a {
        for y in b {
            if deps_overlap(x, y) {
                return false;
            }
        }
    }
    true
}

fn has_concrete_dep(deps: &[DepSource]) -> bool {
    deps.iter().any(|d| !matches!(d, DepSource::Literal))
}

fn deps_overlap(x: &DepSource, y: &DepSource) -> bool {
    match (x, y) {
        (DepSource::Literal, _) | (_, DepSource::Literal) => false,
        (DepSource::State { key: a }, DepSource::State { key: b }) => a == b,
        (DepSource::UserInput { span_id: a }, DepSource::UserInput { span_id: b })
        | (DepSource::ToolOutput { span_id: a }, DepSource::ToolOutput { span_id: b })
        | (DepSource::LlmOutput { span_id: a }, DepSource::LlmOutput { span_id: b }) => a == b,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::OptimizerConfig;
    use crate::cost_model::{CostModel, CostModelUpdate, WelfordStats};
    use crate::dag::{Message, Parameters};
    use crate::planner::Optimizer;
    use crate::rules::output_budget::OutputBudgetRule;
    use serde_json::json;
    use std::sync::Arc;

    fn hot_profile() -> CallSiteProfile {
        let mut p = CallSiteProfile::new("site");
        p.n_observations = 10;
        p.cost_usd = WelfordStats::from_persisted(10, 0.01, 0.0);
        p.latency_ms = WelfordStats::from_persisted(10, 200.0, 0.0);
        p
    }

    fn base_call() -> Call {
        Call {
            call_site_id: "site".into(),
            trace_id: [0u8; 16],
            span_id: [1u8; 8],
            model: "gpt-4o".into(),
            messages: vec![Message {
                role: "user".into(),
                content: "hi".into(),
            }],
            parameters: Parameters::default(),
            tools: vec![],
            input_deps: vec![DepSource::UserInput { span_id: [2u8; 8] }],
            occurrence_ix: 0,
        }
    }

    fn with_peer(call: Call, peer_input_deps: Vec<DepSource>) -> Call {
        let peer = json!({
            "parallel_peer": {
                "call_site_id": "site-peer",
                "model": "gpt-4o",
                "messages": [],
                "input_deps": peer_input_deps,
            }
        });
        Call {
            parameters: Parameters {
                extra: peer,
                ..Default::default()
            },
            ..call
        }
    }

    #[test]
    fn disjoint_deps_fire_parallel() {
        let call = with_peer(
            base_call(),
            vec![DepSource::UserInput { span_id: [3u8; 8] }],
        );
        let rule = ParallelBranchRule::default();
        let proposal = rule.propose(&call, &hot_profile()).expect("must fire");
        match proposal.rewritten {
            Plan::Parallel { calls, .. } => assert_eq!(calls.len(), 2),
            _ => panic!("expected Plan::Parallel"),
        }
    }

    #[test]
    fn projection_never_converts_latency_to_money() {
        let call = with_peer(
            base_call(),
            vec![DepSource::UserInput { span_id: [3u8; 8] }],
        );
        for cost in [0.0, 0.0001, 1000.0] {
            let mut profile = hot_profile();
            profile.cost_usd = WelfordStats::from_persisted(10, cost, 0.0);
            let proposal = ParallelBranchRule::default()
                .propose(&call, &profile)
                .unwrap();
            assert_eq!(proposal.projected_savings_usd, 0.0);
            match proposal.rewritten {
                Plan::Parallel {
                    projected_savings_usd,
                    ..
                } => {
                    assert_eq!(projected_savings_usd, 0.0);
                }
                _ => panic!("expected parallel observation"),
            }
        }
    }

    fn warmed_model() -> Arc<CostModel> {
        let model = Arc::new(CostModel::new());
        for _ in 0..20 {
            model.observe(CostModelUpdate {
                call_site_id: "site".into(),
                input_tokens: 100,
                output_tokens: 50,
                latency_ms: 200.0,
                cost_usd: 0.01,
                output_is_structured: false,
                output_is_short: true,
                now_us: Some(0),
            });
        }
        model
    }

    #[test]
    fn monetary_rules_precede_parallel_observation_in_greedy_planner() {
        let mut call = with_peer(
            base_call(),
            vec![DepSource::UserInput { span_id: [3u8; 8] }],
        );
        call.parameters.max_output_tokens = Some(1024);
        for compose in [false, true] {
            for parallel_first in [false, true] {
                let mut rules: Vec<Box<dyn RewriteRule>> = vec![
                    Box::new(ParallelBranchRule::default()),
                    Box::new(OutputBudgetRule::default()),
                ];
                if !parallel_first {
                    rules.reverse();
                }
                let optimizer = Optimizer::new(
                    warmed_model(),
                    rules,
                    OptimizerConfig {
                        compose,
                        max_overhead_ms: 1000.0,
                        ..Default::default()
                    },
                );
                match optimizer.plan(&call) {
                    Plan::Rewritten {
                        rule,
                        call: rewritten,
                        projected_savings_usd,
                    } => {
                        assert_eq!(rule, "OutputBudget");
                        assert!(rewritten.parameters.max_output_tokens.unwrap() < 1024);
                        assert!(projected_savings_usd > 0.0 && projected_savings_usd < 0.01);
                    }
                    _ => panic!("real dollar savings must precede a parallel observation"),
                }
            }
        }
    }

    #[test]
    fn zero_cost_parallel_candidate_remains_in_joint_enumeration() {
        let call = with_peer(
            base_call(),
            vec![DepSource::UserInput { span_id: [3u8; 8] }],
        );
        let optimizer = Optimizer::new(
            warmed_model(),
            vec![Box::new(ParallelBranchRule::default())],
            OptimizerConfig {
                max_overhead_ms: 1000.0,
                ..Default::default()
            },
        );
        let catalog = crate::model_catalog::default_model_catalog().unwrap();
        let plans = optimizer.candidate_plans(&call, &catalog);
        assert!(plans.iter().any(|plan| matches!(plan, Plan::PassThrough)));
        assert!(plans.iter().any(|plan| matches!(
            plan,
            Plan::Parallel {
                projected_savings_usd: 0.0,
                ..
            }
        )));
    }

    #[test]
    fn shared_span_id_blocks_fire() {
        // Same span id on both sides → not disjoint.
        let call = with_peer(
            base_call(),
            vec![DepSource::UserInput { span_id: [2u8; 8] }],
        );
        let rule = ParallelBranchRule::default();
        assert!(rule.propose(&call, &hot_profile()).is_none());
    }

    #[test]
    fn no_peer_no_proposal() {
        let rule = ParallelBranchRule::default();
        assert!(rule.propose(&base_call(), &hot_profile()).is_none());
    }

    #[test]
    fn literal_deps_never_block() {
        let mut call = base_call();
        call.input_deps = vec![DepSource::Literal];
        let call = with_peer(call, vec![DepSource::Literal]);
        let rule = ParallelBranchRule::default();
        // Literals are non-overlapping (no upstream node).
        // But the rule requires at least one non-literal dep to fire.
        // We want safety: purely-literal calls can't be parallelized
        // because there's no provenance at all — mis-tagged as safe is
        // risky. Rule elects not to fire.
        assert!(rule.propose(&call, &hot_profile()).is_none());
    }

    #[test]
    fn safety_check_recomputes_disjointness() {
        let call = with_peer(
            base_call(),
            vec![DepSource::UserInput { span_id: [3u8; 8] }],
        );
        let rule = ParallelBranchRule::default();
        let proposal = rule.propose(&call, &hot_profile()).unwrap();
        assert!((proposal.safety_check)(&call));
    }

    #[test]
    fn accuracy_budget_is_zero() {
        assert_eq!(ParallelBranchRule::default().accuracy_budget(), 0.0);
    }
}
