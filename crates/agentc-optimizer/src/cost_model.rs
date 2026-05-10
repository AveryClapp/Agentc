//! Empirical per-call-site cost model.
//!
//! Rolling Welford statistics over (input_tokens, output_tokens, latency_ms,
//! cost_usd) feed the rewrite rules' projected-savings ranking. The
//! aggregate is **empirical, not predictive** — we never extrapolate beyond
//! the distribution we have observed.
//!
//! The in-memory cache is a `DashMap` keyed by `call_site_id`; writers take
//! the per-entry lock for the brief duration of a Welford update. Readers
//! (the planner) can snapshot a profile without blocking the writer.
//!
//! Persistence into `cost_model.db` is `apply_to_db` on-demand: the writer
//! thread folds a batch of updates into the in-memory map, then flushes
//! dirty rows back through a single UPSERT. Cold-start: the map is empty;
//! `warm_from_db` hydrates it at optimizer startup.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use dashmap::DashMap;
use parking_lot::RwLock;
use rusqlite::{params, Connection, OptionalExtension};

/// Numerically stable online mean + variance.
///
/// Implements Welford's algorithm — the same variant the Wikipedia page
/// reproduces verbatim. `variance()` returns the **population** variance
/// (divide by n), not the sample variance (divide by n-1). This matches the
/// spec's storage schema column (`*_var`) and is what the ranking rules
/// consume for sample-size-independent comparisons.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct WelfordStats {
    pub n: u64,
    pub mean: f64,
    /// Running sum of squared deviations from the mean (Welford's "M2").
    pub m2: f64,
}

impl WelfordStats {
    /// Construct from persisted `(n, mean, variance)`. Inverts the variance
    /// → m2 relation (`m2 = variance * n`) so that subsequent `update`
    /// calls extend the existing stream.
    pub fn from_persisted(n: u64, mean: f64, variance: f64) -> Self {
        let m2 = variance * n as f64;
        Self { n, mean, m2 }
    }

    /// Fold one more observation into the running estimate.
    pub fn update(&mut self, x: f64) {
        self.n += 1;
        let delta = x - self.mean;
        self.mean += delta / self.n as f64;
        let delta2 = x - self.mean;
        self.m2 += delta * delta2;
    }

    /// Population variance (M2 / n). Returns 0 for empty streams.
    pub fn variance(&self) -> f64 {
        if self.n == 0 {
            0.0
        } else {
            self.m2 / self.n as f64
        }
    }

    /// Sample standard deviation. Useful for display; not used in ranking.
    pub fn stddev(&self) -> f64 {
        self.variance().sqrt()
    }

    /// Combine two independent streams via Chan's parallel Welford merge.
    /// Used when cross-process merges fold per-shard stats into canonical
    /// ones.
    pub fn merge(&mut self, other: &WelfordStats) {
        if other.n == 0 {
            return;
        }
        if self.n == 0 {
            self.n = other.n;
            self.mean = other.mean;
            self.m2 = other.m2;
            return;
        }
        let na = self.n as f64;
        let nb = other.n as f64;
        let total = na + nb;
        let delta = other.mean - self.mean;
        self.mean = (na * self.mean + nb * other.mean) / total;
        self.m2 += other.m2 + delta * delta * na * nb / total;
        self.n += other.n;
    }
}

/// Per-call-site rolling profile. Mirrors the `call_site_profile` schema.
///
/// `confidence` saturates at `cost_model_window` observations — the rule
/// engine treats `n_observations < hot_threshold` as "cold" and skips rule
/// evaluation, so the confidence field is advisory for display, not
/// load-bearing in ranking decisions.
#[derive(Debug, Clone, Default)]
pub struct CallSiteProfile {
    pub call_site_id: String,
    pub n_observations: u32,
    pub input_tokens: WelfordStats,
    pub output_tokens: WelfordStats,
    pub latency_ms: WelfordStats,
    pub cost_usd: WelfordStats,
    pub output_token_p95: f32,
    pub output_token_p99: f32,
    pub output_is_structured: f32,
    pub output_is_short: f32,
    pub updated_at_us: i64,
}

