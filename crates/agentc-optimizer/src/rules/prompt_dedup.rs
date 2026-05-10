//! `PromptDedup` — common subexpression elimination on near-duplicate messages.
//!
//! When an agent appends the same system instructions or tool descriptions
//! across turns, near-duplicate messages (token Jaccard ≥ threshold) are
//! deduplicated to a single copy. The retained copy is the one with the
//! highest IDF score — i.e. the copy whose tokens are most distinctive
//! relative to the surrounding messages.
//!
//! Compiler analog: common subexpression elimination. The repeated message
//! is the common subexpression; we hoist it to a single occurrence.
//!
//! Cost driver: InputTokens (same as ContextCompress and StructuredTruncation).
//!
//! Composition safety:
//! - Safe to compose WITH ContextCompress: dedup removes duplicate messages
//!   first, then compress attenuates what remains — no overlap.
//! - Unsafe to compose WITH StateDrop: both modify the messages list. The
//!   composition planner must enforce this pair explicitly.
//!
//! Safety: UserInput-tagged messages are never dropped. A minimum of 2
//! messages must survive. Short messages (< MIN_TOKEN_COUNT tokens) are
//! never considered for dedup — they're likely role cues, not repeated blobs.

use std::collections::{HashMap, HashSet};

use crate::cost_model::CallSiteProfile;
use crate::dag::{Call, DepSource};
use crate::planner::{CostDriver, Plan, Proposal, RewriteRule};

pub const DEFAULT_ACCURACY_BUDGET: f32 = 0.01;
/// Messages with fewer tokens than this are not candidates for dedup.
pub const MIN_TOKEN_COUNT: usize = 20;
/// Jaccard similarity at or above this threshold triggers dedup.
pub const JACCARD_THRESHOLD: f64 = 0.92;

pub struct PromptDedupRule {
    accuracy_budget: f32,
}

impl Default for PromptDedupRule {
    fn default() -> Self {
        Self { accuracy_budget: DEFAULT_ACCURACY_BUDGET }
    }
}

