//! Forced rule materialization for a fixed-model benchmark, NOT plan admission.
//!
//! One JSON request on stdin: {"rule":"ContextCompress"|"StateDrop","call":Call}.
//! One JSON response: {rule,applied,call,retained_indices,materialization_only:true}.
//! `call` preserves the input JSON exactly except for native-selected messages.
//! Aligned metadata remains RAW, as in the native proposal: the caller must remap
//! message_deps/attention_scores/input_deps using retained_indices before reuse.
//! No optimizer, catalog, profile observations, routing, or provider calls run.

use std::collections::BTreeMap;
use std::io::{self, Read};

use agentc_optimizer::{
    Call, CallSiteProfile, ContextCompressRule, Message, Plan, RewriteRule, StateDropRule,
};
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;

const MAX_REQUEST_BYTES: u64 = 8 * 1024 * 1024;

#[derive(Debug, Deserialize)]
enum Rule {
    ContextCompress,
    StateDrop,
}

impl Rule {
    fn implementation(&self) -> Box<dyn RewriteRule> {
        match self {
            Self::ContextCompress => Box::new(ContextCompressRule::default()),
            Self::StateDrop => Box::new(StateDropRule::default()),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    rule: Rule,
    call: Box<RawValue>,
}

#[derive(Serialize)]
struct Response {
    rule: String,
    applied: bool,
    call: BTreeMap<String, Box<RawValue>>,
    retained_indices: Vec<usize>,
    materialization_only: bool,
}

fn same_message(left: &Message, right: &Message) -> bool {
    left.role == right.role && left.content == right.content
}

/// Earliest and latest embeddings coincide exactly when the mapping is unique.
/// Reject ambiguity rather than attaching the wrong provenance to equal text.
fn retained_indices(
    original: &[Message],
    selected: &[Message],
) -> Result<Vec<usize>, &'static str> {
    let mut earliest = Vec::with_capacity(selected.len());
    let mut cursor = 0;
    for message in selected {
        let offset = original[cursor..]
            .iter()
            .position(|candidate| same_message(candidate, message))
            .ok_or("native proposal is not an ordered message deletion")?;
        cursor += offset;
        earliest.push(cursor);
        cursor += 1;
    }
    let mut latest = Vec::with_capacity(selected.len());
    cursor = original.len();
    for message in selected.iter().rev() {
        cursor = original[..cursor]
            .iter()
            .rposition(|candidate| same_message(candidate, message))
            .ok_or("native proposal is not an ordered message deletion")?;
        latest.push(cursor);
    }
    latest.reverse();
    if earliest != latest {
        return Err("native message deletion has ambiguous provenance");
    }
    Ok(earliest)
}

fn materialize(request: Request) -> Result<String, &'static str> {
    let original: Call =
        serde_json::from_str(request.call.get()).map_err(|_| "invalid native call shape")?;
    if original.has_opaque_native_messages() || !original.tools.is_empty() {
        return Err("only plain text calls without tools are supported");
    }
    let mut raw_call: BTreeMap<String, Box<RawValue>> =
        serde_json::from_str(request.call.get()).map_err(|_| "call must be an object")?;
    let raw_messages: Vec<Box<RawValue>> =
        serde_json::from_str(raw_call.get("messages").ok_or("missing messages")?.get())
            .map_err(|_| "messages must be an array")?;
    for message in &raw_messages {
        let fields: BTreeMap<String, Box<RawValue>> =
            serde_json::from_str(message.get()).map_err(|_| "message must be an object")?;
        if fields.len() != 2 || !fields.contains_key("role") || !fields.contains_key("content") {
            return Err("native message representation would lose fields");
        }
    }

    let rule = request.rule.implementation();
    // These two rules use this empty profile only for an unused savings
    // projection. No measured statistics are invented or observed by a planner.
    let profile = CallSiteProfile::new(original.call_site_id.clone());
    let selected = if rule.applies(&original, &profile) {
        match rule.propose(&original, &profile) {
            Some(proposal) if (proposal.safety_check)(&original) => match proposal.rewritten {
                Plan::Rewritten { call, .. } => call,
                _ => return Err("rule produced an unsupported plan kind"),
            },
            _ => original.clone(),
        }
    } else {
        original.clone()
    };