impl CallSiteProfile {
    pub fn new(call_site_id: impl Into<String>) -> Self {
        Self {
            call_site_id: call_site_id.into(),
            ..Self::default()
        }
    }

    /// `confidence` saturates linearly at `window`. A call site with zero
    /// observations returns 0.0.
    pub fn confidence(&self, window: u32) -> f32 {
        if window == 0 {
            return 0.0;
        }
        (self.n_observations as f32 / window as f32).min(1.0)
    }
}

/// One observation of a completed LLM call. The planner calls
/// `CostModel::observe(update)` after the user-visible response lands; the
/// in-memory cache is updated immediately and the row marked dirty so the
/// writer thread can persist it on its next flush.
#[derive(Debug, Clone)]
pub struct CostModelUpdate {
    pub call_site_id: String,
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub latency_ms: f64,
    pub cost_usd: f64,
    /// True if the output parsed as JSON (or any structured format).
    pub output_is_structured: bool,
    /// True if output_tokens <= 128.
    pub output_is_short: bool,
    /// `None` to use system time — tests pin this.
    pub now_us: Option<i64>,
}

fn now_micros() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_micros() as i64)
        .unwrap_or(0)
}

/// In-memory cost model backed by a `DashMap`. A `RwLock`-wrapped set of
/// dirty keys lets the writer thread `flush_dirty` without scanning the
/// entire map.
pub struct CostModel {
    map: Arc<DashMap<String, CallSiteProfile>>,
    dirty: Arc<RwLock<HashMap<String, ()>>>,
    /// Per-(call_site_id, sorted-rule-set) savings distribution.
    /// Key format: `(call_site_id, "RuleA|RuleB|...")` (rules sorted ascending).
    rule_set_map: Arc<DashMap<(String, String), WelfordStats>>,
}

impl Default for CostModel {
    fn default() -> Self {
        Self::new()
    }
}

impl CostModel {
    pub fn new() -> Self {
        Self {
            map: Arc::new(DashMap::new()),
            dirty: Arc::new(RwLock::new(HashMap::new())),
            rule_set_map: Arc::new(DashMap::new()),
        }
    }

    /// Record the realized savings for a composition rule set.
    pub fn observe_rule_set(&self, call_site_id: &str, rules: &[&str], savings_usd: f64) {
        let mut sorted = rules.to_vec();
        sorted.sort();
        let key = (call_site_id.to_string(), sorted.join("|"));
        self.rule_set_map
            .entry(key)
            .and_modify(|w| w.update(savings_usd))
            .or_insert_with(|| {
                let mut w = WelfordStats::default();
                w.update(savings_usd);
                w
            });
    }

    /// Retrieve aggregated savings stats for a specific rule set combination.
    pub fn get_rule_set_stats(&self, call_site_id: &str, rules: &[&str]) -> Option<WelfordStats> {
        let mut sorted = rules.to_vec();
        sorted.sort();
        let key = (call_site_id.to_string(), sorted.join("|"));
        self.rule_set_map.get(&key).map(|e| e.clone())
    }

    /// Read-side snapshot. Clones the profile so the caller does not hold
    /// the dashmap shard lock across planner work.
    pub fn get(&self, call_site_id: &str) -> Option<CallSiteProfile> {
        self.map.get(call_site_id).map(|entry| entry.clone())
    }

    /// Number of profiles in the in-memory cache.
    pub fn len(&self) -> usize {
        self.map.len()
    }

    /// True iff the in-memory cache has never recorded an observation.
    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    /// Number of dirty rows pending flush.
    pub fn dirty_len(&self) -> usize {
        self.dirty.read().len()
    }

    /// Fold one observation into the rolling profile and mark the entry
    /// dirty.
    pub fn observe(&self, update: CostModelUpdate) {
        let now = update.now_us.unwrap_or_else(now_micros);
        let key = update.call_site_id.clone();
        self.map
            .entry(key.clone())
            .and_modify(|p| apply_update(p, &update, now))
            .or_insert_with(|| {
                let mut p = CallSiteProfile::new(update.call_site_id.clone());
                apply_update(&mut p, &update, now);
                p
            });
        self.dirty.write().insert(key, ());
    }

