//! `CompositionPlanner` — multi-pass rule application.
//!
//! Selects which proposals to compose based on cost driver orthogonality and
//! an explicit safe/unsafe pair table. Applies proposals in dependency order.
//! Produces either `Plan::Rewritten` (solo) or `Plan::Composed` (multi-rule).
//!
//! Compiler analog: multi-pass compilation. Each pass (rule) targets an
//! orthogonal cost dimension; chaining them compounds savings the same way
//! a compiler chains register allocation, dead-code elimination, and
//! strength reduction.
//!
//! Safety proof: see "Composition Safety Proof" in docs/plans/2026-05-10-agentc-v2.md.
//! Two rules with distinct `CostDriver` values modify non-overlapping `Call` fields
//! (messages vs. max_output_tokens vs. model), so their rewrites commute.
//!
//! Note: `apply_rewrite` merges by field: model, max_output_tokens, and messages
//! (by count change). Content-only message mutations (StructuredTruncation) are
//! not carried through in the multi-rule path — hence `(ContextCompress,
//! StructuredTruncation)` is explicitly unsafe.

use crate::dag::Call;
use crate::planner::{CostDriver, Plan, Proposal, RuleApplication};

/// Explicitly safe same-driver pairs. `(A, B)` means A can compose with B
/// even though they share a cost driver. A runs before B (dependency order).
const EXPLICIT_SAFE: &[(&str, &str)] = &[
    ("PromptDedup", "ContextCompress"),
    ("StateDrop", "ContextCompress"),
    ("StateDrop", "OutputBudget"),
];

/// Explicitly unsafe pairs — cannot compose even with different drivers.
const EXPLICIT_UNSAFE: &[(&str, &str)] = &[
    ("StateDrop", "PromptDedup"),
    ("PromptDedup", "StateDrop"),
    ("ContextCompress", "StructuredTruncation"),
    ("StructuredTruncation", "ContextCompress"),
];

fn sort_key(rule: &str) -> usize {
    match rule {
        "StateDrop" => 0,
        "PromptDedup" => 1,
        "ContextCompress" => 2,
        "StructuredTruncation" => 3,
        "OutputBudget" => 4,
        "DeadOutputTruncation" => 5,
        "ModelDowngrade" => 6,
        "PrefixAlign" => 7,
        _ => 99,
    }
}

pub struct CompositionResult {
    pub plan: Plan,
    pub rules_applied: Vec<RuleApplication>,
    pub net_savings_usd: f32,
}