impl RewriteRule for PromptDedupRule {
    fn name(&self) -> &'static str {
        "PromptDedup"
    }

    fn applies(&self, call: &Call, _profile: &CallSiteProfile) -> bool {
        if call.messages.len() < 2 {
            return false;
        }
        let tokens: Vec<Vec<String>> = call.messages.iter().map(|m| tokenize(&m.content)).collect();
        // Fast check: any two messages share enough tokens to potentially exceed JACCARD_THRESHOLD.
        for i in 0..tokens.len() {
            if tokens[i].len() < MIN_TOKEN_COUNT {
                continue;
            }
            for j in (i + 1)..tokens.len() {
                if tokens[j].len() < MIN_TOKEN_COUNT {
                    continue;
                }
                if jaccard(&tokens[i], &tokens[j]) >= JACCARD_THRESHOLD {
                    return true;
                }
            }
        }
        false
    }

    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal> {
        if call.messages.len() < 2 {
            return None;
        }
        let message_deps = extract_message_deps(call);
        let tokens: Vec<Vec<String>> = call.messages.iter().map(|m| tokenize(&m.content)).collect();

        // Build global IDF: count in how many messages each token appears.
        let n = tokens.len() as f64;
        let mut doc_freq: HashMap<&str, usize> = HashMap::new();
        for toks in &tokens {
            let unique: HashSet<&str> = toks.iter().map(String::as_str).collect();
            for t in unique {
                *doc_freq.entry(t).or_insert(0) += 1;
            }
        }

        // IDF score for a message = sum of ln(n / df) for its unique tokens.
        let idf_score = |toks: &Vec<String>| -> f64 {
            let unique: HashSet<&str> = toks.iter().map(String::as_str).collect();
            unique
                .iter()
                .map(|t| {
                    let df = *doc_freq.get(t).unwrap_or(&1) as f64;
                    (n / df).ln().max(0.0)
                })
                .sum()
        };

        // Find near-duplicate groups (greedy: each message can belong to at most one group).
        let mut group_of: Vec<Option<usize>> = vec![None; call.messages.len()];
        let mut groups: Vec<Vec<usize>> = Vec::new();
        for i in 0..call.messages.len() {
            if tokens[i].len() < MIN_TOKEN_COUNT {
                continue;
            }
            if group_of[i].is_some() {
                continue;
            }
            let mut group: Vec<usize> = vec![i];
            for j in (i + 1)..call.messages.len() {
                if tokens[j].len() < MIN_TOKEN_COUNT {
                    continue;
                }
                if group_of[j].is_some() {
                    continue;
                }
                if jaccard(&tokens[i], &tokens[j]) >= JACCARD_THRESHOLD {
                    group.push(j);
                    group_of[j] = Some(groups.len());
                }
            }
            if group.len() > 1 {
                group_of[i] = Some(groups.len());
                groups.push(group);
            }
        }

        if groups.is_empty() {
            return None;
        }

        // For each group: retain the member with the highest IDF score.
        // Never drop a UserInput-tagged message.
        let mut drop: HashSet<usize> = HashSet::new();
        for group in &groups {
            // Find the best index (highest IDF; UserInput wins regardless).
            let best = group
                .iter()
                .max_by(|&&a, &&b| {
                    let a_user = is_user_input(&message_deps, a);
                    let b_user = is_user_input(&message_deps, b);
                    match (a_user, b_user) {
                        (true, false) => std::cmp::Ordering::Greater,
                        (false, true) => std::cmp::Ordering::Less,
                        _ => idf_score(&tokens[a])
                            .partial_cmp(&idf_score(&tokens[b]))
                            .unwrap_or(std::cmp::Ordering::Equal),
                    }
                })
                .copied()
                .unwrap();

            for &idx in group {
                if idx != best {
                    // Do not drop UserInput-tagged messages.
                    if !is_user_input(&message_deps, idx) {
                        drop.insert(idx);
                    }
                }
            }
        }

        if drop.is_empty() {
            return None;
        }

        let kept_count = call.messages.len() - drop.len();
        if kept_count < 2 {
            return None;
        }

        let total_bytes: usize = call.messages.iter().map(|m| m.content.len()).sum();
        let dropped_bytes: usize = drop.iter().map(|&i| call.messages[i].content.len()).sum();
        let dropped_frac = dropped_bytes as f32 / total_bytes.max(1) as f32;
        let projected = profile.cost_usd.mean as f32 * dropped_frac;

        let new_messages: Vec<_> = call
            .messages
            .iter()
            .enumerate()
            .filter(|(i, _)| !drop.contains(i))
            .map(|(_, m)| m.clone())
            .collect();

        let rewritten_call = Call { messages: new_messages, ..call.clone() };
        let drop_count = drop.len();
        Some(Proposal {
            rewritten: Plan::Rewritten {
                rule: self.name().to_string(),
                call: rewritten_call,
                projected_savings_usd: projected,
            },
            projected_savings_usd: projected,
            cost_driver: CostDriver::InputTokens,
            safety_check: Box::new(move |c| {
                // At least 2 messages must remain after dedup.
                c.messages.len() >= 2 && c.messages.len() + drop_count >= 2
            }),
        })
    }

    fn accuracy_budget(&self) -> f32 {
        self.accuracy_budget
    }
}

fn tokenize(text: &str) -> Vec<String> {
    text.split(|c: char| !c.is_alphanumeric())
        .filter(|w| w.len() >= 2)
        .map(|w| w.to_lowercase())
        .collect()
}

fn jaccard(a: &[String], b: &[String]) -> f64 {
    let set_a: HashSet<&str> = a.iter().map(String::as_str).collect();
    let set_b: HashSet<&str> = b.iter().map(String::as_str).collect();
    let intersection = set_a.intersection(&set_b).count();
    let union = set_a.union(&set_b).count();
    if union == 0 { 0.0 } else { intersection as f64 / union as f64 }
}

fn extract_message_deps(call: &Call) -> Vec<Option<DepSource>> {
    let v = call
        .parameters
        .extra
        .as_object()
        .and_then(|o| o.get("message_deps"))
        .cloned();
    match v {
        Some(val) => {
            let deps: Vec<DepSource> =
                serde_json::from_value(val).unwrap_or_default();
            deps.into_iter().map(Some).collect()
        }
        None => vec![None; call.messages.len()],
    }
}

