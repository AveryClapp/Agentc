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
//! Persistence into `cost_model.db` is caller-driven: observations update the
//! in-memory map, and lifecycle or periodic flushes write dirty profiles and
//! their retained samples in one transaction. Cold-start: the map is empty;
//! `warm_from_db` hydrates it at optimizer startup.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use dashmap::DashMap;
use parking_lot::RwLock;
use rusqlite::{params, Connection, OptionalExtension};

const DEFAULT_COST_MODEL_WINDOW: usize = 50;

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

#[derive(Debug, Clone)]
struct CostSample {
    sequence: u64,
    input_tokens: u32,
    output_tokens: u32,
    latency_ms: f64,
    cost_usd: f64,
    output_is_structured: bool,
    output_is_short: bool,
    observed_at_us: i64,
}

/// Per-call-site rolling profile. Mirrors the `call_site_profile` schema.
///
/// `confidence` saturates at `cost_model_window` observations — the rule
/// engine treats `window_observations < hot_threshold` as "cold" and skips
/// rule evaluation, so the confidence field is advisory for display, not
/// load-bearing in ranking decisions. `n_observations` remains a lifetime
/// invocation count for operator reporting; every statistic below is derived
/// only from the retained window.
#[derive(Debug, Clone, Default)]
pub struct CallSiteProfile {
    pub call_site_id: String,
    /// Lifetime observations, including samples that have aged out.
    pub n_observations: u32,
    /// Samples currently retained by `cost_model_window`.
    pub window_observations: u32,
    pub input_tokens: WelfordStats,
    pub output_tokens: WelfordStats,
    pub latency_ms: WelfordStats,
    pub cost_usd: WelfordStats,
    pub output_token_p95: f32,
    pub output_token_p99: f32,
    pub output_is_structured: f32,
    pub output_is_short: f32,
    pub updated_at_us: i64,
    samples: Arc<VecDeque<CostSample>>,
    generation: u64,
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
        (self.window_observations as f32 / window as f32).min(1.0)
    }
}

#[derive(Debug, Clone, Default)]
struct RollingStats {
    samples: VecDeque<f64>,
    stats: WelfordStats,
}

impl RollingStats {
    fn update(&mut self, value: f64, window_size: usize) {
        self.samples.push_back(value);
        while self.samples.len() > window_size {
            self.samples.pop_front();
        }
        self.stats = WelfordStats::default();
        for sample in &self.samples {
            self.stats.update(*sample);
        }
    }
}

/// One observation of a completed LLM call. The planner calls
/// `CostModel::observe(update)` after the user-visible response lands; the
/// in-memory cache is updated immediately and the row marked dirty so the
/// native runtime can persist it on its next periodic or lifecycle flush.
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

/// In-memory cost model backed by a `DashMap`. A `RwLock`-wrapped set of dirty
/// generations lets `flush_dirty` persist changed profiles without scanning
/// the entire map or clearing an update that raced with the flush.
pub struct CostModel {
    map: Arc<DashMap<String, CallSiteProfile>>,
    dirty: Arc<RwLock<HashMap<String, u64>>>,
    window_size: usize,
    /// Per-(call_site_id, sorted-rule-set) savings distribution.
    /// Key format: `(call_site_id, "RuleA|RuleB|...")` (rules sorted ascending).
    rule_set_map: Arc<DashMap<(String, String), RollingStats>>,
}

impl Default for CostModel {
    fn default() -> Self {
        Self::new()
    }
}

impl CostModel {
    pub fn new() -> Self {
        Self::with_window(DEFAULT_COST_MODEL_WINDOW as u32)
    }

