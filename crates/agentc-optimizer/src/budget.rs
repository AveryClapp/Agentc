//! Per-rule accuracy budget enforcement.
//!
//! Every rule declares a maximum tolerated shadow-mode divergence
//! (e.g. `ModelDowngrade = 0.03`). We keep one cumulative divergence
//! estimate per `(call_site_id, rule)` pair, fed by [`crate::shadow`].
//! When the observed divergence exceeds the budget for `BREACH_STREAK`
//! consecutive samples the rule is written into `optimizer_disabled`
//! with a 24-hour cooldown; queries check the cooldown before letting
//! the rule fire again.
//!
//! Design notes:
//!
//! - The in-memory state is an ordinary `DashMap`. Samples mark one
//!   `(site, rule)` entry dirty; [`Budget::flush_divergence`] snapshots dirty
//!   generations into `cost_model.db` without clearing a concurrent update.
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
/// One instance per optimizer process; shared via `Arc` between planning,
/// shadow-result recording, and lifecycle persistence paths.
pub struct Budget {
    /// `(call_site_id, rule)` → cumulative Welford + consecutive breach
    /// count. `DashMap` permits independent site/rule pairs to update in
    /// parallel.
    divergence: Arc<DashMap<(String, String), BudgetEntry>>,
    /// Dirty generation per divergence entry. A flush only clears the exact
    /// generation it persisted, so a concurrent sample remains pending.
    dirty: Arc<RwLock<HashMap<(String, String), u64>>>,
    /// Snapshot cache of `optimizer_disabled` rows, keyed the same way.
    /// Populated at startup and on every successful disable; consulted
    /// by [`Budget::is_disabled`] without a round-trip to SQLite.
    disabled: Arc<RwLock<HashMap<(String, String), DisabledEntry>>>,
}

#[derive(Debug, Clone, Default)]
pub struct BudgetEntry {
    pub stats: WelfordStats,
    pub consecutive_breaches: u32,
    generation: u64,
}

#[derive(Debug, Clone, Copy)]
pub struct DisabledEntry {
    pub disabled_at_us: i64,
    pub reenable_at_us: i64,
}

/// Outcome of [`Budget::record_sample`]. The native boundary uses this to
/// decide whether to persist a disable row.
#[derive(Debug, Clone, PartialEq)]
pub enum SampleOutcome {
    RejectedInvalidInput,
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
            dirty: Arc::new(RwLock::new(HashMap::new())),
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

    /// Warm per-`(call_site_id, rule)` divergence and breach-streak state.
    /// Call once at startup after [`crate::schema::ensure_cost_model_schema`].
    pub fn warm_divergence_from_db(&self, conn: &Connection) -> Result<usize> {
        let mut stmt = conn
            .prepare(
                "SELECT call_site_id, rule, n_samples, divergence_mean, \
                        divergence_var, consecutive_breaches \
                 FROM rule_divergence",
            )
            .context("prepare divergence warmup")?;
        let rows = stmt
            .query_map([], |row| {
                let n_samples = row.get::<_, i64>(2)? as u64;
                Ok((
                    (row.get::<_, String>(0)?, row.get::<_, String>(1)?),
                    BudgetEntry {
                        stats: WelfordStats::from_persisted(
                            n_samples,
                            row.get(3)?,
                            row.get(4)?,
                        ),
                        consecutive_breaches: row.get::<_, i64>(5)? as u32,
                        generation: 0,
                    },
                ))
            })
            .context("query rule_divergence")?;

        let mut count = 0;
        for row in rows {
            let (key, entry) = row.context("decode rule_divergence")?;
            self.divergence.insert(key, entry);
            count += 1;
        }
        Ok(count)
    }

