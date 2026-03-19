//! Cost computation and pricing management.
//!
//! Loads bundled pricing, user overrides, and backfills `cost_usd` on spans.
//! Cost is computed at query time (not capture time) so pricing updates
//! retroactively apply to old spans.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use serde::Deserialize;

use agentc_core::span::ModelPricing;

/// Bundled pricing data embedded at compile time.
const BUNDLED_PRICING_JSON: &str = include_str!("../../../data/pricing.json");

/// Frontier-tier threshold: models with input_cost >= this are "frontier".
pub const FRONTIER_THRESHOLD_USD: f64 = 3.0;

/// Staleness threshold: warn if bundled pricing is older than this.
const STALENESS_DAYS: u64 = 90;

/// Stats returned from a backfill operation.
#[derive(Debug, Clone, Default)]
pub struct BackfillStats {
    pub spans_updated: usize,
    pub unknown_models: Vec<String>,
}

/// Deserialized bundled pricing file.
#[derive(Debug, Deserialize)]
struct BundledPricing {
    updated_at: String,
    models: HashMap<String, BundledModelPricing>,
}

/// Per-model pricing entry in bundled JSON.
#[derive(Debug, Deserialize)]
struct BundledModelPricing {
    input_cost: f64,
    output_cost: f64,
    cache_creation_cost: Option<f64>,
    cache_read_cost: Option<f64>,
    context_window: Option<i64>,
}

/// User pricing override from TOML.
#[derive(Debug, Deserialize)]
struct UserPricingEntry {
    input_cost: f64,
    output_cost: f64,
    cache_creation_cost: Option<f64>,
    cache_read_cost: Option<f64>,
    context_window: Option<i64>,
}

/// Return the default path for user pricing overrides.
pub fn user_pricing_path() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".agentc")
        .join("pricing.toml")
}

/// Parse the bundled pricing timestamp and check if it's older than 90 days.
pub fn check_pricing_staleness() -> Option<String> {
    let pricing: BundledPricing = serde_json::from_str(BUNDLED_PRICING_JSON).ok()?;
    // Parse ISO 8601 timestamp manually (avoid adding chrono dep).
    // Format: "2026-03-19T00:00:00Z"
    let ts = &pricing.updated_at;
    let year: u64 = ts.get(0..4)?.parse().ok()?;
    let month: u64 = ts.get(5..7)?.parse().ok()?;
    let day: u64 = ts.get(8..10)?.parse().ok()?;

    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?;
    let now_days = now.as_secs() / 86400;

    let pricing_epoch_days = days_since_epoch(year as i32, month as u32, day as u32)?;
    let staleness_days = now_days as i64 - pricing_epoch_days;

    if staleness_days > STALENESS_DAYS as i64 {
        Some(format!(
            "Bundled pricing is {staleness_days} days old (> {STALENESS_DAYS} days). \
             Run `agentc pricing update` to refresh."
        ))
    } else {
        None
    }
}

/// Rough days since Unix epoch for a date.
fn days_since_epoch(year: i32, month: u32, day: u32) -> Option<i64> {
    // Simplified Julian Day Number calculation.
    let y = if month <= 2 { year - 1 } else { year } as i64;
    let m = if month <= 2 { month + 12 } else { month } as i64;
    let d = day as i64;

    let jdn = 365 * y + y / 4 - y / 100 + y / 400 + (153 * (m - 3) + 2) / 5 + d - 719469;
    Some(jdn)
}

/// Load bundled pricing into the model_pricing table (INSERT OR IGNORE).
pub fn load_bundled_pricing(conn: &Connection) -> Result<usize> {
    let pricing: BundledPricing =
        serde_json::from_str(BUNDLED_PRICING_JSON).context("Failed to parse bundled pricing")?;

    let now_us = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_micros() as i64;

    let mut loaded = 0;
    for (model_id, model_pricing) in &pricing.models {
        let mp = ModelPricing {
            model_id: model_id.clone(),
            input_cost: model_pricing.input_cost,
            output_cost: model_pricing.output_cost,
            cache_creation_cost: model_pricing.cache_creation_cost,
            cache_read_cost: model_pricing.cache_read_cost,
            context_window: model_pricing.context_window,
            updated_at: now_us,
            source: "bundled".to_string(),
        };
        agentc_core::db::insert_pricing(conn, &mp)?;
        loaded += 1;
    }

    Ok(loaded)
}

