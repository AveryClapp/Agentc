//! `StateDrop` — drop `DepSource::State { key }` entries that no
//! downstream span in the window reads.
//!
//! Spec § Rule specifications > StateDrop:
//! - Applies when the call's messages contain State-tagged content AND
//!   none of the last N spans read any of those keys.
//! - Safety: dropped keys are not in the system prompt; post-drop
//!   `messages` retains ≥ 50% of the original list.
//! - Projection: `cost_usd.mean * dropped_state_fraction`.
//!
//! The interceptor tags each `Message` with the `DepSource` that
//! produced it by stashing a parallel array under
//! `parameters.extra.message_deps`. This rule reads that, drops the
//! messages whose dep is a `State { key }` not observed in any of the
//! peer span reads (carried on `parameters.extra.window_state_reads`),
//! and emits a `Plan::Rewritten` with the trimmed messages.

use std::collections::HashSet;

use serde::Deserialize;
use serde_json::Value;

use crate::cost_model::CallSiteProfile;
use crate::dag::{Call, DepSource, Message};
use crate::planner::{Plan, Proposal, RewriteRule};

pub const DEFAULT_ACCURACY_BUDGET: f32 = 0.01;

/// Minimum fraction of the original messages list that must survive the
/// rewrite. Anything below this and we treat the drop as too risky.
pub const MIN_RETENTION_FRACTION: f32 = 0.5;

pub struct StateDropRule {
    accuracy_budget: f32,
}

impl Default for StateDropRule {
    fn default() -> Self {
        Self { accuracy_budget: DEFAULT_ACCURACY_BUDGET }
    }
}

impl RewriteRule for StateDropRule {
    fn name(&self) -> &'static str {
        "StateDrop"
    }

    fn applies(&self, call: &Call, _profile: &CallSiteProfile) -> bool {
        let Some(deps) = extract_message_deps(call) else { return false; };
        // At least one message must carry a State tag.
        deps.iter().any(|d| matches!(d, DepSource::State { .. }))
    }

    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal> {
        let message_deps = extract_message_deps(call)?;
        if message_deps.len() != call.messages.len() {
            return None;
        }
        let window_reads = extract_window_state_reads(call);

        let mut dropped_keys: HashSet<String> = HashSet::new();
        let keep: Vec<bool> = call
            .messages
            .iter()
            .zip(&message_deps)
            .map(|(msg, dep)| {
                if should_drop(msg, dep, &window_reads) {
                    if let DepSource::State { key } = dep {
                        dropped_keys.insert(key.clone());
                    }
                    false
                } else {
                    true
                }
            })
            .collect();

        let kept = keep.iter().filter(|x| **x).count();
        let original = call.messages.len();
        if kept == original {
            return None;
        }
        // Retention floor.
        let retained_frac = kept as f32 / (original.max(1) as f32);
        if retained_frac < MIN_RETENTION_FRACTION {
            return None;
        }
        // The dropped-state fraction drives the projection.
        let dropped = (original - kept) as f32 / (original.max(1) as f32);
        let projected = profile.cost_usd.mean as f32 * dropped;

        // Rewrite.
        let mut rewritten_messages = Vec::with_capacity(kept);
        for (msg, k) in call.messages.iter().zip(&keep) {
            if *k {
                rewritten_messages.push(msg.clone());
            }
        }
        let rewritten_call = Call {
            messages: rewritten_messages,
            ..call.clone()
        };

        let dropped_keys_for_check = dropped_keys.clone();
        Some(Proposal {
            rewritten: Plan::Rewritten {
                rule: self.name().to_string(),
                call: rewritten_call,
                projected_savings_usd: projected,
            },
            projected_savings_usd: projected,
            safety_check: Box::new(move |call| {
                let Some(deps) = extract_message_deps(call) else { return false; };
                if deps.len() != call.messages.len() {
                    return false;
                }
                // Spec § StateDrop safety: "The dropped keys are not
                // present in the system prompt." If any key we chose to
                // drop is ALSO tagged on a system message, refuse — the
                // key might encode an invariant upstream.
                for (msg, dep) in call.messages.iter().zip(&deps) {
                    if msg.role != "system" {
                        continue;
                    }
                    if let DepSource::State { key } = dep {
                        if dropped_keys_for_check.contains(key) {
                            return false;
                        }
                    }
                }
                true
            }),
        })
    }

    fn accuracy_budget(&self) -> f32 {
        self.accuracy_budget
    }
}

fn should_drop(msg: &Message, dep: &DepSource, window_reads: &HashSet<String>) -> bool {
    match dep {
        DepSource::State { key } => {
            // Never drop the system prompt.
            if msg.role == "system" {
                return false;
            }
            !window_reads.contains(key)
        }
        _ => false,
    }
}

fn extract_message_deps(call: &Call) -> Option<Vec<DepSource>> {
    let v = call.parameters.extra.as_object()?.get("message_deps")?.clone();
    serde_json::from_value::<Vec<DepSource>>(v).ok()
}

#[derive(Debug, Deserialize)]
struct WindowReads {
    #[serde(default)]
    state_keys: Vec<String>,
}