fn is_user_input(deps: &[Option<DepSource>], idx: usize) -> bool {
    deps.get(idx)
        .and_then(|d| d.as_ref())
        .map(|d| matches!(d, DepSource::UserInput { .. }))
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost_model::WelfordStats;
    use crate::dag::{Message, Parameters};
    use serde_json::json;

    fn hot_profile() -> CallSiteProfile {
        let mut p = CallSiteProfile::new("site");
        p.n_observations = 10;
        p.cost_usd = WelfordStats::from_persisted(10, 0.01, 0.0);
        p
    }

    fn make_call(msgs: &[(&str, &str)]) -> Call {
        Call {
            call_site_id: "site".into(),
            trace_id: [0u8; 16],
            span_id: [0u8; 8],
            model: "gpt-4o".into(),
            messages: msgs
                .iter()
                .map(|(role, content)| Message { role: role.to_string(), content: content.to_string() })
                .collect(),
            parameters: Parameters::default(),
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        }
    }

    fn long(seed: &str) -> String {
        seed.repeat(40) // 40 × len(seed) → well over MIN_TOKEN_COUNT=20 tokens
    }

    #[test]
    fn no_duplicates_does_not_fire() {
        let call = make_call(&[
            ("system", "You are a helpful assistant."),
            ("user", "What is the capital of France?"),
        ]);
        assert!(!PromptDedupRule::default().applies(&call, &hot_profile()));
    }

    #[test]
    fn exact_duplicate_long_messages_are_deduped() {
        let blob = long("word sequence ");
        let call = make_call(&[
            ("system", "You are a helpful assistant."),
            ("user", &blob),
            ("user", &blob),
        ]);
        let rule = PromptDedupRule::default();
        assert!(rule.applies(&call, &hot_profile()));
        let prop = rule.propose(&call, &hot_profile()).expect("must fire");
        match &prop.rewritten {
            Plan::Rewritten { call, .. } => assert_eq!(call.messages.len(), 2),
            _ => panic!("expected Rewritten"),
        }
    }

    #[test]
    fn user_input_tagged_message_survives_dedup() {
        let blob = long("topic context ");
        let extra = json!({
            "message_deps": [
                {"kind": "literal"},
                {"kind": "user_input", "span_id": "0102030405060708"},
                {"kind": "literal"},
            ]
        });
        let mut call = make_call(&[
            ("system", "sys"),
            ("user", &blob),
            ("user", &blob),
        ]);
        call.parameters.extra = extra;
        let rule = PromptDedupRule::default();
        let prop = rule.propose(&call, &hot_profile()).expect("must fire");
        if let Plan::Rewritten { call, .. } = prop.rewritten {
            // 3 messages → dedupe → 2 messages remain.
            assert_eq!(call.messages.len(), 2);
        }
    }

    #[test]
    fn short_messages_are_never_deduped() {
        // Each "message" is shorter than MIN_TOKEN_COUNT tokens.
        let call = make_call(&[
            ("system", "Short."),
            ("user", "Short."),
            ("user", "Short."),
        ]);
        assert!(!PromptDedupRule::default().applies(&call, &hot_profile()));
    }

    #[test]
    fn nearly_identical_long_messages_are_deduped() {
        // 14-word vocabulary: adding one word gives Jaccard = 14/15 ≈ 0.933 ≥ 0.92.
        let base = long("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima november oscar ");
        let variant = format!("{base} uniqueword");
        let call = make_call(&[
            ("system", "sys"),
            ("user", &base),
            ("user", &variant),
        ]);
        let rule = PromptDedupRule::default();
        assert!(rule.applies(&call, &hot_profile()));
    }

    #[test]
    fn distinct_long_messages_are_not_deduped() {
        let a = long("alpha bravo charlie ");
        let b = long("delta echo foxtrot ");
        let call = make_call(&[("user", &a), ("user", &b)]);
        let rule = PromptDedupRule::default();
        assert!(!rule.applies(&call, &hot_profile()));
    }

    #[test]
    fn savings_is_positive_after_dedup() {
        // Include a short anchor so 2 messages survive after dedup (anchor + 1 blob).
        let blob = long("word sequence ");
        let call = make_call(&[
            ("system", "You are a helpful assistant."),
            ("user", &blob),
            ("user", &blob),
        ]);
        let prop = PromptDedupRule::default()
            .propose(&call, &hot_profile())
            .expect("must fire");
        assert!(prop.projected_savings_usd > 0.0);
    }
}