/// Select and apply a compatible subset of proposals.
///
/// Expects proposals sorted by `projected_savings_usd` descending (highest
/// first) — the planner does this before calling. Selection is greedy;
/// dependency order governs application order, not savings rank.
///
/// When the selected proposal(s) all fail safety checks, falls back to V1
/// behavior: iterate proposals in savings order and return the first one
/// whose safety check passes. This preserves the V1 "first safety-check
/// pass wins" invariant.
pub fn compose_proposals(proposals: Vec<(String, Proposal)>, call: &Call) -> CompositionResult {
    if proposals.is_empty() {
        return passthrough();
    }

    // Select a compatible subset.  Proposals rejected by a driver conflict or
    // unsafe-pair rule are stored in `unselected` for the V1 safety fallback.
    let mut selected: Vec<(String, Proposal)> = Vec::new();
    let mut unselected: Vec<(String, Proposal)> = Vec::new();
    for (name, prop) in proposals {
        // CallElimination is always solo: a cache hit supersedes all rewrites.
        if prop.cost_driver == CostDriver::CallElimination {
            if selected.is_empty() {
                selected.push((name, prop));
            } else {
                unselected.push((name, prop));
            }
            break;
        }
        // Reject explicitly unsafe pairings with already-selected rules.
        let explicitly_unsafe =
            selected.iter().any(|(sel_name, _)| is_explicit_unsafe(sel_name, &name));
        if explicitly_unsafe {
            unselected.push((name, prop));
            continue;
        }
        // Require driver uniqueness OR an explicit safe allowlist entry.
        let driver_conflict = selected.iter().any(|(sel_name, sel_prop)| {
            sel_prop.cost_driver == prop.cost_driver && !is_explicit_safe(sel_name, &name)
        });
        if driver_conflict {
            unselected.push((name, prop));
            continue;
        }
        selected.push((name, prop));
    }

    if selected.is_empty() {
        return passthrough();
    }

    // Solo path — skip composition overhead.
    if selected.len() == 1 {
        let (name, prop) = selected.remove(0);
        if (prop.safety_check)(call) {
            let savings = prop.projected_savings_usd;
            let driver = prop.cost_driver;
            return CompositionResult {
                plan: prop.rewritten,
                rules_applied: vec![RuleApplication {
                    rule: name,
                    projected_savings_usd: savings,
                    cost_driver: driver,
                }],
                net_savings_usd: savings,
            };
        }
        // Safety failed — V1 fallback: try the next unselected proposal.
        return compose_proposals(unselected, call);
    }

    // Multi-rule path: apply in dependency order.
    selected.sort_by_key(|(name, _)| sort_key(name));

    let mut current_call = call.clone();
    let mut rules_applied: Vec<RuleApplication> = Vec::new();
    let mut net_savings = 0.0f32;

    for (name, prop) in &selected {
        if !(prop.safety_check)(&current_call) {
            continue;
        }
        if let Plan::Rewritten { call: rewritten, projected_savings_usd, .. } = &prop.rewritten {
            current_call = apply_rewrite(&current_call, rewritten);
            net_savings += projected_savings_usd;
            rules_applied.push(RuleApplication {
                rule: name.clone(),
                projected_savings_usd: *projected_savings_usd,
                cost_driver: prop.cost_driver,
            });
        }
    }

    if rules_applied.is_empty() {
        // All selected proposals failed safety — V1 fallback.
        return compose_proposals(unselected, call);
    }
    if rules_applied.len() == 1 {
        let r = &rules_applied[0];
        return CompositionResult {
            plan: Plan::Rewritten {
                rule: r.rule.clone(),
                call: current_call,
                projected_savings_usd: r.projected_savings_usd,
            },
            rules_applied,
            net_savings_usd: net_savings,
        };
    }

    CompositionResult {
        plan: Plan::Composed {
            rules: rules_applied.clone(),
            call: current_call,
            net_savings_usd: net_savings,
        },
        rules_applied,
        net_savings_usd: net_savings,
    }
}

fn passthrough() -> CompositionResult {
    CompositionResult { plan: Plan::PassThrough, rules_applied: vec![], net_savings_usd: 0.0 }
}

fn is_explicit_safe(a: &str, b: &str) -> bool {
    EXPLICIT_SAFE.iter().any(|(x, y)| (*x == a && *y == b) || (*x == b && *y == a))
}

fn is_explicit_unsafe(a: &str, b: &str) -> bool {
    EXPLICIT_UNSAFE.iter().any(|(x, y)| (*x == a && *y == b) || (*x == b && *y == a))
}

