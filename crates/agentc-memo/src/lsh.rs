//! LSH candidate retrieval on top of `memoization_lsh_bucket` and
//! `memoization_embedding`.
//!
//! The hyperplane signature and band split live in `agentc_embed::lsh`. This
//! module is the SQL-facing glue: it writes the eight band rows and the raw
//! embedding on insert, and on lookup it queries the buckets, reranks by
//! cosine similarity, and returns the best above-threshold match.

use anyhow::{Context, Result};
use rusqlite::{params, Transaction};

use agentc_embed::lsh::{lsh_bands, lsh_signature, NUM_BANDS};
use agentc_embed::EMBEDDING_DIM;

/// f32 cosine similarity. Mirrors the semantics of
/// `agentc_embed::cosine_similarity`, but operates on the raw f32 vectors
/// stored in `memoization_embedding`.
fn cosine_f32(a: &[f32], b: &[f32]) -> f32 {
    debug_assert_eq!(a.len(), b.len());
    let mut dot = 0f32;
    let mut na = 0f32;
    let mut nb = 0f32;
    for i in 0..a.len() {
        dot += a[i] * b[i];
        na += a[i] * a[i];
        nb += b[i] * b[i];
    }
    let denom = (na.sqrt()) * (nb.sqrt());
    if denom == 0.0 {
        0.0
    } else {
        (dot / denom).clamp(-1.0, 1.0)
    }
}

/// Default cosine threshold. Matches the spec-calibrated value — tight enough
/// to exclude false paraphrases, loose enough to catch the ones we care about.
pub const DEFAULT_SIMILARITY_THRESHOLD: f32 = 0.92;

