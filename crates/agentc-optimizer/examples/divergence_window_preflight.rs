//! Release-mode, zero-network preflight for the bounded guard-divergence window.
//!
//! This is Stage E0 engineering evidence. It exercises deterministic synthetic
//! samples directly against the Rust guard; it does not call an LLM provider or
//! establish controller quality, application quality, savings, or latency.

use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use agentc_optimizer::budget::{Budget, SampleOutcome};
use agentc_optimizer::schema::ensure_cost_model_schema;
use rusqlite::Connection;
use serde_json::json;

const WINDOW: u32 = 50;
const RESIZED_WINDOW: u32 = 10;
const OLD_DIVERGENCE: f32 = 0.8;
const NEW_DIVERGENCE: f32 = 0.02;

fn git_commit() -> Option<String> {
    let result = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .output()
        .ok()?;
    result
        .status
        .success()
        .then(|| String::from_utf8_lossy(&result.stdout).trim().to_string())
}

fn main() -> anyhow::Result<()> {
    let guard = Budget::with_window(WINDOW);
    for sequence in 1..=WINDOW as i64 {
        assert_eq!(
            guard.record_sample(
                "distribution-shift",
                "OutputBudget",
                OLD_DIVERGENCE,
                1.0,
                sequence,
            ),
            SampleOutcome::WithinBudget,
        );
    }
    let before_shift = guard
        .get_entry("distribution-shift", "OutputBudget")
        .expect("warm guard entry");

    for sequence in (WINDOW as i64 + 1)..=(WINDOW as i64 * 2) {
        assert_eq!(
            guard.record_sample(
                "distribution-shift",
                "OutputBudget",
                NEW_DIVERGENCE,
                1.0,
                sequence,
            ),
            SampleOutcome::WithinBudget,
        );
    }
    let after_shift = guard
        .get_entry("distribution-shift", "OutputBudget")
        .expect("shifted guard entry");

    assert_eq!(before_shift.n_samples, WINDOW as u64);
    assert_eq!(before_shift.stats.n, WINDOW as u64);
    assert!((before_shift.stats.mean - OLD_DIVERGENCE as f64).abs() < 1e-7);
    assert_eq!(after_shift.n_samples, (WINDOW * 2) as u64);
    assert_eq!(after_shift.stats.n, WINDOW as u64);
    assert!((after_shift.stats.mean - NEW_DIVERGENCE as f64).abs() < 1e-7);

    let temp = tempfile::TempDir::new()?;
    let database_path = temp.path().join("cost_model.db");
    let mut connection = Connection::open(&database_path)?;
    ensure_cost_model_schema(&connection)?;
    guard.flush_divergence(&mut connection)?;
    let persisted: (i64, i64, f64, i64, i64, i64) = connection.query_row(
        "SELECT summary.n_samples, summary.window_samples, \
                summary.divergence_mean, COUNT(observation.sample_sequence), \
                MIN(observation.sample_sequence), MAX(observation.sample_sequence) \
         FROM rule_divergence AS summary \
         JOIN rule_divergence_observation AS observation \
           ON observation.call_site_id = summary.call_site_id \
          AND observation.rule = summary.rule \
         WHERE summary.call_site_id = 'distribution-shift' \
           AND summary.rule = 'OutputBudget' \
         GROUP BY summary.call_site_id, summary.rule",
        [],
        |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
            ))
        },
    )?;
    assert_eq!(persisted.0, 100);
    assert_eq!(persisted.1, 50);
    assert!((persisted.2 - NEW_DIVERGENCE as f64).abs() < 1e-7);
    assert_eq!((persisted.3, persisted.4, persisted.5), (50, 51, 100));

    let restarted = Budget::with_window(WINDOW);
    assert_eq!(restarted.warm_divergence_from_db(&connection)?, 1);
    let after_restart = restarted
        .get_entry("distribution-shift", "OutputBudget")
        .expect("restarted guard entry");
    assert_eq!(after_restart.n_samples, after_shift.n_samples);
    assert_eq!(after_restart.stats, after_shift.stats);

    let resized = Budget::with_window(RESIZED_WINDOW);
    assert_eq!(resized.warm_divergence_from_db(&connection)?, 1);
    let after_resize = resized
        .get_entry("distribution-shift", "OutputBudget")
        .expect("resized guard entry");
    assert_eq!(after_resize.n_samples, 100);
    assert_eq!(after_resize.stats.n, RESIZED_WINDOW as u64);
    assert!((after_resize.stats.mean - NEW_DIVERGENCE as f64).abs() < 1e-7);
    assert_eq!(resized.dirty_len(), 1);
    resized.flush_divergence(&mut connection)?;
    let resized_persisted: (i64, i64, i64) = connection.query_row(
        "SELECT COUNT(*), MIN(sample_sequence), MAX(sample_sequence) \
         FROM rule_divergence_observation \
         WHERE call_site_id = 'distribution-shift' AND rule = 'OutputBudget'",
        [],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )?;
    assert_eq!(resized_persisted, (10, 91, 100));

    let resized_restart = Budget::with_window(RESIZED_WINDOW);
    assert_eq!(resized_restart.warm_divergence_from_db(&connection)?, 1);
    assert_eq!(resized_restart.dirty_len(), 0);
    let after_resized_restart = resized_restart
        .get_entry("distribution-shift", "OutputBudget")
        .expect("resized restarted guard entry");
    assert_eq!(after_resized_restart.stats, after_resize.stats);

    let legacy_connection = Connection::open_in_memory()?;
    legacy_connection.execute_batch(
        "CREATE TABLE rule_divergence (\
            call_site_id TEXT NOT NULL, rule TEXT NOT NULL, \
            n_samples INTEGER NOT NULL, divergence_mean REAL NOT NULL, \
            divergence_var REAL NOT NULL, \
            consecutive_breaches INTEGER NOT NULL DEFAULT 0, \
            PRIMARY KEY (call_site_id, rule)\
         ) STRICT, WITHOUT ROWID; \
         INSERT INTO rule_divergence (\
            call_site_id, rule, n_samples, divergence_mean, divergence_var, \
            consecutive_breaches\
         ) VALUES ('legacy', 'OutputBudget', 100, 0.8, 0.01, 4);",
    )?;
    ensure_cost_model_schema(&legacy_connection)?;
    let legacy_summary: (i64, i64, f64, f64, i64) = legacy_connection.query_row(
        "SELECT n_samples, window_samples, divergence_mean, divergence_var, \
                consecutive_breaches \
         FROM rule_divergence WHERE call_site_id = 'legacy' AND rule = 'OutputBudget'",
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
    )?;
    assert_eq!(legacy_summary, (100, 0, 0.0, 0.0, 4));
    let migrated = Budget::with_window(WINDOW);
    assert_eq!(migrated.warm_divergence_from_db(&legacy_connection)?, 1);
    let migrated_entry = migrated
        .get_entry("legacy", "OutputBudget")
        .expect("migrated guard entry");
    assert_eq!(migrated_entry.n_samples, 100);
    assert_eq!(migrated_entry.stats.n, 0);
    assert_eq!(migrated_entry.consecutive_breaches, 4);

    let created_at_unix_us = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_micros())
        .unwrap_or(0);
    let payload = json!({
        "schema_version": 1,
        "experiment_kind": "bounded_divergence_window_preflight",
        "created_at_unix_us": created_at_unix_us,
        "agentc_git_commit": git_commit(),
        "paper_evidence": false,
        "network_calls": 0,
        "release_build": !cfg!(debug_assertions),
        "settings": {
            "window": WINDOW,
            "resized_window": RESIZED_WINDOW,
            "old_distribution_samples": WINDOW,
            "new_distribution_samples": WINDOW,
            "old_divergence": OLD_DIVERGENCE,
            "new_divergence": NEW_DIVERGENCE
        },
        "distribution_shift": {
            "before": {
                "lifetime_samples": before_shift.n_samples,
                "window_samples": before_shift.stats.n,
                "divergence_mean": before_shift.stats.mean
            },
            "after": {
                "lifetime_samples": after_shift.n_samples,
                "window_samples": after_shift.stats.n,
                "divergence_mean": after_shift.stats.mean
            },
            "old_distribution_fully_aged_out": true
        },
        "persistence": {
            "lifetime_samples": persisted.0,
            "window_samples": persisted.1,
            "retained_rows": persisted.3,
            "first_retained_sequence": persisted.4,
            "last_retained_sequence": persisted.5,
            "restart_window_equal": true
        },
        "resize": {
            "window_samples": after_resize.stats.n,
            "retained_rows": resized_persisted.0,
            "first_retained_sequence": resized_persisted.1,
            "last_retained_sequence": resized_persisted.2,
            "second_restart_window_equal": true
        },
        "legacy_migration": {
            "lifetime_samples_preserved": legacy_summary.0,
            "window_samples": legacy_summary.1,
            "divergence_mean": legacy_summary.2,
            "divergence_var": legacy_summary.3,
            "consecutive_breaches_preserved": legacy_summary.4,
            "unreconstructable_statistics_cold_started": true
        },
        "controller_contract": {
            "comparison_basis": "raw sample against rule budget",
            "consecutive_breach_logic_changed": false
        },
        "interpretation_limits": [
            "This is Stage E0 engineering evidence, not a provider-backed or paper result.",
            "The inputs are deterministic synthetic divergence values and do not measure application quality or savings.",
            "The preflight validates estimator aging and restart equivalence, not harmful-site detection, false disables, or cumulative damage.",
            "Durable-write overhead and mutex contention are measured separately under bd-rm0w."
        ]
    });
    println!("{}", serde_json::to_string_pretty(&payload)?);
    Ok(())
}
