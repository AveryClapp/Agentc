//! DDL for `cost_model.db` and `optimizer_audit.db`.
//!
//! Both databases live alongside `traces.db` in the user's storage directory.
//! Schemas are applied idempotently (`CREATE … IF NOT EXISTS`) so opening the
//! same DB twice never fails.

use anyhow::{Context, Result};
use rusqlite::Connection;

/// Schema for `cost_model.db`:
///
/// - `call_site_profile` — one row per `call_site_id`, rolling Welford stats.
/// - `rule_divergence` — one row per `(call_site, rule)` divergence estimate.
/// - `optimizer_disabled` — per-`(call_site, rule)` disable entries with a
///   TTL (`reenable_at`).
///
/// STRICT typing is used per the spec; the tables are plain rowid tables so
/// the Welford stats can be updated in place.
pub const COST_MODEL_SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS call_site_profile (
    call_site_id          TEXT PRIMARY KEY NOT NULL,
    n_observations        INTEGER NOT NULL,
    input_tokens_mean     REAL NOT NULL,
    input_tokens_var      REAL NOT NULL,
    output_tokens_mean    REAL NOT NULL,
    output_tokens_var     REAL NOT NULL,
    latency_ms_mean       REAL NOT NULL,
    latency_ms_var        REAL NOT NULL,
    cost_usd_mean         REAL NOT NULL,
    cost_usd_var          REAL NOT NULL,
    output_token_p95      REAL NOT NULL,
    output_token_p99      REAL NOT NULL,
    output_is_structured  REAL NOT NULL,
    output_is_short       REAL NOT NULL,
    updated_at            INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS rule_divergence (
    call_site_id          TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    n_samples             INTEGER NOT NULL,
    divergence_mean       REAL NOT NULL,
    divergence_var        REAL NOT NULL,
    PRIMARY KEY (call_site_id, rule)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS optimizer_disabled (
    call_site_id          TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    reason                TEXT NOT NULL,
    disabled_at           INTEGER NOT NULL,
    reenable_at           INTEGER NOT NULL,
    PRIMARY KEY (call_site_id, rule)
) STRICT, WITHOUT ROWID;
"#;

/// Schema for `optimizer_audit.db`:
///
/// - `plan_audit` — one row per optimize_plan dispatch. Ring-buffered by
///   audit_id (prune oldest when count > `RING_BUFFER_CAP`).
///
/// Uses `INTEGER PRIMARY KEY AUTOINCREMENT` so pruned rowids are not reused
/// — prevents a confusing "audit_id 42 refers to three different plans
/// over the lifetime of the DB" scenario in `agentc optimize inspect`.
pub const AUDIT_SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS plan_audit (
    audit_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_us                 INTEGER NOT NULL,
    call_site_id          TEXT NOT NULL,
    span_id               BLOB NOT NULL,
    plan_kind             TEXT NOT NULL,
    rule                  TEXT,
    projected_savings_usd REAL,
    measured_savings_usd  REAL,
    overhead_us           INTEGER NOT NULL,
    shadow_sampled        INTEGER NOT NULL DEFAULT 0,
    shadow_divergence     REAL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_audit_call_site ON plan_audit(call_site_id, ts_us DESC);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON plan_audit(ts_us);
"#;

/// Apply `cost_model.db` DDL to a connection. Idempotent.
///
/// Also runs column-addition migrations for databases created before
/// `output_token_p99` was added. SQLite silently errors on duplicate column
/// names; we treat that as "already migrated".
pub fn ensure_cost_model_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(COST_MODEL_SCHEMA)
        .context("applying cost_model schema")?;
    // Migration: add output_token_p99 if absent (old DB). The error
    // "duplicate column name" means the column already exists — safe to
    // ignore.
    let _ = conn.execute_batch(
        "ALTER TABLE call_site_profile \
         ADD COLUMN output_token_p99 REAL NOT NULL DEFAULT 0.0",
    );
    Ok(())
}

/// Apply `optimizer_audit.db` DDL to a connection. Idempotent.
pub fn ensure_audit_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(AUDIT_SCHEMA)
        .context("applying optimizer_audit schema")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ensure_cost_model_schema_is_idempotent() {
        let conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        ensure_cost_model_schema(&conn).unwrap();

        let tables: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' \
                 AND name IN ('call_site_profile','rule_divergence','optimizer_disabled')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(tables, 3);
    }

    #[test]
    fn ensure_audit_schema_is_idempotent() {
        let conn = Connection::open_in_memory().unwrap();
        ensure_audit_schema(&conn).unwrap();
        ensure_audit_schema(&conn).unwrap();

        let tables: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='plan_audit'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(tables, 1);
    }

    #[test]
    fn plan_audit_autoincrements_across_deletes() {
        // AUTOINCREMENT guarantees that pruned audit_ids are not reused. We
        // exercise that here because the ring-buffer prune relies on it.
        let conn = Connection::open_in_memory().unwrap();
        ensure_audit_schema(&conn).unwrap();
        for i in 0..3 {
            conn.execute(
                "INSERT INTO plan_audit (ts_us, call_site_id, span_id, plan_kind, overhead_us) \
                 VALUES (?1, ?2, ?3, 'pass_through', 0)",
                rusqlite::params![i as i64, format!("site-{i}"), vec![0u8; 8]],
            )
            .unwrap();
        }
        conn.execute("DELETE FROM plan_audit", []).unwrap();
        conn.execute(
            "INSERT INTO plan_audit (ts_us, call_site_id, span_id, plan_kind, overhead_us) \
             VALUES (1, 'next', ?1, 'pass_through', 0)",
            rusqlite::params![vec![0u8; 8]],
        )
        .unwrap();
        let next_id: i64 = conn
            .query_row("SELECT audit_id FROM plan_audit", [], |r| r.get(0))
            .unwrap();
        assert_eq!(next_id, 4, "autoincrement must not reuse pruned ids");
    }
}
