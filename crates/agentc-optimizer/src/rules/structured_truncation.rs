//! `StructuredTruncation` — projection-pushdown on JSON tool outputs.
//!
//! When a `DepSource::ToolOutput`-tagged message contains a JSON object,
//! top-level keys not referenced in the last consumer message are dropped.
//! This mirrors the compiler's projection-pushdown optimisation: push the
//! consumer's field predicate down into the data source, eliminating keys
//! the caller never reads.
//!
//! Compiler analogy: projection pushdown (§3 of the paper).
//!
//! Applies when:
//! - `parameters.extra.message_deps` contains at least one `tool_output`
//!   entry whose message content starts with `{` (cheap JSON probe).
//! - A non-ToolOutput user message exists after the tool output (the
//!   "consumer signal" — defines which keys are referenced).
//!
//! Safety: at least 1 key must survive the projection per tool output
//! message. System messages are never modified. Non-JSON content is left
//! unchanged.
//!
//! Projection: (dropped bytes / total input bytes) × cost_usd.mean.

use std::collections::HashSet;

use serde_json::{Map, Value};

use crate::cost_model::CallSiteProfile;
use crate::dag::{Call, DepSource, Message};
use crate::planner::{Plan, Proposal, RewriteRule};

pub const DEFAULT_ACCURACY_BUDGET: f32 = 0.02;

pub struct StructuredTruncationRule {
    accuracy_budget: f32,
}

impl Default for StructuredTruncationRule {
    fn default() -> Self {
        Self { accuracy_budget: DEFAULT_ACCURACY_BUDGET }
    }
}

