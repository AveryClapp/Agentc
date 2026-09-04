//! DAG-level types the planner reasons about.
//!
//! A [`Call`] is one intercepted LLM invocation; [`Outcome`] is the measured
//! result that the planner feeds back into the cost model. All fields are
//! `serde`-serializable so the Python interceptor can emit a `Call` as JSON
//! across the FFI boundary.

use serde::{Deserialize, Serialize};

/// `Parameters.extra` marker set by provider adapters when the native request
/// carries message fields that the string-only DAG cannot round-trip (for
/// example images, tool-use blocks, cache controls, or vendor message models).
pub const NATIVE_MESSAGES_OPAQUE_KEY: &str = "agentc_native_messages_opaque";

/// One message in a `Call`'s `messages` list. We treat `role` opaquely so
/// `system`/`user`/`assistant`/`tool` all round-trip without an enum per
/// vendor convention.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub role: String,
    pub content: String,
}

/// Sampling + shape parameters. Optional so a minimal `Call` round-trips
/// without every vendor field populated. The `extra` bag catches
/// vendor-specific fields (`response_format`, `seed`, etc.) without losing
/// them.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Parameters {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub top_p: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_output_tokens: Option<u32>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub stop: Vec<String>,
    #[serde(default, skip_serializing_if = "serde_json::Value::is_null")]
    pub extra: serde_json::Value,
}

/// Tool declaration as passed to the model. `schema` is opaque JSON (the
/// optimizer doesn't introspect it).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Tool {
    pub name: String,
    #[serde(default)]
    pub schema: serde_json::Value,
}

/// Provenance of a message's content. `ParallelBranch` and `StateDrop` need
/// these to be non-`Literal` to fire; otherwise the rule no-ops.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum DepSource {
    /// Hardcoded in user code (string literal in a template).
    Literal,
    /// Came from the trace's root input.
    UserInput {
        #[serde(with = "serde_hex8")]
        span_id: [u8; 8],
    },
    /// Came from a prior tool call's output.
    ToolOutput {
        #[serde(with = "serde_hex8")]
        span_id: [u8; 8],
    },
    /// Came from a prior LLM call's output.
    LlmOutput {
        #[serde(with = "serde_hex8")]
        span_id: [u8; 8],
    },
    /// Came from named agent state (needed for `StateDrop`).
    State { key: String },
}

/// One intercepted LLM call. The planner receives this from Python, looks
/// up the profile keyed on `call_site_id`, and emits a [`crate::Plan`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Call {
    pub call_site_id: String,
    #[serde(with = "serde_hex16")]
    pub trace_id: [u8; 16],
    #[serde(with = "serde_hex8")]
    pub span_id: [u8; 8],
    pub model: String,
    pub messages: Vec<Message>,
    #[serde(default)]
    pub parameters: Parameters,
    #[serde(default)]
    pub tools: Vec<Tool>,
    #[serde(default)]
    pub input_deps: Vec<DepSource>,
    #[serde(default)]
    pub occurrence_ix: u32,
}

impl Call {
    /// Whether message reconstruction from this DAG would be lossy.
    ///
    /// Provider adapters retain the original native objects on the Python
    /// side. The planner uses this marker to admit only rules that leave those
    /// objects untouched; message-reading/mutating rules conservatively
    /// abstain until the DAG can represent the full vendor shape.
    pub fn has_opaque_native_messages(&self) -> bool {
        self.parameters
            .extra
            .as_object()
            .and_then(|extra| extra.get(NATIVE_MESSAGES_OPAQUE_KEY))
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false)
    }

    /// Whether this call's messages are an ordered, byte-identical subset of
    /// `reference`. Message-drop passes use this to prove that composition did
    /// not reintroduce or replace content removed by an earlier pass.
    pub(crate) fn messages_are_ordered_subsequence_of(&self, reference: &Self) -> bool {
        if self.messages.len() > reference.messages.len() {
            return false;
        }
        let mut next_reference = 0;
        for retained in &self.messages {
            let Some(relative_index) = reference.messages[next_reference..]
                .iter()
                .position(|original| {
                    original.role == retained.role && original.content == retained.content
                })
            else {
                return false;
            };
            next_reference += relative_index + 1;
        }
        true
    }
}

/// Measured result of executing a [`Plan`]. Fed into
/// `Optimizer::observe` after the user-visible response lands.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Outcome {
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub latency_ms: f64,
    pub cost_usd: f64,
    /// True if the output parsed as JSON.
    #[serde(default)]
    pub output_is_structured: bool,
    /// True if `output_tokens <= 128`.
    #[serde(default)]
    pub output_is_short: bool,
    /// Call site this outcome belongs to. Required for `Plan::PassThrough`
    /// (where we can't recover the site from the plan itself); the FFI
    /// path falls back to this when the plan doesn't carry a `Call`.
    /// Optional so existing callers that always wrap a Rewritten/Parallel
    /// plan keep compiling.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub call_site_id: Option<String>,
    /// True when the selected plan failed before returning a usable response
    /// and the adapter replayed the exact reference request once.
    #[serde(default)]
    pub dispatch_fallback: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dispatch_fallback_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_protocol: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_namespace: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_model_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_model_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub price_table_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub catalog_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub executed_model_id: Option<String>,
}

