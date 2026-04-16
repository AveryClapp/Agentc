//! Per-rule accuracy budget enforcement.
//!
//! Every rule declares a maximum tolerated shadow-mode divergence
//! (e.g. `ModelDowngrade = 0.03`). We keep one rolling divergence
//! estimate per `(call_site_id, rule)` pair, fed by [`crate::shadow`].
//! When the observed divergence exceeds the budget for `BREACH_STREAK`
//! consecutive samples the rule is written into `optimizer_disabled`
//! with a 24-hour cooldown; queries check the cooldown before letting
//! the rule fire again.
//!
//! Design notes:
//!
//! - The in-memory state is an ordinary `DashMap`. We don't persist
//!   per-(site, rule) divergence on every sample — that would make
//!   shadow-mode a write amplifier for cost_model.db. Persistence is a
//!   snapshot on flush (same pattern as the cost model).
//! - "Auto-disable" is a row in `optimizer_disabled`; the planner reads
//!   that row on each call via [`Budget::is_disabled`]. No background
//!   thread needs to touch state at re-enable time — we just compare
//!   `now_us` against `reenable_at`.

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{Context, Result};
use dashmap::DashMap;
use parking_lot::RwLock;
use rusqlite::{params, Connection};

use crate::cost_model::WelfordStats;

/// Number of *consecutive* over-budget samples before the rule is
/// auto-disabled. Per spec § Architecture > Accuracy budget (k = 5).
pub const BREACH_STREAK: u32 = 5;

/// Cooldown after auto-disable before the rule becomes eligible again.
/// Spec pins this at 24 hours.
pub const COOLDOWN_US: i64 = 24 * 60 * 60 * 1_000_000;

/// In-memory accuracy-budget state plus the `optimizer_disabled` row
/// cache.
///
/// One instance per optimizer process; shared via `Arc` between the
/// planner and the cost-model writer thread.
pub struct Budget {
    /// `(call_site_id, rule)` → rolling Welford + consecutive breach
    /// count. `DashMap` so the planner and the observe-writer thread
    /// can update in parallel.
    divergence: Arc<DashMap<(String, String), BudgetEntry>>,
    /// Snapshot cache of `optimizer_disabled` rows, keyed the same way.
    /// Populated at startup and on every successful disable; consulted
    /// by [`Budget::is_disabled`] without a round-trip to SQLite.
    disabled: Arc<RwLock<HashMap<(String, String), DisabledEntry>>>,
}

#[derive(Debug, Clone, Default)]
pub struct BudgetEntry {
    pub stats: WelfordStats,
    pub consecutive_breaches: u32,
}

#[derive(Debug, Clone, Copy)]
pub struct DisabledEntry {
    pub disabled_at_us: i64,
    pub reenable_at_us: i64,
}

/// Outcome of [`Budget::record_sample`]. The caller (usually the
/// writer thread handling an `observe`) uses this to decide whether to
/// emit a disable row.
#[derive(Debug, Clone, PartialEq)]
pub enum SampleOutcome {
    WithinBudget,
    Breached {
        consecutive: u32,
    },
    Disable {
        disabled_at_us: i64,
        reenable_at_us: i64,
    },
}

impl Default for Budget {
    fn default() -> Self {
        Self::new()
    }
}

