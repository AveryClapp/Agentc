//! model2vec potion-base-8M inference.
//!
//! Model weights and tokenizer are loaded from `~/.agentc/models/potion-base-8M/`.
//! If files are absent, inference gracefully returns `None`. The global instance
//! is lazy-initialized via `OnceCell`; on failure, a flag short-circuits further
//! attempts so a single missing asset doesn't become a per-call retry storm.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

use model2vec_rs::model::StaticModel;
use once_cell::sync::OnceCell;

static MODEL: OnceCell<StaticModel> = OnceCell::new();
static LOAD_FAILED: AtomicBool = AtomicBool::new(false);

/// Dimension of the potion-base-8M embedding space.
pub const EMBEDDING_DIM: usize = 256;

/// Size of a float16 embedding in bytes (256 dims × 2 bytes).
pub const EMBEDDING_BYTES: usize = EMBEDDING_DIM * 2;

const ZERO_NORM_THRESHOLD: f32 = 1e-7;

fn default_model_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".agentc")
        .join("models")
        .join("potion-base-8M")
}

fn get_or_init_model() -> Option<&'static StaticModel> {
    if let Some(model) = MODEL.get() {
        return Some(model);
    }
    if LOAD_FAILED.load(Ordering::Acquire) {
        return None;
    }

    let result = MODEL.get_or_try_init(|| -> anyhow::Result<StaticModel> {
        let model_dir = default_model_dir();
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
            eprintln!("ERROR: Failed to load model2vec weights: {e}. Embeddings will be NULL.");
            None
        }
    }
}

/// Convert a float32 slice to float16 little-endian bytes.
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

fn l2_norm(v: &[f32]) -> f32 {
    v.iter().map(|x| x * x).sum::<f32>().sqrt()
}

/// Compute embedding and return raw f32 values. The preferred form for LSH and
/// memoization, where extra f16 round-trip precision is not wanted.
pub fn embed_text_f32(text: &str) -> Option<Vec<f32>> {
    let model = get_or_init_model()?;
    let embedding = model.encode_single(text);
    if embedding.len() != EMBEDDING_DIM {
        eprintln!(
            "WARNING: model2vec returned {}-dim embedding, expected {EMBEDDING_DIM}",
            embedding.len()
        );
        return None;
    }
    Some(embedding)
}

/// Compute embedding and return as f16 bytes (the storage form used by the profiler).
pub fn embed_text(text: &str) -> Option<Vec<u8>> {
    embed_text_f32(text).map(|v| f32_to_f16_bytes(&v))
}

/// Extract text content from structured message JSON for embedding. Concatenates
/// `text`-typed content blocks and joins messages with newlines. Images, tool_use,
/// and unknown block types are skipped.
pub fn extract_text_for_embedding(messages_json: &str) -> String {
    let Ok(messages) = serde_json::from_str::<serde_json::Value>(messages_json) else {
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

fn extract_text_from_message(msg: &serde_json::Value, parts: &mut Vec<String>) {
    if let Some(content) = msg.get("content") {
        match content {
            serde_json::Value::String(s) => parts.push(s.clone()),
            serde_json::Value::Array(blocks) => {
                for block in blocks {
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

/// Return true if a f16-packed embedding is effectively a zero vector.
pub fn is_zero_embedding(bytes: &[u8]) -> bool {
    let values = f16_bytes_to_f32(bytes);
    l2_norm(&values) < ZERO_NORM_THRESHOLD
}

/// Cosine similarity on two f16-packed embedding byte arrays.
pub fn cosine_similarity(a: &[u8], b: &[u8]) -> f32 {
    let a_f32 = f16_bytes_to_f32(a);
    let b_f32 = f16_bytes_to_f32(b);
    cosine_similarity_f32(&a_f32, &b_f32)
}

/// Cosine similarity on raw f32 vectors.
pub fn cosine_similarity_f32(a: &[f32], b: &[f32]) -> f32 {
    let norm_a = l2_norm(a);
    let norm_b = l2_norm(b);
    if norm_a < ZERO_NORM_THRESHOLD || norm_b < ZERO_NORM_THRESHOLD {
        return 0.0;
    }
    let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
    dot / (norm_a * norm_b)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn f32_to_f16_roundtrip() {
        let values = vec![1.0f32, -0.5, 0.0, 3.25, -2.75];
        let bytes = f32_to_f16_bytes(&values);
        assert_eq!(bytes.len(), values.len() * 2);
        let recovered = f16_bytes_to_f32(&bytes);
        for (orig, rec) in values.iter().zip(recovered.iter()) {
            assert!((orig - rec).abs() < 0.01, "f16 roundtrip failed: {orig} -> {rec}");
        }
    }

    #[test]
    fn f16_embedding_size() {
        let values = vec![0.0f32; EMBEDDING_DIM];
        let bytes = f32_to_f16_bytes(&values);
        assert_eq!(bytes.len(), EMBEDDING_BYTES);
    }

    #[test]
    fn zero_embedding_detected() {
        let zero = f32_to_f16_bytes(&vec![0.0f32; EMBEDDING_DIM]);
        assert!(is_zero_embedding(&zero));
        let mut v = vec![0.0f32; EMBEDDING_DIM];
        v[0] = 1.0;
        assert!(!is_zero_embedding(&f32_to_f16_bytes(&v)));
    }

    #[test]
    fn cosine_similarity_identical() {
        let v: Vec<f32> = (0..EMBEDDING_DIM).map(|i| (i as f32) * 0.01).collect();
        let bytes = f32_to_f16_bytes(&v);
        let sim = cosine_similarity(&bytes, &bytes);
        assert!((sim - 1.0).abs() < 0.01);
    }

    #[test]
    fn cosine_similarity_orthogonal() {
        let mut a = vec![0.0f32; EMBEDDING_DIM];
        let mut b = vec![0.0f32; EMBEDDING_DIM];
        a[0] = 1.0;
        b[1] = 1.0;
        let sim = cosine_similarity(&f32_to_f16_bytes(&a), &f32_to_f16_bytes(&b));
        assert!(sim.abs() < 0.01);
    }

    #[test]
    fn cosine_similarity_zero_short_circuit() {
        let zero = f32_to_f16_bytes(&vec![0.0f32; EMBEDDING_DIM]);
        let mut v = vec![0.0f32; EMBEDDING_DIM];
        v[0] = 1.0;
        assert_eq!(cosine_similarity(&zero, &f32_to_f16_bytes(&v)), 0.0);
    }

    #[test]
    fn extract_text_simple_string() {
        let json = r#"[{"content": "Hello world"}]"#;
        assert_eq!(extract_text_for_embedding(json), "Hello world");
    }

    #[test]
    fn extract_text_structured_blocks() {
        let json = r#"[{"content": [
            {"type": "text", "text": "Hello"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "World"}
        ]}]"#;
        assert_eq!(extract_text_for_embedding(json), "Hello\nWorld");
    }

    #[test]
    fn extract_text_multiple_messages() {
        let json = r#"[
            {"content": "First"},
            {"content": "Second"}
        ]"#;
        assert_eq!(extract_text_for_embedding(json), "First\nSecond");
    }

    #[test]
    fn extract_text_plain_string() {
        assert_eq!(extract_text_for_embedding("plain"), "plain");
    }

    #[test]
    fn embed_text_no_model_is_safe() {
        // Without model files the function must not panic.
        let _ = embed_text("test");
    }
}
