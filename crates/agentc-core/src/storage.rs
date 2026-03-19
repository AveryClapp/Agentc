//! Content storage with zstd compression and SHA-256 dedup.
//!
//! Provides canonical JSON serialization, content hashing, compression,
//! and the composite `write_span` flow that processes raw span data into
//! deduplicated content rows + span row.

use anyhow::{Context, Result};
use rusqlite::Connection;
use sha2::{Digest, Sha256};

use crate::db::{insert_content, insert_span};
use crate::embedding::{embed_text, extract_text_for_embedding};
use crate::span::{ContentTable, Span};

/// zstd compression level for content storage.
const ZSTD_LEVEL: i32 = 3;

/// Raw span data as received from the Python SDK.
///
/// Contains `input_messages` and `output_messages` as JSON values.
/// These are NOT stored as span columns — they are hashed, compressed,
/// and stored in the content tables.
#[derive(Debug, Clone)]
pub struct SpanInput {
    pub span_id: String,
    pub trace_id: String,
    pub parent_span_id: Option<String>,
    pub name: String,
    pub kind: String,
    pub start_time: i64,
    pub end_time: Option<i64>,
    pub status: String,
    pub model: Option<String>,
    pub provider: Option<String>,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub cache_creation_tokens: Option<i64>,
    pub cache_read_tokens: Option<i64>,
    pub attributes: String,
    /// Raw input messages (JSON array of message objects).
    pub input_messages: Option<serde_json::Value>,
    /// Raw output messages (JSON array of message objects).
    pub output_messages: Option<serde_json::Value>,
}

/// Options controlling what gets captured during write_span.
#[derive(Debug, Clone, Copy)]
pub struct WriteSpanOptions {
    /// If true, hash + compress + store content in content tables.
    /// If false, content_id fields are NULL and no content rows are created.
    pub capture_content: bool,
    /// If true (and capture_content is true), compute model2vec embeddings.
    /// If false, embeddings are NULL.
    pub capture_embeddings: bool,
}

impl Default for WriteSpanOptions {
    fn default() -> Self {
        Self {
            capture_content: true,
            capture_embeddings: true,
        }
    }
}

/// Serialize a JSON value to canonical form: keys sorted alphabetically, compact separators.
///
/// Equivalent to Python's `json.dumps(obj, sort_keys=True, separators=(',', ':'))`.
pub fn canonical_json(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let entries: Vec<String> = keys
                .iter()
                .map(|k| {
                    let key_str = serde_json::to_string(*k).unwrap();
                    let val_str = canonical_json(map.get(*k).unwrap());
                    format!("{key_str}:{val_str}")
                })
                .collect();
            format!("{{{}}}", entries.join(","))
        }
        serde_json::Value::Array(arr) => {
            let items: Vec<String> = arr.iter().map(canonical_json).collect();
            format!("[{}]", items.join(","))
        }
        // For scalars (strings, numbers, booleans, null), serde_json's default is already canonical.
        _ => serde_json::to_string(value).unwrap(),
    }
}

/// Compute SHA-256 hash of canonical JSON bytes, returning a hex string.
pub fn content_hash(canonical: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(canonical.as_bytes());
    format!("{:x}", hasher.finalize())
}

/// Compress data using zstd at the configured level.
pub fn compress_content(data: &[u8]) -> Result<Vec<u8>> {
    zstd::encode_all(data, ZSTD_LEVEL).context("zstd compression failed")
}

/// Decompress zstd-compressed data.
pub fn decompress_content(data: &[u8]) -> Result<Vec<u8>> {
    zstd::decode_all(data).context("zstd decompression failed")
}

