//! Release-mode, zero-network preflight for the bounded cost-model window.
//!
//! This is Stage E0 engineering evidence. It exercises deterministic synthetic
//! observations directly against the Rust cost model; it does not call an LLM
//! provider or establish workload quality, savings, or latency.

use std::hint::black_box;
use std::process::Command;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use agentc_optimizer::cost_model::{CostModel, CostModelUpdate};
use agentc_optimizer::schema::ensure_cost_model_schema;
use rusqlite::Connection;
use serde_json::json;

const WINDOW: u32 = 50;
const OLD_VALUE: u32 = 1_000;
const NEW_VALUE: u32 = 80;
const SNAPSHOT_ITERATIONS: u64 = 200_000;
const OBSERVE_ITERATIONS: u64 = 20_000;

fn update(value: u32, sequence: i64) -> CostModelUpdate {
    CostModelUpdate {
        call_site_id: "distribution-shift".to_string(),
        input_tokens: value.saturating_mul(2),
        output_tokens: value,
        latency_ms: value as f64,
        cost_usd: value as f64 / 10_000.0,
        output_is_structured: value == NEW_VALUE,
        output_is_short: value <= 128,
        now_us: Some(sequence),
    }
}

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

fn nanos_per_iteration(elapsed: std::time::Duration, iterations: u64) -> f64 {
    elapsed.as_nanos() as f64 / iterations as f64
}

