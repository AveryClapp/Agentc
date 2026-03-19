//! Embedding computation via model2vec-rs (potion-base-8M).
//!
//! Model weights and tokenizer are loaded from `~/.agentc/models/potion-base-8M/`.
//! If files are absent, embedding computation gracefully returns `None`.
//!
//! The model is lazily initialized on first use via `OnceCell`. If initialization fails,
//! a global flag prevents repeated attempts and all subsequent calls return `None`.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

use model2vec_rs::model::StaticModel;
use once_cell::sync::OnceCell;

/// Global model instance, loaded lazily on first embedding request.
static MODEL: OnceCell<StaticModel> = OnceCell::new();

/// Flag: if true, model loading already failed — don't retry.
static LOAD_FAILED: AtomicBool = AtomicBool::new(false);

/// Dimension of the potion-base-8M embedding space.
pub const EMBEDDING_DIM: usize = 256;

/// Size of a float16 embedding in bytes (256 dims * 2 bytes).
pub const EMBEDDING_BYTES: usize = EMBEDDING_DIM * 2;

/// L2 norm threshold below which a vector is considered "zero".
const ZERO_NORM_THRESHOLD: f32 = 1e-7;

/// Default model directory under the user's home.
fn default_model_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".agentc")
        .join("models")
        .join("potion-base-8M")
}

/// Initialize the global model. Returns a reference if successful.
fn get_or_init_model() -> Option<&'static StaticModel> {
    // Fast path: already loaded.
    if let Some(model) = MODEL.get() {
        return Some(model);
    }

    // Already failed — don't retry.
    if LOAD_FAILED.load(Ordering::Acquire) {
        return None;
    }

    // Try to load.
    let result = MODEL.get_or_try_init(|| -> anyhow::Result<StaticModel> {
        let model_dir = default_model_dir();

        // Verify required files exist.
        let required = ["tokenizer.json", "model.safetensors", "config.json"];
        for name in &required {
            let path = model_dir.join(name);
            if !path.exists() {
                anyhow::bail!(
                    "Model file not found: {}. Run `scripts/download_model.sh` to fetch the model.",
                    path.display()
                );
            }
        }

        let model = StaticModel::from_pretrained(&model_dir, None, Some(true), None)?;

        Ok(model)
    });

    match result {
        Ok(model) => Some(model),
        Err(e) => {
            LOAD_FAILED.store(true, Ordering::Release);
            eprintln!("ERROR: Failed to load model2vec weights: {e}. Embeddings will be NULL for all spans.");
            None
        }
    }
}

/// Convert a float32 array to float16 bytes (little-endian).
pub fn f32_to_f16_bytes(values: &[f32]) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(values.len() * 2);
    for &v in values {
        let h = half::f16::from_f32(v);
        bytes.extend_from_slice(&h.to_le_bytes());
    }
    bytes
}

/// Convert float16 bytes back to float32 values.
pub fn f16_bytes_to_f32(bytes: &[u8]) -> Vec<f32> {
    bytes
        .chunks_exact(2)
        .map(|chunk| half::f16::from_le_bytes([chunk[0], chunk[1]]).to_f32())
        .collect()
}

/// Compute the L2 norm of a vector.
fn l2_norm(v: &[f32]) -> f32 {
    v.iter().map(|x| x * x).sum::<f32>().sqrt()
}

/// Compute embedding for a text string.
///
/// Returns `Some(Vec<u8>)` containing 512 bytes (256 float16 values) on success,
/// or `None` if the model is unavailable.
pub fn embed_text(text: &str) -> Option<Vec<u8>> {
    let model = get_or_init_model()?;

    let embedding = model.encode_single(text);

    if embedding.len() != EMBEDDING_DIM {
        eprintln!(
            "WARNING: model2vec returned {}-dim embedding, expected {EMBEDDING_DIM}",
            embedding.len()
        );
        return None;
    }

    Some(f32_to_f16_bytes(&embedding))
}

/// Extract text content from structured message content for embedding.
///
/// Concatenates text-type content blocks, excluding image and tool_use blocks.
/// Multiple messages are joined by newlines.
pub fn extract_text_for_embedding(messages_json: &str) -> String {
    // Parse as JSON array of messages.
    let Ok(messages) = serde_json::from_str::<serde_json::Value>(messages_json) else {
        // If it's not valid JSON, treat the whole string as text.
        return messages_json.to_string();
    };

    let mut parts = Vec::new();

    match &messages {
        serde_json::Value::Array(arr) => {
            for msg in arr {
                extract_text_from_message(msg, &mut parts);
            }
        }
        serde_json::Value::Object(_) => {
            extract_text_from_message(&messages, &mut parts);
        }
        serde_json::Value::String(s) => {
            parts.push(s.clone());
        }
        _ => {}
    }

    parts.join("\n")
}

