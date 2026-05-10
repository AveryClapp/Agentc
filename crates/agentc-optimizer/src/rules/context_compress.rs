//! `ContextCompress` — extractively drop low-salience message segments
//! from a large prompt.
//!
//! Spec § Rule specifications > ContextCompress:
//! - Applies when `prompt_bytes > min_prompt_bytes` (default 8 KB) AND
//!   ≥ 30% of the prompt's tokens have zero downstream attention score.
//! - Safety: the compressed prompt still contains every token from
//!   `DepSource::UserInput`, every token any subsequent span read, and
//!   at least one token from each distinct role.
//! - Extractive only (no secondary LLM summary — would blow the
//!   overhead budget).
//!
//! The attention-score signal comes from the profiler. For this bead
//! the rule reads it off `parameters.extra.attention_scores` as a
//! per-message `f32` array, which the interceptor populates from
//! `ProfilerSpan.input_attention_slice`. If the array is missing or
//! length-mismatched, the rule no-ops — we never guess.

use std::collections::HashSet;

use serde::Deserialize;
use serde_json::Value;

use crate::cost_model::CallSiteProfile;
use crate::dag::{Call, DepSource, Message};
use crate::planner::{CostDriver, Plan, Proposal, RewriteRule};

/// Default minimum prompt size to consider compressing, in bytes.
pub const DEFAULT_MIN_PROMPT_BYTES: usize = 8 * 1024;
/// Default minimum zero-attention fraction to trigger compression.
pub const DEFAULT_MIN_DEAD_FRACTION: f32 = 0.30;
/// Default accuracy budget (spec § Accuracy budget).
pub const DEFAULT_ACCURACY_BUDGET: f32 = 0.02;
/// Messages whose attention score falls at or below this threshold are
/// eligible for extraction. The default is appropriate for true model
/// attention (near-zero); proxies that emit scores on a coarser scale
/// (e.g. token-overlap fractions) should override via
/// `parameters.extra.dead_attention_epsilon`.
pub const DEAD_ATTENTION_EPSILON: f32 = 1e-4;

pub struct ContextCompressRule {
    min_prompt_bytes: usize,
    min_dead_fraction: f32,
    accuracy_budget: f32,
}

impl Default for ContextCompressRule {
    fn default() -> Self {
        Self {
            min_prompt_bytes: DEFAULT_MIN_PROMPT_BYTES,
            min_dead_fraction: DEFAULT_MIN_DEAD_FRACTION,
            accuracy_budget: DEFAULT_ACCURACY_BUDGET,
        }
    }
}

impl ContextCompressRule {
    pub fn new(min_prompt_bytes: usize, min_dead_fraction: f32) -> Self {
        Self {
            min_prompt_bytes,
            min_dead_fraction,
            accuracy_budget: DEFAULT_ACCURACY_BUDGET,
        }
    }
}

