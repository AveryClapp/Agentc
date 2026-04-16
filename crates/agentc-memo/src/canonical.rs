//! Canonicalization mirror used for FFI parity tests.
//!
//! The authoritative canonicalizer lives in
//! `python/agentc/_canonicalize/`. This module implements the same
//! contract in Rust so fixture-level equality tests catch drift between
//! the two implementations before they corrupt cache hashes.
//!
//! The public surface is two functions:
//!
//! * [`canonicalize_prompt`] — accepts a `serde_json::Value` prompt plus a
//!   provider tag and returns deterministic UTF-8 JSON bytes.
//! * [`canonicalize_parameters`] — filters and normalizes sampling
//!   parameters.
//!
//! Both functions produce byte-for-byte identical output to their Python
//! counterparts on the fixture corpus.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const FLOAT_ROUND_PLACES: i32 = 6;

const PARAM_RETAIN: &[&str] = &[
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "stop",
    "seed",
    "response_format",
    "tool_choice",
    "frequency_penalty",
    "presence_penalty",
    "logit_bias",
];

/// Canonicalize a prompt given the vendor format tag.
pub fn canonicalize_prompt(raw: &Value, provider: &str) -> Vec<u8> {
    let prov = provider.trim().to_ascii_lowercase();
    let envelope = match prov.as_str() {
        "openai" => openai_envelope(raw),
        "anthropic" => anthropic_envelope(raw),
        "cohere" => cohere_envelope(raw),
        _ => raw_envelope(raw),
    };
    deterministic_dumps(&envelope)
}

/// Canonicalize sampling parameters.
pub fn canonicalize_parameters(raw: &Value) -> Vec<u8> {
    let obj = match raw.as_object() {
        Some(o) => o,
        None => return deterministic_dumps(&json!({})),
    };
    let mut out = Map::new();
    for key in PARAM_RETAIN {
        if let Some(v) = obj.get(*key) {
            out.insert((*key).to_string(), normalize_param_value(key, v));
        }
    }
    deterministic_dumps(&Value::Object(out))
}

/// Serialize any JSON value with sorted keys and no insignificant whitespace.
pub fn deterministic_dumps(value: &Value) -> Vec<u8> {
    let mut buf = Vec::new();
    write_value(&mut buf, value);
    buf
}