fn extract_window_state_reads(call: &Call) -> HashSet<String> {
    let mut out = HashSet::new();
    let Some(obj) = call.parameters.extra.as_object() else { return out; };
    let Some(v) = obj.get("window_state_reads") else { return out; };
    if let Value::Array(items) = v {
        for item in items {
            out.insert(item.as_str().unwrap_or_default().to_string());
        }
        return out;
    }
    if let Ok(w) = serde_json::from_value::<WindowReads>(v.clone()) {
        out.extend(w.state_keys);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost_model::WelfordStats;
    use crate::dag::Parameters;
    use serde_json::json;

    fn hot_profile(cost: f64) -> CallSiteProfile {
        let mut p = CallSiteProfile::new("site");
        p.n_observations = 10;
        p.cost_usd = WelfordStats::from_persisted(10, cost, 0.0);
        p
    }

    fn call_with(messages: Vec<Message>, deps: Vec<DepSource>, reads: Vec<String>) -> Call {
        let extra = json!({
            "message_deps": deps,
            "window_state_reads": reads,
        });
        Call {
            call_site_id: "site".into(),
            trace_id: [0u8; 16],
            span_id: [0u8; 8],
            model: "gpt-4o".into(),
            messages,
            parameters: Parameters { extra, ..Default::default() },
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        }
    }

    #[test]
    fn no_state_tags_does_not_fire() {
        let call = call_with(
            vec![Message { role: "user".into(), content: "hi".into() }],
            vec![DepSource::Literal],
            vec![],
        );
        let rule = StateDropRule::default();
        assert!(!rule.applies(&call, &hot_profile(0.01)));
    }

    #[test]
    fn unread_state_key_is_dropped() {
        let msgs = vec![
            Message { role: "system".into(), content: "sys".into() },
            Message { role: "user".into(), content: "keep".into() },
            Message { role: "user".into(), content: "stale".into() },
            Message { role: "user".into(), content: "stale2".into() },
        ];
        let deps = vec![
            DepSource::Literal,
            DepSource::Literal,
            DepSource::State { key: "scratch".into() },
            DepSource::State { key: "scratch".into() },
        ];
        // window reads nothing: both scratch messages are drop-eligible.
        // Original = 4, kept = 2 → retention 50% (at floor).
        let call = call_with(msgs, deps, vec![]);
        let rule = StateDropRule::default();
        let prop = rule.propose(&call, &hot_profile(0.02)).expect("must fire");
        match &prop.rewritten {
            Plan::Rewritten { call, .. } => assert_eq!(call.messages.len(), 2),
            _ => panic!("expected Rewritten"),
        }
        assert!((prop.projected_savings_usd - 0.01).abs() < 1e-5);
    }

    #[test]
    fn read_state_key_is_retained() {
        let msgs = vec![
            Message { role: "user".into(), content: "a".into() },
            Message { role: "user".into(), content: "b".into() },
        ];
        let deps = vec![
            DepSource::Literal,
            DepSource::State { key: "scratch".into() },
        ];
        let call = call_with(msgs, deps, vec!["scratch".into()]);
        let rule = StateDropRule::default();
        assert!(rule.propose(&call, &hot_profile(0.02)).is_none());
    }

    #[test]
    fn retention_below_50pct_rejects() {
        let msgs = vec![
            Message { role: "user".into(), content: "a".into() },
            Message { role: "user".into(), content: "b".into() },
            Message { role: "user".into(), content: "c".into() },
        ];
        // All three state-tagged, none read → would drop everything.
        let deps = vec![
            DepSource::State { key: "s".into() },
            DepSource::State { key: "s".into() },
            DepSource::State { key: "s".into() },
        ];
        let call = call_with(msgs, deps, vec![]);
        let rule = StateDropRule::default();
        assert!(rule.propose(&call, &hot_profile(0.02)).is_none());
    }

    #[test]
    fn system_prompt_state_tag_is_never_dropped() {
        let msgs = vec![
            Message { role: "system".into(), content: "invariant".into() },
            Message { role: "user".into(), content: "a".into() },
            Message { role: "user".into(), content: "b".into() },
            Message { role: "user".into(), content: "c".into() },
        ];
        let deps = vec![
            DepSource::State { key: "sys".into() },
            DepSource::Literal,
            DepSource::Literal,
            DepSource::State { key: "scratch".into() },
        ];
        let call = call_with(msgs, deps, vec![]);
        let rule = StateDropRule::default();
        let prop = rule.propose(&call, &hot_profile(0.04)).expect("user scratch drops");
        match &prop.rewritten {
            Plan::Rewritten { call, .. } => {
                assert_eq!(call.messages.len(), 3);
                assert!(call.messages.iter().any(|m| m.role == "system"));
            }
            _ => panic!("expected Rewritten"),
        }
        // Safety check refuses when the *same* key we dropped also
        // appears as a system tag (it might encode an invariant).
        let deps_bad = vec![
            DepSource::State { key: "scratch".into() }, // system carries the dropped key
            DepSource::Literal,
            DepSource::Literal,
            DepSource::State { key: "scratch".into() },
        ];
        let call_bad = call_with(
            vec![
                Message { role: "system".into(), content: "inv".into() },
                Message { role: "user".into(), content: "a".into() },
                Message { role: "user".into(), content: "b".into() },
                Message { role: "user".into(), content: "c".into() },
            ],
            deps_bad,
            vec![],
        );
        assert!(!(prop.safety_check)(&call_bad));
    }
}