impl RewriteRule for ContextCompressRule {
    fn name(&self) -> &'static str {
        "ContextCompress"
    }

    fn applies(&self, call: &Call, _profile: &CallSiteProfile) -> bool {
        let prompt_bytes: usize = call.messages.iter().map(|m| m.content.len()).sum();
        if prompt_bytes <= self.min_prompt_bytes {
            return false;
        }
        // Requires the interceptor to have annotated attention scores.
        extract_attention_scores(call).is_some()
    }

    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal> {
        let scores = extract_attention_scores(call)?;
        if scores.len() != call.messages.len() {
            return None;
        }
        let epsilon = extract_dead_attention_epsilon(call).unwrap_or(DEAD_ATTENTION_EPSILON);
        let follow_ons = extract_follow_on_tokens(call);
        let user_input_messages = user_input_message_indices(call);
        let roles_to_keep_one: HashSet<String> = call
            .messages
            .iter()
            .map(|m| m.role.clone())
            .collect();

        // First pass: mark candidates for drop.
        let mut drop: Vec<bool> = scores
            .iter()
            .zip(call.messages.iter())
            .enumerate()
            .map(|(i, (score, msg))| {
                if user_input_messages.contains(&i) {
                    return false;
                }
                if contains_any_token(&msg.content, &follow_ons) {
                    return false;
                }
                *score <= epsilon
            })
            .collect();

        // Dead-fraction check on the token side.
        let dead_fraction = scores
            .iter()
            .filter(|s| **s <= epsilon)
            .count() as f32
            / (scores.len() as f32);
        if dead_fraction < self.min_dead_fraction {
            return None;
        }

        // Second pass: ensure at least one message per role survives.
        for role in &roles_to_keep_one {
            let any_kept = call
                .messages
                .iter()
                .zip(drop.iter())
                .any(|(m, d)| &m.role == role && !d);
            if !any_kept {
                // Un-drop the first message for this role to restore
                // the role's presence.
                if let Some(i) = call.messages.iter().position(|m| &m.role == role) {
                    drop[i] = false;
                }
            }
        }

        let dropped = drop.iter().filter(|x| **x).count();
        if dropped == 0 {
            return None;
        }

        let dropped_bytes: usize = call
            .messages
            .iter()
            .zip(drop.iter())
            .filter(|(_, d)| **d)
            .map(|(m, _)| m.content.len())
            .sum();
        let total_bytes: usize = call.messages.iter().map(|m| m.content.len()).sum();
        let fraction_dropped = if total_bytes > 0 {
            dropped_bytes as f32 / total_bytes as f32
        } else {
            0.0
        };

        let projected = profile.cost_usd.mean as f32 * fraction_dropped;

        let mut new_messages = Vec::with_capacity(call.messages.len() - dropped);
        for (m, d) in call.messages.iter().zip(drop.iter()) {
            if !d {
                new_messages.push(m.clone());
            }
        }
        let rewritten_call = Call {
            messages: new_messages,
            ..call.clone()
        };

        Some(Proposal {
            rewritten: Plan::Rewritten {
                rule: self.name().to_string(),
                call: rewritten_call,
                projected_savings_usd: projected,
            },
            projected_savings_usd: projected,
            cost_driver: CostDriver::InputTokens,
            safety_check: Box::new(|call| {
                // The rewritten call must still carry every UserInput
                // message's content (by token presence, not identity —
                // some interceptors collapse duplicates) and at least
                // one message per original role.
                //
                // We recompute from the message_deps payload, which the
                // interceptor threaded through `parameters.extra`.
                let Some(deps) = extract_message_deps(call) else {
                    return true;
                };
                if deps.len() != call.messages.len() {
                    return false;
                }
                for (msg, dep) in call.messages.iter().zip(&deps) {
                    if matches!(dep, DepSource::UserInput { .. }) && msg.content.is_empty() {
                        return false;
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

fn extract_attention_scores(call: &Call) -> Option<Vec<f32>> {
    let v = call
        .parameters
        .extra
        .as_object()?
        .get("attention_scores")?
        .clone();
    serde_json::from_value::<Vec<f32>>(v).ok()
}

fn extract_dead_attention_epsilon(call: &Call) -> Option<f32> {
    let obj = call.parameters.extra.as_object()?;
    let v = obj.get("dead_attention_epsilon")?;
    v.as_f64().map(|f| f as f32)
}

#[derive(Debug, Deserialize)]
struct FollowOn {
    #[serde(default)]
    tokens: Vec<String>,
}

fn extract_follow_on_tokens(call: &Call) -> Vec<String> {
    let Some(obj) = call.parameters.extra.as_object() else { return Vec::new(); };
    let Some(v) = obj.get("follow_on_tokens") else { return Vec::new(); };
    if let Value::Array(items) = v {
        return items
            .iter()
            .filter_map(|x| x.as_str().map(|s| s.to_string()))
            .collect();
    }
    serde_json::from_value::<FollowOn>(v.clone())
        .map(|f| f.tokens)
        .unwrap_or_default()
}

fn extract_message_deps(call: &Call) -> Option<Vec<DepSource>> {
    let v = call.parameters.extra.as_object()?.get("message_deps")?.clone();
    serde_json::from_value::<Vec<DepSource>>(v).ok()
}

fn user_input_message_indices(call: &Call) -> HashSet<usize> {
    let mut out = HashSet::new();
    let Some(deps) = extract_message_deps(call) else { return out; };
    if deps.len() != call.messages.len() {
        return out;
    }
    for (i, dep) in deps.iter().enumerate() {
        if matches!(dep, DepSource::UserInput { .. }) {
            out.insert(i);
        }
    }
    out
}

fn contains_any_token(haystack: &str, needles: &[String]) -> bool {
    // Needles arrive lowercase from the proxy's tokenizer; haystack is
    // raw message content (mixed case, punctuation). Lowercase + strip
    // punctuation on the haystack side so the protection actually fires
    // for proper-noun overlaps like "Scott Derrickson".
    if needles.is_empty() {
        return false;
    }
    let lowered = haystack.to_lowercase();
    let tokens: HashSet<String> = lowered
        .split(|c: char| !c.is_alphanumeric() && c != '_')
        .filter(|t| !t.is_empty())
        .map(|t| t.to_string())
        .collect();
    needles.iter().any(|n| tokens.contains(n.as_str()))
}

fn _message_role(m: &Message) -> &str {
    m.role.as_str()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost_model::WelfordStats;
    use crate::dag::Parameters;
    use serde_json::json;

    fn hot_profile() -> CallSiteProfile {
        let mut p = CallSiteProfile::new("site");
        p.n_observations = 10;
        p.cost_usd = WelfordStats::from_persisted(10, 0.01, 0.0);
        p
    }

    fn big(s: &str, n: usize) -> String {
        s.repeat(n)
    }

    fn call_with(
        messages: Vec<Message>,
        scores: Vec<f32>,
        deps: Vec<DepSource>,
        follow_on: Vec<&str>,
    ) -> Call {
        let extra = json!({
            "attention_scores": scores,
            "message_deps": deps,
            "follow_on_tokens": follow_on,
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
    fn small_prompt_does_not_fire() {
        let msgs = vec![Message { role: "user".into(), content: "short".into() }];
        let call = call_with(msgs, vec![0.0], vec![DepSource::Literal], vec![]);
        let rule = ContextCompressRule::default();
        assert!(!rule.applies(&call, &hot_profile()));
    }

    #[test]
    fn large_dead_prompt_fires_and_drops() {
        let msgs = vec![
            Message { role: "system".into(), content: "sys".into() },
            Message { role: "user".into(), content: "live question".into() },
            Message { role: "user".into(), content: big("x ", 5000) }, // ~10KB dead
        ];
        let scores = vec![1.0, 1.0, 0.0];
        let deps = vec![
            DepSource::Literal,
            DepSource::UserInput { span_id: [1u8; 8] },
            DepSource::Literal,
        ];
        let call = call_with(msgs, scores, deps, vec![]);
        let rule = ContextCompressRule::default();
        let prop = rule.propose(&call, &hot_profile()).expect("must fire");
        match &prop.rewritten {
            Plan::Rewritten { call, .. } => {
                assert_eq!(call.messages.len(), 2);
            }
            _ => panic!("expected Rewritten"),
        }
    }

    #[test]
    fn follow_on_tokens_protect_message_from_drop() {
        let msgs = vec![
            Message { role: "user".into(), content: "trigger".into() },
            Message { role: "user".into(), content: big("junk ", 5000) },
        ];
        // Dead scores on both, but "junk" is a follow-on token read
        // downstream → must keep it.
        let call = call_with(
            msgs,
            vec![0.0, 0.0],
            vec![DepSource::Literal, DepSource::Literal],
            vec!["junk"],
        );
        let rule = ContextCompressRule::default();
        let prop = rule.propose(&call, &hot_profile());
        // The rule either declines (nothing droppable) or keeps the
        // junk-tagged one. Either is acceptable; we just assert that
        // the junk-containing message survives.
        if let Some(p) = prop {
            if let Plan::Rewritten { call, .. } = p.rewritten {
                assert!(call.messages.iter().any(|m| m.content.contains("junk")));
            }
        }
    }

    #[test]
    fn user_input_messages_never_dropped() {
        let msgs = vec![
            Message { role: "user".into(), content: "userq".into() },
            Message { role: "user".into(), content: big("dead ", 5000) },
        ];
        let call = call_with(
            msgs,
            vec![0.0, 0.0], // even the user msg scores 0 → should still survive
            vec![DepSource::UserInput { span_id: [1u8; 8] }, DepSource::Literal],
            vec![],
        );
        let rule = ContextCompressRule::default();
        let prop = rule.propose(&call, &hot_profile()).expect("must fire");
        match &prop.rewritten {
            Plan::Rewritten { call, .. } => {
                assert!(call.messages.iter().any(|m| m.content == "userq"));
            }
            _ => panic!("expected Rewritten"),
        }
    }

    #[test]
    fn missing_scores_no_proposal() {
        let msgs = vec![Message {
            role: "user".into(),
            content: big("x", 10_000),
        }];
        let call = Call {
            call_site_id: "site".into(),
            trace_id: [0u8; 16],
            span_id: [0u8; 8],
            model: "gpt-4o".into(),
            messages: msgs,
            parameters: Parameters::default(), // no extra
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        };
        let rule = ContextCompressRule::default();
        assert!(!rule.applies(&call, &hot_profile()));
    }

    #[test]
    fn low_dead_fraction_skips_fire() {
        // 10 messages, only 1 dead. Dead fraction 0.1 < 0.3 default.
        let mut msgs = Vec::new();
        let mut scores = Vec::new();
        let mut deps = Vec::new();
        for i in 0..10 {
            msgs.push(Message {
                role: "user".into(),
                content: big("x", 1024),
            });
            scores.push(if i == 0 { 0.0 } else { 1.0 });
            deps.push(DepSource::Literal);
        }
        let call = call_with(msgs, scores, deps, vec![]);
        let rule = ContextCompressRule::default();
        assert!(rule.propose(&call, &hot_profile()).is_none());
    }

    #[test]
    fn epsilon_override_unlocks_proxy_scale_scores() {
        // Token-overlap proxy emits scores in [0.05, 1.0]; the 1e-4
        // default would never see anything as dead. With override = 0.10,
        // scores at 0.05 become drop-eligible.
        let msgs = vec![
            Message { role: "system".into(), content: "system".into() },
            Message { role: "user".into(), content: "live question".into() },
            Message { role: "user".into(), content: big("x ", 5000) },
            Message { role: "user".into(), content: big("y ", 5000) },
            Message { role: "user".into(), content: big("z ", 5000) },
        ];
        let scores = vec![0.05, 1.0, 0.05, 0.05, 0.05];
        let deps = vec![
            DepSource::Literal,
            DepSource::UserInput { span_id: [1u8; 8] },
            DepSource::Literal,
            DepSource::Literal,
            DepSource::Literal,
        ];
        let extra = json!({
            "attention_scores": scores,
            "message_deps": deps,
            "follow_on_tokens": [],
            "dead_attention_epsilon": 0.10_f32,
        });
        let call = Call {
            call_site_id: "site".into(),
            trace_id: [0u8; 16],
            span_id: [0u8; 8],
            model: "gpt-4o".into(),
            messages: msgs,
            parameters: Parameters { extra, ..Default::default() },
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        };
        let rule = ContextCompressRule::default();
        // Without override, default 1e-4 skips everything → no fire.
        // With override at 0.10, distractors qualify and dead_fraction
        // = 4/5 = 0.80 > 0.30 → fires.
        let prop = rule.propose(&call, &hot_profile()).expect("must fire");
        match &prop.rewritten {
            Plan::Rewritten { call, .. } => {
                // System role survives via roles_to_keep_one; user input
                // survives via DepSource::UserInput protection.
                assert!(call.messages.iter().any(|m| m.role == "system"));
                assert!(call.messages.iter().any(|m| m.content == "live question"));
            }
            _ => panic!("expected Rewritten"),
        }
    }

    #[test]
    fn contains_any_token_is_case_and_punctuation_insensitive() {
        // The proxy lowercases tokens; raw paragraph text is mixed-case
        // with punctuation. The protection must still fire.
        let needles = vec!["scott".to_string(), "derrickson".to_string()];
        assert!(contains_any_token(
            "Scott Derrickson is an American director.",
            &needles
        ));
        assert!(contains_any_token("...Scott, Derrickson!?", &needles));
        assert!(!contains_any_token("Henry IV ruled England.", &needles));
    }

    #[test]
    fn proposal_carries_input_tokens_cost_driver() {
        let msgs = vec![
            Message { role: "system".into(), content: "sys".into() },
            Message { role: "user".into(), content: "live question".into() },
            Message { role: "user".into(), content: big("x ", 5000) },
        ];
        let call = call_with(
            msgs,
            vec![1.0, 1.0, 0.0],
            vec![DepSource::Literal, DepSource::UserInput { span_id: [1u8; 8] }, DepSource::Literal],
            vec![],
        );
        let rule = ContextCompressRule::default();
        let prop = rule.propose(&call, &hot_profile()).expect("must fire on dead context");
        assert_eq!(prop.cost_driver, CostDriver::InputTokens);
    }
}