    // Compare native-normalized values only to prove the rule touched messages
    // alone. Emit RawValue tokens from the original JSON: even parsing into
    // Value can round f64 attention metadata without serde's float_roundtrip.
    // Neither serde defaults nor numeric normalization may alter the request.
    let mut original_normalized =
        serde_json::to_value(&original).map_err(|_| "native original serialization failed")?;
    let mut selected_normalized =
        serde_json::to_value(&selected).map_err(|_| "native proposal serialization failed")?;
    original_normalized
        .as_object_mut()
        .unwrap()
        .remove("messages");
    selected_normalized
        .as_object_mut()
        .unwrap()
        .remove("messages");
    if original_normalized != selected_normalized {
        return Err("native rule changed non-message fields");
    }
    let retained = retained_indices(&original.messages, &selected.messages)?;
    let messages: Vec<_> = retained
        .iter()
        .map(|&index| raw_messages[index].clone())
        .collect();
    let applied = retained.len() != original.messages.len();
    raw_call.insert(
        "messages".into(),
        RawValue::from_string(
            serde_json::to_string(&messages).map_err(|_| "message serialization failed")?,
        )
        .map_err(|_| "invalid serialized messages")?,
    );
    serde_json::to_string(&Response {
        rule: rule.name().into(),
        applied,
        call: raw_call,
        retained_indices: retained,
        materialization_only: true,
    })
    .map_err(|_| "response serialization failed")
}

fn run(input: &str) -> Result<String, &'static str> {
    if input.len() as u64 > MAX_REQUEST_BYTES {
        return Err("request exceeds materializer byte limit");
    }
    let request = serde_json::from_str(input).map_err(|_| "invalid materializer request JSON")?;
    materialize(request)
}