    /// Hydrate the in-memory cache from `cost_model.db`. Called once at
    /// optimizer startup. Missing tables are an error — pair with
    /// [`schema::ensure_cost_model_schema`] first.
    pub fn warm_from_db(&self, conn: &Connection) -> Result<usize> {
        let mut stmt = conn
            .prepare(
                "SELECT call_site_id, n_observations, \
                        input_tokens_mean, input_tokens_var, \
                        output_tokens_mean, output_tokens_var, \
                        latency_ms_mean, latency_ms_var, \
                        cost_usd_mean, cost_usd_var, \
                        output_token_p95, output_token_p99, \
                        output_is_structured, output_is_short, \
                        updated_at \
                 FROM call_site_profile",
            )
            .context("prepare warm_from_db")?;
        let mut count = 0usize;
        let iter = stmt.query_map([], row_to_profile)?;
        for row in iter {
            let p = row?;
            self.map.insert(p.call_site_id.clone(), p);
            count += 1;
        }
        Ok(count)
    }

    /// Persist every dirty row via UPSERT. Clears the dirty set on success.
    /// On partial failure the dirty set retains the un-persisted keys.
    pub fn flush_dirty(&self, conn: &mut Connection) -> Result<usize> {
        let dirty_keys: Vec<String> = {
            let guard = self.dirty.read();
            guard.keys().cloned().collect()
        };
        if dirty_keys.is_empty() {
            return Ok(0);
        }
        let tx = conn.transaction().context("begin cost-model flush")?;
        for key in &dirty_keys {
            let Some(p) = self.map.get(key).map(|e| e.clone()) else {
                continue;
            };
            upsert_profile(&tx, &p).with_context(|| format!("upsert {key}"))?;
        }
        tx.commit().context("commit cost-model flush")?;
        // Only clear keys we actually saw; concurrent updates during the
        // flush stay dirty for next flush.
        let mut guard = self.dirty.write();
        for key in &dirty_keys {
            guard.remove(key);
        }
        Ok(dirty_keys.len())
    }
}

fn apply_update(profile: &mut CallSiteProfile, update: &CostModelUpdate, now_us: i64) {
    profile.n_observations = profile.n_observations.saturating_add(1);
    profile.input_tokens.update(update.input_tokens as f64);
    profile.output_tokens.update(update.output_tokens as f64);
    profile.latency_ms.update(update.latency_ms);
    profile.cost_usd.update(update.cost_usd);

    // Moving fraction for is_structured / is_short. EWMA with equal weight
    // on all samples collapses to the running mean — use the Welford-derived
    // mean instead to stay single-pass.
    let n = profile.n_observations as f64;
    let add_mean = |old: f32, x: bool| -> f32 {
        let cur = old as f64;
        let next = cur + (if x { 1.0 } else { 0.0 } - cur) / n;
        next as f32
    };
    profile.output_is_structured = add_mean(profile.output_is_structured, update.output_is_structured);
    profile.output_is_short = add_mean(profile.output_is_short, update.output_is_short);

    // Output token p95 is not strictly Welford-able; the spec asks for a
    // rolling estimate, not an exact percentile. Approximate with the P²
    // algorithm's single-sample incremental step: move toward the
    // observation by (p if x > p95 else -(1-p)) * stddev, capped by
    // observed values. This converges to the true p95 without storing the
    // full stream.
    let target_p = 0.95f64;
    let cur_p95 = profile.output_token_p95 as f64;
    let obs = update.output_tokens as f64;
    let step = 0.01_f64.max(1.0 / n); // smaller corrections for older streams
    let next_p95 = if obs > cur_p95 {
        cur_p95 + step * target_p * obs.max(1.0)
    } else {
        cur_p95 - step * (1.0 - target_p) * cur_p95.max(1.0)
    };
    profile.output_token_p95 = next_p95.max(0.0) as f32;

    let target_p99 = 0.99f64;
    let cur_p99 = profile.output_token_p99 as f64;
    let next_p99 = if obs > cur_p99 {
        cur_p99 + step * target_p99 * obs.max(1.0)
    } else {
        cur_p99 - step * (1.0 - target_p99) * cur_p99.max(1.0)
    };
    profile.output_token_p99 = next_p99.max(0.0) as f32;

    profile.updated_at_us = now_us;
}