/// Extract text from a single message object.
fn extract_text_from_message(msg: &serde_json::Value, parts: &mut Vec<String>) {
    if let Some(content) = msg.get("content") {
        match content {
            serde_json::Value::String(s) => {
                parts.push(s.clone());
            }
            serde_json::Value::Array(blocks) => {
                for block in blocks {
                    // Only include "text" type blocks.
                    let block_type = block.get("type").and_then(|t| t.as_str());
                    if block_type == Some("text") {
                        if let Some(text) = block.get("text").and_then(|t| t.as_str()) {
                            parts.push(text.to_string());
                        }
                    }
                }
            }
            _ => {}
        }
    }
}

/// Stats from an embedding backfill operation.
#[derive(Debug, Clone, Default)]
pub struct BackfillEmbeddingStats {
    /// Number of spans that received new embeddings.
    pub computed: usize,
    /// Number of spans skipped (NULL content_id).
    pub skipped_null_content: usize,
    /// Number of spans where embedding computation failed.
    pub failed: usize,
    /// Total spans examined.
    pub total: usize,
}

/// Backfill embeddings for spans with NULL input_embedding or output_embedding.
///
/// For each qualifying span:
/// 1. Look up content from content table by content_id
/// 2. Decompress zstd content
/// 3. Extract text (text blocks only)
/// 4. Compute model2vec embedding
/// 5. Update span row
///
/// Skips spans whose content_id is NULL (capture_content was False).
pub fn backfill_embeddings(conn: &rusqlite::Connection) -> anyhow::Result<BackfillEmbeddingStats> {
    use crate::storage::decompress_content;

    let mut stats = BackfillEmbeddingStats::default();

    // Find spans needing input embeddings.
    let mut input_stmt = conn.prepare(
        "SELECT s.span_id, s.input_content_id, ic.content_text \
         FROM spans s \
         LEFT JOIN input_content ic ON s.input_content_id = ic.content_id \
         WHERE s.input_embedding IS NULL AND s.input_content_id IS NOT NULL",
    )?;

    let input_rows: Vec<(String, String, Vec<u8>)> = input_stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Vec<u8>>(2)?,
            ))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    stats.total += input_rows.len();

    for (span_id, _content_id, compressed) in &input_rows {
        match decompress_content(compressed) {
            Ok(decompressed) => {
                let text_content = String::from_utf8_lossy(&decompressed);
                let text = extract_text_for_embedding(&text_content);
                if let Some(embedding) = embed_text(&text) {
                    conn.execute(
                        "UPDATE spans SET input_embedding = ?1, embedding_model = 'potion-base-8M' WHERE span_id = ?2",
                        rusqlite::params![embedding, span_id],
                    )?;
                    stats.computed += 1;
                } else {
                    stats.failed += 1;
                }
            }
            Err(_) => {
                stats.failed += 1;
            }
        }
    }

    // Find spans needing output embeddings.
    let mut output_stmt = conn.prepare(
        "SELECT s.span_id, s.output_content_id, oc.content_text \
         FROM spans s \
         LEFT JOIN output_content oc ON s.output_content_id = oc.content_id \
         WHERE s.output_embedding IS NULL AND s.output_content_id IS NOT NULL",
    )?;

    let output_rows: Vec<(String, String, Vec<u8>)> = output_stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Vec<u8>>(2)?,
            ))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    stats.total += output_rows.len();

    for (span_id, _content_id, compressed) in &output_rows {
        match decompress_content(compressed) {
            Ok(decompressed) => {
                let text_content = String::from_utf8_lossy(&decompressed);
                let text = extract_text_for_embedding(&text_content);
                if let Some(embedding) = embed_text(&text) {
                    conn.execute(
                        "UPDATE spans SET output_embedding = ?1, embedding_model = 'potion-base-8M' WHERE span_id = ?2",
                        rusqlite::params![embedding, span_id],
                    )?;
                    stats.computed += 1;
                } else {
                    stats.failed += 1;
                }
            }
            Err(_) => {
                stats.failed += 1;
            }
        }
    }

    // Count spans with NULL content_id (skipped entirely).
    let null_content_count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM spans \
         WHERE (input_embedding IS NULL AND input_content_id IS NULL) \
            OR (output_embedding IS NULL AND output_content_id IS NULL)",
        [],
        |row| row.get(0),
    )?;
    stats.skipped_null_content = null_content_count as usize;

    Ok(stats)
}

/// Check if an embedding vector (as float16 bytes) is effectively zero.
pub fn is_zero_embedding(bytes: &[u8]) -> bool {
    let values = f16_bytes_to_f32(bytes);
    l2_norm(&values) < ZERO_NORM_THRESHOLD
}