/// Load user pricing overrides from a TOML file (INSERT OR REPLACE).
pub fn load_user_pricing(conn: &Connection, toml_path: &Path) -> Result<usize> {
    if !toml_path.exists() {
        return Ok(0);
    }

    let content = std::fs::read_to_string(toml_path)
        .with_context(|| format!("Failed to read pricing TOML at {}", toml_path.display()))?;

    let table: HashMap<String, UserPricingEntry> =
        toml::from_str(&content).context("Failed to parse pricing TOML")?;

    let now_us = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_micros() as i64;

    let mut loaded = 0;
    for (model_id, entry) in &table {
        let mp = ModelPricing {
            model_id: model_id.clone(),
            input_cost: entry.input_cost,
            output_cost: entry.output_cost,
            cache_creation_cost: entry.cache_creation_cost,
            cache_read_cost: entry.cache_read_cost,
            context_window: entry.context_window,
            updated_at: now_us,
            source: "user".to_string(),
        };
        agentc_core::db::insert_pricing(conn, &mp)?;
        loaded += 1;
    }

    Ok(loaded)
}

/// Backfill cost_usd for spans that don't yet have it.
///
/// Uses the cost formula from the spec with COALESCE fallbacks:
/// - cache_creation_cost falls back to input_cost when NULL
/// - cache_read_cost falls back to input_cost when NULL
/// - Token counts COALESCE to 0 when NULL
///
/// Returns stats including number of spans updated and any unknown models.
pub fn backfill_costs(conn: &Connection) -> Result<BackfillStats> {
    // Find unknown models (spans with model not in pricing table).
    let mut unknown_stmt = conn.prepare(
        "SELECT DISTINCT s.model FROM spans s \
         LEFT JOIN model_pricing mp ON s.model = mp.model_id \
         WHERE s.cost_usd IS NULL AND s.model IS NOT NULL AND mp.model_id IS NULL",
    )?;
    let unknown_models: Vec<String> = unknown_stmt
        .query_map([], |row| row.get(0))?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    for model in &unknown_models {
        eprintln!("WARN: Unknown model '{model}' — cost_usd will remain NULL.");
    }

    // Batch UPDATE for all spans with known pricing.
    let updated = conn.execute(
        "UPDATE spans SET cost_usd = (
            COALESCE(input_tokens, 0) * mp.input_cost
            + COALESCE(output_tokens, 0) * mp.output_cost
            + COALESCE(cache_creation_tokens, 0) * COALESCE(mp.cache_creation_cost, mp.input_cost)
            + COALESCE(cache_read_tokens, 0) * COALESCE(mp.cache_read_cost, mp.input_cost)
        ) / 1000000.0
        FROM model_pricing mp
        WHERE spans.model = mp.model_id AND spans.cost_usd IS NULL",
        [],
    )?;

    Ok(BackfillStats {
        spans_updated: updated,
        unknown_models,
    })
}

/// Full backfill flow: load pricing, then backfill costs.
///
/// Called by `agentc analyze`, `agentc traces`, etc.
pub fn full_cost_backfill(conn: &Connection) -> Result<BackfillStats> {
    load_bundled_pricing(conn)?;

    let user_path = user_pricing_path();
    if user_path.exists() {
        load_user_pricing(conn, &user_path)?;
    }

    if let Some(warning) = check_pricing_staleness() {
        eprintln!("WARN: {warning}");
    }

    backfill_costs(conn)
}