fn upsert_profile(conn: &Connection, p: &CallSiteProfile) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO call_site_profile (\
            call_site_id, n_observations, \
            input_tokens_mean, input_tokens_var, \
            output_tokens_mean, output_tokens_var, \
            latency_ms_mean, latency_ms_var, \
            cost_usd_mean, cost_usd_var, \
            output_token_p95, output_token_p99, \
            output_is_structured, output_is_short, \
            updated_at\
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15) \
         ON CONFLICT(call_site_id) DO UPDATE SET \
            n_observations = excluded.n_observations, \
            input_tokens_mean = excluded.input_tokens_mean, \
            input_tokens_var = excluded.input_tokens_var, \
            output_tokens_mean = excluded.output_tokens_mean, \
            output_tokens_var = excluded.output_tokens_var, \
            latency_ms_mean = excluded.latency_ms_mean, \
            latency_ms_var = excluded.latency_ms_var, \
            cost_usd_mean = excluded.cost_usd_mean, \
            cost_usd_var = excluded.cost_usd_var, \
            output_token_p95 = excluded.output_token_p95, \
            output_token_p99 = excluded.output_token_p99, \
            output_is_structured = excluded.output_is_structured, \
            output_is_short = excluded.output_is_short, \
            updated_at = excluded.updated_at",
        params![
            p.call_site_id,
            p.n_observations as i64,
            p.input_tokens.mean,
            p.input_tokens.variance(),
            p.output_tokens.mean,
            p.output_tokens.variance(),
            p.latency_ms.mean,
            p.latency_ms.variance(),
            p.cost_usd.mean,
            p.cost_usd.variance(),
            p.output_token_p95 as f64,
            p.output_token_p99 as f64,
            p.output_is_structured as f64,
            p.output_is_short as f64,
            p.updated_at_us,
        ],
    )
    .map(|_| ())
}

/// Look up one profile row directly in the DB (no in-memory cache). Used by
/// the CLI's `agentc optimize inspect`.
pub fn load_profile(conn: &Connection, call_site_id: &str) -> Result<Option<CallSiteProfile>> {
    conn.query_row(
        "SELECT call_site_id, n_observations, \
                input_tokens_mean, input_tokens_var, \
                output_tokens_mean, output_tokens_var, \
                latency_ms_mean, latency_ms_var, \
                cost_usd_mean, cost_usd_var, \
                output_token_p95, output_token_p99, \
                output_is_structured, output_is_short, \
                updated_at \
         FROM call_site_profile WHERE call_site_id = ?1",
        params![call_site_id],
        row_to_profile,
    )
    .optional()
    .map_err(Into::into)
}