    /// Construct a cost model retaining exactly the most recent `window_size`
    /// samples per call site. Zero is treated as one so malformed runtime
    /// configuration cannot create a permanently empty profile.
    pub fn with_window(window_size: u32) -> Self {
        Self {
            map: Arc::new(DashMap::new()),
            dirty: Arc::new(RwLock::new(HashMap::new())),
            window_size: window_size.max(1) as usize,
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
            .and_modify(|rolling| rolling.update(savings_usd, self.window_size))
            .or_insert_with(|| {
                let mut rolling = RollingStats::default();
                rolling.update(savings_usd, self.window_size);
                rolling
            });
    }

    /// Retrieve aggregated savings stats for a specific rule set combination.
    pub fn get_rule_set_stats(&self, call_site_id: &str, rules: &[&str]) -> Option<WelfordStats> {
        let mut sorted = rules.to_vec();
        sorted.sort();
        let key = (call_site_id.to_string(), sorted.join("|"));
        self.rule_set_map.get(&key).map(|entry| entry.stats.clone())
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
        let generation = {
            let mut profile = self
                .map
                .entry(key.clone())
                .or_insert_with(|| CallSiteProfile::new(update.call_site_id.clone()));
            apply_update(&mut profile, &update, now, self.window_size);
            profile.generation
        };
        self.dirty.write().insert(key, generation);
    }

    /// Hydrate the in-memory cache from `cost_model.db`. Called once at
    /// optimizer startup. Missing tables are an error — pair with
    /// [`schema::ensure_cost_model_schema`] first.
    pub fn warm_from_db(&self, conn: &Connection) -> Result<usize> {
        let mut stmt = conn
            .prepare(
                "SELECT call_site_id, n_observations, window_observations, \
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
        let persisted = stmt
            .query_map([], row_to_profile)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        drop(stmt);

        let mut count = 0usize;
        for mut profile in persisted {
            let persisted_window = profile.window_observations as usize;
            profile.samples = Arc::new(load_samples(conn, &profile.call_site_id)?);
            let mut truncated = false;
            while profile.samples.len() > self.window_size {
                Arc::make_mut(&mut profile.samples).pop_front();
                truncated = true;
            }
            recompute_window(&mut profile);
            let needs_rewrite =
                truncated || persisted_window != profile.window_observations as usize;
            if needs_rewrite {
                profile.generation = 1;
                self.dirty
                    .write()
                    .insert(profile.call_site_id.clone(), profile.generation);
            }
            self.map.insert(profile.call_site_id.clone(), profile);
            count += 1;
        }
        Ok(count)
    }

    /// Persist every dirty row via UPSERT. Clears the dirty set on success.
    /// On partial failure the dirty set retains the un-persisted keys.
    pub fn flush_dirty(&self, conn: &mut Connection) -> Result<usize> {
        self.flush_dirty_with_hook(conn, || {})
    }

    fn flush_dirty_with_hook<F>(&self, conn: &mut Connection, before_clear: F) -> Result<usize>
    where
        F: FnOnce(),
    {
        let dirty_generations: Vec<(String, u64)> = {
            let guard = self.dirty.read();
            guard
                .iter()
                .map(|(key, generation)| (key.clone(), *generation))
                .collect()
        };
        if dirty_generations.is_empty() {
            return Ok(0);
        }
        let snapshots: Vec<(String, u64, CallSiteProfile)> = dirty_generations
            .into_iter()
            .filter_map(|(key, generation)| {
                self.map
                    .get(&key)
                    .map(|profile| (key, generation, profile.clone()))
            })
            .collect();

        let tx = conn.transaction().context("begin cost-model flush")?;
        for (key, _, profile) in &snapshots {
            persist_profile(&tx, profile).with_context(|| format!("persist {key}"))?;
        }
        tx.commit().context("commit cost-model flush")?;

        before_clear();
        let mut guard = self.dirty.write();
        for (key, generation, _) in &snapshots {
            if guard.get(key).copied() == Some(*generation) {
                guard.remove(key);
            }
        }
        Ok(snapshots.len())
    }
}

fn apply_update(
    profile: &mut CallSiteProfile,
    update: &CostModelUpdate,
    now_us: i64,
    window_size: usize,
) {
    let samples = Arc::make_mut(&mut profile.samples);
    let sequence = samples
        .back()
        .map(|sample| sample.sequence)
        .unwrap_or(profile.n_observations as u64)
        .saturating_add(1);
    profile.n_observations = profile.n_observations.saturating_add(1);
    samples.push_back(CostSample {
        sequence,
        input_tokens: update.input_tokens,
        output_tokens: update.output_tokens,
        latency_ms: update.latency_ms,
        cost_usd: update.cost_usd,
        output_is_structured: update.output_is_structured,
        output_is_short: update.output_is_short,
        observed_at_us: now_us,
    });
    while samples.len() > window_size {
        samples.pop_front();
    }
    recompute_window(profile);
    profile.generation = profile.generation.saturating_add(1);
}

fn recompute_window(profile: &mut CallSiteProfile) {
    profile.window_observations = profile.samples.len() as u32;
    profile.input_tokens = WelfordStats::default();
    profile.output_tokens = WelfordStats::default();
    profile.latency_ms = WelfordStats::default();
    profile.cost_usd = WelfordStats::default();

    let mut output_tokens = Vec::with_capacity(profile.samples.len());
    let mut structured = 0usize;
    let mut short = 0usize;
    for sample in profile.samples.iter() {
        profile.input_tokens.update(sample.input_tokens as f64);
        profile.output_tokens.update(sample.output_tokens as f64);
        profile.latency_ms.update(sample.latency_ms);
        profile.cost_usd.update(sample.cost_usd);
        output_tokens.push(sample.output_tokens);
        structured += usize::from(sample.output_is_structured);
        short += usize::from(sample.output_is_short);
    }

    if profile.samples.is_empty() {
        profile.output_token_p95 = 0.0;
        profile.output_token_p99 = 0.0;
        profile.output_is_structured = 0.0;
        profile.output_is_short = 0.0;
        return;
    }

    output_tokens.sort_unstable();
    profile.output_token_p95 = nearest_rank(&output_tokens, 95) as f32;
    profile.output_token_p99 = nearest_rank(&output_tokens, 99) as f32;
    profile.output_is_structured = structured as f32 / profile.samples.len() as f32;
    profile.output_is_short = short as f32 / profile.samples.len() as f32;
    profile.updated_at_us = profile
        .samples
        .back()
        .map(|sample| sample.observed_at_us)
        .unwrap_or(profile.updated_at_us);
}

fn nearest_rank(sorted: &[u32], percentile: usize) -> u32 {
    debug_assert!(!sorted.is_empty());
    let rank = (percentile * sorted.len()).div_ceil(100);
    sorted[rank.saturating_sub(1).min(sorted.len() - 1)]
}

fn load_samples(conn: &Connection, call_site_id: &str) -> Result<VecDeque<CostSample>> {
    let mut stmt = conn
        .prepare(
            "SELECT sample_sequence, input_tokens, output_tokens, latency_ms, \
                    cost_usd, output_is_structured, output_is_short, observed_at \
             FROM call_site_observation \
             WHERE call_site_id = ?1 ORDER BY sample_sequence",
        )
        .context("prepare retained-sample load")?;
    let samples = stmt
        .query_map(params![call_site_id], |row| {
            Ok(CostSample {
                sequence: row.get::<_, i64>(0)? as u64,
                input_tokens: row.get::<_, i64>(1)? as u32,
                output_tokens: row.get::<_, i64>(2)? as u32,
                latency_ms: row.get(3)?,
                cost_usd: row.get(4)?,
                output_is_structured: row.get::<_, i64>(5)? != 0,
                output_is_short: row.get::<_, i64>(6)? != 0,
                observed_at_us: row.get(7)?,
            })
        })?
        .collect::<rusqlite::Result<VecDeque<_>>>()?;
    Ok(samples)
}

fn persist_profile(conn: &Connection, profile: &CallSiteProfile) -> rusqlite::Result<()> {
    conn.execute(
        "DELETE FROM call_site_observation WHERE call_site_id = ?1",
        params![profile.call_site_id],
    )?;
    {
        let mut stmt = conn.prepare(
            "INSERT INTO call_site_observation (\
                call_site_id, sample_sequence, input_tokens, output_tokens, \
                latency_ms, cost_usd, output_is_structured, output_is_short, observed_at\
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        )?;
        for sample in profile.samples.iter() {
            stmt.execute(params![
                profile.call_site_id,
                sample.sequence as i64,
                sample.input_tokens as i64,
                sample.output_tokens as i64,
                sample.latency_ms,
                sample.cost_usd,
                i64::from(sample.output_is_structured),
                i64::from(sample.output_is_short),
                sample.observed_at_us,
            ])?;
        }
    }
    upsert_profile(conn, profile)
}

fn upsert_profile(conn: &Connection, p: &CallSiteProfile) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO call_site_profile (\
            call_site_id, n_observations, window_observations, \
            input_tokens_mean, input_tokens_var, \
            output_tokens_mean, output_tokens_var, \
            latency_ms_mean, latency_ms_var, \
            cost_usd_mean, cost_usd_var, \
            output_token_p95, output_token_p99, \
            output_is_structured, output_is_short, \
            updated_at\
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16) \
         ON CONFLICT(call_site_id) DO UPDATE SET \
            n_observations = excluded.n_observations, \
            window_observations = excluded.window_observations, \
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
            p.window_observations as i64,
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
        "SELECT call_site_id, n_observations, window_observations, \
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
    let window_i: i64 = r.get(2)?;
    let window_n = window_i as u64;
    let in_mean: f64 = r.get(3)?;
    let in_var: f64 = r.get(4)?;
    let out_mean: f64 = r.get(5)?;
    let out_var: f64 = r.get(6)?;
    let lat_mean: f64 = r.get(7)?;
    let lat_var: f64 = r.get(8)?;
    let cost_mean: f64 = r.get(9)?;
    let cost_var: f64 = r.get(10)?;
    let p95: f64 = r.get(11)?;
    let p99: f64 = r.get(12)?;
    let is_struct: f64 = r.get(13)?;
    let is_short: f64 = r.get(14)?;
    let updated_at: i64 = r.get(15)?;
    Ok(CallSiteProfile {
        call_site_id,
        n_observations: n_i as u32,
        window_observations: window_i as u32,
        input_tokens: WelfordStats::from_persisted(window_n, in_mean, in_var),
        output_tokens: WelfordStats::from_persisted(window_n, out_mean, out_var),
        latency_ms: WelfordStats::from_persisted(window_n, lat_mean, lat_var),
        cost_usd: WelfordStats::from_persisted(window_n, cost_mean, cost_var),
        output_token_p95: p95 as f32,
        output_token_p99: p99 as f32,
        output_is_structured: is_struct as f32,
        output_is_short: is_short as f32,
        updated_at_us: updated_at,
        samples: Arc::new(VecDeque::new()),
        generation: 0,
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
        assert_eq!(p.window_observations, 2);
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
    fn planner_snapshots_share_samples_and_observations_use_copy_on_write() {
        let cm = CostModel::new();
        cm.observe(an_update("site", 10));
        let first = cm.get("site").unwrap();
        let second = cm.get("site").unwrap();
        assert!(Arc::ptr_eq(&first.samples, &second.samples));

        cm.observe(an_update("site", 30));
        let current = cm.get("site").unwrap();
        assert!(!Arc::ptr_eq(&first.samples, &current.samples));
        assert_eq!(first.window_observations, 1);
        assert_eq!(current.window_observations, 2);
        assert_eq!(current.output_tokens.mean, 20.0);
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
        assert_eq!(loaded.window_observations, 5);
        assert!((loaded.output_tokens.mean - 70.0).abs() < 1e-9);
        let retained: i64 = conn
            .query_row("SELECT COUNT(*) FROM call_site_observation", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(retained, 5);
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
        assert_eq!(p.window_observations, 1);
        // After warm_from_db the cache is NOT marked dirty — we just loaded
        // the exact rows already in the DB.
        assert_eq!(fresh.dirty_len(), 0);
    }

    #[test]
    fn confidence_saturates_at_window() {
        let mut p = CallSiteProfile::new("site");
        p.window_observations = 50;
        assert!((p.confidence(100) - 0.5).abs() < 1e-6);
        p.window_observations = 1_000;
        assert_eq!(p.confidence(100), 1.0);
        assert_eq!(p.confidence(0), 0.0);
    }

    #[test]
    fn quantile_trackers_do_not_overshoot_constant_stream() {
        let cm = CostModel::new();
        for _ in 0..3 {
            cm.observe(an_update("site", 80));
        }
        let p = cm.get("site").unwrap();
        assert_eq!(p.output_token_p95, 80.0);
        assert_eq!(p.output_token_p99, 80.0);
    }

    #[test]
    fn quantile_trackers_stay_within_observed_range() {
        let cm = CostModel::new();
        for tokens in [10, 20, 40, 80, 160, 320] {
            cm.observe(an_update("site", tokens));
        }
        let p = cm.get("site").unwrap();
        assert!((10.0..=320.0).contains(&p.output_token_p95));
        assert_eq!(p.output_token_p99, 320.0);
    }

    #[test]
    fn p99_ages_out_observed_outlier() {
        let cm = CostModel::new();
        for _ in 0..100 {
            cm.observe(an_update("site", 80));
        }
        cm.observe(an_update("site", 1_000));
        for _ in 0..100 {
            cm.observe(an_update("site", 80));
        }
        let p = cm.get("site").unwrap();
        assert_eq!(p.n_observations, 201);
        assert_eq!(p.window_observations, 50);
        assert_eq!(p.output_token_p99, 80.0);
        assert!(p.output_token_p95 <= p.output_token_p99);
    }

    #[test]
    fn rolling_window_recomputes_every_stat_after_distribution_shift() {
        let cm = CostModel::with_window(3);
        for _ in 0..3 {
            cm.observe(CostModelUpdate {
                call_site_id: "shift".to_string(),
                input_tokens: 1_000,
                output_tokens: 1_000,
                latency_ms: 1_000.0,
                cost_usd: 1.0,
                output_is_structured: false,
                output_is_short: false,
                now_us: Some(1),
            });
        }
        for _ in 0..3 {
            cm.observe(CostModelUpdate {
                call_site_id: "shift".to_string(),
                input_tokens: 10,
                output_tokens: 10,
                latency_ms: 10.0,
                cost_usd: 0.01,
                output_is_structured: true,
                output_is_short: true,
                now_us: Some(2),
            });
        }

        let profile = cm.get("shift").unwrap();
        assert_eq!(profile.n_observations, 6);
        assert_eq!(profile.window_observations, 3);
        assert_eq!(profile.input_tokens.mean, 10.0);
        assert_eq!(profile.output_tokens.mean, 10.0);
        assert_eq!(profile.latency_ms.mean, 10.0);
        assert_eq!(profile.cost_usd.mean, 0.01);
        assert_eq!(profile.output_tokens.variance(), 0.0);
        assert_eq!(profile.output_token_p95, 10.0);
        assert_eq!(profile.output_token_p99, 10.0);
        assert_eq!(profile.output_is_structured, 1.0);
        assert_eq!(profile.output_is_short, 1.0);
    }

    #[test]
    fn retained_window_survives_restart_and_continues_eviction() {
        let mut conn = fresh_conn();
        let cm = CostModel::with_window(3);
        for output in [10, 20, 30, 40] {
            cm.observe(an_update("persisted.window", output));
        }
        cm.flush_dirty(&mut conn).unwrap();

        let restarted = CostModel::with_window(3);
        restarted.warm_from_db(&conn).unwrap();
        let warm = restarted.get("persisted.window").unwrap();
        assert_eq!(warm.n_observations, 4);
        assert_eq!(warm.window_observations, 3);
        assert_eq!(warm.output_tokens.mean, 30.0);
        assert_eq!(warm.output_token_p99, 40.0);

        restarted.observe(an_update("persisted.window", 50));
        restarted.flush_dirty(&mut conn).unwrap();
        let after_second_restart = CostModel::with_window(3);
        after_second_restart.warm_from_db(&conn).unwrap();
        let warm = after_second_restart.get("persisted.window").unwrap();
        assert_eq!(warm.n_observations, 5);
        assert_eq!(warm.window_observations, 3);
        assert_eq!(warm.output_tokens.mean, 40.0);
        assert_eq!(warm.output_token_p95, 50.0);

        let persisted: (i64, i64, i64, i64) = conn
            .query_row(
                "SELECT COUNT(*), MIN(sample_sequence), MAX(sample_sequence), \
                        (SELECT window_observations FROM call_site_profile \
                         WHERE call_site_id = 'persisted.window') \
                 FROM call_site_observation \
                 WHERE call_site_id = 'persisted.window'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(persisted, (3, 3, 5, 3));
    }

    #[test]
    fn smaller_window_is_applied_and_persisted_on_restart() {
        let mut conn = fresh_conn();
        let original = CostModel::with_window(5);
        for output in 1..=5 {
            original.observe(an_update("resized.window", output));
        }
        original.flush_dirty(&mut conn).unwrap();

        let resized = CostModel::with_window(3);
        resized.warm_from_db(&conn).unwrap();
        let profile = resized.get("resized.window").unwrap();
        assert_eq!(profile.n_observations, 5);
        assert_eq!(profile.window_observations, 3);
        assert_eq!(profile.output_tokens.mean, 4.0);
        assert_eq!(resized.dirty_len(), 1);
        resized.flush_dirty(&mut conn).unwrap();

        let retained: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM call_site_observation \
                 WHERE call_site_id = 'resized.window'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(retained, 3);
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
        assert_eq!(p.window_observations, 50);
    }

    #[test]
    fn flush_keeps_dirty_marker_for_post_snapshot_observation() {
        let cm = CostModel::with_window(2);
        cm.observe(an_update("racing.site", 10));
        let mut conn = fresh_conn();

        cm.flush_dirty_with_hook(&mut conn, || {
            cm.observe(an_update("racing.site", 30));
        })
        .unwrap();
        assert_eq!(cm.dirty_len(), 1);
        cm.flush_dirty(&mut conn).unwrap();
        assert_eq!(cm.dirty_len(), 0);

        let restarted = CostModel::with_window(2);
        restarted.warm_from_db(&conn).unwrap();
        let profile = restarted.get("racing.site").unwrap();
        assert_eq!(profile.n_observations, 2);
        assert_eq!(profile.window_observations, 2);
        assert_eq!(profile.output_tokens.mean, 20.0);
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

    #[test]
    fn rule_set_statistics_use_the_same_bounded_window() {
        let cm = CostModel::with_window(2);
        cm.observe_rule_set("s", &["A"], 100.0);
        cm.observe_rule_set("s", &["A"], 2.0);
        cm.observe_rule_set("s", &["A"], 4.0);

        let stats = cm.get_rule_set_stats("s", &["A"]).unwrap();
        assert_eq!(stats.n, 2);
        assert_eq!(stats.mean, 3.0);
        assert_eq!(stats.variance(), 1.0);
    }
}
