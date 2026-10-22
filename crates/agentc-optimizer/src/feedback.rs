//! Commit complete-plan feedback profiles and exposure guards together.

use anyhow::{Context, Result};
use rusqlite::Connection;

use crate::{plan_guard::PlanGuard, plan_profile::PlanProfiles};

/// Persist all pending complete-plan evidence and guards in one transaction.
/// Neither store clears dirty generations until the shared commit succeeds.
/// Updates arriving after the snapshots remain dirty for a later flush.
pub fn flush(conn: &mut Connection, profiles: &PlanProfiles, guard: &PlanGuard) -> Result<()> {
    flush_with_hook(conn, profiles, guard, || {})
}

fn flush_with_hook(
    conn: &mut Connection,
    profiles: &PlanProfiles,
    guard: &PlanGuard,
    before_acknowledge: impl FnOnce(),
) -> Result<()> {
    // Profiles are updated before their exposure samples. Snapshot guards
    // first so a concurrent new guard cannot outrun its profile snapshot.
    let guard_snapshot = guard.snapshot_dirty();
    let profiles_snapshot = profiles.snapshot_dirty();
    if profiles_snapshot.is_empty() && guard_snapshot.is_empty() {
        return Ok(());
    }
    let transaction = conn
        .transaction()
        .context("begin complete-plan feedback flush")?;
    profiles_snapshot.persist(&transaction)?;
    guard_snapshot.persist(&transaction)?;
    transaction
        .commit()
        .context("commit complete-plan feedback flush")?;
    before_acknowledge();
    profiles_snapshot.acknowledge(profiles);
    guard_snapshot.acknowledge(guard);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::execution_plan::ExecutionPlanId;
    use crate::plan_profile::{
        CallSiteVersion, PlanProfileKey, PlanProfileUpdate, PlanRuntimeVersion,
    };

    fn key() -> PlanProfileKey {
        PlanProfileKey {
            call_site_version: CallSiteVersion::parse("a".repeat(64)).unwrap(),
            execution_plan_id: ExecutionPlanId::parse("b".repeat(64)).unwrap(),
        }
    }

    fn runtime(version: &str) -> PlanRuntimeVersion {
        PlanRuntimeVersion {
            provider_protocol: "openai.chat.completions.v1".into(),
            target_model_id: "alias".into(),
            target_model_version: version.into(),
            price_table_version: "prices-v1".into(),
        }
    }

    fn fixture() -> (Connection, PlanProfiles, PlanGuard) {
        let connection = Connection::open_in_memory().unwrap();
        crate::schema::ensure_cost_model_schema(&connection).unwrap();
        (
            connection,
            PlanProfiles::with_window(2),
            PlanGuard::with_limits(0.3, i64::MAX, i64::MAX).unwrap(),
        )
    }

    fn record(profiles: &PlanProfiles, guard: &PlanGuard, number: i64) {
        record_version(profiles, guard, number, "revision-v1");
    }

    fn record_version(profiles: &PlanProfiles, guard: &PlanGuard, number: i64, version: &str) {
        let divergence = if number == 1 { 0.2 } else { 0.4 };
        let observation = profiles
            .observe_provider_outcome(
                PlanProfileUpdate {
                    cost_evidence: Default::default(),
                    key: key(),
                    runtime_version: runtime(version),
                    input_tokens: 20,
                    output_tokens: 10,
                    latency_ms: 100.,
                    cost_usd: 0.01,
                    output_is_structured: false,
                    output_is_short: false,
                    divergence: Some(divergence),
                    dispatch_fallback: false,
                    observed_at_us: Some(number),
                },
                &runtime("alias@catalog-v1"),
            )
            .unwrap();
        guard
            .record_sample(&observation, divergence, 0.1, Some(number))
            .unwrap();
    }

    fn unpaired(profiles: &PlanProfiles, number: i64) {
        profiles
            .observe(PlanProfileUpdate {
                cost_evidence: Default::default(),
                key: key(),
                runtime_version: runtime("revision-v1"),
                input_tokens: 20,
                output_tokens: 10,
                latency_ms: 100.,
                cost_usd: 0.01,
                output_is_structured: false,
                output_is_short: false,
                divergence: None,
                dispatch_fallback: false,
                observed_at_us: Some(number),
            })
            .unwrap();
    }

    const RETAINED_TABLES: [&str; 3] = [
        "execution_plan_observation",
        "execution_plan_divergence_observation",
        "execution_plan_guard_observation",
    ];

    fn watch_rows(conn: &Connection) {
        conn.execute_batch("CREATE TABLE feedback_writes(tab TEXT,op TEXT)")
            .unwrap();
        for table in RETAINED_TABLES {
            for operation in ["INSERT", "UPDATE", "DELETE"] {
                conn.execute_batch(&format!("CREATE TRIGGER watch_{table}_{operation} AFTER {operation} ON {table} BEGIN INSERT INTO feedback_writes VALUES('{table}','{operation}'); END;")).unwrap();
            }
        }
    }

    fn writes(conn: &Connection, table: &str, operation: &str) -> u64 {
        conn.query_row(
            "SELECT COUNT(*) FROM feedback_writes WHERE tab=?1 AND op=?2",
            [table, operation],
            |r| r.get(0),
        )
        .unwrap()
    }

    fn sequences(conn: &Connection, table: &str, column: &str) -> Vec<u64> {
        conn.prepare(&format!("SELECT {column} FROM {table} ORDER BY {column}"))
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap()
    }

    fn paired(conn: &Connection) -> u64 {
        conn.query_row(
            "SELECT n_paired_observations FROM execution_plan_profile",
            [],
            |r| r.get(0),
        )
        .unwrap()
    }

    fn assert_empty(conn: &Connection) {
        for table in [
            "execution_plan_profile",
            "execution_plan_observation",
            "execution_plan_divergence_observation",
            "execution_plan_guard",
            "execution_plan_guard_observation",
            "execution_plan_disabled",
            "execution_plan_model_alias",
        ] {
            let count: u64 = conn
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))
                .unwrap();
            assert_eq!(count, 0, "{table}");
        }
    }

    #[test]
    fn joint_commit_retains_exact_profiles_aliases_exposure_and_disable_after_restart() {
        let (mut conn, profiles, guard) = fixture();
        record(&profiles, &guard, 1);
        record(&profiles, &guard, 2);
        flush(&mut conn, &profiles, &guard).unwrap();
        assert_eq!(profiles.dirty_len(), 0);
        assert_eq!(guard.dirty_len(), 0);
        assert_eq!(paired(&conn), 2);
        let restarted = PlanProfiles::with_window(2);
        restarted.warm_from_db(&conn).unwrap();
        let profile = restarted
            .get_provider_alias(&key(), &runtime("alias@catalog-v1"))
            .unwrap();
        assert_eq!(profile.runtime_version, runtime("revision-v1"));
        assert_eq!(profile.n_paired_observations, 2);
        let restarted_guard = PlanGuard::with_limits(0.3, i64::MAX, i64::MAX).unwrap();
        restarted_guard.warm_from_db(&conn).unwrap();
        assert!(restarted_guard.decision(&key(), 3).blocks_user_visible());
        assert!((restarted_guard.get(&key()).unwrap().divergence_exposure - 0.4).abs() < 1e-12);
    }

    #[test]
    fn guard_write_failure_rolls_back_profiles_and_aliases_without_losing_dirty_state() {
        let (mut conn, profiles, guard) = fixture();
        record(&profiles, &guard, 1);
        conn.execute_batch("CREATE TRIGGER reject_guard BEFORE INSERT ON execution_plan_guard BEGIN SELECT RAISE(ABORT,'injected guard failure'); END;").unwrap();
        assert!(flush(&mut conn, &profiles, &guard).is_err());
        assert_empty(&conn);
        assert_eq!(profiles.dirty_len(), 1);
        assert_eq!(guard.dirty_len(), 1);
        conn.execute_batch("DROP TRIGGER reject_guard").unwrap();
        flush(&mut conn, &profiles, &guard).unwrap();
        assert_eq!(paired(&conn), 1);
        assert_eq!(profiles.dirty_len(), 0);
        assert_eq!(guard.dirty_len(), 0);
        let restarted = PlanProfiles::with_window(2);
        restarted.warm_from_db(&conn).unwrap();
        assert!(restarted
            .get_provider_alias(&key(), &runtime("alias@catalog-v1"))
            .is_some());
    }

    #[test]
    fn commit_failure_keeps_both_stores_dirty_and_retries_the_complete_transaction() {
        let (mut conn, profiles, guard) = fixture();
        conn.execute_batch("PRAGMA foreign_keys=ON; CREATE TABLE feedback_parent(id INTEGER PRIMARY KEY); CREATE TABLE feedback_child(parent INTEGER REFERENCES feedback_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER reject_commit AFTER INSERT ON execution_plan_guard BEGIN INSERT INTO feedback_child VALUES(1); END;").unwrap();
        record(&profiles, &guard, 1);
        assert!(flush(&mut conn, &profiles, &guard).is_err());
        assert!(conn.is_autocommit());
        assert_empty(&conn);
        assert_eq!((profiles.dirty_len(), guard.dirty_len()), (1, 1));
        conn.execute_batch("DROP TRIGGER reject_commit").unwrap();
        flush(&mut conn, &profiles, &guard).unwrap();
        assert_eq!(paired(&conn), 1);
        assert_eq!((profiles.dirty_len(), guard.dirty_len()), (0, 0));
    }

    #[test]
    fn updates_after_commit_remain_dirty_and_preserve_the_new_disable() {
        let (mut conn, profiles, guard) = fixture();
        record(&profiles, &guard, 1);
        flush_with_hook(&mut conn, &profiles, &guard, || {
            record(&profiles, &guard, 2)
        })
        .unwrap();
        assert_eq!(paired(&conn), 1);
        assert_eq!((profiles.dirty_len(), guard.dirty_len()), (1, 1));
        flush(&mut conn, &profiles, &guard).unwrap();
        assert_eq!(paired(&conn), 2);
        let restarted_guard = PlanGuard::with_limits(0.3, i64::MAX, i64::MAX).unwrap();
        restarted_guard.warm_from_db(&conn).unwrap();
        assert!(restarted_guard.decision(&key(), 3).blocks_user_visible());
    }

    #[test]
    fn empty_flush_does_not_open_a_transaction() {
        let (mut conn, profiles, guard) = fixture();
        conn.execute_batch("BEGIN").unwrap();
        flush(&mut conn, &profiles, &guard).unwrap();
        assert!(!conn.is_autocommit());
        conn.execute_batch("ROLLBACK").unwrap();
    }

    #[test]
    fn new_pair_writes_only_new_retained_rows_and_unpaired_work_preserves_prior_safety() {
        let (mut conn, profiles, guard) = fixture();
        record(&profiles, &guard, 1);
        flush(&mut conn, &profiles, &guard).unwrap();
        watch_rows(&conn);
        record(&profiles, &guard, 2);
        flush(&mut conn, &profiles, &guard).unwrap();
        for table in RETAINED_TABLES {
            assert_eq!(writes(&conn, table, "INSERT"), 1, "{table}");
            assert_eq!(writes(&conn, table, "UPDATE"), 0, "{table}");
            assert_eq!(writes(&conn, table, "DELETE"), 0, "{table}");
        }
        conn.execute("DELETE FROM feedback_writes", []).unwrap();
        unpaired(&profiles, 3);
        flush(&mut conn, &profiles, &guard).unwrap();
        assert_eq!(writes(&conn, RETAINED_TABLES[0], "INSERT"), 1);
        assert_eq!(writes(&conn, RETAINED_TABLES[0], "DELETE"), 1);
        assert_eq!(writes(&conn, RETAINED_TABLES[0], "UPDATE"), 0);
        for table in &RETAINED_TABLES[1..] {
            for operation in ["INSERT", "UPDATE", "DELETE"] {
                assert_eq!(writes(&conn, table, operation), 0);
            }
        }
        assert_eq!(
            sequences(&conn, RETAINED_TABLES[1], "plan_observation_sequence"),
            [1, 2]
        );
        assert_eq!(
            sequences(&conn, RETAINED_TABLES[2], "plan_observation_sequence"),
            [1, 2]
        );
    }

    #[test]
    fn retained_windows_evict_only_expired_outcomes_and_pairs_while_exposure_remains() {
        let (mut conn, profiles, guard) = fixture();
        record(&profiles, &guard, 1);
        record(&profiles, &guard, 2);
        flush(&mut conn, &profiles, &guard).unwrap();
        watch_rows(&conn);
        record(&profiles, &guard, 3);
        flush(&mut conn, &profiles, &guard).unwrap();
        for table in &RETAINED_TABLES[..2] {
            assert_eq!(writes(&conn, table, "INSERT"), 1);
            assert_eq!(writes(&conn, table, "DELETE"), 1);
            assert_eq!(writes(&conn, table, "UPDATE"), 0);
            assert_eq!(sequences(&conn, table, "sample_sequence"), [2, 3]);
        }
        assert_eq!(
            sequences(&conn, RETAINED_TABLES[2], "plan_observation_sequence"),
            [1, 2, 3]
        );
        assert_eq!(writes(&conn, RETAINED_TABLES[2], "INSERT"), 1);
        assert_eq!(writes(&conn, RETAINED_TABLES[2], "DELETE"), 0);
    }

    #[test]
    fn changed_retained_rows_are_repaired_from_the_current_snapshot() {
        let (mut conn, profiles, guard) = fixture();
        record(&profiles, &guard, 1);
        flush(&mut conn, &profiles, &guard).unwrap();
        conn.execute_batch("UPDATE execution_plan_observation SET output_tokens=999,cost_evidence_json='invalid'; UPDATE execution_plan_divergence_observation SET divergence=.9; UPDATE execution_plan_guard_observation SET divergence=.9,excess=.8;").unwrap();
        watch_rows(&conn);
        record(&profiles, &guard, 2);
        flush(&mut conn, &profiles, &guard).unwrap();
        for table in RETAINED_TABLES {
            assert_eq!(writes(&conn, table, "INSERT"), 1);
            assert_eq!(writes(&conn, table, "UPDATE"), 1);
            assert_eq!(writes(&conn, table, "DELETE"), 0);
        }
        let restarted = PlanProfiles::with_window(2);
        restarted.warm_from_db(&conn).unwrap();
        assert_eq!(
            restarted
                .get_for_reporting(&key())
                .unwrap()
                .retained_samples(),
            profiles
                .get_for_reporting(&key())
                .unwrap()
                .retained_samples()
        );
        let restarted_guard = PlanGuard::with_limits(0.3, i64::MAX, i64::MAX).unwrap();
        restarted_guard.warm_from_db(&conn).unwrap();
        assert!((restarted_guard.get(&key()).unwrap().divergence_exposure - 0.4).abs() < 1e-12);
    }

    #[test]
    fn exact_sets_remove_unexpected_guard_rows_inside_the_retained_sequence_bounds() {
        let (mut conn, profiles, guard) = fixture();
        record(&profiles, &guard, 1);
        unpaired(&profiles, 2);
        record(&profiles, &guard, 3);
        flush(&mut conn, &profiles, &guard).unwrap();
        conn.execute_batch("INSERT INTO execution_plan_guard_observation SELECT call_site_version,execution_plan_id,2,divergence,excess,provider_protocol,target_model_id,target_model_version,price_table_version,observed_at FROM execution_plan_guard_observation WHERE plan_observation_sequence=1;").unwrap();
        watch_rows(&conn);
        record(&profiles, &guard, 4);
        flush(&mut conn, &profiles, &guard).unwrap();
        assert_eq!(
            sequences(&conn, RETAINED_TABLES[2], "plan_observation_sequence"),
            [1, 3, 4]
        );
        assert_eq!(writes(&conn, RETAINED_TABLES[2], "DELETE"), 1);
        assert_eq!(writes(&conn, RETAINED_TABLES[2], "UPDATE"), 0);
    }

    #[test]
    fn missing_retained_rows_are_restored_without_rewriting_surviving_rows() {
        let (mut conn, profiles, guard) = fixture();
        record(&profiles, &guard, 1);
        flush(&mut conn, &profiles, &guard).unwrap();
        conn.execute("DELETE FROM execution_plan_divergence_observation", [])
            .unwrap();
        watch_rows(&conn);
        record(&profiles, &guard, 2);
        flush(&mut conn, &profiles, &guard).unwrap();
        assert_eq!(writes(&conn, RETAINED_TABLES[0], "INSERT"), 1);
        assert_eq!(writes(&conn, RETAINED_TABLES[1], "INSERT"), 2);
        for table in RETAINED_TABLES {
            assert_eq!(writes(&conn, table, "UPDATE"), 0);
            assert_eq!(writes(&conn, table, "DELETE"), 0);
        }
        assert_eq!(
            sequences(&conn, RETAINED_TABLES[1], "sample_sequence"),
            [1, 2]
        );
    }

    #[test]
    fn runtime_drift_retires_old_rows_and_empty_readmission_clears_both_profile_windows() {
        let (mut conn, profiles, guard) = fixture();
        record(&profiles, &guard, 1);
        record(&profiles, &guard, 2);
        flush(&mut conn, &profiles, &guard).unwrap();
        record_version(&profiles, &guard, 3, "revision-v2");
        flush(&mut conn, &profiles, &guard).unwrap();
        for table in RETAINED_TABLES {
            let column = if table == RETAINED_TABLES[2] {
                "plan_observation_sequence"
            } else {
                "sample_sequence"
            };
            assert_eq!(sequences(&conn, table, column), [3]);
        }
        assert!(profiles.begin_readmission(&key(), &runtime("revision-v2"), 4));
        flush(&mut conn, &profiles, &guard).unwrap();
        for table in &RETAINED_TABLES[..2] {
            assert!(sequences(&conn, table, "sample_sequence").is_empty());
        }
        let restarted = PlanProfiles::with_window(2);
        restarted.warm_from_db(&conn).unwrap();
        let profile = restarted.get_for_reporting(&key()).unwrap();
        assert_eq!(profile.n_observations, 3);
        assert_eq!(profile.n_paired_observations, 3);
        assert_eq!(profile.window_observations, 0);
        assert_eq!(profile.paired_observations, 0);
    }
}