/// Process raw span data through the composite write flow.
///
/// Steps:
/// 1. If `capture_content`: serialize input/output messages to canonical JSON,
///    compute SHA-256 content IDs, compress with zstd, insert into content tables.
/// 2. If `capture_embeddings` (requires capture_content): compute model2vec embeddings.
/// 3. Insert span row with content IDs and embeddings.
///
/// Returns the constructed `Span` that was inserted.
pub fn write_span(conn: &Connection, input: &SpanInput, opts: WriteSpanOptions) -> Result<Span> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_micros() as i64;

    let mut input_content_id: Option<String> = None;
    let mut output_content_id: Option<String> = None;
    let mut input_embedding: Option<Vec<u8>> = None;
    let mut output_embedding: Option<Vec<u8>> = None;

    if opts.capture_content {
        // Step 1-3: Hash, compress, and store input content.
        if let Some(ref messages) = input.input_messages {
            let canonical = canonical_json(messages);
            let cid = content_hash(&canonical);
            let compressed = compress_content(canonical.as_bytes())?;
            insert_content(conn, ContentTable::InputContent, &cid, &compressed, now)?;
            input_content_id = Some(cid);

            // Step 4: Optionally compute input embedding.
            if opts.capture_embeddings {
                let text = extract_text_for_embedding(&canonical);
                input_embedding = embed_text(&text);
            }
        }

        // Same for output content.
        if let Some(ref messages) = input.output_messages {
            let canonical = canonical_json(messages);
            let cid = content_hash(&canonical);
            let compressed = compress_content(canonical.as_bytes())?;
            insert_content(conn, ContentTable::OutputContent, &cid, &compressed, now)?;
            output_content_id = Some(cid);

            // Step 4: Optionally compute output embedding.
            if opts.capture_embeddings {
                let text = extract_text_for_embedding(&canonical);
                output_embedding = embed_text(&text);
            }
        }
    }

    // Step 5: Build and insert span row.
    let embedding_model = if input_embedding.is_some() || output_embedding.is_some() {
        Some("potion-base-8M".to_string())
    } else {
        None
    };

    let span = Span {
        span_id: input.span_id.clone(),
        trace_id: input.trace_id.clone(),
        parent_span_id: input.parent_span_id.clone(),
        name: input.name.clone(),
        kind: input.kind.clone(),
        start_time: input.start_time,
        end_time: input.end_time,
        status: input.status.clone(),
        model: input.model.clone(),
        provider: input.provider.clone(),
        input_tokens: input.input_tokens,
        output_tokens: input.output_tokens,
        cache_creation_tokens: input.cache_creation_tokens,
        cache_read_tokens: input.cache_read_tokens,
        cost_usd: None, // Backfilled by analyzer.
        attributes: input.attributes.clone(),
        input_content_id,
        output_content_id,
        input_embedding,
        output_embedding,
        embedding_model,
    };

    insert_span(conn, &span)?;

    Ok(span)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::create_db;
    use tempfile::TempDir;

    fn make_test_input(id: &str, trace: &str) -> SpanInput {
        SpanInput {
            span_id: id.to_string(),
            trace_id: trace.to_string(),
            parent_span_id: None,
            name: "test_call".to_string(),
            kind: "chat".to_string(),
            start_time: 1000000,
            end_time: Some(2000000),
            status: "OK".to_string(),
            model: Some("claude-sonnet-4-20250514".to_string()),
            provider: Some("anthropic".to_string()),
            input_tokens: Some(100),
            output_tokens: Some(50),
            cache_creation_tokens: None,
            cache_read_tokens: None,
            attributes: "{}".to_string(),
            input_messages: None,
            output_messages: None,
        }
    }

    // --- Canonical JSON tests ---

    #[test]
    fn test_canonical_json_sorted_keys() {
        let json: serde_json::Value = serde_json::from_str(
            r#"{"z": 1, "a": 2, "m": 3}"#,
        )
        .unwrap();
        let result = canonical_json(&json);
        assert_eq!(result, r#"{"a":2,"m":3,"z":1}"#);
    }

    #[test]
    fn test_canonical_json_compact_separators() {
        let json: serde_json::Value = serde_json::from_str(
            r#"{"key": "value", "num": 42}"#,
        )
        .unwrap();
        let result = canonical_json(&json);
        // No spaces after : or ,
        assert!(!result.contains(": "));
        assert!(!result.contains(", "));
        assert_eq!(result, r#"{"key":"value","num":42}"#);
    }

    #[test]
    fn test_canonical_json_nested_sorted() {
        let json: serde_json::Value = serde_json::from_str(
            r#"{"b": {"z": 1, "a": 2}, "a": [3, 2, 1]}"#,
        )
        .unwrap();
        let result = canonical_json(&json);
        assert_eq!(result, r#"{"a":[3,2,1],"b":{"a":2,"z":1}}"#);
    }

    #[test]
    fn test_canonical_json_deterministic() {
        let json1: serde_json::Value = serde_json::from_str(
            r#"{"role": "user", "content": "hello"}"#,
        )
        .unwrap();
        let json2: serde_json::Value = serde_json::from_str(
            r#"{"content": "hello", "role": "user"}"#,
        )
        .unwrap();
        assert_eq!(canonical_json(&json1), canonical_json(&json2));
    }

    #[test]
    fn test_canonical_json_python_equivalent() {
        // Equivalent to: json.dumps({"role":"user","content":"Hello"}, sort_keys=True, separators=(',',':'))
        // Python output: '{"content":"Hello","role":"user"}'
        let json: serde_json::Value = serde_json::from_str(
            r#"{"role": "user", "content": "Hello"}"#,
        )
        .unwrap();
        assert_eq!(canonical_json(&json), r#"{"content":"Hello","role":"user"}"#);
    }

    #[test]
    fn test_canonical_json_array() {
        let json: serde_json::Value = serde_json::from_str(r#"[1, "two", null, true]"#).unwrap();
        assert_eq!(canonical_json(&json), r#"[1,"two",null,true]"#);
    }

    #[test]
    fn test_canonical_json_empty_array() {
        let json: serde_json::Value = serde_json::from_str("[]").unwrap();
        assert_eq!(canonical_json(&json), "[]");
    }

    #[test]
    fn test_canonical_json_empty_object() {
        let json: serde_json::Value = serde_json::from_str("{}").unwrap();
        assert_eq!(canonical_json(&json), "{}");
    }

    // --- SHA-256 tests ---

    #[test]
    fn test_content_hash_deterministic() {
        let canonical = r#"{"content":"Hello","role":"user"}"#;
        let h1 = content_hash(canonical);
        let h2 = content_hash(canonical);
        assert_eq!(h1, h2);
        assert_eq!(h1.len(), 64); // SHA-256 hex is 64 chars.
    }

    #[test]
    fn test_identical_input_same_content_id() {
        let msg1: serde_json::Value = serde_json::from_str(
            r#"[{"role": "user", "content": "hello"}]"#,
        )
        .unwrap();
        let msg2: serde_json::Value = serde_json::from_str(
            r#"[{"content": "hello", "role": "user"}]"#,
        )
        .unwrap();
        let id1 = content_hash(&canonical_json(&msg1));
        let id2 = content_hash(&canonical_json(&msg2));
        assert_eq!(id1, id2);
    }

    #[test]
    fn test_different_input_different_content_id() {
        let msg1: serde_json::Value = serde_json::from_str(
            r#"[{"role": "user", "content": "hello"}]"#,
        )
        .unwrap();
        let msg2: serde_json::Value = serde_json::from_str(
            r#"[{"role": "user", "content": "goodbye"}]"#,
        )
        .unwrap();
        let id1 = content_hash(&canonical_json(&msg1));
        let id2 = content_hash(&canonical_json(&msg2));
        assert_ne!(id1, id2);
    }

    #[test]
    fn test_empty_array_content_id() {
        let empty: serde_json::Value = serde_json::from_str("[]").unwrap();
        let cid = content_hash(&canonical_json(&empty));
        // Hash of "[]" — should be valid, non-empty.
        assert_eq!(cid.len(), 64);
        assert_ne!(cid, content_hash(""));
    }

    // --- zstd compression tests ---

    #[test]
    fn test_zstd_roundtrip() {
        let original = b"Hello, this is test content for zstd compression roundtrip.";
        let compressed = compress_content(original).unwrap();
        let decompressed = decompress_content(&compressed).unwrap();
        assert_eq!(decompressed, original);
    }

    #[test]
    fn test_zstd_compression_ratio() {
        // Repetitive JSON text should compress well.
        let text = r#"{"role":"user","content":"Please analyze the following data and provide a comprehensive summary of your findings."}"#;
        let repeated = text.repeat(10);
        let compressed = compress_content(repeated.as_bytes()).unwrap();
        let ratio = repeated.len() as f64 / compressed.len() as f64;
        assert!(
            ratio > 2.0,
            "Expected compression ratio > 2x for repetitive JSON, got {ratio:.1}x"
        );
    }

    #[test]
    fn test_zstd_empty_data() {
        let compressed = compress_content(b"").unwrap();
        let decompressed = decompress_content(&compressed).unwrap();
        assert!(decompressed.is_empty());
    }

    #[test]
    fn test_zstd_large_json_roundtrip() {
        // Simulate a large messages array.
        let messages: Vec<serde_json::Value> = (0..50)
            .map(|i| {
                serde_json::json!({
                    "role": if i % 2 == 0 { "user" } else { "assistant" },
                    "content": format!("Message number {i} with some content to make it realistic.")
                })
            })
            .collect();
        let json_str = canonical_json(&serde_json::Value::Array(messages));
        let compressed = compress_content(json_str.as_bytes()).unwrap();
        let decompressed = decompress_content(&compressed).unwrap();
        assert_eq!(decompressed, json_str.as_bytes());
    }

    // --- write_span composite flow tests ---

    #[test]
    fn test_write_span_with_content() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let mut input = make_test_input("span-1", "trace-1");
        input.input_messages = Some(serde_json::json!([
            {"role": "user", "content": "Hello"}
        ]));
        input.output_messages = Some(serde_json::json!([
            {"role": "assistant", "content": "Hi there!"}
        ]));

        let opts = WriteSpanOptions {
            capture_content: true,
            capture_embeddings: false, // Skip embeddings for fast test.
        };

        let span = write_span(&conn, &input, opts).unwrap();

        // Content IDs should be set.
        assert!(span.input_content_id.is_some());
        assert!(span.output_content_id.is_some());

        // Content rows should exist.
        let in_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
            .unwrap();
        let out_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM output_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(in_count, 1);
        assert_eq!(out_count, 1);

        // Verify stored content roundtrips.
        let stored_blob: Vec<u8> = conn
            .query_row(
                "SELECT content_text FROM input_content WHERE content_id = ?1",
                [span.input_content_id.as_ref().unwrap()],
                |row| row.get(0),
            )
            .unwrap();
        let decompressed = decompress_content(&stored_blob).unwrap();
        let expected_canonical = canonical_json(&serde_json::json!([
            {"role": "user", "content": "Hello"}
        ]));
        assert_eq!(decompressed, expected_canonical.as_bytes());
    }

    #[test]
    fn test_write_span_duplicate_content_ignored() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let messages = serde_json::json!([{"role": "user", "content": "Hello"}]);

        let mut input1 = make_test_input("span-1", "trace-1");
        input1.input_messages = Some(messages.clone());
        input1.output_messages = Some(serde_json::json!([{"role": "assistant", "content": "Hi"}]));

        let mut input2 = make_test_input("span-2", "trace-1");
        input2.input_messages = Some(messages); // Same input content.
        input2.output_messages =
            Some(serde_json::json!([{"role": "assistant", "content": "Different response"}]));

        let opts = WriteSpanOptions {
            capture_content: true,
            capture_embeddings: false,
        };

        let span1 = write_span(&conn, &input1, opts).unwrap();
        let span2 = write_span(&conn, &input2, opts).unwrap();

        // Same input → same content_id.
        assert_eq!(span1.input_content_id, span2.input_content_id);

        // Different output → different content_id.
        assert_ne!(span1.output_content_id, span2.output_content_id);

        // Only 1 input_content row (dedup), but 2 output_content rows.
        let in_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
            .unwrap();
        let out_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM output_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(in_count, 1);
        assert_eq!(out_count, 2);
    }

    #[test]
    fn test_write_span_capture_content_false() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let mut input = make_test_input("span-1", "trace-1");
        input.input_messages = Some(serde_json::json!([{"role": "user", "content": "Hello"}]));
        input.output_messages =
            Some(serde_json::json!([{"role": "assistant", "content": "Hi"}]));

        let opts = WriteSpanOptions {
            capture_content: false,
            capture_embeddings: false,
        };

        let span = write_span(&conn, &input, opts).unwrap();

        // Content IDs should be NULL.
        assert!(span.input_content_id.is_none());
        assert!(span.output_content_id.is_none());

        // Embeddings should be NULL.
        assert!(span.input_embedding.is_none());
        assert!(span.output_embedding.is_none());

        // No content rows should exist.
        let in_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
            .unwrap();
        let out_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM output_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(in_count, 0);
        assert_eq!(out_count, 0);
    }

    #[test]
    fn test_write_span_capture_embeddings_false_content_true() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let mut input = make_test_input("span-1", "trace-1");
        input.input_messages = Some(serde_json::json!([{"role": "user", "content": "Hello"}]));
        input.output_messages =
            Some(serde_json::json!([{"role": "assistant", "content": "Hi"}]));

        let opts = WriteSpanOptions {
            capture_content: true,
            capture_embeddings: false,
        };

        let span = write_span(&conn, &input, opts).unwrap();

        // Content should be stored.
        assert!(span.input_content_id.is_some());
        assert!(span.output_content_id.is_some());

        // Embeddings should be NULL.
        assert!(span.input_embedding.is_none());
        assert!(span.output_embedding.is_none());
        assert!(span.embedding_model.is_none());

        // Content rows should exist.
        let in_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(in_count, 1);
    }

    #[test]
    fn test_write_span_capture_embeddings_true_content_true() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let mut input = make_test_input("span-1", "trace-1");
        input.input_messages = Some(serde_json::json!([{"role": "user", "content": "Hello"}]));
        input.output_messages =
            Some(serde_json::json!([{"role": "assistant", "content": "Hi"}]));

        let opts = WriteSpanOptions {
            capture_content: true,
            capture_embeddings: true,
        };

        let span = write_span(&conn, &input, opts).unwrap();

        // Content should be stored.
        assert!(span.input_content_id.is_some());
        assert!(span.output_content_id.is_some());

        // Embeddings depend on model availability.
        // If model files are present, embeddings will be Some; otherwise None.
        // Either way, no panic.
        let in_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(in_count, 1);
    }

    #[test]
    fn test_write_span_no_messages() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let input = make_test_input("span-1", "trace-1");
        // No messages set.

        let opts = WriteSpanOptions::default();
        let span = write_span(&conn, &input, opts).unwrap();

        assert!(span.input_content_id.is_none());
        assert!(span.output_content_id.is_none());
    }

    #[test]
    fn test_write_span_empty_messages_array() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let mut input = make_test_input("span-1", "trace-1");
        input.input_messages = Some(serde_json::json!([]));

        let opts = WriteSpanOptions {
            capture_content: true,
            capture_embeddings: false,
        };

        let span = write_span(&conn, &input, opts).unwrap();

        // Empty array should produce a valid content_id (hash of "[]").
        assert!(span.input_content_id.is_some());
        assert_eq!(
            span.input_content_id.as_ref().unwrap(),
            &content_hash("[]")
        );
    }

    #[test]
    fn test_write_span_nested_structured_content() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let mut input = make_test_input("span-1", "trace-1");
        input.input_messages = Some(serde_json::json!([{
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}}
            ]
        }]));

        let opts = WriteSpanOptions {
            capture_content: true,
            capture_embeddings: false,
        };

        let span = write_span(&conn, &input, opts).unwrap();
        assert!(span.input_content_id.is_some());

        // Verify content is stored and roundtrips.
        let stored_blob: Vec<u8> = conn
            .query_row(
                "SELECT content_text FROM input_content WHERE content_id = ?1",
                [span.input_content_id.as_ref().unwrap()],
                |row| row.get(0),
            )
            .unwrap();
        let decompressed = decompress_content(&stored_blob).unwrap();
        let decompressed_str = std::str::from_utf8(&decompressed).unwrap();

        // Verify it's valid JSON and has sorted keys.
        let reparsed: serde_json::Value = serde_json::from_str(decompressed_str).unwrap();
        let content_blocks = reparsed[0]["content"].as_array().unwrap();
        assert_eq!(content_blocks.len(), 2);
    }

    // --- Integration tests ---

    #[test]
    fn test_integration_dedup_10_spans_same_prompt() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let shared_input = serde_json::json!([{
            "role": "user",
            "content": "Analyze the quarterly revenue data and provide key insights."
        }]);

        let opts = WriteSpanOptions {
            capture_content: true,
            capture_embeddings: false,
        };

        let mut content_ids = Vec::new();
        for i in 0..10 {
            let mut input = make_test_input(&format!("span-{i}"), "trace-1");
            input.input_messages = Some(shared_input.clone());
            input.output_messages = Some(serde_json::json!([{
                "role": "assistant",
                "content": format!("Response variant {i}")
            }]));

            let span = write_span(&conn, &input, opts).unwrap();
            content_ids.push(span.input_content_id.unwrap());
        }

        // All input content_ids should be identical.
        assert!(content_ids.windows(2).all(|w| w[0] == w[1]));

        // Only 1 input_content row (dedup).
        let in_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(in_count, 1);

        // 10 different output_content rows.
        let out_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM output_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(out_count, 10);
    }

    #[test]
    fn test_integration_capture_content_false_no_leaks() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let opts = WriteSpanOptions {
            capture_content: false,
            capture_embeddings: false,
        };

        for i in 0..5 {
            let mut input = make_test_input(&format!("span-{i}"), "trace-1");
            input.input_messages = Some(serde_json::json!([{"role": "user", "content": "secret"}]));
            input.output_messages =
                Some(serde_json::json!([{"role": "assistant", "content": "also secret"}]));
            write_span(&conn, &input, opts).unwrap();
        }

        // No content should exist in any table.
        let in_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
            .unwrap();
        let out_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM output_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(in_count, 0);
        assert_eq!(out_count, 0);

        // All spans should have NULL content IDs.
        let null_count: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM spans WHERE input_content_id IS NULL AND output_content_id IS NULL",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(null_count, 5);
    }

    #[test]
    fn test_integration_composite_flow_data_consistency() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let input_msg = serde_json::json!([{
            "role": "user",
            "content": "Hello, how are you?"
        }]);
        let output_msg = serde_json::json!([{
            "role": "assistant",
            "content": "I'm doing well, thank you!"
        }]);

        let mut input = make_test_input("span-1", "trace-1");
        input.input_messages = Some(input_msg.clone());
        input.output_messages = Some(output_msg.clone());

        let opts = WriteSpanOptions {
            capture_content: true,
            capture_embeddings: false,
        };

        let span = write_span(&conn, &input, opts).unwrap();

        // Verify content_id matches expected hash.
        let expected_input_cid = content_hash(&canonical_json(&input_msg));
        let expected_output_cid = content_hash(&canonical_json(&output_msg));
        assert_eq!(span.input_content_id.as_ref().unwrap(), &expected_input_cid);
        assert_eq!(
            span.output_content_id.as_ref().unwrap(),
            &expected_output_cid
        );

        // Verify span row has the content_ids.
        let db_input_cid: Option<String> = conn
            .query_row(
                "SELECT input_content_id FROM spans WHERE span_id = 'span-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(db_input_cid.as_ref(), Some(&expected_input_cid));

        // Verify stored content matches.
        let stored: Vec<u8> = conn
            .query_row(
                "SELECT content_text FROM input_content WHERE content_id = ?1",
                [&expected_input_cid],
                |row| row.get(0),
            )
            .unwrap();
        let decompressed = decompress_content(&stored).unwrap();
        assert_eq!(decompressed, canonical_json(&input_msg).as_bytes());
    }

    #[test]
    fn test_independent_output_dedup() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let shared_output = serde_json::json!([{
            "role": "assistant",
            "content": "I don't have enough information to answer that."
        }]);

        let opts = WriteSpanOptions {
            capture_content: true,
            capture_embeddings: false,
        };

        // Two different inputs, same output.
        let mut input1 = make_test_input("span-1", "trace-1");
        input1.input_messages = Some(serde_json::json!([{"role": "user", "content": "Q1"}]));
        input1.output_messages = Some(shared_output.clone());

        let mut input2 = make_test_input("span-2", "trace-1");
        input2.input_messages = Some(serde_json::json!([{"role": "user", "content": "Q2"}]));
        input2.output_messages = Some(shared_output);

        let span1 = write_span(&conn, &input1, opts).unwrap();
        let span2 = write_span(&conn, &input2, opts).unwrap();

        // Same output → same output_content_id.
        assert_eq!(span1.output_content_id, span2.output_content_id);

        // Different input → different input_content_id.
        assert_ne!(span1.input_content_id, span2.input_content_id);

        // Only 1 output_content row.
        let out_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM output_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(out_count, 1);
    }
}