impl Budget {
    pub fn new() -> Self {
        Self {
            divergence: Arc::new(DashMap::new()),
            disabled: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Warm the in-memory `disabled` cache from SQLite at startup.
    /// Unexpired rows survive across restarts.
    pub fn warm_from_db(&self, conn: &Connection) -> Result<usize> {
        let mut stmt = conn
            .prepare(
                "SELECT call_site_id, rule, disabled_at, reenable_at \
                 FROM optimizer_disabled",
            )
            .context("prepare warm_from_db")?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, i64>(3)?,
                ))
            })
            .context("query optimizer_disabled")?;
        let mut cache = self.disabled.write();
        let mut n = 0;
        for row in rows {
            let (site, rule, disabled_at, reenable_at) = row.context("decode")?;
            cache.insert(
                (site, rule),
                DisabledEntry {
                    disabled_at_us: disabled_at,
                    reenable_at_us: reenable_at,
                },
            );
            n += 1;
        }
        Ok(n)
    }

    /// Returns true iff the `(site, rule)` pair is currently disabled
    /// (i.e. a row exists and `now_us < reenable_at_us`). `now_us` is
    /// caller-supplied so tests can pin time.
    pub fn is_disabled(&self, call_site_id: &str, rule: &str, now_us: i64) -> bool {
        let guard = self.disabled.read();
        if let Some(entry) = guard.get(&(call_site_id.to_string(), rule.to_string())) {
            return now_us < entry.reenable_at_us;
        }
        false
    }

    /// Fold one shadow-mode divergence sample into the rolling state
    /// and return whether the budget is breached.
    ///
    /// Consecutive-breach logic: a within-budget sample **resets** the
    /// streak. A breach at `consecutive >= BREACH_STREAK` emits a
    /// `Disable` with `now_us + COOLDOWN_US` as the re-enable time.
    pub fn record_sample(
        &self,
        call_site_id: &str,
        rule: &str,
        divergence: f32,
        budget: f32,
        now_us: i64,
    ) -> SampleOutcome {
        let key = (call_site_id.to_string(), rule.to_string());
        let mut entry = self.divergence.entry(key.clone()).or_default();
        entry.stats.update(divergence as f64);
        let over_budget = (divergence as f64) > (budget as f64);
        if over_budget {
            entry.consecutive_breaches = entry.consecutive_breaches.saturating_add(1);
            if entry.consecutive_breaches >= BREACH_STREAK {
                let reenable_at = now_us.saturating_add(COOLDOWN_US);
                self.disabled.write().insert(
                    key,
                    DisabledEntry {
                        disabled_at_us: now_us,
                        reenable_at_us: reenable_at,
                    },
                );
                // Reset the streak so a post-cooldown re-enable starts
                // from a clean slate.
                entry.consecutive_breaches = 0;
                return SampleOutcome::Disable {
                    disabled_at_us: now_us,
                    reenable_at_us: reenable_at,
                };
            }
            return SampleOutcome::Breached {
                consecutive: entry.consecutive_breaches,
            };
        }
        entry.consecutive_breaches = 0;
        SampleOutcome::WithinBudget
    }

    /// Insert/refresh a disable row in SQLite. Called by the writer
    /// thread in response to a [`SampleOutcome::Disable`]; we do not
    /// couple [`record_sample`] to SQLite so the planner's observe path
    /// stays lock-minimal.
    pub fn persist_disable(
        &self,
        conn: &Connection,
        call_site_id: &str,
        rule: &str,
        reason: &str,
        disabled_at_us: i64,
        reenable_at_us: i64,
    ) -> Result<()> {
        conn.execute(
            "INSERT INTO optimizer_disabled \
                (call_site_id, rule, reason, disabled_at, reenable_at) \
             VALUES (?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(call_site_id, rule) DO UPDATE SET \
                reason = excluded.reason, \
                disabled_at = excluded.disabled_at, \
                reenable_at = excluded.reenable_at",
            params![call_site_id, rule, reason, disabled_at_us, reenable_at_us],
        )
        .context("insert optimizer_disabled")?;
        Ok(())
    }

    /// Explicitly re-enable a rule. Removes both the in-memory cache
    /// entry and the SQLite row. Used by the CLI's
    /// `agentc optimize disable --reenable` subcommand (bead O8) and by
    /// tests that need to simulate cooldown elapsing.
    pub fn reenable(
        &self,
        conn: Option<&Connection>,
        call_site_id: &str,
        rule: &str,
    ) -> Result<()> {
        self.disabled
            .write()
            .remove(&(call_site_id.to_string(), rule.to_string()));
        if let Some(c) = conn {
            c.execute(
                "DELETE FROM optimizer_disabled WHERE call_site_id = ?1 AND rule = ?2",
                params![call_site_id, rule],
            )
            .context("delete optimizer_disabled")?;
        }
        Ok(())
    }

    /// Peek at the current divergence estimate. Primarily for tests and
    /// the upcoming `agentc optimize inspect` CLI.
    pub fn get_entry(&self, call_site_id: &str, rule: &str) -> Option<BudgetEntry> {
        self.divergence
            .get(&(call_site_id.to_string(), rule.to_string()))
            .map(|e| e.value().clone())
    }

    /// Snapshot the cached disable row (for inspect/CLI output).
    pub fn disabled_entry(
        &self,
        call_site_id: &str,
        rule: &str,
    ) -> Option<DisabledEntry> {
        self.disabled
            .read()
            .get(&(call_site_id.to_string(), rule.to_string()))
            .copied()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::ensure_cost_model_schema;

    fn fresh_conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&c).unwrap();
        c
    }

    #[test]
    fn samples_within_budget_stay_enabled() {
        let b = Budget::new();
        for _ in 0..20 {
            let out = b.record_sample("site", "RuleA", 0.01, 0.03, 0);
            assert_eq!(out, SampleOutcome::WithinBudget);
        }
        assert!(!b.is_disabled("site", "RuleA", 0));
    }

    /// Exit-criterion: auto-disable fires at exactly k=5 consecutive
    /// over-budget samples.
    #[test]
    fn auto_disable_after_five_consecutive_breaches() {
        let b = Budget::new();
        for i in 1..=4 {
            let out = b.record_sample("site", "RuleA", 0.10, 0.03, 0);
            assert_eq!(out, SampleOutcome::Breached { consecutive: i });
        }
        let out = b.record_sample("site", "RuleA", 0.10, 0.03, 1000);
        match out {
            SampleOutcome::Disable { disabled_at_us, reenable_at_us } => {
                assert_eq!(disabled_at_us, 1000);
                assert_eq!(reenable_at_us, 1000 + COOLDOWN_US);
            }
            other => panic!("expected Disable, got {other:?}"),
        }
        assert!(b.is_disabled("site", "RuleA", 1000));
    }

    #[test]
    fn within_budget_sample_resets_streak() {
        let b = Budget::new();
        for _ in 0..4 {
            b.record_sample("site", "RuleA", 0.10, 0.03, 0);
        }
        let out = b.record_sample("site", "RuleA", 0.005, 0.03, 0);
        assert_eq!(out, SampleOutcome::WithinBudget);
        // Next breach should be "consecutive: 1", not "5".
        let out = b.record_sample("site", "RuleA", 0.10, 0.03, 0);
        assert_eq!(out, SampleOutcome::Breached { consecutive: 1 });
    }

    /// Exit-criterion: re-enable fires exactly 24h after the disable.
    #[test]
    fn reenables_exactly_after_24h_cooldown() {
        let b = Budget::new();
        for _ in 0..5 {
            b.record_sample("site", "RuleA", 0.10, 0.03, 0);
        }
        assert!(b.is_disabled("site", "RuleA", 0));
        // 1 µs before cooldown → still disabled.
        assert!(b.is_disabled("site", "RuleA", COOLDOWN_US - 1));
        // Exactly at cooldown boundary → no longer disabled.
        assert!(!b.is_disabled("site", "RuleA", COOLDOWN_US));
        assert!(!b.is_disabled("site", "RuleA", COOLDOWN_US + 3600));
    }

    #[test]
    fn persist_and_reload_via_warm_from_db() {
        let c = fresh_conn();
        let b = Budget::new();
        b.persist_disable(&c, "site", "RuleA", "breached", 1_000, 1_000 + COOLDOWN_US)
            .unwrap();
        let b2 = Budget::new();
        let n = b2.warm_from_db(&c).unwrap();
        assert_eq!(n, 1);
        assert!(b2.is_disabled("site", "RuleA", 2_000));
    }

    #[test]
    fn persist_disable_is_idempotent() {
        let c = fresh_conn();
        let b = Budget::new();
        b.persist_disable(&c, "site", "RuleA", "first", 1_000, 1_000 + COOLDOWN_US)
            .unwrap();
        // A second disable at a later ts should UPSERT, not duplicate.
        b.persist_disable(&c, "site", "RuleA", "second", 5_000, 5_000 + COOLDOWN_US)
            .unwrap();
        let count: i64 = c
            .query_row("SELECT COUNT(*) FROM optimizer_disabled", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
        let reason: String = c
            .query_row(
                "SELECT reason FROM optimizer_disabled WHERE call_site_id = 'site' AND rule = 'RuleA'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(reason, "second");
    }

    #[test]
    fn explicit_reenable_removes_row() {
        let c = fresh_conn();
        let b = Budget::new();
        b.persist_disable(&c, "site", "RuleA", "x", 1_000, 1_000 + COOLDOWN_US)
            .unwrap();
        b.warm_from_db(&c).unwrap();
        assert!(b.is_disabled("site", "RuleA", 2_000));
        b.reenable(Some(&c), "site", "RuleA").unwrap();
        assert!(!b.is_disabled("site", "RuleA", 2_000));
        let count: i64 = c
            .query_row("SELECT COUNT(*) FROM optimizer_disabled", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn rolling_welford_tracks_divergence_distribution() {
        let b = Budget::new();
        for _ in 0..10 {
            b.record_sample("site", "RuleA", 0.02, 0.05, 0);
        }
        let entry = b.get_entry("site", "RuleA").unwrap();
        assert_eq!(entry.stats.n, 10);
        assert!((entry.stats.mean - 0.02).abs() < 1e-9);
    }
}
