//! DDL for `cost_model.db` and `optimizer_audit.db`.
//!
//! Both databases live alongside `traces.db` in the user's storage directory.
//! Schemas are applied idempotently (`CREATE … IF NOT EXISTS`) so opening the
//! same DB twice never fails.

use anyhow::{Context, Result};
use rusqlite::Connection;

/// Schema for `cost_model.db`:
///
/// - `call_site_profile` — one summary row per `call_site_id`.
/// - `call_site_observation` — the exact retained samples for each profile.
/// - `execution_plan_profile` — one summary per exact call-site-version/plan.
/// - `execution_plan_observation` — exact retained complete-plan samples.
/// - `execution_plan_divergence_observation` — independent paired samples.
/// - `execution_plan_guard` — rolling complete-plan exposure summaries.
/// - `execution_plan_guard_observation` — exact positive-exposure events.
/// - `execution_plan_disabled` — durable plan-level guard state.
/// - `execution_plan_exploration_site` — monotonic per-site lease sequence.
/// - `execution_plan_exploration` — bounded counterfactual leases and feedback.
/// - `rule_divergence` — one summary row per `(call_site, rule)`.
/// - `rule_divergence_observation` — exact retained divergence samples.
/// - `optimizer_disabled` — per-`(call_site, rule)` disable entries with a
///   TTL (`reenable_at`).
///
/// STRICT typing is used per the spec. The single-row profile table keeps its
/// text primary key, while composite-key tables use `WITHOUT ROWID`.
pub const COST_MODEL_SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS call_site_profile (
    call_site_id          TEXT PRIMARY KEY NOT NULL,
    n_observations        INTEGER NOT NULL,
    window_observations   INTEGER NOT NULL,
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

CREATE TABLE IF NOT EXISTS call_site_observation (
    call_site_id          TEXT NOT NULL,
    sample_sequence       INTEGER NOT NULL,
    input_tokens          INTEGER NOT NULL,
    output_tokens         INTEGER NOT NULL,
    latency_ms            REAL NOT NULL,
    cost_usd              REAL NOT NULL,
    output_is_structured  INTEGER NOT NULL CHECK (output_is_structured IN (0, 1)),
    output_is_short       INTEGER NOT NULL CHECK (output_is_short IN (0, 1)),
    observed_at           INTEGER NOT NULL,
    PRIMARY KEY (call_site_id, sample_sequence)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_profile (
    call_site_version       TEXT NOT NULL,
    execution_plan_id       TEXT NOT NULL,
    n_observations          INTEGER NOT NULL CHECK (n_observations >= 0),
    n_paired_observations   INTEGER NOT NULL CHECK (n_paired_observations >= 0),
    window_observations     INTEGER NOT NULL CHECK (window_observations >= 0),
    paired_observations     INTEGER NOT NULL CHECK (paired_observations >= 0),
    input_tokens_mean       REAL NOT NULL CHECK (input_tokens_mean >= 0.0),
    input_tokens_var        REAL NOT NULL CHECK (input_tokens_var >= 0.0),
    output_tokens_mean      REAL NOT NULL CHECK (output_tokens_mean >= 0.0),
    output_tokens_var       REAL NOT NULL CHECK (output_tokens_var >= 0.0),
    latency_ms_mean         REAL NOT NULL CHECK (latency_ms_mean >= 0.0),
    latency_ms_var          REAL NOT NULL CHECK (latency_ms_var >= 0.0),
    cost_usd_mean           REAL NOT NULL CHECK (cost_usd_mean >= 0.0),
    cost_usd_var            REAL NOT NULL CHECK (cost_usd_var >= 0.0),
    output_token_p95        REAL NOT NULL CHECK (output_token_p95 >= 0.0),
    output_token_p99        REAL NOT NULL CHECK (output_token_p99 >= 0.0),
    output_is_structured    REAL NOT NULL CHECK (output_is_structured BETWEEN 0.0 AND 1.0),
    output_is_short         REAL NOT NULL CHECK (output_is_short BETWEEN 0.0 AND 1.0),
    divergence_upper_p95    REAL CHECK (
        divergence_upper_p95 IS NULL OR divergence_upper_p95 BETWEEN 0.0 AND 1.0
    ),
    dispatch_fallback_rate  REAL NOT NULL CHECK (dispatch_fallback_rate BETWEEN 0.0 AND 1.0),
    provider_protocol       TEXT NOT NULL,
    target_model_id         TEXT NOT NULL,
    target_model_version    TEXT NOT NULL,
    price_table_version     TEXT NOT NULL,
    updated_at              INTEGER NOT NULL CHECK (updated_at >= 0),
    last_paired_at          INTEGER CHECK (last_paired_at IS NULL OR last_paired_at >= 0),
    PRIMARY KEY (call_site_version, execution_plan_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_observation (
    call_site_version       TEXT NOT NULL,
    execution_plan_id       TEXT NOT NULL,
    sample_sequence         INTEGER NOT NULL CHECK (sample_sequence > 0),
    input_tokens            INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens           INTEGER NOT NULL CHECK (output_tokens >= 0),
    latency_ms              REAL NOT NULL CHECK (latency_ms >= 0.0),
    cost_usd                REAL NOT NULL CHECK (cost_usd >= 0.0),
    output_is_structured    INTEGER NOT NULL CHECK (output_is_structured IN (0, 1)),
    output_is_short         INTEGER NOT NULL CHECK (output_is_short IN (0, 1)),
    dispatch_fallback       INTEGER NOT NULL CHECK (dispatch_fallback IN (0, 1)),
    provider_protocol       TEXT NOT NULL,
    target_model_id         TEXT NOT NULL,
    target_model_version    TEXT NOT NULL,
    price_table_version     TEXT NOT NULL,
    observed_at             INTEGER NOT NULL CHECK (observed_at >= 0),
    PRIMARY KEY (call_site_version, execution_plan_id, sample_sequence)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_divergence_observation (
    call_site_version          TEXT NOT NULL,
    execution_plan_id          TEXT NOT NULL,
    sample_sequence            INTEGER NOT NULL CHECK (sample_sequence > 0),
    plan_observation_sequence  INTEGER NOT NULL CHECK (plan_observation_sequence > 0),
    divergence                 REAL NOT NULL CHECK (divergence BETWEEN 0.0 AND 1.0),
    provider_protocol          TEXT NOT NULL,
    target_model_id            TEXT NOT NULL,
    target_model_version       TEXT NOT NULL,
    price_table_version        TEXT NOT NULL,
    observed_at                INTEGER NOT NULL CHECK (observed_at >= 0),
    PRIMARY KEY (call_site_version, execution_plan_id, sample_sequence),
    UNIQUE (call_site_version, execution_plan_id, plan_observation_sequence)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_guard (
    call_site_version       TEXT NOT NULL,
    execution_plan_id       TEXT NOT NULL,
    divergence_threshold    REAL NOT NULL CHECK (divergence_threshold BETWEEN 0.0 AND 1.0),
    divergence_exposure     REAL NOT NULL CHECK (divergence_exposure >= 0.0),
    window_samples          INTEGER NOT NULL CHECK (window_samples >= 0),
    provider_protocol       TEXT NOT NULL,
    target_model_id         TEXT NOT NULL,
    target_model_version    TEXT NOT NULL,
    price_table_version     TEXT NOT NULL,
    updated_at              INTEGER NOT NULL CHECK (updated_at >= 0),
    PRIMARY KEY (call_site_version, execution_plan_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_guard_observation (
    call_site_version          TEXT NOT NULL,
    execution_plan_id          TEXT NOT NULL,
    plan_observation_sequence  INTEGER NOT NULL CHECK (plan_observation_sequence > 0),
    divergence                 REAL NOT NULL CHECK (divergence BETWEEN 0.0 AND 1.0),
    excess                     REAL NOT NULL CHECK (excess > 0.0),
    provider_protocol          TEXT NOT NULL,
    target_model_id            TEXT NOT NULL,
    target_model_version       TEXT NOT NULL,
    price_table_version        TEXT NOT NULL,
    observed_at                INTEGER NOT NULL CHECK (observed_at >= 0),
    PRIMARY KEY (call_site_version, execution_plan_id, plan_observation_sequence)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_disabled (
    call_site_version       TEXT NOT NULL,
    execution_plan_id       TEXT NOT NULL,
    reason                  TEXT NOT NULL,
    exposure                REAL NOT NULL CHECK (exposure >= 0.0),
    disabled_at             INTEGER NOT NULL CHECK (disabled_at >= 0),
    reenable_at             INTEGER NOT NULL CHECK (reenable_at >= disabled_at),
    PRIMARY KEY (call_site_version, execution_plan_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_exploration_site (
    call_site_version       TEXT PRIMARY KEY NOT NULL,
    next_sequence           INTEGER NOT NULL CHECK (next_sequence > 0)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS execution_plan_exploration (
    call_site_version       TEXT NOT NULL,
    exploration_sequence    INTEGER NOT NULL CHECK (exploration_sequence > 0),
    execution_plan_id       TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (
        status IN ('reserved', 'completed', 'failed', 'abandoned')
    ),
    divergence_threshold    REAL NOT NULL CHECK (divergence_threshold BETWEEN 0.0 AND 1.0),
    divergence              REAL CHECK (divergence IS NULL OR divergence BETWEEN 0.0 AND 1.0),
    divergence_exposure     REAL CHECK (divergence_exposure IS NULL OR divergence_exposure >= 0.0),
    feedback_kind           TEXT NOT NULL CHECK (
        feedback_kind IN ('none', 'observation_only', 'task_quality')
    ),
    reference_quality       REAL CHECK (reference_quality IS NULL OR reference_quality BETWEEN 0.0 AND 1.0),
    candidate_quality       REAL CHECK (candidate_quality IS NULL OR candidate_quality BETWEEN 0.0 AND 1.0),
    task_damage             REAL CHECK (task_damage IS NULL OR task_damage >= 0.0),
    cost_usd                REAL CHECK (cost_usd IS NULL OR cost_usd >= 0.0),
    latency_ms              REAL CHECK (latency_ms IS NULL OR latency_ms >= 0.0),
    started_at              INTEGER NOT NULL CHECK (started_at >= 0),
    lease_expires_at        INTEGER NOT NULL CHECK (lease_expires_at >= started_at),
    completed_at            INTEGER CHECK (completed_at IS NULL OR completed_at >= started_at),
    PRIMARY KEY (call_site_version, exploration_sequence),
    CHECK (
        (status = 'reserved' AND feedback_kind = 'none' AND completed_at IS NULL)
        OR (status = 'completed' AND feedback_kind != 'none' AND completed_at IS NOT NULL)
        OR (status IN ('failed', 'abandoned') AND feedback_kind = 'none' AND completed_at IS NOT NULL)
    ),
    CHECK (
        (feedback_kind = 'none' AND divergence IS NULL AND divergence_exposure IS NULL
            AND reference_quality IS NULL AND candidate_quality IS NULL AND task_damage IS NULL
            AND cost_usd IS NULL AND latency_ms IS NULL)
        OR (feedback_kind = 'observation_only' AND divergence IS NOT NULL
            AND divergence_exposure IS NOT NULL AND reference_quality IS NULL
            AND candidate_quality IS NULL AND task_damage IS NULL
            AND cost_usd IS NOT NULL AND latency_ms IS NOT NULL)
        OR (feedback_kind = 'task_quality' AND divergence IS NOT NULL
            AND divergence_exposure IS NOT NULL AND reference_quality IS NOT NULL
            AND candidate_quality IS NOT NULL AND task_damage IS NOT NULL
            AND cost_usd IS NOT NULL AND latency_ms IS NOT NULL)
    )
) STRICT, WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_execution_plan_exploration_site_time
ON execution_plan_exploration(call_site_version, started_at);

CREATE INDEX IF NOT EXISTS idx_execution_plan_exploration_active
ON execution_plan_exploration(call_site_version, status, lease_expires_at);

CREATE TABLE IF NOT EXISTS rule_divergence (
    call_site_id          TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    n_samples             INTEGER NOT NULL,
    window_samples        INTEGER NOT NULL,
    divergence_mean       REAL NOT NULL,
    divergence_var        REAL NOT NULL,
    consecutive_breaches  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (call_site_id, rule)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rule_divergence_observation (
    call_site_id          TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    sample_sequence       INTEGER NOT NULL,
    divergence            REAL NOT NULL CHECK (divergence >= 0.0 AND divergence <= 1.0),
    observed_at           INTEGER NOT NULL,
    PRIMARY KEY (call_site_id, rule, sample_sequence)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS optimizer_disabled (
    call_site_id          TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    reason                TEXT NOT NULL,
    disabled_at           INTEGER NOT NULL,
    reenable_at           INTEGER NOT NULL,
    PRIMARY KEY (call_site_id, rule)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rule_set_stats (
    call_site_id          TEXT NOT NULL,
    rule_set              TEXT NOT NULL,
    n                     INTEGER NOT NULL,
    mean                  REAL NOT NULL,
    m2                    REAL NOT NULL,
    updated_at            INTEGER NOT NULL,
    PRIMARY KEY (call_site_id, rule_set)
) STRICT, WITHOUT ROWID;
"#;

/// Schema for `optimizer_audit.db`:
///
/// - `plan_audit` — one row per optimize_plan dispatch. Append-only; a
///   maintenance job may cap it via `audit::prune` (nothing prunes
///   automatically — see the `audit` module docs).
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
    shadow_divergence     REAL,
    planner_diagnostics_json TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_audit_call_site ON plan_audit(call_site_id, ts_us DESC);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON plan_audit(ts_us);
"#;

/// Apply `cost_model.db` DDL to a connection. Idempotent.
///
/// Also runs column-addition migrations and cold-starts legacy unbounded
/// summaries. Their lifetime invocation count is preserved, but their
/// statistics cannot be converted into an exact retained window and therefore
/// must not influence rewrites after migration.
pub fn ensure_cost_model_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(COST_MODEL_SCHEMA)
        .context("applying cost_model schema")?;
    add_column_if_missing(
        conn,
        "ALTER TABLE call_site_profile \
         ADD COLUMN output_token_p99 REAL NOT NULL DEFAULT 0.0",
        "output_token_p99",
    )?;
    add_column_if_missing(
        conn,
        "ALTER TABLE call_site_profile \
         ADD COLUMN window_observations INTEGER NOT NULL DEFAULT 0",
        "window_observations",
    )?;
    add_column_if_missing(
        conn,
        "ALTER TABLE rule_divergence \
         ADD COLUMN consecutive_breaches INTEGER NOT NULL DEFAULT 0",
        "consecutive_breaches",
    )?;
    add_column_if_missing(
        conn,
        "ALTER TABLE rule_divergence \
         ADD COLUMN window_samples INTEGER NOT NULL DEFAULT 0",
        "window_samples",
    )?;

    conn.execute(
        "UPDATE call_site_profile SET \
            window_observations = 0, \
            input_tokens_mean = 0.0, input_tokens_var = 0.0, \
            output_tokens_mean = 0.0, output_tokens_var = 0.0, \
            latency_ms_mean = 0.0, latency_ms_var = 0.0, \
            cost_usd_mean = 0.0, cost_usd_var = 0.0, \
            output_token_p95 = 0.0, output_token_p99 = 0.0, \
            output_is_structured = 0.0, output_is_short = 0.0 \
         WHERE NOT EXISTS (\
            SELECT 1 FROM call_site_observation AS observation \
            WHERE observation.call_site_id = call_site_profile.call_site_id\
         )",
        [],
    )
    .context("cold-starting legacy unbounded profiles")?;
    conn.execute(
        "DELETE FROM call_site_observation \
         WHERE NOT EXISTS (\
            SELECT 1 FROM call_site_profile AS profile \
            WHERE profile.call_site_id = call_site_observation.call_site_id\
         )",
        [],
    )
    .context("removing orphaned retained samples")?;
    conn.execute(
        "UPDATE rule_divergence SET \
            window_samples = 0, divergence_mean = 0.0, divergence_var = 0.0 \
         WHERE NOT EXISTS (\
            SELECT 1 FROM rule_divergence_observation AS observation \
            WHERE observation.call_site_id = rule_divergence.call_site_id \
              AND observation.rule = rule_divergence.rule\
         )",
        [],
    )
    .context("cold-starting legacy cumulative divergence summaries")?;
    conn.execute(
        "DELETE FROM rule_divergence_observation \
         WHERE NOT EXISTS (\
            SELECT 1 FROM rule_divergence AS summary \
            WHERE summary.call_site_id = rule_divergence_observation.call_site_id \
              AND summary.rule = rule_divergence_observation.rule\
         )",
        [],
    )
    .context("removing orphaned retained divergence samples")?;
    conn.execute(
        "DELETE FROM execution_plan_observation \
         WHERE NOT EXISTS (\
            SELECT 1 FROM execution_plan_profile AS profile \
            WHERE profile.call_site_version = execution_plan_observation.call_site_version \
              AND profile.execution_plan_id = execution_plan_observation.execution_plan_id\
         )",
        [],
    )
    .context("removing orphaned plan observations")?;
    conn.execute(
        "DELETE FROM execution_plan_divergence_observation \
         WHERE NOT EXISTS (\
            SELECT 1 FROM execution_plan_profile AS profile \
            WHERE profile.call_site_version = execution_plan_divergence_observation.call_site_version \
              AND profile.execution_plan_id = execution_plan_divergence_observation.execution_plan_id\
         )",
        [],
    )
    .context("removing orphaned plan-divergence observations")?;
    conn.execute(
        "DELETE FROM execution_plan_guard_observation \
         WHERE NOT EXISTS (\
            SELECT 1 FROM execution_plan_guard AS guard \
            WHERE guard.call_site_version = execution_plan_guard_observation.call_site_version \
              AND guard.execution_plan_id = execution_plan_guard_observation.execution_plan_id\
         )",
        [],
    )
    .context("removing orphaned plan-guard observations")?;
    conn.execute(
        "DELETE FROM execution_plan_disabled \
         WHERE NOT EXISTS (\
            SELECT 1 FROM execution_plan_guard AS guard \
            WHERE guard.call_site_version = execution_plan_disabled.call_site_version \
              AND guard.execution_plan_id = execution_plan_disabled.execution_plan_id\
         )",
        [],
    )
    .context("removing orphaned plan disables")?;
    Ok(())
}

fn add_column_if_missing(conn: &Connection, statement: &str, column: &str) -> Result<()> {
    if let Err(error) = conn.execute_batch(statement) {
        if !error.to_string().contains("duplicate column name") {
            return Err(error).with_context(|| format!("adding {column} column"));
        }
    }
    Ok(())
}

/// Apply `optimizer_audit.db` DDL to a connection. Idempotent.
pub fn ensure_audit_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(AUDIT_SCHEMA)
        .context("applying optimizer_audit schema")?;
    add_column_if_missing(
        conn,
        "ALTER TABLE plan_audit ADD COLUMN planner_diagnostics_json TEXT",
        "planner_diagnostics_json",
    )?;
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
                 AND name IN ('call_site_profile','call_site_observation',\
                              'execution_plan_profile','execution_plan_observation',\
                              'execution_plan_divergence_observation',\
                              'execution_plan_guard','execution_plan_guard_observation',\
                              'execution_plan_disabled',\
                              'execution_plan_exploration_site',\
                              'execution_plan_exploration','rule_divergence',\
                              'rule_divergence_observation','optimizer_disabled',\
                              'rule_set_stats')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(tables, 14);
    }

    #[test]
    fn legacy_audit_schema_gains_planner_diagnostics_column() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE plan_audit (\
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT, \
                ts_us INTEGER NOT NULL, call_site_id TEXT NOT NULL, \
                span_id BLOB NOT NULL, plan_kind TEXT NOT NULL, rule TEXT, \
                projected_savings_usd REAL, measured_savings_usd REAL, \
                overhead_us INTEGER NOT NULL, \
                shadow_sampled INTEGER NOT NULL DEFAULT 0, \
                shadow_divergence REAL\
             ) STRICT",
        )
        .unwrap();

        ensure_audit_schema(&conn).unwrap();
        ensure_audit_schema(&conn).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM pragma_table_info('plan_audit') \
                 WHERE name = 'planner_diagnostics_json'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn exploration_schema_requires_spend_only_for_completed_feedback() {
        let conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();

        let missing_spend = conn.execute(
            "INSERT INTO execution_plan_exploration (\
                call_site_version, exploration_sequence, execution_plan_id, status, \
                divergence_threshold, divergence, divergence_exposure, feedback_kind, \
                reference_quality, candidate_quality, task_damage, cost_usd, latency_ms, \
                started_at, lease_expires_at, completed_at\
             ) VALUES (?1, 1, ?2, 'completed', 0.1, 0.0, 0.0, \
                       'observation_only', NULL, NULL, NULL, NULL, NULL, 1, 2, 2)",
            rusqlite::params!["a".repeat(64), "b".repeat(64)],
        );
        assert!(missing_spend.is_err());

        let premature_spend = conn.execute(
            "INSERT INTO execution_plan_exploration (\
                call_site_version, exploration_sequence, execution_plan_id, status, \
                divergence_threshold, divergence, divergence_exposure, feedback_kind, \
                reference_quality, candidate_quality, task_damage, cost_usd, latency_ms, \
                started_at, lease_expires_at, completed_at\
             ) VALUES (?1, 2, ?2, 'reserved', 0.1, NULL, NULL, \
                       'none', NULL, NULL, NULL, 0.01, 1.0, 1, 2, NULL)",
            rusqlite::params!["a".repeat(64), "b".repeat(64)],
        );
        assert!(premature_spend.is_err());
    }

    #[test]
    fn legacy_site_aggregates_are_not_promoted_to_plan_evidence() {
        let conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        conn.execute(
            "INSERT INTO call_site_profile (\
                call_site_id, n_observations, window_observations, \
                input_tokens_mean, input_tokens_var, output_tokens_mean, output_tokens_var, \
                latency_ms_mean, latency_ms_var, cost_usd_mean, cost_usd_var, \
                output_token_p95, output_token_p99, output_is_structured, output_is_short, \
                updated_at\
             ) VALUES ('legacy', 100, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, \
                       0.0, 0.0, 0.0, 0.0, 1)",
            [],
        )
        .unwrap();

        ensure_cost_model_schema(&conn).unwrap();
        let plan_profiles: i64 = conn
            .query_row("SELECT COUNT(*) FROM execution_plan_profile", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(plan_profiles, 0);
    }

    #[test]
    fn output_token_p99_column_present_after_migration() {
        // The migration must actually add the column (and be idempotent). If a
        // NON-duplicate error were swallowed here, warm_from_db would later
        // fail selecting this column and silently disable the optimizer (bd-c0l).
        let conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        ensure_cost_model_schema(&conn).unwrap();

        let has_col: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM pragma_table_info('call_site_profile') \
                 WHERE name = 'output_token_p99'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(has_col, 1, "output_token_p99 must exist after migration");
    }

    #[test]
    fn legacy_unbounded_profile_is_cold_started_without_losing_lifetime_count() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE call_site_profile (\
                call_site_id TEXT PRIMARY KEY NOT NULL, \
                n_observations INTEGER NOT NULL, \
                input_tokens_mean REAL NOT NULL, input_tokens_var REAL NOT NULL, \
                output_tokens_mean REAL NOT NULL, output_tokens_var REAL NOT NULL, \
                latency_ms_mean REAL NOT NULL, latency_ms_var REAL NOT NULL, \
                cost_usd_mean REAL NOT NULL, cost_usd_var REAL NOT NULL, \
                output_token_p95 REAL NOT NULL, output_token_p99 REAL NOT NULL, \
                output_is_structured REAL NOT NULL, output_is_short REAL NOT NULL, \
                updated_at INTEGER NOT NULL\
             ) STRICT",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO call_site_profile (\
                call_site_id, n_observations, \
                input_tokens_mean, input_tokens_var, \
                output_tokens_mean, output_tokens_var, \
                latency_ms_mean, latency_ms_var, cost_usd_mean, cost_usd_var, \
                output_token_p95, output_token_p99, output_is_structured, \
                output_is_short, updated_at\
             ) VALUES ('legacy', 17, 100.0, 1.0, 200.0, 4.0, \
                       300.0, 9.0, 0.1, 0.01, 250.0, 500.0, 0.5, 0.5, 1)",
            [],
        )
        .unwrap();

        ensure_cost_model_schema(&conn).unwrap();

        let migrated: (i64, i64, f64, f64) = conn
            .query_row(
                "SELECT n_observations, window_observations, \
                        output_tokens_mean, output_token_p99 \
                 FROM call_site_profile WHERE call_site_id = 'legacy'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(migrated, (17, 0, 0.0, 0.0));
    }

    #[test]
    fn legacy_divergence_rows_cold_start_window_and_gain_zero_breach_streak() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE rule_divergence (\
                call_site_id TEXT NOT NULL, rule TEXT NOT NULL, \
                n_samples INTEGER NOT NULL, divergence_mean REAL NOT NULL, \
                divergence_var REAL NOT NULL, \
                PRIMARY KEY (call_site_id, rule)\
             ) STRICT, WITHOUT ROWID; \
             INSERT INTO rule_divergence \
                (call_site_id, rule, n_samples, divergence_mean, divergence_var) \
             VALUES ('legacy', 'RuleA', 3, 0.2, 0.01)",
        )
        .unwrap();

        ensure_cost_model_schema(&conn).unwrap();
        ensure_cost_model_schema(&conn).unwrap();

        let migrated: (i64, i64, f64, f64, i64) = conn
            .query_row(
                "SELECT n_samples, window_samples, divergence_mean, \
                        divergence_var, consecutive_breaches \
                 FROM rule_divergence WHERE call_site_id = 'legacy' AND rule = 'RuleA'",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(migrated, (3, 0, 0.0, 0.0, 0));
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