/// Check if a model is considered "frontier tier" based on its input cost.
pub fn is_frontier_model(conn: &Connection, model_id: &str) -> Result<bool> {
    let result: Option<f64> = conn
        .query_row(
            "SELECT input_cost FROM model_pricing WHERE model_id = ?1",
            params![model_id],
            |row| row.get(0),
        )
        .ok();

    Ok(result.is_some_and(|cost| cost >= FRONTIER_THRESHOLD_USD))
}

/// Update pricing from a JSON string (e.g., fetched from remote).
///
/// Parses the same format as bundled pricing and inserts via INSERT OR REPLACE.
pub fn update_pricing_from_json(conn: &Connection, json_str: &str) -> Result<usize> {
    let pricing: BundledPricing =
        serde_json::from_str(json_str).context("Failed to parse pricing JSON")?;

    let now_us = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_micros() as i64;

    let mut updated = 0;
    for (model_id, model_pricing) in &pricing.models {
        // Use INSERT OR REPLACE for remote updates (like user overrides).
        conn.execute(
            "INSERT OR REPLACE INTO model_pricing (
                model_id, input_cost, output_cost, cache_creation_cost, cache_read_cost,
                context_window, updated_at, source
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'bundled')",
            params![
                model_id,
                model_pricing.input_cost,
                model_pricing.output_cost,
                model_pricing.cache_creation_cost,
                model_pricing.cache_read_cost,
                model_pricing.context_window,
                now_us,
            ],
        )?;
        updated += 1;
    }

    Ok(updated)
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentc_core::db::{create_db, insert_span};
    use agentc_core::span::Span;
    use tempfile::TempDir;

    fn test_span(id: &str, model: &str, input_tok: i64, output_tok: i64) -> Span {
        Span {
            span_id: id.to_string(),
            trace_id: "trace-1".to_string(),
            parent_span_id: None,
            name: "test_call".to_string(),
            kind: "chat".to_string(),
            start_time: 1000000,
            end_time: Some(2000000),
            status: "OK".to_string(),
            model: Some(model.to_string()),
            provider: Some("anthropic".to_string()),
            input_tokens: Some(input_tok),
            output_tokens: Some(output_tok),
            cache_creation_tokens: None,
            cache_read_tokens: None,
            cost_usd: None,
            attributes: "{}".to_string(),
            input_content_id: None,
            output_content_id: None,
            input_embedding: None,
            output_embedding: None,
            embedding_model: None,
        }
    }

    fn setup_canonical_db() -> (TempDir, Connection) {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("traces.db");
        let conn = create_db(&path, true).unwrap();
        (dir, conn)
    }

    #[test]
    fn test_cost_formula_all_token_types() {
        let (_dir, conn) = setup_canonical_db();

        // Insert pricing with cache costs.
        let mp = ModelPricing {
            model_id: "claude-sonnet-4-20250514".to_string(),
            input_cost: 3.0,
            output_cost: 15.0,
            cache_creation_cost: Some(3.75),
            cache_read_cost: Some(0.30),
            context_window: Some(200000),
            updated_at: 1000000,
            source: "bundled".to_string(),
        };
        agentc_core::db::insert_pricing(&conn, &mp).unwrap();

        // Span with all token types.
        let mut span = test_span("s1", "claude-sonnet-4-20250514", 1000, 500);
        span.cache_creation_tokens = Some(200);
        span.cache_read_tokens = Some(300);
        insert_span(&conn, &span).unwrap();

        let stats = backfill_costs(&conn).unwrap();
        assert_eq!(stats.spans_updated, 1);

        let cost: f64 = conn
            .query_row(
                "SELECT cost_usd FROM spans WHERE span_id = 's1'",
                [],
                |row| row.get(0),
            )
            .unwrap();

        // Expected: (1000*3.0 + 500*15.0 + 200*3.75 + 300*0.30) / 1_000_000
        // = (3000 + 7500 + 750 + 90) / 1_000_000 = 11340 / 1_000_000 = 0.01134
        assert!(
            (cost - 0.01134).abs() < 1e-10,
            "Expected 0.01134, got {cost}"
        );
    }

    #[test]
    fn test_cost_formula_null_cache_costs_fallback() {
        let (_dir, conn) = setup_canonical_db();

        // Pricing WITHOUT cache costs (like gpt-4o).
        let mp = ModelPricing {
            model_id: "gpt-4o".to_string(),
            input_cost: 2.50,
            output_cost: 10.0,
            cache_creation_cost: None,
            cache_read_cost: None,
            context_window: Some(128000),
            updated_at: 1000000,
            source: "bundled".to_string(),
        };
        agentc_core::db::insert_pricing(&conn, &mp).unwrap();

        let mut span = test_span("s1", "gpt-4o", 1000, 500);
        span.cache_creation_tokens = Some(200);
        span.cache_read_tokens = Some(100);
        insert_span(&conn, &span).unwrap();

        backfill_costs(&conn).unwrap();

        let cost: f64 = conn
            .query_row(
                "SELECT cost_usd FROM spans WHERE span_id = 's1'",
                [],
                |row| row.get(0),
            )
            .unwrap();

        // Cache costs fall back to input_cost (2.50).
        // (1000*2.50 + 500*10.0 + 200*2.50 + 100*2.50) / 1_000_000
        // = (2500 + 5000 + 500 + 250) / 1_000_000 = 8250 / 1_000_000 = 0.00825
        assert!(
            (cost - 0.00825).abs() < 1e-10,
            "Expected 0.00825, got {cost}"
        );
    }

    #[test]
    fn test_cost_formula_null_token_types() {
        let (_dir, conn) = setup_canonical_db();

        let mp = ModelPricing {
            model_id: "test-model".to_string(),
            input_cost: 3.0,
            output_cost: 15.0,
            cache_creation_cost: Some(3.75),
            cache_read_cost: Some(0.30),
            context_window: None,
            updated_at: 1000000,
            source: "bundled".to_string(),
        };
        agentc_core::db::insert_pricing(&conn, &mp).unwrap();

        // Span with NULL cache tokens.
        let span = test_span("s1", "test-model", 1000, 500);
        insert_span(&conn, &span).unwrap();

        backfill_costs(&conn).unwrap();

        let cost: f64 = conn
            .query_row(
                "SELECT cost_usd FROM spans WHERE span_id = 's1'",
                [],
                |row| row.get(0),
            )
            .unwrap();

        // NULL cache tokens → COALESCE to 0.
        // (1000*3.0 + 500*15.0 + 0*3.75 + 0*0.30) / 1_000_000
        // = (3000 + 7500) / 1_000_000 = 10500 / 1_000_000 = 0.0105
        assert!(
            (cost - 0.0105).abs() < 1e-10,
            "Expected 0.0105, got {cost}"
        );
    }

    #[test]
    fn test_backfill_preserves_existing_cost() {
        let (_dir, conn) = setup_canonical_db();

        let mp = ModelPricing {
            model_id: "test-model".to_string(),
            input_cost: 3.0,
            output_cost: 15.0,
            cache_creation_cost: None,
            cache_read_cost: None,
            context_window: None,
            updated_at: 1000000,
            source: "bundled".to_string(),
        };
        agentc_core::db::insert_pricing(&conn, &mp).unwrap();

        // Span with existing cost.
        let mut span = test_span("s1", "test-model", 1000, 500);
        span.cost_usd = Some(0.999);
        insert_span(&conn, &span).unwrap();

        // Span without cost.
        let span2 = test_span("s2", "test-model", 1000, 500);
        insert_span(&conn, &span2).unwrap();

        let stats = backfill_costs(&conn).unwrap();
        assert_eq!(stats.spans_updated, 1); // Only s2 updated.

        let cost1: f64 = conn
            .query_row(
                "SELECT cost_usd FROM spans WHERE span_id = 's1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cost1, 0.999); // Preserved.
    }

    #[test]
    fn test_bundled_pricing_insert_or_ignore() {
        let (_dir, conn) = setup_canonical_db();

        load_bundled_pricing(&conn).unwrap();

        // Check a known model exists.
        let cost: f64 = conn
            .query_row(
                "SELECT input_cost FROM model_pricing WHERE model_id = 'claude-sonnet-4-20250514'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cost, 3.0);

        // Modify and reload — should NOT overwrite (INSERT OR IGNORE).
        conn.execute(
            "UPDATE model_pricing SET input_cost = 999.0 WHERE model_id = 'claude-sonnet-4-20250514'",
            [],
        )
        .unwrap();

        load_bundled_pricing(&conn).unwrap();

        let cost2: f64 = conn
            .query_row(
                "SELECT input_cost FROM model_pricing WHERE model_id = 'claude-sonnet-4-20250514'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cost2, 999.0); // Not overwritten.
    }

    #[test]
    fn test_user_override_replaces_bundled() {
        let (_dir, conn) = setup_canonical_db();

        load_bundled_pricing(&conn).unwrap();

        // Write user pricing TOML.
        let dir = TempDir::new().unwrap();
        let toml_path = dir.path().join("pricing.toml");
        std::fs::write(
            &toml_path,
            r#"
[claude-sonnet-4-20250514]
input_cost = 1.50
output_cost = 7.50
"#,
        )
        .unwrap();

        load_user_pricing(&conn, &toml_path).unwrap();

        let cost: f64 = conn
            .query_row(
                "SELECT input_cost FROM model_pricing WHERE model_id = 'claude-sonnet-4-20250514'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cost, 1.50); // User override applied.

        let source: String = conn
            .query_row(
                "SELECT source FROM model_pricing WHERE model_id = 'claude-sonnet-4-20250514'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(source, "user");
    }

    #[test]
    fn test_user_override_required_fields_only() {
        let (_dir, conn) = setup_canonical_db();

        let dir = TempDir::new().unwrap();
        let toml_path = dir.path().join("pricing.toml");
        std::fs::write(
            &toml_path,
            r#"
[my-custom-model]
input_cost = 5.00
output_cost = 20.00
"#,
        )
        .unwrap();

        let loaded = load_user_pricing(&conn, &toml_path).unwrap();
        assert_eq!(loaded, 1);

        let cost: f64 = conn
            .query_row(
                "SELECT input_cost FROM model_pricing WHERE model_id = 'my-custom-model'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cost, 5.0);
    }

    #[test]
    fn test_unknown_model_stays_null() {
        let (_dir, conn) = setup_canonical_db();

        load_bundled_pricing(&conn).unwrap();

        let span = test_span("s1", "unknown-model-xyz", 1000, 500);
        insert_span(&conn, &span).unwrap();

        let stats = backfill_costs(&conn).unwrap();
        assert_eq!(stats.spans_updated, 0);
        assert!(stats.unknown_models.contains(&"unknown-model-xyz".to_string()));

        let cost: Option<f64> = conn
            .query_row(
                "SELECT cost_usd FROM spans WHERE span_id = 's1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(cost.is_none());
    }

    #[test]
    fn test_frontier_model_threshold() {
        let (_dir, conn) = setup_canonical_db();

        load_bundled_pricing(&conn).unwrap();

        // claude-sonnet-4 has input_cost = 3.00, which is >= 3.0 → frontier.
        assert!(is_frontier_model(&conn, "claude-sonnet-4-20250514").unwrap());

        // gpt-4o has input_cost = 2.50, which is < 3.0 → not frontier.
        assert!(!is_frontier_model(&conn, "gpt-4o-2024-11-20").unwrap());

        // Unknown model → not frontier.
        assert!(!is_frontier_model(&conn, "nonexistent-model").unwrap());
    }

    #[test]
    fn test_pricing_staleness_not_stale() {
        // Bundled pricing is dated 2026-03-19, which is today. Should not be stale.
        let result = check_pricing_staleness();
        assert!(result.is_none());
    }

    #[test]
    fn test_update_pricing_from_json() {
        let (_dir, conn) = setup_canonical_db();

        let json = r#"{
            "updated_at": "2026-03-19T00:00:00Z",
            "models": {
                "new-model": {
                    "input_cost": 5.0,
                    "output_cost": 20.0
                }
            }
        }"#;

        let updated = update_pricing_from_json(&conn, json).unwrap();
        assert_eq!(updated, 1);

        let cost: f64 = conn
            .query_row(
                "SELECT input_cost FROM model_pricing WHERE model_id = 'new-model'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cost, 5.0);
    }

    #[test]
    fn test_update_pricing_overwrites_existing() {
        let (_dir, conn) = setup_canonical_db();

        load_bundled_pricing(&conn).unwrap();

        // Fetch updated pricing with different costs.
        let json = r#"{
            "updated_at": "2026-03-20T00:00:00Z",
            "models": {
                "claude-sonnet-4-20250514": {
                    "input_cost": 2.00,
                    "output_cost": 10.00
                }
            }
        }"#;

        update_pricing_from_json(&conn, json).unwrap();

        let cost: f64 = conn
            .query_row(
                "SELECT input_cost FROM model_pricing WHERE model_id = 'claude-sonnet-4-20250514'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cost, 2.0); // Updated.
    }

    #[test]
    fn test_user_pricing_nonexistent_file() {
        let (_dir, conn) = setup_canonical_db();
        let loaded = load_user_pricing(&conn, Path::new("/nonexistent/pricing.toml")).unwrap();
        assert_eq!(loaded, 0);
    }

    // --- Integration tests ---

    #[test]
    fn test_integration_full_backfill_flow() {
        let (_dir, conn) = setup_canonical_db();

        // Insert spans with NULL cost.
        for i in 0..5 {
            let span = test_span(
                &format!("s{i}"),
                "claude-sonnet-4-20250514",
                1000 + i * 100,
                500 + i * 50,
            );
            insert_span(&conn, &span).unwrap();
        }

        // Full backfill.
        let stats = full_cost_backfill(&conn).unwrap();
        assert_eq!(stats.spans_updated, 5);

        // All spans should have costs.
        let null_count: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM spans WHERE cost_usd IS NULL",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(null_count, 0);
    }

    #[test]
    fn test_integration_pricing_update_retroactive() {
        let (_dir, conn) = setup_canonical_db();

        // Insert pricing and spans.
        let mp = ModelPricing {
            model_id: "test-model".to_string(),
            input_cost: 3.0,
            output_cost: 15.0,
            cache_creation_cost: None,
            cache_read_cost: None,
            context_window: None,
            updated_at: 1000000,
            source: "bundled".to_string(),
        };
        agentc_core::db::insert_pricing(&conn, &mp).unwrap();

        let span = test_span("s1", "test-model", 1000, 500);
        insert_span(&conn, &span).unwrap();

        backfill_costs(&conn).unwrap();

        let cost1: f64 = conn
            .query_row(
                "SELECT cost_usd FROM spans WHERE span_id = 's1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        // (1000*3.0 + 500*15.0) / 1M = 10500 / 1M = 0.0105
        assert!((cost1 - 0.0105).abs() < 1e-10);

        // Update pricing (user override with lower costs).
        conn.execute(
            "INSERT OR REPLACE INTO model_pricing \
             (model_id, input_cost, output_cost, updated_at, source) \
             VALUES ('test-model', 1.5, 7.5, 2000000, 'user')",
            [],
        )
        .unwrap();

        // Reset cost_usd to NULL to simulate re-backfill.
        conn.execute("UPDATE spans SET cost_usd = NULL", [])
            .unwrap();

        backfill_costs(&conn).unwrap();

        let cost2: f64 = conn
            .query_row(
                "SELECT cost_usd FROM spans WHERE span_id = 's1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        // (1000*1.5 + 500*7.5) / 1M = 5250 / 1M = 0.00525
        assert!((cost2 - 0.00525).abs() < 1e-10);
    }

    #[test]
    fn test_days_since_epoch() {
        // 1970-01-01 = day 0
        assert_eq!(days_since_epoch(1970, 1, 1), Some(0));
        // 2026-03-19 should be positive
        let d = days_since_epoch(2026, 3, 19).unwrap();
        assert!(d > 20000);
    }
}