fn main() -> anyhow::Result<()> {
    let model = CostModel::with_window(WINDOW);
    for sequence in 1..=WINDOW as i64 {
        model.observe(update(OLD_VALUE, sequence));
    }
    let before_shift = model.get("distribution-shift").expect("warm profile");

    for sequence in (WINDOW as i64 + 1)..=(WINDOW as i64 * 2) {
        model.observe(update(NEW_VALUE, sequence));
    }
    let after_shift = model.get("distribution-shift").expect("shifted profile");

    assert_eq!(before_shift.n_observations, WINDOW);
    assert_eq!(before_shift.window_observations, WINDOW);
    assert_eq!(before_shift.output_tokens.mean, OLD_VALUE as f64);
    assert_eq!(before_shift.output_token_p95, OLD_VALUE as f32);
    assert_eq!(before_shift.output_token_p99, OLD_VALUE as f32);
    assert_eq!(after_shift.n_observations, WINDOW * 2);
    assert_eq!(after_shift.window_observations, WINDOW);
    assert_eq!(after_shift.input_tokens.mean, (NEW_VALUE * 2) as f64);
    assert_eq!(after_shift.output_tokens.mean, NEW_VALUE as f64);
    assert_eq!(after_shift.latency_ms.mean, NEW_VALUE as f64);
    assert_eq!(after_shift.cost_usd.mean, NEW_VALUE as f64 / 10_000.0);
    assert_eq!(after_shift.output_token_p95, NEW_VALUE as f32);
    assert_eq!(after_shift.output_token_p99, NEW_VALUE as f32);
    assert_eq!(after_shift.output_is_structured, 1.0);
    assert_eq!(after_shift.output_is_short, 1.0);

    for _ in 0..WINDOW {
        model.observe_rule_set("distribution-shift", &["OutputBudget"], 1.0);
    }
    for _ in 0..WINDOW {
        model.observe_rule_set("distribution-shift", &["OutputBudget"], 0.01);
    }
    let rule_set = model
        .get_rule_set_stats("distribution-shift", &["OutputBudget"])
        .expect("rule-set profile");
    assert_eq!(rule_set.n, WINDOW as u64);
    assert!((rule_set.mean - 0.01).abs() < 1e-12);

    let temp = tempfile::TempDir::new()?;
    let database_path = temp.path().join("cost_model.db");
    let mut connection = Connection::open(&database_path)?;
    ensure_cost_model_schema(&connection)?;
    model.flush_dirty(&mut connection)?;
    let persisted: (i64, i64, i64) = connection.query_row(
        "SELECT profile.n_observations, profile.window_observations, COUNT(observation.sample_sequence) \
         FROM call_site_profile AS profile \
         LEFT JOIN call_site_observation AS observation \
           ON observation.call_site_id = profile.call_site_id \
         WHERE profile.call_site_id = 'distribution-shift' \
         GROUP BY profile.call_site_id",
        [],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )?;
    assert_eq!(persisted, (100, 50, 50));

    let restarted = CostModel::with_window(WINDOW);
    let reloaded_profiles = restarted.warm_from_db(&connection)?;
    let after_restart = restarted
        .get("distribution-shift")
        .expect("restarted profile");
    assert_eq!(reloaded_profiles, 1);
    assert_eq!(after_restart.n_observations, after_shift.n_observations);
    assert_eq!(
        after_restart.window_observations,
        after_shift.window_observations
    );
    assert_eq!(after_restart.output_tokens, after_shift.output_tokens);
    assert_eq!(after_restart.output_token_p95, after_shift.output_token_p95);
    assert_eq!(after_restart.output_token_p99, after_shift.output_token_p99);

    let started = Instant::now();
    for _ in 0..SNAPSHOT_ITERATIONS {
        black_box(restarted.get("distribution-shift"));
    }
    let snapshot_elapsed = started.elapsed();

    let observe_model = CostModel::with_window(WINDOW);
    for sequence in 1..=WINDOW as i64 {
        observe_model.observe(update(NEW_VALUE, sequence));
    }
    let started = Instant::now();
    for sequence in 1..=OBSERVE_ITERATIONS as i64 {
        observe_model.observe(update(NEW_VALUE, sequence));
    }
    let observe_elapsed = started.elapsed();

    let created_at_unix_us = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_micros())
        .unwrap_or(0);
    let payload = json!({
        "schema_version": 1,
        "experiment_kind": "bounded_cost_model_window_preflight",
        "created_at_unix_us": created_at_unix_us,
        "agentc_git_commit": git_commit(),
        "paper_evidence": false,
        "network_calls": 0,
        "release_build": !cfg!(debug_assertions),
        "settings": {
            "window": WINDOW,
            "old_distribution_observations": WINDOW,
            "new_distribution_observations": WINDOW,
            "old_value": OLD_VALUE,
            "new_value": NEW_VALUE
        },
        "distribution_shift": {
            "before": {
                "lifetime_observations": before_shift.n_observations,
                "window_observations": before_shift.window_observations,
                "output_mean": before_shift.output_tokens.mean,
                "output_p95": before_shift.output_token_p95,
                "output_p99": before_shift.output_token_p99
            },
            "after": {
                "lifetime_observations": after_shift.n_observations,
                "window_observations": after_shift.window_observations,
                "input_mean": after_shift.input_tokens.mean,
                "output_mean": after_shift.output_tokens.mean,
                "latency_mean_ms": after_shift.latency_ms.mean,
                "cost_mean_usd": after_shift.cost_usd.mean,
                "output_p95": after_shift.output_token_p95,
                "output_p99": after_shift.output_token_p99,
                "structured_fraction": after_shift.output_is_structured,
                "short_fraction": after_shift.output_is_short
            },
            "old_distribution_fully_aged_out": true
        },
        "rule_set_window": {
            "retained_observations": rule_set.n,
            "mean_savings_usd": rule_set.mean,
            "old_distribution_fully_aged_out": true
        },
        "persistence": {
            "lifetime_observations": persisted.0,
            "window_observations": persisted.1,
            "retained_rows": persisted.2,
            "reloaded_profiles": reloaded_profiles,
            "restart_profile_equal": true
        },
        "microbenchmark": {
            "profile_snapshot": {
                "iterations": SNAPSHOT_ITERATIONS,
                "elapsed_ns": snapshot_elapsed.as_nanos(),
                "mean_ns_per_iteration": nanos_per_iteration(
                    snapshot_elapsed,
                    SNAPSHOT_ITERATIONS
                )
            },
            "window_observe": {
                "iterations": OBSERVE_ITERATIONS,
                "elapsed_ns": observe_elapsed.as_nanos(),
                "mean_ns_per_iteration": nanos_per_iteration(
                    observe_elapsed,
                    OBSERVE_ITERATIONS
                )
            }
        },
        "interpretation_limits": [
            "This is Stage E0 engineering evidence, not a provider-backed or paper result.",
            "The inputs are deterministic synthetic observations and do not measure application quality or savings.",
            "Microbenchmark timings are single-process wall-clock diagnostics, not a publication-grade benchmark.",
            "The optimizer planning path is not measured by this preflight."
        ]
    });
    println!("{}", serde_json::to_string_pretty(&payload)?);
    Ok(())
}