fn write_value(buf: &mut Vec<u8>, value: &Value) {
    match value {
        Value::Null => buf.extend_from_slice(b"null"),
        Value::Bool(b) => buf.extend_from_slice(if *b { b"true" } else { b"false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                buf.extend_from_slice(i.to_string().as_bytes());
            } else if let Some(u) = n.as_u64() {
                buf.extend_from_slice(u.to_string().as_bytes());
            } else if let Some(f) = n.as_f64() {
                buf.extend_from_slice(format_float(f).as_bytes());
            } else {
                buf.extend_from_slice(n.to_string().as_bytes());
            }
        }
        Value::String(s) => {
            buf.push(b'"');
            write_json_string(buf, s);
            buf.push(b'"');
        }
        Value::Array(arr) => {
            buf.push(b'[');
            for (i, v) in arr.iter().enumerate() {
                if i > 0 {
                    buf.push(b',');
                }
                write_value(buf, v);
            }
            buf.push(b']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            buf.push(b'{');
            for (i, key) in keys.iter().enumerate() {
                if i > 0 {
                    buf.push(b',');
                }
                buf.push(b'"');
                write_json_string(buf, key);
                buf.extend_from_slice(b"\":");
                write_value(buf, &map[*key]);
            }
            buf.push(b'}');
        }
    }
}

fn write_json_string(buf: &mut Vec<u8>, s: &str) {
    for c in s.chars() {
        match c {
            '"' => buf.extend_from_slice(b"\\\""),
            '\\' => buf.extend_from_slice(b"\\\\"),
            '\n' => buf.extend_from_slice(b"\\n"),
            '\r' => buf.extend_from_slice(b"\\r"),
            '\t' => buf.extend_from_slice(b"\\t"),
            '\u{08}' => buf.extend_from_slice(b"\\b"),
            '\u{0C}' => buf.extend_from_slice(b"\\f"),
            c if (c as u32) < 0x20 => {
                buf.extend_from_slice(format!("\\u{:04x}", c as u32).as_bytes());
            }
            c => {
                let mut tmp = [0u8; 4];
                let encoded = c.encode_utf8(&mut tmp);
                buf.extend_from_slice(encoded.as_bytes());
            }
        }
    }
}

fn format_float(f: f64) -> String {
    // Mirror Python's json.dumps for simple decimals. For integer-valued
    // floats Python emits "1.0" etc. We round at call-sites before
    // serializing so this path only formats finite, already-rounded values.
    if f == 0.0 {
        return "0.0".to_string();
    }
    if f.fract() == 0.0 && f.abs() < 1e16 {
        return format!("{:.1}", f);
    }
    // Python's repr for floats is round-trippable; format with enough digits.
    let s = format!("{}", f);
    if s.contains('.') || s.contains('e') {
        s
    } else {
        format!("{}.0", s)
    }
}

fn round_float(value: f64) -> f64 {
    let scale = 10f64.powi(FLOAT_ROUND_PLACES);
    let rounded = (value * scale).round() / scale;
    if rounded == 0.0 {
        0.0
    } else {
        rounded
    }
}

fn sha256_hex(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    let mut hex = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write;
        let _ = write!(hex, "{:02x}", byte);
    }
    hex
}

fn sha256_value_hex(value: &Value) -> String {
    sha256_hex(&deterministic_dumps(value))
}

fn sha256_str_hex(s: &str) -> String {
    sha256_hex(s.as_bytes())
}

fn normalize_role(role: Option<&Value>) -> String {
    let raw = match role {
        Some(Value::String(s)) => s.trim().to_ascii_lowercase(),
        _ => return "user".to_string(),
    };
    match raw.as_str() {
        "system" | "user" | "assistant" | "tool" => raw,
        "human" => "user".to_string(),
        "ai" | "model" => "assistant".to_string(),
        _ => "user".to_string(),
    }
}

fn normalize_content(content: Option<&Value>) -> Value {
    match content {
        None | Some(Value::Null) => Value::String(String::new()),
        Some(Value::String(s)) => Value::String(s.trim().to_string()),
        Some(Value::Array(parts)) => {
            let normalized: Vec<Value> = parts.iter().map(normalize_part).collect();
            let all_text = normalized
                .iter()
                .all(|p| p.get("type").and_then(Value::as_str) == Some("text"));
            if all_text {
                let joined: String = normalized
                    .iter()
                    .map(|p| p.get("text").and_then(Value::as_str).unwrap_or(""))
                    .collect();
                Value::String(joined.trim().to_string())
            } else {
                Value::Array(normalized)
            }
        }
        Some(other) => Value::String(value_to_plain_string(other).trim().to_string()),
    }
}

fn normalize_part(part: &Value) -> Value {
    let obj = match part {
        Value::String(s) => {
            return json!({"type": "text", "text": s});
        }
        Value::Object(m) => m,
        other => {
            return json!({"type": "text", "text": value_to_plain_string(other)});
        }
    };
    let kind = obj
        .get("type")
        .and_then(Value::as_str)
        .map(|s| s.to_string())
        .unwrap_or_else(|| infer_part_type(obj));
    match kind.as_str() {
        "text" => {
            let text = obj
                .get("text")
                .or_else(|| obj.get("content"))
                .map(value_to_plain_string)
                .unwrap_or_default();
            json!({"type": "text", "text": text})
        }
        "image" | "image_url" => {
            json!({"type": "image", "sha256": hash_multimodal_payload(obj)})
        }
        "audio" | "input_audio" => {
            json!({"type": "audio", "sha256": hash_multimodal_payload(obj)})
        }
        "document" | "file" => {
            json!({"type": "document", "sha256": hash_multimodal_payload(obj)})
        }
        "tool_use" => {
            let name = obj
                .get("name")
                .map(value_to_plain_string)
                .unwrap_or_default();
            let input = obj.get("input").cloned().unwrap_or(Value::Object(Map::new()));
            json!({
                "type": "tool_use",
                "name": name,
                "input_sha256": sha256_value_hex(&input),
            })
        }
        "tool_result" => {
            let id = obj
                .get("tool_use_id")
                .map(value_to_plain_string)
                .unwrap_or_default();
            let inner = obj.get("content");
            let inner_norm = match inner {
                Some(Value::Array(_)) => normalize_content(inner),
                Some(other) => Value::String(value_to_plain_string(other).trim().to_string()),
                None => Value::String(String::new()),
            };
            json!({
                "type": "tool_result",
                "tool_use_id": id,
                "content": inner_norm,
            })
        }
        other => json!({
            "type": other.to_string(),
            "sha256": hash_multimodal_payload(obj),
        }),
    }
}

fn infer_part_type(obj: &Map<String, Value>) -> String {
    if obj.contains_key("text") {
        return "text".to_string();
    }
    if obj.contains_key("image_url") || obj.contains_key("image") || obj.contains_key("source") {
        return "image".to_string();
    }
    if obj.contains_key("audio") || obj.contains_key("input_audio") {
        return "audio".to_string();
    }
    "unknown".to_string()
}

fn hash_multimodal_payload(obj: &Map<String, Value>) -> String {
    for key in ["data", "bytes", "b64", "base64"] {
        if let Some(v) = obj.get(key) {
            if let Some(s) = v.as_str() {
                if !s.is_empty() {
                    return sha256_str_hex(s);
                }
            }
        }
    }
    if let Some(Value::Object(source)) = obj.get("source") {
        for key in ["data", "bytes", "b64", "base64", "url"] {
            if let Some(v) = source.get(key) {
                if let Some(s) = v.as_str() {
                    if !s.is_empty() {
                        return sha256_str_hex(s);
                    }
                }
            }
        }
    }
    if let Some(iu) = obj.get("image_url") {
        match iu {
            Value::Object(m) => {
                if let Some(Value::String(url)) = m.get("url") {
                    return sha256_str_hex(url);
                }
            }
            Value::String(url) => return sha256_str_hex(url),
            _ => {}
        }
    }
    sha256_value_hex(&Value::Object(obj.clone()))
}

fn value_to_plain_string(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        other => serde_json::to_string(other).unwrap_or_default(),
    }
}

