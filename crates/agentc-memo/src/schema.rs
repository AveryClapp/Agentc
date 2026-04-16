//! SQLite schema for memoization tables.
//!
//! The DDL is idempotent (`CREATE TABLE IF NOT EXISTS` throughout) so it can be
//! invoked repeatedly — on cache construction, during cross-process merge, and
//! during profiler `create_db` without caring whether the tables already exist.
//!
//! M2 creates all four objects (cache, lsh_bucket, embedding, stats view) even
//! though the M2 insert path only populates `memoization_cache`. M3 fills the
//! LSH and embedding rows without touching the schema again.

use anyhow::{Context, Result};
use rusqlite::Connection;

/// Memoization DDL. Creates:
///
/// - `memoization_cache` — primary key = `cache_key_hash` (hex text).
/// - `memoization_lsh_bucket` — composite PK, cascades on cache delete.
/// - `memoization_embedding` — 256 × f32 bytes, cascades on cache delete.
/// - `memoization_stats` — aggregate view for `agentc cache stats`.
///
/// `output_content_id` is TEXT (hex SHA-256) to match the existing
/// `output_content.content_id` column. The spec draft uses `BLOB(32)`; we
/// ground out at TEXT to stay compatible with the profiler's dedup table.
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

/// Apply the memoization DDL to a connection. Idempotent.
///
/// The migration runs without referencing `PRAGMA user_version` — the profiler
/// bumps its own version gate, and memoization piggybacks on whatever DB the
/// caller hands us. Callers that want a hard version gate should combine this
/// with `db::migrate_db`.
pub fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(MEMOIZATION_SCHEMA)
        .context("applying memoization schema")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fresh_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        // Memoization FK points at output_content, so we need that table too.
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
    fn ensure_schema_is_idempotent() {
        let conn = fresh_conn();
        ensure_schema(&conn).unwrap();
        ensure_schema(&conn).unwrap();
        ensure_schema(&conn).unwrap();

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
    fn ensure_schema_creates_stats_view() {
        let conn = fresh_conn();
        ensure_schema(&conn).unwrap();
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
    fn ensure_schema_creates_expected_indexes() {
        let conn = fresh_conn();
        ensure_schema(&conn).unwrap();
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