/// Merge mutations from `proposed` into `current` by field.
///
/// Merges model, max_output_tokens (takes tighter cap), and messages
/// only when the proposed call has strictly fewer messages than the
/// current (a drop). Expansions — proposed carrying the original message
/// list after a prior rule already reduced it — are intentionally
/// ignored so that a parameter-only rule (e.g. OutputBudget) can
/// compose without undoing an earlier message-drop rule (e.g. StateDrop
/// or ContextCompress). Content-only mutations (same count, different
/// content) are likewise not merged — those must run solo or via a
/// different composition strategy.
fn apply_rewrite(current: &Call, proposed: &Call) -> Call {
    let mut result = current.clone();
    if proposed.model != current.model {
        result.model = proposed.model.clone();
    }
    match (current.parameters.max_output_tokens, proposed.parameters.max_output_tokens) {
        (Some(a), Some(b)) => result.parameters.max_output_tokens = Some(a.min(b)),
        (None, Some(b)) => result.parameters.max_output_tokens = Some(b),
        _ => {}
    }
    if proposed.messages.len() < current.messages.len() {
        result.messages = proposed.messages.clone();
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dag::{Message, Parameters};

    fn make_call() -> Call {
        Call {
            call_site_id: "site".into(),
            trace_id: [0u8; 16],
            span_id: [0u8; 8],
            model: "gpt-4o".into(),
            messages: vec![
                Message { role: "system".into(), content: "sys".into() },
                Message { role: "user".into(), content: "hello".into() },
            ],
            parameters: Parameters::default(),
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        }
    }

    fn make_prop(name: &str, driver: CostDriver, savings: f32) -> (String, Proposal) {
        let call = make_call();
        (
            name.to_string(),
            Proposal {
                rewritten: Plan::Rewritten {
                    rule: name.to_string(),
                    call: call.clone(),
                    projected_savings_usd: savings,
                },
                projected_savings_usd: savings,
                cost_driver: driver,
                safety_check: Box::new(|_| true),
            },
        )
    }

    #[test]
    fn orthogonal_drivers_compose() {
        let proposals = vec![
            make_prop("ContextCompress", CostDriver::InputTokens, 0.5),
            make_prop("OutputBudget", CostDriver::OutputTokens, 0.3),
        ];
        let result = compose_proposals(proposals, &make_call());
        assert_eq!(result.rules_applied.len(), 2);
        assert!(result.rules_applied.iter().any(|r| r.rule == "ContextCompress"));
        assert!(result.rules_applied.iter().any(|r| r.rule == "OutputBudget"));
    }

    #[test]
    fn call_elimination_is_always_solo() {
        let proposals = vec![
            make_prop("CacheHit", CostDriver::CallElimination, 1.0),
            make_prop("ContextCompress", CostDriver::InputTokens, 0.5),
        ];
        let result = compose_proposals(proposals, &make_call());
        assert_eq!(result.rules_applied.len(), 1);
        assert_eq!(result.rules_applied[0].rule, "CacheHit");
    }

    #[test]
    fn unsafe_pair_keeps_first_match() {
        let proposals = vec![
            make_prop("StateDrop", CostDriver::InputTokens, 0.5),
            make_prop("PromptDedup", CostDriver::InputTokens, 0.3),
        ];
        let result = compose_proposals(proposals, &make_call());
        assert_eq!(result.rules_applied.len(), 1);
        assert_eq!(result.rules_applied[0].rule, "StateDrop");
    }

    #[test]
    fn explicit_safe_pair_composes_despite_same_driver() {
        let proposals = vec![
            make_prop("ContextCompress", CostDriver::InputTokens, 0.5),
            make_prop("PromptDedup", CostDriver::InputTokens, 0.3),
        ];
        let result = compose_proposals(proposals, &make_call());
        assert_eq!(result.rules_applied.len(), 2);
    }

    #[test]
    fn dependency_order_puts_dedup_before_compress() {
        let proposals = vec![
            make_prop("ContextCompress", CostDriver::InputTokens, 0.8),
            make_prop("PromptDedup", CostDriver::InputTokens, 0.2),
        ];
        let result = compose_proposals(proposals, &make_call());
        // Even though ContextCompress ranked higher by savings, PromptDedup runs first.
        assert_eq!(result.rules_applied[0].rule, "PromptDedup");
        assert_eq!(result.rules_applied[1].rule, "ContextCompress");
    }

    #[test]
    fn solo_proposal_returns_plan_rewritten_not_composed() {
        let proposals = vec![make_prop("OutputBudget", CostDriver::OutputTokens, 0.4)];
        let result = compose_proposals(proposals, &make_call());
        assert_eq!(result.rules_applied.len(), 1);
        assert!(matches!(result.plan, Plan::Rewritten { .. }), "expected Rewritten, got {:?}", result.plan);
    }

    #[test]
    fn empty_proposals_yields_pass_through() {
        let result = compose_proposals(vec![], &make_call());
        assert!(matches!(result.plan, Plan::PassThrough));
        assert!(result.rules_applied.is_empty());
    }

    #[test]
    fn failing_safety_check_skips_proposal() {
        let call = make_call();
        let bad_prop = (
            "BadRule".to_string(),
            Proposal {
                rewritten: Plan::Rewritten {
                    rule: "BadRule".into(),
                    call: call.clone(),
                    projected_savings_usd: 1.0,
                },
                projected_savings_usd: 1.0,
                cost_driver: CostDriver::InputTokens,
                safety_check: Box::new(|_| false),
            },
        );
        let result = compose_proposals(vec![bad_prop], &call);
        assert!(matches!(result.plan, Plan::PassThrough));
    }

    #[test]
    fn three_orthogonal_rules_all_compose() {
        let proposals = vec![
            make_prop("ContextCompress", CostDriver::InputTokens, 0.5),
            make_prop("OutputBudget", CostDriver::OutputTokens, 0.3),
            make_prop("ModelDowngrade", CostDriver::ModelPrice, 0.2),
        ];
        let result = compose_proposals(proposals, &make_call());
        assert_eq!(result.rules_applied.len(), 3);
        assert!(result.net_savings_usd > 0.0);
        assert!(matches!(result.plan, Plan::Composed { .. }));
    }

    // Regression: a parameter-only rule (e.g. OutputBudget) must not undo
    // an earlier message-drop rule's reduction.  Before the fix, apply_rewrite
    // used `!=` which caused OB (carrying the original N-message call) to
    // overwrite SD's (N-1)-message result.
    #[test]
    fn parameter_only_rule_does_not_undo_prior_message_drop() {
        let original = make_call(); // 2 messages

        // A "StateDrop-like" rule proposes 1 message (drops one).
        let mut sd_rewritten = original.clone();
        sd_rewritten.messages = vec![Message { role: "system".into(), content: "sys".into() }];
        let sd_prop = (
            "StateDrop".to_string(),
            Proposal {
                rewritten: Plan::Rewritten {
                    rule: "StateDrop".into(),
                    call: sd_rewritten,
                    projected_savings_usd: 0.5,
                },
                projected_savings_usd: 0.5,
                cost_driver: CostDriver::InputTokens,
                safety_check: Box::new(|_| true),
            },
        );

        // An "OutputBudget-like" rule only changes parameters, but its
        // proposed call still carries the original 2 messages.
        let mut ob_rewritten = original.clone();
        ob_rewritten.parameters.max_output_tokens = Some(64);
        let ob_prop = (
            "OutputBudget".to_string(),
            Proposal {
                rewritten: Plan::Rewritten {
                    rule: "OutputBudget".into(),
                    call: ob_rewritten,
                    projected_savings_usd: 0.3,
                },
                projected_savings_usd: 0.3,
                cost_driver: CostDriver::OutputTokens,
                safety_check: Box::new(|_| true),
            },
        );

        let result = compose_proposals(vec![sd_prop, ob_prop], &original);
        match &result.plan {
            Plan::Composed { call, rules, .. } => {
                // SD ran first (sort_key 0 < 4). Its 1-message result must survive.
                assert_eq!(call.messages.len(), 1, "OB must not restore SD-dropped messages");
                // OB's parameter cap must be applied.
                assert_eq!(call.parameters.max_output_tokens, Some(64));
                assert_eq!(rules.len(), 2);
            }
            other => panic!("expected Composed, got {:?}", other),
        }
    }
}