    /// Returns true iff the `(site, rule)` pair is currently disabled
    /// (i.e. a row exists and `now_us < reenable_at_us`). `now_us` is
    /// caller-supplied so tests can pin time.
    ///
    /// A wildcard row with `call_site_id == "*"` disables the rule for
    /// every site — the operator-override path used by ablation sweeps
    /// where no `call_site_profile` rows exist yet.
    pub fn is_disabled(&self, call_site_id: &str, rule: &str, now_us: i64) -> bool {
        let guard = self.disabled.read();
        if let Some(entry) = guard.get(&(call_site_id.to_string(), rule.to_string())) {
            if now_us < entry.reenable_at_us {
                return true;
            }
        }
        if let Some(entry) = guard.get(&("*".to_string(), rule.to_string())) {
            if now_us < entry.reenable_at_us {
                return true;
            }
        }
        false
    }

    /// Fold one shadow-mode divergence sample into the cumulative state
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
        if !is_unit_fraction(divergence) || !is_unit_fraction(budget) {
            return SampleOutcome::RejectedInvalidInput;
        }

        let key = (call_site_id.to_string(), rule.to_string());
        let (outcome, disabled, generation) = {
            let mut entry = self.divergence.entry(key.clone()).or_default();
            entry.stats.update(divergence as f64);
            entry.generation = entry.generation.saturating_add(1);

            if (divergence as f64) > (budget as f64) {
                entry.consecutive_breaches = entry.consecutive_breaches.saturating_add(1);
                if entry.consecutive_breaches >= BREACH_STREAK {
                    let reenable_at_us = now_us.saturating_add(COOLDOWN_US);
                    // A post-cooldown re-enable starts from a clean streak.
                    entry.consecutive_breaches = 0;
                    (
                        SampleOutcome::Disable {
                            disabled_at_us: now_us,
                            reenable_at_us,
                        },
                        Some(DisabledEntry {
                            disabled_at_us: now_us,
                            reenable_at_us,
                        }),
                        entry.generation,
                    )
                } else {
                    (
                        SampleOutcome::Breached {
                            consecutive: entry.consecutive_breaches,
                        },
                        None,
                        entry.generation,
                    )
                }
            } else {
                entry.consecutive_breaches = 0;
                (SampleOutcome::WithinBudget, None, entry.generation)
            }
        };