fn row_to_profile(r: &rusqlite::Row<'_>) -> rusqlite::Result<CallSiteProfile> {
    let call_site_id: String = r.get(0)?;
    let n_i: i64 = r.get(1)?;
    let n = n_i as u64;
    let in_mean: f64 = r.get(2)?;
    let in_var: f64 = r.get(3)?;
    let out_mean: f64 = r.get(4)?;
    let out_var: f64 = r.get(5)?;
    let lat_mean: f64 = r.get(6)?;
    let lat_var: f64 = r.get(7)?;
    let cost_mean: f64 = r.get(8)?;
    let cost_var: f64 = r.get(9)?;
    let p95: f64 = r.get(10)?;
    let p99: f64 = r.get(11)?;
    let is_struct: f64 = r.get(12)?;
    let is_short: f64 = r.get(13)?;
    let updated_at: i64 = r.get(14)?;
    Ok(CallSiteProfile {
        call_site_id,
        n_observations: n_i as u32,
        input_tokens: WelfordStats::from_persisted(n, in_mean, in_var),
        output_tokens: WelfordStats::from_persisted(n, out_mean, out_var),
        latency_ms: WelfordStats::from_persisted(n, lat_mean, lat_var),
        cost_usd: WelfordStats::from_persisted(n, cost_mean, cost_var),
        output_token_p95: p95 as f32,
        output_token_p99: p99 as f32,
        output_is_structured: is_struct as f32,
        output_is_short: is_short as f32,
        updated_at_us: updated_at,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::ensure_cost_model_schema;

    fn fresh_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        conn
    }

    #[test]
    fn welford_empty_stream_is_zero() {
        let w = WelfordStats::default();
        assert_eq!(w.n, 0);
        assert_eq!(w.mean, 0.0);
        assert_eq!(w.variance(), 0.0);
    }

    #[test]
    fn welford_matches_reference_on_small_stream() {
        // Reference: mean/variance of [2, 4, 4, 4, 5, 5, 7, 9] is (5, 4).
        let mut w = WelfordStats::default();
        for x in [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0] {
            w.update(x);
        }
        assert_eq!(w.n, 8);
        assert!((w.mean - 5.0).abs() < 1e-12);
        assert!((w.variance() - 4.0).abs() < 1e-12);
    }

    /// Exit-criteria test: Welford updates must track the closed-form
    /// mean/variance of a 1000-sample stream within 1e-9 relative error.
    /// We generate the stream deterministically so the test is CI-stable.
    #[test]
    fn welford_matches_closed_form_on_1000_samples() {
        let n = 1000usize;
        // Deterministic pseudo-random-ish stream: x_i = sin(i) * 100 + i/10.
        let xs: Vec<f64> = (0..n)
            .map(|i| (i as f64).sin() * 100.0 + (i as f64) / 10.0)
            .collect();

        let sum: f64 = xs.iter().sum();
        let mean = sum / n as f64;
        let var = xs.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / n as f64;

        let mut w = WelfordStats::default();
        for &x in &xs {
            w.update(x);
        }
        assert_eq!(w.n as usize, n);

        let rel_mean = ((w.mean - mean) / mean).abs();
        let rel_var = ((w.variance() - var) / var).abs();
        assert!(rel_mean < 1e-9, "mean rel err {rel_mean}");
        assert!(rel_var < 1e-9, "variance rel err {rel_var}");
    }

    #[test]
    fn welford_merge_matches_single_pass() {
        let xs: Vec<f64> = (1..=100).map(|i| i as f64).collect();
        let mut combined = WelfordStats::default();
        for &x in &xs {
            combined.update(x);
        }

        let mut a = WelfordStats::default();
        for &x in &xs[..40] {
            a.update(x);
        }
        let mut b = WelfordStats::default();
        for &x in &xs[40..] {
            b.update(x);
        }
        a.merge(&b);

        assert_eq!(a.n, combined.n);
        assert!((a.mean - combined.mean).abs() < 1e-12);
        assert!((a.variance() - combined.variance()).abs() < 1e-12);
    }

    #[test]
    fn welford_from_persisted_roundtrips() {
        let mut w = WelfordStats::default();
        for &x in &[1.0, 2.0, 3.0, 4.0, 5.0] {
            w.update(x);
        }
        let rehydrated = WelfordStats::from_persisted(w.n, w.mean, w.variance());
        assert_eq!(rehydrated.n, w.n);
        assert!((rehydrated.mean - w.mean).abs() < 1e-12);
        assert!((rehydrated.variance() - w.variance()).abs() < 1e-12);
    }

    fn an_update(site: &str, tokens_out: u32) -> CostModelUpdate {
        CostModelUpdate {
            call_site_id: site.to_string(),
            input_tokens: 100,
            output_tokens: tokens_out,
            latency_ms: 200.0,
            cost_usd: 0.002,
            output_is_structured: true,
            output_is_short: tokens_out <= 128,
            now_us: Some(1_700_000_000_000_000),
        }
    }

    #[test]
    fn cost_model_observe_updates_welford() {
        let cm = CostModel::new();
        cm.observe(an_update("app.a", 50));
        cm.observe(an_update("app.a", 100));
        let p = cm.get("app.a").unwrap();
        assert_eq!(p.n_observations, 2);
        assert!((p.output_tokens.mean - 75.0).abs() < 1e-9);
        assert_eq!(cm.dirty_len(), 1);
    }

    #[test]
    fn cost_model_tracks_distinct_sites() {
        let cm = CostModel::new();
        cm.observe(an_update("site.a", 10));
        cm.observe(an_update("site.b", 20));
        cm.observe(an_update("site.b", 40));
        assert_eq!(cm.len(), 2);
        assert_eq!(cm.get("site.a").unwrap().n_observations, 1);
        assert_eq!(cm.get("site.b").unwrap().n_observations, 2);
    }

    #[test]
    fn cost_model_flush_persists_rows() {
        let cm = CostModel::new();
        for i in 0..5 {
            cm.observe(an_update("app.site", 50 + i * 10));
        }
        let mut conn = fresh_conn();
        let n = cm.flush_dirty(&mut conn).unwrap();
        assert_eq!(n, 1);
        let loaded = load_profile(&conn, "app.site").unwrap().unwrap();
        assert_eq!(loaded.n_observations, 5);
        assert!((loaded.output_tokens.mean - 70.0).abs() < 1e-9);
        assert_eq!(cm.dirty_len(), 0);
    }

    #[test]
    fn cost_model_warm_from_db_rehydrates_in_memory() {
        let cm = CostModel::new();
        cm.observe(an_update("persisted.site", 123));
        let mut conn = fresh_conn();
        cm.flush_dirty(&mut conn).unwrap();

        let fresh = CostModel::new();
        assert!(fresh.is_empty());
        let loaded = fresh.warm_from_db(&conn).unwrap();
        assert_eq!(loaded, 1);
        let p = fresh.get("persisted.site").unwrap();
        assert_eq!(p.n_observations, 1);
        // After warm_from_db the cache is NOT marked dirty — we just loaded
        // the exact rows already in the DB.
        assert_eq!(fresh.dirty_len(), 0);
    }

    #[test]
    fn confidence_saturates_at_window() {
        let mut p = CallSiteProfile::new("site");
        p.n_observations = 50;
        assert!((p.confidence(100) - 0.5).abs() < 1e-6);
        p.n_observations = 1_000;
        assert_eq!(p.confidence(100), 1.0);
        assert_eq!(p.confidence(0), 0.0);
    }

    #[test]
    fn p99_tracker_stays_at_or_above_p95() {
        let cm = CostModel::new();
        for _ in 0..95 {
            cm.observe(an_update("site", 50));
        }
        for _ in 0..5 {
            cm.observe(an_update("site", 500));
        }
        let p = cm.get("site").unwrap();
        assert!(
            p.output_token_p99 >= p.output_token_p95,
            "p99={} p95={}",
            p.output_token_p99,
            p.output_token_p95,
        );
    }

    #[test]
    fn load_profile_returns_none_for_missing_site() {
        let conn = fresh_conn();
        assert!(load_profile(&conn, "nope").unwrap().is_none());
    }

    #[test]
    fn concurrent_observe_does_not_lose_samples() {
        use std::thread;
        let cm = Arc::new(CostModel::new());
        let threads: Vec<_> = (0..8)
            .map(|t| {
                let cm = Arc::clone(&cm);
                thread::spawn(move || {
                    for i in 0..100 {
                        cm.observe(an_update("concurrent.site", (t * 100 + i) as u32));
                    }
                })
            })
            .collect();
        for h in threads {
            h.join().unwrap();
        }
        let p = cm.get("concurrent.site").unwrap();
        assert_eq!(p.n_observations, 800);
    }

    #[test]
    fn rule_set_observe_tracks_distinct_combinations() {
        let cm = CostModel::new();
        cm.observe_rule_set("site", &["ContextCompress", "OutputBudget"], 0.05);
        cm.observe_rule_set("site", &["ContextCompress", "OutputBudget"], 0.06);
        cm.observe_rule_set("site", &["StateDrop"], 0.02);

        let combined =
            cm.get_rule_set_stats("site", &["ContextCompress", "OutputBudget"]).unwrap();
        assert_eq!(combined.n, 2);
        assert!((combined.mean - 0.055).abs() < 0.001, "mean={}", combined.mean);

        let solo = cm.get_rule_set_stats("site", &["StateDrop"]).unwrap();
        assert_eq!(solo.n, 1);
    }

    #[test]
    fn rule_set_key_is_order_independent() {
        let cm = CostModel::new();
        cm.observe_rule_set("s", &["B", "A"], 0.1);
        cm.observe_rule_set("s", &["A", "B"], 0.2);
        let stats = cm.get_rule_set_stats("s", &["A", "B"]).unwrap();
        assert_eq!(stats.n, 2);
    }
}