mod serde_hex8 {
    use serde::{de::Error, Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(bytes: &[u8; 8], s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&hex_encode(bytes))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<[u8; 8], D::Error> {
        let s = String::deserialize(d)?;
        let v = hex_decode(&s).map_err(D::Error::custom)?;
        v.try_into()
            .map_err(|_| D::Error::custom("expected 8-byte hex span_id"))
    }

    fn hex_encode(bytes: &[u8]) -> String {
        let mut out = String::with_capacity(bytes.len() * 2);
        for b in bytes {
            out.push_str(&format!("{:02x}", b));
        }
        out
    }

    fn hex_decode(s: &str) -> Result<Vec<u8>, String> {
        if !s.len().is_multiple_of(2) {
            return Err("odd-length hex".into());
        }
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).map_err(|e| e.to_string()))
            .collect()
    }
}

mod serde_hex16 {
    use serde::{de::Error, Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(bytes: &[u8; 16], s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&hex_encode(bytes))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<[u8; 16], D::Error> {
        let s = String::deserialize(d)?;
        let v = hex_decode(&s).map_err(D::Error::custom)?;
        v.try_into()
            .map_err(|_| D::Error::custom("expected 16-byte hex trace_id"))
    }

    fn hex_encode(bytes: &[u8]) -> String {
        let mut out = String::with_capacity(bytes.len() * 2);
        for b in bytes {
            out.push_str(&format!("{:02x}", b));
        }
        out
    }

    fn hex_decode(s: &str) -> Result<Vec<u8>, String> {
        if !s.len().is_multiple_of(2) {
            return Err("odd-length hex".into());
        }
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).map_err(|e| e.to_string()))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn call_roundtrips_through_json() {
        let call = Call {
            call_site_id: "app.agents.planner:plan_next_step".into(),
            trace_id: [1u8; 16],
            span_id: [2u8; 8],
            model: "gpt-4o".into(),
            messages: vec![
                Message {
                    role: "system".into(),
                    content: "You are a planner.".into(),
                },
                Message {
                    role: "user".into(),
                    content: "Plan lunch.".into(),
                },
            ],
            parameters: Parameters {
                temperature: Some(0.7),
                ..Parameters::default()
            },
            tools: vec![],
            input_deps: vec![
                DepSource::Literal,
                DepSource::UserInput { span_id: [3u8; 8] },
            ],
            occurrence_ix: 0,
        };
        let json = serde_json::to_string(&call).unwrap();
        let back: Call = serde_json::from_str(&json).unwrap();
        assert_eq!(back.call_site_id, call.call_site_id);
        assert_eq!(back.trace_id, call.trace_id);
        assert_eq!(back.span_id, call.span_id);
        assert_eq!(back.messages.len(), 2);
        assert!(matches!(back.input_deps[1], DepSource::UserInput { .. }));
    }

    #[test]
    fn outcome_roundtrips_through_json() {
        let outcome = Outcome {
            input_tokens: 200,
            output_tokens: 50,
            latency_ms: 123.4,
            cost_usd: 0.0012,
            output_is_structured: false,
            output_is_short: true,
            call_site_id: None,
            dispatch_fallback: true,
            target_model_id: Some("gpt-5.4-mini-2026-03-17".into()),
            ..Outcome::default()
        };
        let json = serde_json::to_string(&outcome).unwrap();
        let back: Outcome = serde_json::from_str(&json).unwrap();
        assert_eq!(back.input_tokens, outcome.input_tokens);
        assert!(back.output_is_short);
        assert!(back.dispatch_fallback);
        assert_eq!(back.target_model_id, outcome.target_model_id);
    }

    #[test]
    fn opaque_native_message_marker_is_conservative() {
        let mut call = Call {
            call_site_id: "site".into(),
            trace_id: [0u8; 16],
            span_id: [0u8; 8],
            model: "model".into(),
            messages: vec![],
            parameters: Parameters::default(),
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        };
        assert!(!call.has_opaque_native_messages());
        call.parameters.extra = serde_json::json!({NATIVE_MESSAGES_OPAQUE_KEY: true});
        assert!(call.has_opaque_native_messages());
        call.parameters.extra = serde_json::json!({NATIVE_MESSAGES_OPAQUE_KEY: "true"});
        assert!(!call.has_opaque_native_messages());
    }

    #[test]
    fn dep_source_state_encodes_as_tagged_object() {
        let dep = DepSource::State {
            key: "plan_memory".into(),
        };
        let json = serde_json::to_string(&dep).unwrap();
        assert!(json.contains("\"state\""), "tag missing: {json}");
        assert!(json.contains("plan_memory"));
    }
}