fn normalize_tools(tools: Option<&Value>) -> Vec<Value> {
    let arr = match tools {
        Some(Value::Array(a)) => a,
        _ => return Vec::new(),
    };
    let mut out: Vec<(String, String)> = Vec::new();
    for tool in arr {
        let obj = match tool {
            Value::Object(m) => m,
            _ => continue,
        };
        let (name, schema) = extract_tool_name_and_schema(obj);
        if name.is_empty() {
            continue;
        }
        let schema_hash = sha256_value_hex(&schema);
        out.push((name, schema_hash));
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out.into_iter()
        .map(|(name, schema_hash)| json!({"name": name, "schema_hash": schema_hash}))
        .collect()
}

fn extract_tool_name_and_schema(obj: &Map<String, Value>) -> (String, Value) {
    if let Some(Value::Object(fn_obj)) = obj.get("function") {
        let name = fn_obj
            .get("name")
            .map(value_to_plain_string)
            .unwrap_or_default();
        let schema = fn_obj
            .get("parameters")
            .or_else(|| fn_obj.get("schema"))
            .cloned()
            .unwrap_or(Value::Object(Map::new()));
        return (name, schema);
    }
    let name = obj
        .get("name")
        .map(value_to_plain_string)
        .unwrap_or_default();
    let schema = obj
        .get("input_schema")
        .or_else(|| obj.get("parameters"))
        .or_else(|| obj.get("parameter_definitions"))
        .or_else(|| obj.get("schema"))
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    (name, schema)
}

fn response_schema_hash(response_format: Option<&Value>) -> Value {
    let rf = match response_format {
        Some(Value::Null) | None => return Value::Null,
        Some(v) => v,
    };
    match rf {
        Value::Object(m) => {
            if m.is_empty() {
                return Value::Null;
            }
            let schema = m
                .get("json_schema")
                .or_else(|| m.get("schema"))
                .cloned()
                .unwrap_or_else(|| Value::Object(m.clone()));
            Value::String(sha256_value_hex(&schema))
        }
        Value::String(s) => {
            if s.is_empty() {
                Value::Null
            } else {
                Value::String(sha256_str_hex(s))
            }
        }
        _ => Value::String(sha256_value_hex(rf)),
    }
}

fn build_envelope(
    provider: &str,
    messages: Vec<Value>,
    tools: Option<&Value>,
    response_format: Option<&Value>,
) -> Value {
    json!({
        "provider": provider,
        "messages": messages,
        "tools": normalize_tools(tools),
        "response_schema_hash": response_schema_hash(response_format),
    })
}

fn openai_envelope(raw: &Value) -> Value {
    let (messages, tools, response_format) = openai_extract(raw);
    let norm: Vec<Value> = messages.iter().map(openai_message).collect();
    build_envelope("openai", norm, tools.as_ref(), response_format.as_ref())
}

fn openai_extract(raw: &Value) -> (Vec<Value>, Option<Value>, Option<Value>) {
    match raw {
        Value::Object(m) => {
            let messages = m
                .get("messages")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            (
                messages,
                m.get("tools").cloned(),
                m.get("response_format").cloned(),
            )
        }
        Value::Array(a) => (a.clone(), None, None),
        _ => (Vec::new(), None, None),
    }
}

fn openai_message(msg: &Value) -> Value {
    let obj = match msg {
        Value::Object(m) => m,
        other => {
            return json!({
                "role": "user",
                "content": value_to_plain_string(other).trim().to_string(),
            });
        }
    };
    let role = normalize_role(obj.get("role"));
    let mut content = normalize_content(obj.get("content"));

    if let Some(Value::Array(calls)) = obj.get("tool_calls") {
        let call_parts: Vec<Value> = calls.iter().map(openai_tool_call).collect();
        content = match content {
            Value::String(text) if !text.is_empty() => {
                let mut combined = vec![json!({"type": "text", "text": text})];
                combined.extend(call_parts);
                Value::Array(combined)
            }
            Value::String(_) => Value::Array(call_parts),
            Value::Array(mut existing) => {
                existing.extend(call_parts);
                Value::Array(existing)
            }
            other => {
                let mut combined = vec![other];
                combined.extend(call_parts);
                Value::Array(combined)
            }
        };
    }

    if role == "tool" {
        let tool_call_id = obj
            .get("tool_call_id")
            .map(value_to_plain_string)
            .unwrap_or_default();
        return json!({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        });
    }
    json!({"role": role, "content": content})
}

fn openai_tool_call(call: &Value) -> Value {
    let obj = match call {
        Value::Object(m) => m,
        _ => {
            return json!({
                "type": "tool_use",
                "name": "",
                "input_sha256": sha256_str_hex("{}"),
            });
        }
    };
    let (name, arg_bytes) = if let Some(Value::Object(fn_obj)) = obj.get("function") {
        let name = fn_obj
            .get("name")
            .map(value_to_plain_string)
            .unwrap_or_default();
        let args = fn_obj.get("arguments");
        let bytes = match args {
            Some(Value::String(s)) => s.as_bytes().to_vec(),
            Some(other) => value_to_plain_string(other).into_bytes(),
            None => Vec::new(),
        };
        (name, bytes)
    } else {
        let name = obj
            .get("name")
            .map(value_to_plain_string)
            .unwrap_or_default();
        let args = obj.get("arguments");
        let bytes = match args {
            Some(Value::String(s)) => s.as_bytes().to_vec(),
            Some(other) => value_to_plain_string(other).into_bytes(),
            None => Vec::new(),
        };
        (name, bytes)
    };
    json!({
        "type": "tool_use",
        "name": name,
        "input_sha256": sha256_hex(&arg_bytes),
    })
}

fn anthropic_envelope(raw: &Value) -> Value {
    let (messages, system, tools, response_format) = anthropic_extract(raw);
    let mut norm: Vec<Value> = Vec::new();
    if let Some(sys) = system {
        let content = normalize_content(Some(&sys));
        norm.push(json!({"role": "system", "content": content}));
    }
    for m in messages {
        norm.push(anthropic_message(&m));
    }
    build_envelope("anthropic", norm, tools.as_ref(), response_format.as_ref())
}

fn anthropic_extract(
    raw: &Value,
) -> (Vec<Value>, Option<Value>, Option<Value>, Option<Value>) {
    match raw {
        Value::Object(m) => {
            let messages = m
                .get("messages")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let response_format = m
                .get("response_format")
                .cloned()
                .or_else(|| m.get("output_schema").cloned());
            (
                messages,
                m.get("system").cloned(),
                m.get("tools").cloned(),
                response_format,
            )
        }
        Value::Array(a) => (a.clone(), None, None, None),
        _ => (Vec::new(), None, None, None),
    }
}

fn anthropic_message(msg: &Value) -> Value {
    let obj = match msg {
        Value::Object(m) => m,
        other => {
            return json!({
                "role": "user",
                "content": value_to_plain_string(other).trim().to_string(),
            });
        }
    };
    json!({
        "role": normalize_role(obj.get("role")),
        "content": normalize_content(obj.get("content")),
    })
}

fn cohere_envelope(raw: &Value) -> Value {
    let (preamble, history, message, tools, response_format) = cohere_extract(raw);
    let mut norm: Vec<Value> = Vec::new();
    if let Some(p) = preamble {
        norm.push(json!({"role": "system", "content": normalize_content(Some(&p))}));
    }
    for turn in history {
        norm.push(cohere_turn(&turn));
    }
    if let Some(msg) = message {
        let is_empty_string = matches!(&msg, Value::String(s) if s.is_empty());
        if !is_empty_string {
            norm.push(json!({"role": "user", "content": normalize_content(Some(&msg))}));
        }
    }
    build_envelope("cohere", norm, tools.as_ref(), response_format.as_ref())
}

fn cohere_extract(
    raw: &Value,
) -> (
    Option<Value>,
    Vec<Value>,
    Option<Value>,
    Option<Value>,
    Option<Value>,
) {
    match raw {
        Value::Object(m) => {
            let history = m
                .get("chat_history")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            (
                m.get("preamble").cloned(),
                history,
                m.get("message").cloned(),
                m.get("tools").cloned(),
                m.get("response_format").cloned(),
            )
        }
        Value::Array(a) => (None, a.clone(), None, None, None),
        Value::String(_) => (None, Vec::new(), Some(raw.clone()), None, None),
        _ => (None, Vec::new(), None, None, None),
    }
}

fn cohere_turn(turn: &Value) -> Value {
    let obj = match turn {
        Value::Object(m) => m,
        other => {
            return json!({
                "role": "user",
                "content": value_to_plain_string(other).trim().to_string(),
            });
        }
    };
    let role = match obj.get("role").and_then(Value::as_str) {
        Some(s) => {
            let lowered = s.trim().to_ascii_lowercase();
            match lowered.as_str() {
                "user" => "user".to_string(),
                "chatbot" => "assistant".to_string(),
                "system" => "system".to_string(),
                "tool" => "tool".to_string(),
                _ => normalize_role(obj.get("role")),
            }
        }
        None => "user".to_string(),
    };
    let content = if obj.contains_key("message") {
        normalize_content(obj.get("message"))
    } else {
        normalize_content(obj.get("content"))
    };
    json!({"role": role, "content": content})
}

fn raw_envelope(raw: &Value) -> Value {
    let mut messages: Vec<Value> = Vec::new();
    match raw {
        Value::String(s) => messages.push(json!({
            "role": "user",
            "content": s.trim().to_string(),
        })),
        Value::Array(arr) => {
            for item in arr {
                match item {
                    Value::Object(m) => {
                        let role = normalize_role(m.get("role"));
                        messages.push(json!({
                            "role": role,
                            "content": normalize_content(m.get("content")),
                        }));
                    }
                    other => messages.push(json!({
                        "role": "user",
                        "content": value_to_plain_string(other).trim().to_string(),
                    })),
                }
            }
        }
        Value::Object(m) => {
            let role = normalize_role(m.get("role"));
            let content = if m.contains_key("content") {
                normalize_content(m.get("content"))
            } else {
                normalize_content(Some(raw))
            };
            messages.push(json!({"role": role, "content": content}));
        }
        other => messages.push(json!({
            "role": "user",
            "content": value_to_plain_string(other).trim().to_string(),
        })),
    }
    build_envelope("raw", messages, None, None)
}

fn normalize_param_value(key: &str, value: &Value) -> Value {
    match value {
        Value::Null => Value::Null,
        _ => match key {
            "temperature" | "top_p" | "frequency_penalty" | "presence_penalty" => {
                if let Some(f) = value.as_f64() {
                    json_number(round_float(f))
                } else {
                    value.clone()
                }
            }
            "stop" => match value {
                Value::String(s) => Value::Array(vec![Value::String(s.clone())]),
                Value::Array(arr) => {
                    let mut strs: Vec<String> =
                        arr.iter().map(value_to_plain_string).collect();
                    strs.sort();
                    Value::Array(strs.into_iter().map(Value::String).collect())
                }
                _ => value.clone(),
            },
            "response_format" => {
                let hash = response_schema_hash(Some(value));
                json!({"schema_hash": hash})
            }
            "tool_choice" => normalize_tool_choice(value),
            "logit_bias" => match value {
                Value::Object(m) => {
                    let mut keys: Vec<&String> = m.keys().collect();
                    keys.sort();
                    let mut out = Map::new();
                    for k in keys {
                        let v = &m[k];
                        out.insert(k.clone(), normalize_numeric(v));
                    }
                    Value::Object(out)
                }
                _ => value.clone(),
            },
            _ => value.clone(),
        },
    }
}

fn normalize_tool_choice(value: &Value) -> Value {
    match value {
        Value::String(s) => Value::String(s.clone()),
        Value::Object(m) => {
            if let Some(Value::Object(fn_obj)) = m.get("function") {
                let name = fn_obj
                    .get("name")
                    .map(value_to_plain_string)
                    .unwrap_or_default();
                return json!({"type": "function", "name": name});
            }
            if let Some(name) = m.get("name") {
                return json!({"type": "tool", "name": value_to_plain_string(name)});
            }
            value.clone()
        }
        _ => value.clone(),
    }
}

fn normalize_numeric(value: &Value) -> Value {
    if let Some(f) = value.as_f64() {
        if value.is_f64() {
            return json_number(round_float(f));
        }
    }
    value.clone()
}

fn json_number(f: f64) -> Value {
    Value::Number(
        serde_json::Number::from_f64(f)
            .unwrap_or_else(|| serde_json::Number::from_f64(0.0).expect("0.0 is finite")),
    )
}

/// Returns SHA-256 digest of the canonical prompt bytes.
pub fn prompt_hash(raw: &Value, provider: &str) -> [u8; 32] {
    let bytes = canonicalize_prompt(raw, provider);
    let digest = Sha256::digest(&bytes);
    let mut out = [0u8; 32];
    out.copy_from_slice(&digest);
    out
}

/// Returns SHA-256 digest of the canonical parameter bytes.
pub fn parameters_hash(raw: &Value) -> [u8; 32] {
    let bytes = canonicalize_parameters(raw);
    let digest = Sha256::digest(&bytes);
    let mut out = [0u8; 32];
    out.copy_from_slice(&digest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_dumps_sorts_object_keys() {
        let v = json!({"b": 1, "a": 2});
        assert_eq!(deterministic_dumps(&v), br#"{"a":2,"b":1}"#);
    }

    #[test]
    fn raw_wraps_string_as_user_message() {
        let v = Value::String("  hello  ".to_string());
        let bytes = canonicalize_prompt(&v, "raw");
        let expected = br#"{"messages":[{"content":"hello","role":"user"}],"provider":"raw","response_schema_hash":null,"tools":[]}"#;
        assert_eq!(bytes, expected);
    }

    #[test]
    fn openai_role_and_whitespace_normalization() {
        let v = json!({
            "messages": [
                {"role": "System", "content": "  hi "},
                {"role": "USER", "content": "  yo "},
            ]
        });
        let bytes = canonicalize_prompt(&v, "openai");
        let expected = br#"{"messages":[{"content":"hi","role":"system"},{"content":"yo","role":"user"}],"provider":"openai","response_schema_hash":null,"tools":[]}"#;
        assert_eq!(bytes, expected);
    }

    #[test]
    fn parameters_drop_unsupported_and_sort_keys() {
        let v = json!({
            "temperature": 0.1234567,
            "stream": true,
            "user": "u1",
            "agentc_tag": "x",
            "max_tokens": 100,
        });
        let bytes = canonicalize_parameters(&v);
        let expected = br#"{"max_tokens":100,"temperature":0.123457}"#;
        assert_eq!(bytes, expected);
    }

    #[test]
    fn parameters_stop_list_sorts() {
        let v = json!({"stop": ["b", "a"]});
        let bytes = canonicalize_parameters(&v);
        assert_eq!(bytes, br#"{"stop":["a","b"]}"#);
    }

    #[test]
    fn anthropic_lifts_system_message() {
        let v = json!({
            "system": "be helpful",
            "messages": [{"role": "user", "content": "hi"}]
        });
        let bytes = canonicalize_prompt(&v, "anthropic");
        let expected = br#"{"messages":[{"content":"be helpful","role":"system"},{"content":"hi","role":"user"}],"provider":"anthropic","response_schema_hash":null,"tools":[]}"#;
        assert_eq!(bytes, expected);
    }

    #[test]
    fn cohere_maps_chatbot_to_assistant() {
        let v = json!({
            "chat_history": [{"role": "CHATBOT", "message": "prev reply"}],
            "message": "new turn"
        });
        let bytes = canonicalize_prompt(&v, "cohere");
        let expected = br#"{"messages":[{"content":"prev reply","role":"assistant"},{"content":"new turn","role":"user"}],"provider":"cohere","response_schema_hash":null,"tools":[]}"#;
        assert_eq!(bytes, expected);
    }

    #[test]
    fn tools_sorted_and_schema_hashed() {
        let v = json!({
            "messages": [{"role": "user", "content": "x"}],
            "tools": [
                {"type": "function", "function": {"name": "beta", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "alpha", "parameters": {"type": "object"}}},
            ]
        });
        let bytes = canonicalize_prompt(&v, "openai");
        // alpha sorts first; both schemas hash to the same value since they are identical.
        let schema_hash = sha256_hex(br#"{"type":"object"}"#);
        let expected = format!(
            r#"{{"messages":[{{"content":"x","role":"user"}}],"provider":"openai","response_schema_hash":null,"tools":[{{"name":"alpha","schema_hash":"{hash}"}},{{"name":"beta","schema_hash":"{hash}"}}]}}"#,
            hash = schema_hash
        );
        assert_eq!(bytes, expected.as_bytes());
    }
}
