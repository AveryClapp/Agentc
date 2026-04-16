//! Re-exports embedding primitives from `agentc-embed` and owns the trace-DB
//! backfill routine. All the PRNG/LSH/model-loading logic has moved to
//! `agentc-embed`; keep this module thin.

pub use agentc_embed::{
    cosine_similarity, embed_text, embed_text_f32, extract_text_for_embedding, f16_bytes_to_f32,
    f32_to_f16_bytes, is_zero_embedding, EMBEDDING_BYTES, EMBEDDING_DIM,
};

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