impl RewriteRule for StructuredTruncationRule {
    fn name(&self) -> &'static str {
        "StructuredTruncation"
    }

    fn applies(&self, call: &Call, _profile: &CallSiteProfile) -> bool {
        let Some(deps) = extract_message_deps(call) else { return false };
        if deps.len() != call.messages.len() {
            return false;
        }
        // Cheap probe: ToolOutput-tagged non-system message with a JSON
        // object start. Full parse happens in propose().
        deps.iter().zip(&call.messages).any(|(dep, msg)| {
            matches!(dep, DepSource::ToolOutput { .. })
                && msg.role != "system"
                && msg.content.trim_start().starts_with('{')
        })
    }

    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal> {
        let message_deps = extract_message_deps(call)?;
        if message_deps.len() != call.messages.len() {
            return None;
        }

        // Salient signal: last non-ToolOutput user message. This is the
        // consumer that tells us which keys are actually read (matches
        // ContextCompress §3.4: last user message = salient signal).
        let salient = call
            .messages
            .iter()
            .zip(&message_deps)
            .filter(|(msg, dep)| {
                msg.role == "user" && !matches!(dep, DepSource::ToolOutput { .. })
            })
            .last()
            .map(|(msg, _)| msg.content.as_str())
            .unwrap_or("");

        if salient.is_empty() {
            return None;
        }

        let total_input_bytes: usize = call.messages.iter().map(|m| m.content.len()).sum();
        let mut new_messages: Vec<Message> = call.messages.clone();
        // Per-message index: the referenced keys we decided to keep.
        // Captured by the safety check closure.
        let mut referenced_per_msg: Vec<(usize, HashSet<String>)> = Vec::new();
        let mut dropped_bytes = 0usize;

        for (i, (msg, dep)) in call.messages.iter().zip(&message_deps).enumerate() {
            if msg.role == "system" || !matches!(dep, DepSource::ToolOutput { .. }) {
                continue;
            }
            let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&msg.content) else {
                continue;
            };
            if obj.len() < 2 {
                continue;
            }
            let all_keys: Vec<String> = obj.keys().cloned().collect();
            let referenced = keys_referenced_in(salient, &all_keys);
            if referenced.is_empty() || referenced.len() == all_keys.len() {
                // Nothing to drop, or all keys are referenced — no gain.
                continue;
            }
            let mut pruned = Map::with_capacity(referenced.len());
            for (k, v) in &obj {
                if referenced.contains(k) {
                    pruned.insert(k.clone(), v.clone());
                }
            }
            let new_content = serde_json::to_string(&Value::Object(pruned)).ok()?;
            let removed = msg.content.len().saturating_sub(new_content.len());
            if removed == 0 {
                continue;
            }
            dropped_bytes += removed;
            new_messages[i] = Message { role: msg.role.clone(), content: new_content };
            referenced_per_msg.push((i, referenced));
        }

        if dropped_bytes == 0 {
            return None;
        }

        let dropped_frac = dropped_bytes as f32 / total_input_bytes.max(1) as f32;
        let projected = profile.cost_usd.mean as f32 * dropped_frac;

        let rewritten_call = Call { messages: new_messages, ..call.clone() };
        Some(Proposal {
            rewritten: Plan::Rewritten {
                rule: self.name().to_string(),
                call: rewritten_call,
                projected_savings_usd: projected,
            },
            projected_savings_usd: projected,
            safety_check: Box::new(move |call| {
                // Re-verify: the referenced keys we planned to keep still
                // exist in the original call (guards against mutation between
                // propose() and safety_check()).
                let Some(deps) = extract_message_deps(call) else { return false };
                for (i, referenced) in &referenced_per_msg {
                    let Some(msg) = call.messages.get(*i) else { return false };
                    let Some(dep) = deps.get(*i) else { return false };
                    if !matches!(dep, DepSource::ToolOutput { .. }) {
                        return false;
                    }
                    if let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&msg.content) {
                        if !referenced.iter().all(|k| obj.contains_key(k)) {
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

fn extract_message_deps(call: &Call) -> Option<Vec<DepSource>> {
    let v = call.parameters.extra.as_object()?.get("message_deps")?.clone();
    serde_json::from_value::<Vec<DepSource>>(v).ok()
}

fn keys_referenced_in(salient: &str, keys: &[String]) -> HashSet<String> {
    keys.iter().filter(|k| salient.contains(k.as_str())).cloned().collect()
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

    fn call_with(messages: Vec<Message>, deps: Vec<DepSource>) -> Call {
        let extra = json!({"message_deps": deps});
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

    fn tool_dep() -> DepSource {
        DepSource::ToolOutput { span_id: [1u8; 8] }
    }

    fn tool_json_large() -> String {
        json!({
            "label": "answer",
            "raw_text": "x".repeat(500),
            "debug_trace": "y".repeat(400),
        })
        .to_string()
    }

    #[test]
    fn no_tool_output_deps_does_not_apply() {
        let call = call_with(
            vec![Message { role: "user".into(), content: "{\"a\":1,\"b\":2}".into() }],
            vec![DepSource::Literal],
        );
        assert!(!StructuredTruncationRule::default().applies(&call, &hot_profile(0.01)));
    }

    #[test]
    fn unreferenced_keys_are_dropped() {
        let call = call_with(
            vec![
                Message { role: "user".into(), content: tool_json_large() },
                Message { role: "user".into(), content: "The answer label is: label".into() },
            ],
            vec![tool_dep(), DepSource::Literal],
        );
        let rule = StructuredTruncationRule::default();
        let prop = rule.propose(&call, &hot_profile(0.02)).expect("must fire");
        match &prop.rewritten {
            Plan::Rewritten { call, .. } => {
                let obj: Value = serde_json::from_str(&call.messages[0].content).unwrap();
                let keys: Vec<&str> = obj.as_object().unwrap().keys().map(String::as_str).collect();
                assert!(keys.contains(&"label"), "referenced key must survive");
                assert!(!keys.contains(&"raw_text"), "unreferenced key must be dropped");
                assert!(!keys.contains(&"debug_trace"), "unreferenced key must be dropped");
            }
            _ => panic!("expected Rewritten"),
        }
        assert!(prop.projected_savings_usd > 0.0);
        // Safety check must pass on the original call.
        assert!((prop.safety_check)(&call));
    }

    #[test]
    fn all_keys_referenced_does_not_fire() {
        let call = call_with(
            vec![
                Message { role: "user".into(), content: "{\"a\":1,\"b\":2}".into() },
                Message { role: "user".into(), content: "give me a and b please".into() },
            ],
            vec![tool_dep(), DepSource::Literal],
        );
        assert!(StructuredTruncationRule::default().propose(&call, &hot_profile(0.02)).is_none());
    }

    #[test]
    fn non_json_tool_output_is_skipped() {
        let call = call_with(
            vec![
                Message { role: "user".into(), content: "not json at all".into() },
                Message { role: "user".into(), content: "any question".into() },
            ],
            vec![tool_dep(), DepSource::Literal],
        );
        assert!(!StructuredTruncationRule::default().applies(&call, &hot_profile(0.02)));
    }

    #[test]
    fn no_consumer_message_does_not_fire() {
        // Only message is the ToolOutput itself — no salient consumer.
        let call = call_with(
            vec![Message { role: "user".into(), content: tool_json_large() }],
            vec![tool_dep()],
        );
        let rule = StructuredTruncationRule::default();
        assert!(rule.applies(&call, &hot_profile(0.02)));
        assert!(rule.propose(&call, &hot_profile(0.02)).is_none());
    }

    #[test]
    fn system_message_is_never_modified() {
        // System message tagged ToolOutput must not fire.
        let call = call_with(
            vec![
                Message { role: "system".into(), content: "{\"a\":1,\"b\":2}".into() },
                Message { role: "user".into(), content: "tell me about a".into() },
            ],
            vec![tool_dep(), DepSource::Literal],
        );
        assert!(!StructuredTruncationRule::default().applies(&call, &hot_profile(0.02)));
    }

    #[test]
    fn single_key_json_does_not_fire() {
        // Only 1 key — even if unreferenced there is nothing to project.
        let call = call_with(
            vec![
                Message { role: "user".into(), content: "{\"only\":1}".into() },
                Message { role: "user".into(), content: "nothing relevant here".into() },
            ],
            vec![tool_dep(), DepSource::Literal],
        );
        // applies() passes (starts with '{'), but propose() must bail.
        let rule = StructuredTruncationRule::default();
        assert!(rule.applies(&call, &hot_profile(0.02)));
        assert!(rule.propose(&call, &hot_profile(0.02)).is_none());
    }

    #[test]
    fn safety_check_fails_if_key_removed_from_original() {
        let call = call_with(
            vec![
                Message { role: "user".into(), content: tool_json_large() },
                Message { role: "user".into(), content: "label please".into() },
            ],
            vec![tool_dep(), DepSource::Literal],
        );
        let rule = StructuredTruncationRule::default();
        let prop = rule.propose(&call, &hot_profile(0.02)).expect("must fire");

        // Craft a mutated call where "label" is absent from the tool output.
        let mutated = call_with(
            vec![
                Message {
                    role: "user".into(),
                    content: json!({"raw_text": "x"}).to_string(),
                },
                Message { role: "user".into(), content: "label please".into() },
            ],
            vec![tool_dep(), DepSource::Literal],
        );
        assert!(!(prop.safety_check)(&mutated), "safety must fail when referenced key is gone");
    }
}