        if let Some(disabled_entry) = disabled {
            self.disabled.write().insert(key.clone(), disabled_entry);
        }
        self.dirty.write().insert(key, generation);
        outcome
    }

    /// Persist every dirty divergence row in one transaction.
    ///
    /// Only the generation represented by the committed snapshot is cleared;
    /// a sample arriving during the flush remains dirty for the next flush.
    pub fn flush_divergence(&self, conn: &mut Connection) -> Result<usize> {
        self.flush_divergence_with_hook(conn, || {})
    }

    fn flush_divergence_with_hook<F>(&self, conn: &mut Connection, before_clear: F) -> Result<usize>
    where
        F: FnOnce(),
    {
        let dirty_generations: Vec<((String, String), u64)> = self
            .dirty
            .read()
            .iter()
            .map(|(key, generation)| (key.clone(), *generation))
            .collect();
        if dirty_generations.is_empty() {
            return Ok(0);
        }

        let snapshots: Vec<((String, String), u64, BudgetEntry)> = dirty_generations
            .into_iter()
            .filter_map(|(key, generation)| {
                self.divergence
                    .get(&key)
                    .map(|entry| (key, generation, entry.clone()))
            })
            .collect();
        let transaction = conn
            .transaction()
            .context("begin divergence-state flush")?;
        for ((call_site_id, rule), _, entry) in &snapshots {
            transaction
                .execute(
                    "INSERT INTO rule_divergence (\
                        call_site_id, rule, n_samples, divergence_mean, \
                        divergence_var, consecutive_breaches\
                     ) VALUES (?1, ?2, ?3, ?4, ?5, ?6) \
                     ON CONFLICT(call_site_id, rule) DO UPDATE SET \
                        n_samples = excluded.n_samples, \
                        divergence_mean = excluded.divergence_mean, \
                        divergence_var = excluded.divergence_var, \
                        consecutive_breaches = excluded.consecutive_breaches",
                    params![
                        call_site_id,
                        rule,
                        entry.stats.n as i64,
                        entry.stats.mean,
                        entry.stats.variance(),
                        entry.consecutive_breaches as i64,
                    ],
                )
                .with_context(|| format!("persist divergence for {call_site_id}/{rule}"))?;
        }
        transaction
            .commit()
            .context("commit divergence-state flush")?;

        before_clear();
        let mut dirty = self.dirty.write();
        for (key, generation, _) in &snapshots {
            if dirty.get(key).copied() == Some(*generation) {
                dirty.remove(key);
            }
        }
        Ok(snapshots.len())
    }

    /// Insert/refresh a disable row in SQLite. Called by the native boundary
    /// in response to a [`SampleOutcome::Disable`]; we do not couple
    /// [`record_sample`] to SQLite so direct users can control persistence.
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

    /// Number of divergence entries awaiting persistence.
    pub fn dirty_len(&self) -> usize {
        self.dirty.read().len()
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

fn is_unit_fraction(value: f32) -> bool {
    value.is_finite() && (0.0..=1.0).contains(&value)
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
    fn wildcard_disabled_row_matches_every_site() {
        let b = Budget::new();
        let conn = fresh_conn();
        let now = 1_000_i64;
        let until = 9_000_i64;
        conn.execute(
            "INSERT INTO optimizer_disabled \
                (call_site_id, rule, reason, disabled_at, reenable_at) \
             VALUES ('*', 'RuleA', 'ablation', ?1, ?2)",
            params![now, until],
        )
        .unwrap();
        b.warm_from_db(&conn).unwrap();

        assert!(b.is_disabled("never-seen-site", "RuleA", now + 1));
        assert!(b.is_disabled("another-site", "RuleA", now + 1));
        assert!(!b.is_disabled("any-site", "RuleB", now + 1));
        assert!(!b.is_disabled("any-site", "RuleA", until + 1));
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

    #[test]
    fn invalid_samples_and_budgets_do_not_mutate_state() {
        let b = Budget::new();
        let invalid = [
            f32::NAN,
            f32::INFINITY,
            f32::NEG_INFINITY,
            -f32::EPSILON,
            1.0 + f32::EPSILON,
        ];

        for divergence in invalid {
            assert_eq!(
                b.record_sample("site", "RuleA", divergence, 0.1, 0),
                SampleOutcome::RejectedInvalidInput
            );
        }
        for budget in invalid {
            assert_eq!(
                b.record_sample("site", "RuleA", 0.5, budget, 0),
                SampleOutcome::RejectedInvalidInput
            );
        }

        assert!(b.get_entry("site", "RuleA").is_none());
        assert_eq!(b.dirty_len(), 0);
        assert_eq!(
            b.record_sample("site", "RuleA", 0.5, 0.1, 0),
            SampleOutcome::Breached { consecutive: 1 }
        );

        let before = b.get_entry("site", "RuleA").unwrap();
        for divergence in invalid {
            assert_eq!(
                b.record_sample("site", "RuleA", divergence, 0.1, 1),
                SampleOutcome::RejectedInvalidInput
            );
        }
        for budget in invalid {
            assert_eq!(
                b.record_sample("site", "RuleA", 0.5, budget, 1),
                SampleOutcome::RejectedInvalidInput
            );
        }
        let after = b.get_entry("site", "RuleA").unwrap();
        assert_eq!(after.stats.n, before.stats.n);
        assert_eq!(after.stats.mean.to_bits(), before.stats.mean.to_bits());
        assert_eq!(after.stats.m2.to_bits(), before.stats.m2.to_bits());
        assert_eq!(after.consecutive_breaches, before.consecutive_breaches);
        assert_eq!(after.generation, before.generation);
        assert_eq!(b.dirty_len(), 1);
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
    fn cumulative_welford_tracks_divergence_distribution() {
        let b = Budget::new();
        for _ in 0..10 {
            b.record_sample("site", "RuleA", 0.02, 0.05, 0);
        }
        let entry = b.get_entry("site", "RuleA").unwrap();
        assert_eq!(entry.stats.n, 10);
        assert!((entry.stats.mean - 0.02).abs() < 1e-9);
    }

    #[test]
    fn divergence_and_breach_streak_survive_restart() {
        let mut connection = fresh_conn();
        let first = Budget::new();
        for expected in 1..=4 {
            assert_eq!(
                first.record_sample("site", "RuleA", 0.10, 0.03, expected),
                SampleOutcome::Breached {
                    consecutive: expected as u32,
                }
            );
        }
        assert_eq!(first.dirty_len(), 1);
        assert_eq!(first.flush_divergence(&mut connection).unwrap(), 1);
        assert_eq!(first.dirty_len(), 0);

        let restarted = Budget::new();
        assert_eq!(restarted.warm_divergence_from_db(&connection).unwrap(), 1);
        let warm = restarted.get_entry("site", "RuleA").unwrap();
        assert_eq!(warm.stats.n, 4);
        assert!((warm.stats.mean - 0.10).abs() < 1e-7);
        assert_eq!(warm.consecutive_breaches, 4);
        assert_eq!(restarted.dirty_len(), 0);

        let outcome = restarted.record_sample("site", "RuleA", 0.10, 0.03, 5);
        assert_eq!(
            outcome,
            SampleOutcome::Disable {
                disabled_at_us: 5,
                reenable_at_us: 5 + COOLDOWN_US,
            }
        );
        assert!(restarted.is_disabled("site", "RuleA", 5));
        restarted.flush_divergence(&mut connection).unwrap();

        let persisted: (i64, f64, i64) = connection
            .query_row(
                "SELECT n_samples, divergence_mean, consecutive_breaches \
                 FROM rule_divergence WHERE call_site_id = 'site' AND rule = 'RuleA'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(persisted.0, 5);
        assert!((persisted.1 - 0.10).abs() < 1e-7);
        assert_eq!(persisted.2, 0);
    }

    #[test]
    fn within_budget_sample_resets_restarted_streak() {
        let mut connection = fresh_conn();
        let first = Budget::new();
        for _ in 0..4 {
            first.record_sample("site", "RuleA", 0.10, 0.03, 0);
        }
        first.flush_divergence(&mut connection).unwrap();

        let restarted = Budget::new();
        restarted.warm_divergence_from_db(&connection).unwrap();
        assert_eq!(
            restarted.record_sample("site", "RuleA", 0.01, 0.03, 0),
            SampleOutcome::WithinBudget
        );
        restarted.flush_divergence(&mut connection).unwrap();

        let second_restart = Budget::new();
        second_restart
            .warm_divergence_from_db(&connection)
            .unwrap();
        assert_eq!(
            second_restart
                .get_entry("site", "RuleA")
                .unwrap()
                .consecutive_breaches,
            0
        );
        assert_eq!(
            second_restart.record_sample("site", "RuleA", 0.10, 0.03, 0),
            SampleOutcome::Breached { consecutive: 1 }
        );
    }

    #[test]
    fn flush_keeps_post_snapshot_sample_dirty() {
        let mut connection = fresh_conn();
        let budget = Budget::new();
        budget.record_sample("site", "RuleA", 0.10, 0.03, 1);

        budget
            .flush_divergence_with_hook(&mut connection, || {
                budget.record_sample("site", "RuleA", 0.20, 0.03, 2);
            })
            .unwrap();
        assert_eq!(budget.dirty_len(), 1);
        budget.flush_divergence(&mut connection).unwrap();
        assert_eq!(budget.dirty_len(), 0);

        let restarted = Budget::new();
        restarted.warm_divergence_from_db(&connection).unwrap();
        let entry = restarted.get_entry("site", "RuleA").unwrap();
        assert_eq!(entry.stats.n, 2);
        assert!((entry.stats.mean - 0.15).abs() < 1e-7);
        assert_eq!(entry.consecutive_breaches, 2);
    }
}