/// Compute cosine similarity between two float16 embedding byte arrays.
pub fn cosine_similarity(a: &[u8], b: &[u8]) -> f32 {
    let a_f32 = f16_bytes_to_f32(a);
    let b_f32 = f16_bytes_to_f32(b);

    let norm_a = l2_norm(&a_f32);
    let norm_b = l2_norm(&b_f32);

    if norm_a < ZERO_NORM_THRESHOLD || norm_b < ZERO_NORM_THRESHOLD {
        return 0.0;
    }

    let dot: f32 = a_f32.iter().zip(b_f32.iter()).map(|(x, y)| x * y).sum();
    dot / (norm_a * norm_b)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_f32_to_f16_roundtrip() {
        let values = vec![1.0f32, -0.5, 0.0, 3.25, -2.75];
        let bytes = f32_to_f16_bytes(&values);
        assert_eq!(bytes.len(), values.len() * 2);

        let recovered = f16_bytes_to_f32(&bytes);
        assert_eq!(recovered.len(), values.len());

        for (orig, rec) in values.iter().zip(recovered.iter()) {
            assert!(
                (orig - rec).abs() < 0.01,
                "f16 roundtrip failed: {orig} → {rec}"
            );
        }
    }

    #[test]
    fn test_f16_embedding_size() {
        let values = vec![0.0f32; EMBEDDING_DIM];
        let bytes = f32_to_f16_bytes(&values);
        assert_eq!(bytes.len(), EMBEDDING_BYTES);
    }

    #[test]
    fn test_is_zero_embedding() {
        let zero = f32_to_f16_bytes(&vec![0.0f32; EMBEDDING_DIM]);
        assert!(is_zero_embedding(&zero));

        let nonzero = f32_to_f16_bytes(&{
            let mut v = vec![0.0f32; EMBEDDING_DIM];
            v[0] = 1.0;
            v
        });
        assert!(!is_zero_embedding(&nonzero));
    }

    #[test]
    fn test_cosine_similarity_identical() {
        let v: Vec<f32> = (0..EMBEDDING_DIM).map(|i| (i as f32) * 0.01).collect();
        let bytes = f32_to_f16_bytes(&v);
        let sim = cosine_similarity(&bytes, &bytes);
        assert!((sim - 1.0).abs() < 0.01, "Self-similarity should be ~1.0, got {sim}");
    }

    #[test]
    fn test_cosine_similarity_orthogonal() {
        let mut a = vec![0.0f32; EMBEDDING_DIM];
        let mut b = vec![0.0f32; EMBEDDING_DIM];
        a[0] = 1.0;
        b[1] = 1.0;
        let sim = cosine_similarity(&f32_to_f16_bytes(&a), &f32_to_f16_bytes(&b));
        assert!(sim.abs() < 0.01, "Orthogonal vectors should have sim ~0, got {sim}");
    }

    #[test]
    fn test_cosine_similarity_zero_vector() {
        let zero = f32_to_f16_bytes(&vec![0.0f32; EMBEDDING_DIM]);
        let nonzero = f32_to_f16_bytes(&{
            let mut v = vec![0.0f32; EMBEDDING_DIM];
            v[0] = 1.0;
            v
        });
        assert_eq!(cosine_similarity(&zero, &nonzero), 0.0);
    }

    #[test]
    fn test_extract_text_simple_string() {
        let json = r#"[{"content": "Hello world"}]"#;
        assert_eq!(extract_text_for_embedding(json), "Hello world");
    }

    #[test]
    fn test_extract_text_structured_blocks() {
        let json = r#"[{"content": [
            {"type": "text", "text": "Hello"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "World"}
        ]}]"#;
        assert_eq!(extract_text_for_embedding(json), "Hello\nWorld");
    }

    #[test]
    fn test_extract_text_multiple_messages() {
        let json = r#"[
            {"content": "First message"},
            {"content": "Second message"}
        ]"#;
        assert_eq!(
            extract_text_for_embedding(json),
            "First message\nSecond message"
        );
    }

    #[test]
    fn test_extract_text_tool_use_excluded() {
        let json = r#"[{"content": [
            {"type": "text", "text": "Call result:"},
            {"type": "tool_use", "id": "abc", "name": "search", "input": {}},
            {"type": "text", "text": "Done"}
        ]}]"#;
        assert_eq!(extract_text_for_embedding(json), "Call result:\nDone");
    }

    #[test]
    fn test_extract_text_plain_string() {
        assert_eq!(
            extract_text_for_embedding("plain text content"),
            "plain text content"
        );
    }

    #[test]
    fn test_embed_text_no_model_returns_none() {
        // Without model files installed, should return None gracefully.
        // This test always passes — it validates the degradation path.
        let _result = embed_text("test");
        // No assertion on result because it depends on model file availability.
        // The important thing is it doesn't panic.
    }

    #[test]
    fn test_default_model_dir() {
        let dir = default_model_dir();
        assert!(dir.to_str().unwrap().contains(".agentc"));
        assert!(dir.to_str().unwrap().contains("potion-base-8M"));
    }
}
