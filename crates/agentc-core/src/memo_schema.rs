//! Memoization tables schema — part of `traces.db`, which this crate owns.
//!
//! The memoization cache, LSH bucket, embedding, and stats objects live inside
//! `traces.db` alongside spans and `output_content`. `output_content` is owned
//! by [`crate::db`], and the cache's `output_content_id` column references it,
//! so the DDL that creates these tables belongs to `agentc-core` — the crate
//! that owns the file — not to the higher-level `agentc-memo` feature crate.
//! `agentc-memo` re-exports [`ensure_memoization_schema`] as its `ensure_schema`
//! (bd-smg: this removes the previous `agentc-core -> agentc-memo` dependency,
//! which pointed the wrong way and left `traces.db` without a clear owner).

use anyhow::{Context, Result};
use rusqlite::Connection;

/// Memoization DDL. Creates:
///
/// - `memoization_cache` — primary key = `cache_key_hash` (hex text).
/// - `memoization_lsh_bucket` — LSH band rows.
/// - `memoization_embedding` — 256 × f32 bytes.
/// - `memoization_stats` — aggregate view for `agentc cache stats`.
///
/// `output_content_id` is TEXT (hex SHA-256) to match the existing
/// `output_content.content_id` column that `agentc-core` owns.
pub const MEMOIZATION_SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS memoization_cache (
    cache_key_hash          TEXT    PRIMARY KEY NOT NULL,
    prompt_hash             TEXT    NOT NULL,
    model                   TEXT    NOT NULL,
    parameters_hash         TEXT    NOT NULL,
    output_content_id       TEXT    NOT NULL REFERENCES output_content(content_id),
    input_tokens            INTEGER NOT NULL,
    output_tokens           INTEGER NOT NULL,
    recorded_cost_usd       REAL    NOT NULL,
    created_at              INTEGER NOT NULL,
    expires_at              INTEGER NOT NULL,
    last_hit_at             INTEGER NOT NULL,
    hit_count               INTEGER NOT NULL DEFAULT 0,
    call_site_id            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memo_prompt_hash ON memoization_cache(prompt_hash);
CREATE INDEX IF NOT EXISTS idx_memo_expires_at  ON memoization_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_memo_call_site   ON memoization_cache(call_site_id);
CREATE INDEX IF NOT EXISTS idx_memo_last_hit    ON memoization_cache(last_hit_at);

CREATE TABLE IF NOT EXISTS memoization_lsh_bucket (
    band_ix         INTEGER NOT NULL,
    bucket_id       INTEGER NOT NULL,
    cache_key_hash  TEXT    NOT NULL,
    PRIMARY KEY (band_ix, bucket_id, cache_key_hash)
);
CREATE INDEX IF NOT EXISTS idx_lsh_lookup ON memoization_lsh_bucket(band_ix, bucket_id);

CREATE TABLE IF NOT EXISTS memoization_embedding (
    cache_key_hash  TEXT PRIMARY KEY NOT NULL,
    embedding       BLOB NOT NULL
);

CREATE VIEW IF NOT EXISTS memoization_stats AS
    SELECT
        call_site_id,
        COUNT(*)                              AS entries,
        SUM(hit_count)                        AS total_hits,
        SUM(recorded_cost_usd * hit_count)    AS estimated_savings_usd,
        MAX(last_hit_at)                      AS last_hit_at
    FROM memoization_cache
    GROUP BY call_site_id;
"#;

/// Apply the memoization DDL to a connection. Idempotent (every statement is
/// `CREATE ... IF NOT EXISTS`), so it is safe to call on cache construction,
/// during the cross-process merge, and on any `traces.db` handle.
pub fn ensure_memoization_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(MEMOIZATION_SCHEMA)
        .context("applying memoization schema")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fresh_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        // The cache references output_content, so it must exist first.
        conn.execute_batch(
            "CREATE TABLE output_content (
                content_id   TEXT PRIMARY KEY,
                content_text BLOB NOT NULL,
                created_at   INTEGER NOT NULL
            );",
        )
        .unwrap();
        conn
    }

    #[test]
    fn ensure_memoization_schema_is_idempotent() {
        let conn = fresh_conn();
        ensure_memoization_schema(&conn).unwrap();
        ensure_memoization_schema(&conn).unwrap();
        ensure_memoization_schema(&conn).unwrap();

        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'memoization_%'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 3);
    }

    #[test]
    fn ensure_memoization_schema_creates_stats_view() {
        let conn = fresh_conn();
        ensure_memoization_schema(&conn).unwrap();
        let view_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='view' AND name='memoization_stats'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(view_count, 1);
    }

    #[test]
    fn ensure_memoization_schema_creates_expected_indexes() {
        let conn = fresh_conn();
        ensure_memoization_schema(&conn).unwrap();
        let mut stmt = conn
            .prepare("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")
            .unwrap();
        let names: Vec<String> = stmt
            .query_map([], |row| row.get(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        for expected in &[
            "idx_memo_prompt_hash",
            "idx_memo_expires_at",
            "idx_memo_call_site",
            "idx_memo_last_hit",
            "idx_lsh_lookup",
        ] {
            assert!(names.contains(&expected.to_string()), "missing index {expected}");
        }
    }
}