fn main() {
    // catch_unwind alone does not suppress the default panic diagnostic.
    std::panic::set_hook(Box::new(|_| {}));
    let result = std::panic::catch_unwind(|| {
        let mut input = String::new();
        io::stdin()
            .take(MAX_REQUEST_BYTES + 1)
            .read_to_string(&mut input)
            .map_err(|_| "failed to read bounded UTF-8 request")?;
        run(&input)
    });
    match result {
        Ok(Ok(output)) => println!("{output}"),
        _ => {
            // No raw input, prompts, or provider data in diagnostics.
            eprintln!("rule materialization rejected the request");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    fn base(messages: Value, extra: Value) -> Value {
        json!({
            "call_site_id":"typed/test/filter", "trace_id":"00000000000000000000000000000000",
            "span_id":"0000000000000000", "model":"fixed-model",
            "messages":messages, "parameters":{"temperature":0.123456789,"extra":extra},
            "extra_unknown":{"preserve":true},
        })
    }

    fn state_call() -> Value {
        base(
            json!([
                {"role":"system","content":"system"},
                {"role":"user","content":"stale docs"},
                {"role":"assistant","content":"current synthesis"},
                {"role":"user","content":"question"},
            ]),
            json!({
                "message_deps":[{"kind":"literal"},{"kind":"state","key":"docs"},
                    {"kind":"state","key":"synthesis"},{"kind":"user_input","span_id":"0000000000000000"}],
                "window_state_reads":["synthesis"],
                "attention_scores":[1,0,1,1],
                "response_format":{"type":"json_schema","json_schema":{"schema":{"type":"object"}}},
                "agentc_route_context":{"structured_outputs":true},
            }),
        )
    }

    fn cc_call() -> Value {
        let mut messages = vec![json!({"role":"system","content":"system"})];
        let mut deps = vec![json!({"kind":"literal"})];
        let mut scores = vec![json!(1)];
        for index in 0..20 {
            messages.push(
                json!({"role":"user","content":format!("document-{index} {}", "x".repeat(600))}),
            );
            deps.push(json!({"kind":"literal"}));
            scores.push(json!(if index < 3 { 1 } else { 0 }));
        }
        messages.push(json!({"role":"user","content":"question"}));
        deps.push(json!({"kind":"user_input","span_id":"0000000000000000"}));
        scores.push(json!(0));
        base(
            json!(messages),
            json!({"message_deps":deps,"attention_scores":scores}),
        )
    }

    fn apply(rule: &str, call: &Value) -> Result<Value, &'static str> {
        serde_json::from_str(&run(&json!({"rule":rule,"call":call}).to_string())?)
            .map_err(|_| "invalid test output")
    }

    #[test]
    fn raw_numeric_tokens_are_preserved_without_float_roundtrip() {
        let input = json!({"rule":"StateDrop","call":state_call()})
            .to_string()
            .replace("0.123456789", "0.12379559965065445");
        let output = run(&input).unwrap();
        assert!(output.contains("\"temperature\":0.12379559965065445"));
    }

    #[test]
    fn state_drop_calls_real_rule_and_keeps_raw_non_message_values() {
        let call = state_call();
        let output = apply("StateDrop", &call).unwrap();
        assert_eq!(output["retained_indices"], json!([0, 2, 3]));
        assert_eq!(output["applied"], true);
        assert_eq!(output["materialization_only"], true);
        let mut expected = call.clone();
        expected["messages"] = json!([
            call["messages"][0],
            call["messages"][2],
            call["messages"][3]
        ]);
        assert_eq!(output["call"], expected);
        assert!(output["call"].get("tools").is_none());
        assert!(
            output["call"]["parameters"]["extra"]["agentc_route_context"]
                .get("input_tokens_upper_bound")
                .is_none()
        );
        assert_eq!(
            output["call"]["parameters"]["extra"]["message_deps"]
                .as_array()
                .unwrap()
                .len(),
            4
        );
    }

    #[test]
    fn context_compress_uses_native_protection_and_drops_seventeen_docs() {
        let output = apply("ContextCompress", &cc_call()).unwrap();
        assert_eq!(output["retained_indices"], json!([0, 1, 2, 3, 21]));
        assert_eq!(output["call"]["messages"].as_array().unwrap().len(), 5);
        assert_eq!(output["applied"], true);
    }

    #[test]
    fn no_proposal_and_inapplicable_rule_return_exact_original_json() {
        let mut call = state_call();
        call["parameters"]["extra"]["window_state_reads"] = json!(["docs", "synthesis"]);
        for rule in ["StateDrop", "ContextCompress"] {
            let output = apply(rule, &call).unwrap();
            assert_eq!(output["applied"], false);
            assert_eq!(output["call"], call);
            assert_eq!(output["retained_indices"], json!([0, 1, 2, 3]));
        }
    }

    #[test]
    fn actual_native_safety_failure_does_not_materialize_proposal() {
        let mut call = state_call();
        call["parameters"]["extra"]["message_deps"][0] = json!({"kind":"state","key":"docs"});
        let original: Call = serde_json::from_value(call.clone()).unwrap();
        let rule = StateDropRule::default();
        let proposal = rule
            .propose(&original, &CallSiteProfile::new("test"))
            .unwrap();
        assert!(!(proposal.safety_check)(&original));
        let output = apply("StateDrop", &call).unwrap();
        assert_eq!(output["applied"], false);
        assert_eq!(output["call"], call);
    }

    #[test]
    fn ambiguous_identical_message_provenance_is_rejected() {
        let mut call = state_call();
        call["messages"][2] = call["messages"][1].clone();
        assert!(apply("StateDrop", &call).unwrap_err().contains("ambiguous"));
    }

    #[test]
    fn index_mapping_checks_order_content_and_unique_duplicate_embedding() {
        let message = |content: &str| Message {
            role: "user".into(),
            content: content.into(),
        };
        let original = vec![message("a"), message("b"), message("a")];
        assert_eq!(
            retained_indices(&original, &original).unwrap(),
            vec![0, 1, 2]
        );
        assert!(retained_indices(&original, &[message("a")]).is_err());
        assert!(retained_indices(&original, &[message("absent")]).is_err());
        assert!(retained_indices(&original, &[message("b"), message("b")]).is_err());
    }

    #[test]
    fn opaque_tools_or_unknown_message_fields_are_rejected() {
        let mut opaque = state_call();
        opaque["parameters"]["extra"]["agentc_native_messages_opaque"] = json!(true);
        let mut tools = state_call();
        tools["tools"] = json!([{"name":"tool"}]);
        let mut named = state_call();
        named["messages"][0]["name"] = json!("vendor-field");
        for call in [opaque, tools, named] {
            assert!(apply("StateDrop", &call).is_err());
        }
    }

    #[test]
    fn unknown_labels_do_not_affect_native_message_selection() {
        let mut call = cc_call();
        let before = apply("ContextCompress", &call).unwrap();
        call["expected"] = json!("not used by materializer");
        let after = apply("ContextCompress", &call).unwrap();
        assert_eq!(before["retained_indices"], after["retained_indices"]);
        assert_eq!(before["call"]["messages"], after["call"]["messages"]);
    }

    #[test]
    fn unsupported_or_non_json_input_is_rejected() {
        let call = state_call();
        for input in [
            json!({"rule":"ModelDowngrade","call":call}).to_string(),
            json!({"rule":"StateDrop","call":call,"unreviewed":true}).to_string(),
            format!("{} trailing", json!({"rule":"StateDrop","call":call})),
            "null".into(),
            " ".repeat(MAX_REQUEST_BYTES as usize + 1),
        ] {
            assert!(run(&input).is_err());
        }
    }
}