/// Encode an embedding as 256 × little-endian f32 bytes for storage.
pub fn encode_embedding(embedding: &[f32]) -> Vec<u8> {
    assert_eq!(
        embedding.len(),
        EMBEDDING_DIM,
        "encode_embedding expects {EMBEDDING_DIM}-dim input",
    );
    let mut out = Vec::with_capacity(EMBEDDING_DIM * 4);
    for v in embedding {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

/// Reverse of [`encode_embedding`]. Returns `None` when the BLOB has the wrong
/// length so one corrupt row does not poison every lookup.
pub fn decode_embedding(bytes: &[u8]) -> Option<[f32; EMBEDDING_DIM]> {
    if bytes.len() != EMBEDDING_DIM * 4 {
        return None;
    }
    let mut out = [0f32; EMBEDDING_DIM];
    for (i, chunk) in bytes.chunks_exact(4).enumerate() {
        out[i] = f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
    }
    Some(out)
}

/// Insert the band rows and embedding row for `cache_key_hex` inside an open
/// transaction. Called from `SqliteCache::insert` so the cache row and its LSH
/// index land atomically.
pub fn write_lsh_rows(
    tx: &Transaction,
    cache_key_hex: &str,
    embedding: &[f32],
) -> Result<()> {
    let signature = lsh_signature(embedding);
    let bands = lsh_bands(signature);

    // Refresh the band rows — if this cache_key was inserted previously the
    // old buckets may differ. Delete first, then insert the eight new rows.
    tx.execute(
        "DELETE FROM memoization_lsh_bucket WHERE cache_key_hash = ?1",
        params![cache_key_hex],
    )
    .context("clearing stale lsh buckets")?;

    let mut stmt = tx
        .prepare_cached(
            "INSERT INTO memoization_lsh_bucket (band_ix, bucket_id, cache_key_hash) \
             VALUES (?1, ?2, ?3)",
        )
        .context("preparing lsh bucket insert")?;
    for (ix, bucket) in bands.iter().enumerate() {
        stmt.execute(params![ix as i64, *bucket as i64, cache_key_hex])
            .context("inserting lsh bucket row")?;
    }
    drop(stmt);

    let encoded = encode_embedding(embedding);
    tx.execute(
        "INSERT INTO memoization_embedding (cache_key_hash, embedding) VALUES (?1, ?2) \
         ON CONFLICT(cache_key_hash) DO UPDATE SET embedding = excluded.embedding",
        params![cache_key_hex, encoded],
    )
    .context("upserting memoization_embedding")?;
    Ok(())
}

/// Delete the LSH index rows for `cache_key_hex`. Called from eviction paths;
/// `SqliteCache::invalidate` does not yet call this because M6 owns the full
/// eviction sweep — the buckets are harmless until then since lookups cosine
/// rerank against `memoization_embedding`, which is also cleared here.
pub fn delete_lsh_rows(tx: &Transaction, cache_key_hex: &str) -> Result<()> {
    tx.execute(
        "DELETE FROM memoization_lsh_bucket WHERE cache_key_hash = ?1",
        params![cache_key_hex],
    )?;
    tx.execute(
        "DELETE FROM memoization_embedding WHERE cache_key_hash = ?1",
        params![cache_key_hex],
    )?;
    Ok(())
}

/// Candidate produced by LSH rerank. `similarity` is cosine; higher is better.
#[derive(Debug, Clone, PartialEq)]
pub struct LshCandidate {
    pub cache_key_hex: String,
    pub similarity: f32,
}

/// Return the best LSH-driven candidate whose cosine similarity against
/// `query_embedding` is `>= threshold`, or `None` if no candidate qualifies.
///
/// The function opens a read-only connection cursor inside the caller's
/// mutex-guarded `Connection`; it does not start its own transaction.
pub fn best_candidate(
    conn: &rusqlite::Connection,
    query_embedding: &[f32],
    threshold: f32,
    now_micros: i64,
) -> Result<Option<LshCandidate>> {
    debug_assert_eq!(query_embedding.len(), EMBEDDING_DIM);
    if threshold >= 1.0 {
        return Ok(None);
    }

    let signature = lsh_signature(query_embedding);
    let bands = lsh_bands(signature);

    // Collect unique candidate keys across the 8 bands.
    let mut candidates: std::collections::HashSet<String> = std::collections::HashSet::new();
    {
        let mut stmt = conn.prepare_cached(
            "SELECT cache_key_hash FROM memoization_lsh_bucket \
             WHERE band_ix = ?1 AND bucket_id = ?2",
        )?;
        for (ix, bucket) in bands.iter().enumerate().take(NUM_BANDS) {
            let rows = stmt.query_map(params![ix as i64, *bucket as i64], |row| {
                row.get::<_, String>(0)
            })?;
            for r in rows {
                candidates.insert(r?);
            }
        }
    }
    if candidates.is_empty() {
        return Ok(None);
    }

    // Rerank by cosine similarity, skipping expired rows.
    let mut best: Option<LshCandidate> = None;
    let mut embed_stmt = conn.prepare_cached(
        "SELECT e.embedding FROM memoization_embedding e \
         INNER JOIN memoization_cache c ON c.cache_key_hash = e.cache_key_hash \
         WHERE e.cache_key_hash = ?1 AND c.expires_at > ?2",
    )?;
    for key in candidates {
        let blob: Option<Vec<u8>> = embed_stmt
            .query_row(params![key, now_micros], |row| row.get(0))
            .ok();
        let Some(bytes) = blob else { continue };
        let Some(candidate_embedding) = decode_embedding(&bytes) else {
            continue;
        };
        let sim = cosine_f32(query_embedding, &candidate_embedding);
        if sim < threshold {
            continue;
        }
        if best
            .as_ref()
            .map(|b| sim > b.similarity)
            .unwrap_or(true)
        {
            best = Some(LshCandidate {
                cache_key_hex: key,
                similarity: sim,
            });
        }
    }
    Ok(best)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::{CacheValue, SqliteCache};
    use crate::key::CacheKey;
    use rusqlite::Connection;

    fn bootstrap_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE output_content (
                content_id   TEXT PRIMARY KEY,
                content_text BLOB NOT NULL,
                created_at   INTEGER NOT NULL
            );
            INSERT INTO output_content (content_id, content_text, created_at)
            VALUES ('abc123', X'00', 1);",
        )
        .unwrap();
        conn
    }

    fn unit_embedding(seed: u32) -> [f32; EMBEDDING_DIM] {
        let mut e = [0f32; EMBEDDING_DIM];
        for i in 0..EMBEDDING_DIM {
            let v = ((i as u32).wrapping_mul(2654435761) ^ seed) as i32 as f32;
            e[i] = v / 1_000_000.0;
        }
        let norm: f32 = e.iter().map(|x| x * x).sum::<f32>().sqrt();
        if norm > 0.0 {
            for v in e.iter_mut() {
                *v /= norm;
            }
        }
        e
    }

    fn sample_key(tag: u8) -> CacheKey {
        CacheKey {
            prompt_hash: [tag; 32],
            model: "gpt-4o".to_string(),
            parameters_hash: [tag ^ 0xFF; 32],
            call_site_id: format!("tests:fn_{tag}"),
        }
    }

    fn sample_value() -> CacheValue {
        CacheValue {
            output_content_id: "abc123".to_string(),
            input_tokens: 100,
            output_tokens: 50,
            recorded_cost_usd: 0.0042,
        }
    }

    #[test]
    fn encode_decode_embedding_roundtrip() {
        let e = unit_embedding(42);
        let bytes = encode_embedding(&e);
        assert_eq!(bytes.len(), EMBEDDING_DIM * 4);
        let back = decode_embedding(&bytes).unwrap();
        for i in 0..EMBEDDING_DIM {
            assert!((back[i] - e[i]).abs() < 1e-6);
        }
    }

    #[test]
    fn decode_rejects_wrong_length() {
        assert!(decode_embedding(&[0u8; 4]).is_none());
    }

    #[test]
    fn best_candidate_finds_self_at_perfect_similarity() {
        let conn = bootstrap_conn();
        let cache = SqliteCache::with_threshold(conn, 0.5).unwrap();
        let key = sample_key(1);
        let embedding = unit_embedding(7);
        cache
            .insert_with_embedding(&key, &sample_value(), Some(&embedding), 10_000, 100)
            .unwrap();

        let best = cache.with_conn_for_test(|c| {
            best_candidate(c, &embedding, 0.5, 200).unwrap()
        });
        let best = best.expect("self should be a candidate");
        assert!(
            (best.similarity - 1.0).abs() < 1e-4,
            "cosine self-similarity = {}",
            best.similarity
        );
    }

    #[test]
    fn best_candidate_respects_threshold() {
        let conn = bootstrap_conn();
        let cache = SqliteCache::with_threshold(conn, 0.99).unwrap();
        let key = sample_key(1);
        // Use an unambiguous orthogonal pair: e_0 stored, e_1 queried.
        let mut stored = [0f32; EMBEDDING_DIM];
        stored[0] = 1.0;
        let mut query = [0f32; EMBEDDING_DIM];
        query[1] = 1.0;
        cache
            .insert_with_embedding(&key, &sample_value(), Some(&stored), 10_000, 100)
            .unwrap();

        let best = cache.with_conn_for_test(|c| {
            best_candidate(c, &query, 0.99, 200).unwrap()
        });
        assert!(best.is_none(), "orthogonal vectors should not pass threshold 0.99");
    }

    #[test]
    fn best_candidate_skips_expired_entries() {
        let conn = bootstrap_conn();
        let cache = SqliteCache::with_threshold(conn, 0.5).unwrap();
        let key = sample_key(1);
        let stored = unit_embedding(7);
        cache
            .insert_with_embedding(&key, &sample_value(), Some(&stored), 10, 100)
            .unwrap();

        // Expired at t=200 (created 100, ttl 10).
        let best = cache.with_conn_for_test(|c| {
            best_candidate(c, &stored, 0.5, 200).unwrap()
        });
        assert!(best.is_none());
    }
}
