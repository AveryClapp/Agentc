use agentc_optimizer::multi_call::{CallPosition, InferenceCostSource, MultiCallTotals};
use agentc_optimizer::plan_profile::{
    CallSiteVersion, PlanProfileKey, PlanProfileUpdate, PlanProfiles, PlanRuntimeVersion,
};
use agentc_optimizer::provider_publication::{self, ComparisonInput, PublicationInput};
use agentc_optimizer::provider_receipts::{self, Batch, Completion, Invocation, Role, Status};
use agentc_optimizer::schema::ensure_cost_model_schema;
use agentc_optimizer::ExecutionPlanId;
use rusqlite::Connection;

use agentc_optimizer::execution_plan::{
    CachePolicy, ExecutionPlanSpec, RewriteOrdering, ValidationPolicy,
};
use agentc_optimizer::multi_call::{
    MultiCallBranch, MultiCallPlanSpec, MultiCallRuntimeBranch, EXACT_MAP_COMPARISON_VERSION,
    PREPARED_PROVIDER_BATCH_VERSION,
};
use agentc_optimizer::provider_economics::{self, Phase};
use agentc_optimizer::provider_publication::{DeferredEvidence, ScalarProvenance};

const ID: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

#[test]
fn unresolved_original_evidence_waits_but_retired_boundaries_never_publish() {
    for retirement in [
        "none",
        "predecessor",
        "runtime",
        "window",
        "journal",
        "fingerprint",
    ] {
        let mut conn = Connection::open_in_memory().unwrap();
        let profiles = PlanProfiles::new();
        let mut input = fixture(&mut conn, &profiles);
        let position = CallPosition { item: 0, call: 0 };
        let runtime = input.update.runtime_version.clone();
        let spec = MultiCallPlanSpec {
            application_version: "a".repeat(64),
            callback_version: "callback".into(),
            item_count: 1,
            max_workers: 1,
            comparison_version: Some(EXACT_MAP_COMPARISON_VERSION.into()),
            execution_contract: Some(PREPARED_PROVIDER_BATCH_VERSION.into()),
            branches: vec![MultiCallBranch {
                position,
                call_site_version: CallSiteVersion::parse("c".repeat(64)).unwrap(),
                plan: ExecutionPlanSpec {
                    schema_version: 1,
                    provider_protocol: runtime.provider_protocol.clone(),
                    requested_model_id: "model".into(),
                    target_model_id: "model".into(),
                    target_model_revision: Some("revision-v1".into()),
                    rewrite_ordering: RewriteOrdering::Ordered,
                    rewrites: vec![],
                    output_budget: Some(16),
                    cache_policy: CachePolicy {
                        stable_name: "provider-call".into(),
                        implementation_version: "1".into(),
                        parameters: serde_json::json!({}),
                    },
                    validation_policy: ValidationPolicy {
                        stable_name: "none".into(),
                        implementation_version: "1".into(),
                        parameters: serde_json::json!({}),
                    },
                },
            }],
        };
        let branch = &spec.branches[0];
        let key = PlanProfileKey {
            call_site_version: branch.call_site_version.clone(),
            execution_plan_id: branch.plan.execution_plan_id().unwrap(),
        };
        let raw: String = conn
            .query_row(
                "SELECT invocation_json FROM provider_receipt_invocation",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let mut invocation: Invocation = serde_json::from_str(&raw).unwrap();
        invocation.logical_plan_key = Some(key.clone());
        invocation.dispatch_plan_key = Some(key);
        conn.execute("UPDATE provider_receipt_invocation SET invocation_json=?1,completion_json=replace(completion_json,'provider_reported','estimated')", [serde_json::to_string(&invocation).unwrap()]).unwrap();
        provider_economics::enroll_batch(&mut conn, ID, &spec, Phase::Acquisition).unwrap();
        let runtimes = vec![MultiCallRuntimeBranch { position, runtime }];
        input.update.key = spec.profile_key().unwrap();
        input.update.runtime_version =
            agentc_optimizer::multi_call::profile_runtime(&spec, &runtimes).unwrap();
        input.predecessor = profiles.publication_boundary(&input.update.key);
        input.journal_sha256 = provider_publication::journal_fingerprint(&conn, ID).unwrap();
        input.comparison.as_mut().unwrap().reference_plan_id =
            input.update.key.execution_plan_id.clone();
        input.deferred_evidence = Some(DeferredEvidence {
            spec,
            runtimes,
            request_fingerprints: vec![(position, "e".repeat(64))],
            scalar_provenance: vec![ScalarProvenance {
                position,
                cost_source: InferenceCostSource::Estimated,
                observation: None,
            }],
        });
        provider_publication::reserve(&mut conn, ID, "original receipt", &input).unwrap();
        match retirement {
            "predecessor" | "runtime" => {
                let mut next = input.update.clone();
                if retirement == "runtime" {
                    next.runtime_version.target_model_version = "retired-v2".into();
                }
                profiles.observe(next).unwrap();
            }
            "journal" => {
                conn.execute("UPDATE provider_receipt_invocation SET completion_json=replace(completion_json,'revision-v1','revision-v2')", []).unwrap();
            }
            "fingerprint" => {
                conn.execute("UPDATE provider_receipt_publication_input SET input_json=replace(input_json,?1,?2)", ["2".repeat(64), "3".repeat(64)]).unwrap();
            }
            _ => {}
        }
        profiles.flush_dirty(&mut conn).unwrap();
        let recovered = PlanProfiles::with_window(if retirement == "window" { 2 } else { 50 });
        recovered.warm_from_db(&conn).unwrap();
        let result = provider_publication::publish(&mut conn, &recovered, ID);
        if retirement == "fingerprint" {
            assert!(result.is_err());
        } else {
            let result: serde_json::Value = serde_json::from_str(&result.unwrap()).unwrap();
            assert_eq!(
                result["status"],
                if retirement == "none" {
                    "pending_billing"
                } else {
                    "abstained"
                }
            );
            if retirement == "none" {
                assert_eq!(
                    provider_publication::recover_pending(&mut conn, &recovered).unwrap(),
                    0
                );
            } else {
                assert_eq!(result["retired"], true);
            }
        }
        assert_eq!(count(&conn, "multi_call_comparison_execution"), 0);
        assert_eq!(count(&conn, "multi_call_comparison_spend"), 0);
        assert_eq!(count(&conn, "provider_billing_settlement"), 0);
    }
}

fn fixture(conn: &mut Connection, profiles: &PlanProfiles) -> PublicationInput {
    ensure_cost_model_schema(conn).unwrap();
    provider_publication::ensure_schema(conn).unwrap();
    let key = PlanProfileKey {
        call_site_version: CallSiteVersion::parse("c".repeat(64)).unwrap(),
        execution_plan_id: ExecutionPlanId::parse("d".repeat(64)).unwrap(),
    };
    let runtime = PlanRuntimeVersion {
        provider_protocol: "openai.chat.completions.v1".into(),
        target_model_id: "model".into(),
        target_model_version: "revision-v1".into(),
        price_table_version: "prices-v1".into(),
    };
    provider_receipts::begin_batch(
        conn,
        &Batch {
            version: provider_receipts::VERSION.into(),
            batch_id: ID.into(),
            application_version: Some("a".repeat(64)),
            callback_version: "callback".into(),
            item_count: 1,
        },
    )
    .unwrap();
    provider_receipts::begin_call(
        conn,
        ID,
        &Invocation {
            invocation_id: "0".repeat(32),
            position: CallPosition { item: 0, call: 0 },
            ordinal: 0,
            role: Role::Primary,
            provider_protocol: runtime.provider_protocol.clone(),
            requested_model_id: "model".into(),
            request_sha256: Some("e".repeat(64)),
            logical_request_sha256: Some("e".repeat(64)),
            transport_sha256: Some("f".repeat(64)),
            logical_plan_key: Some(key.clone()),
            logical_runtime: Some(runtime.clone()),
            dispatch_plan_key: Some(key.clone()),
            dispatch_runtime: Some(runtime.clone()),
            parent_plan_key: None,
            transport_verified: true,
            tariff_declaration: None,
        },
    )
    .unwrap();
    provider_receipts::finish_call(
        conn,
        ID,
        &"0".repeat(32),
        &Completion {
            status: Status::Completed,
            model_version: Some("revision-v1".into()),
            input_tokens: Some(2),
            output_tokens: Some(1),
            cost_usd: Some(0.01),
            cost_source: InferenceCostSource::ProviderReported,
            latency_ms: 10.0,
            http_attempts: Some(1),
            exception_type: None,
            billing_scope_sha256: None,
            provider_generation_sha256: None,
            provider_request_id_sha256: None,
            billing_usage: None,
        },
    )
    .unwrap();
    provider_receipts::handle_json(conn, &serde_json::json!({"operation":"seal_batch","batch_id":ID,"logical_calls":[{"item":0,"call":0}]}).to_string()).unwrap();
    PublicationInput {
        version: "provider-profile-publication-v1".into(),
        journal_sha256: provider_publication::journal_fingerprint(conn, ID).unwrap(),
        predecessor: profiles.publication_boundary(&key),
        retained_window_size: profiles.retained_window_size(),
        update: PlanProfileUpdate {
            cost_evidence: Default::default(),
            key,
            runtime_version: runtime,
            input_tokens: 2,
            output_tokens: 1,
            latency_ms: 10.0,
            cost_usd: 0.01,
            output_is_structured: false,
            output_is_short: true,
            divergence: None,
            dispatch_fallback: false,
            observed_at_us: Some(100),
        },
        totals: MultiCallTotals {
            input_tokens: 2,
            output_tokens: 1,
            cost_usd: 0.01,
            wall_latency_ms: 10.0,
            summed_call_latency_ms: 10.0,
            call_count: 1,
        },
        charge_acquisition: true,
        original_evidence: None,
        deferred_evidence: None,
        comparison: Some(ComparisonInput {
            reference_plan_id: ExecutionPlanId::parse("a".repeat(64)).unwrap(),
            max_workers: 1,
            input_fingerprint: "1".repeat(64),
            output_fingerprint: "2".repeat(64),
        }),
    }
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
        row.get(0)
    })
    .unwrap()
}

#[test]
fn every_derived_write_failure_rolls_back_memory_and_sql_then_recovers_once() {
    for boundary in [
        "execution_plan_profile",
        "execution_plan_observation",
        "multi_call_comparison_spend",
        "multi_call_comparison_execution",
        "provider_receipt_profile_publication",
    ] {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("cost.db");
        let mut conn = Connection::open(&path).unwrap();
        let profiles = PlanProfiles::new();
        let input = fixture(&mut conn, &profiles);
        provider_publication::reserve(&mut conn, ID, "immutable receipt", &input).unwrap();
        let operation = if boundary == "provider_receipt_profile_publication" {
            "UPDATE"
        } else {
            "INSERT"
        };
        conn.execute_batch(&format!("CREATE TRIGGER fail_write BEFORE {operation} ON {boundary} BEGIN SELECT RAISE(ABORT,'interrupted'); END;")).unwrap();
        assert!(
            provider_publication::publish(&mut conn, &profiles, ID).is_err(),
            "{boundary}"
        );
        assert!(
            profiles.is_empty(),
            "uncommitted observation leaked: {boundary}"
        );
        assert_eq!(profiles.dirty_len(), 0);
        for table in [
            "execution_plan_profile",
            "execution_plan_observation",
            "multi_call_comparison_spend",
            "multi_call_comparison_execution",
        ] {
            assert_eq!(count(&conn, table), 0, "partial write: {boundary}/{table}");
        }
        assert_eq!(count(&conn, "provider_receipt_invocation"), 1);
        drop(conn);
        drop(profiles);
        let mut conn = Connection::open(&path).unwrap();
        conn.execute_batch("DROP TRIGGER fail_write;").unwrap();
        let profiles = PlanProfiles::new();
        profiles.warm_from_db(&conn).unwrap();
        assert_eq!(
            provider_publication::recover_pending(&mut conn, &profiles).unwrap(),
            1
        );
        assert_eq!(
            provider_publication::recover_pending(&mut conn, &profiles).unwrap(),
            0
        );
        let first = provider_publication::publish(&mut conn, &profiles, ID).unwrap();
        assert_eq!(
            provider_publication::reserve(&mut conn, ID, "immutable receipt", &input).unwrap(),
            Some(first.clone())
        );
        assert!(provider_publication::reserve(&mut conn, ID, "edited receipt", &input).is_err());
        drop(conn);
        drop(profiles);
        let mut conn = Connection::open(&path).unwrap();
        let profiles = PlanProfiles::new();
        profiles.warm_from_db(&conn).unwrap();
        assert_eq!(
            provider_publication::publish(&mut conn, &profiles, ID).unwrap(),
            first
        );
        assert_eq!(
            profiles
                .get_for_reporting(&input.update.key)
                .unwrap()
                .n_observations,
            1
        );
        for table in [
            "execution_plan_profile",
            "execution_plan_observation",
            "multi_call_comparison_spend",
            "multi_call_comparison_execution",
        ] {
            assert_eq!(count(&conn, table), 1);
        }
        let (executions, cost): (i64, f64) = conn
            .query_row(
                "SELECT completed_executions,total_cost_usd FROM multi_call_comparison_spend",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!((executions, cost), (1, 0.01));
    }
}

#[test]
fn reservation_only_restart_recovers_but_retired_predecessors_never_resurrect() {
    for retirement in ["none", "runtime", "cohort", "window", "journal"] {
        let mut conn = Connection::open_in_memory().unwrap();
        let profiles = PlanProfiles::new();
        let input = fixture(&mut conn, &profiles);
        provider_publication::reserve(&mut conn, ID, "receipt", &input).unwrap();
        match retirement {
            "runtime" => {
                let mut update = input.update.clone();
                update.runtime_version.target_model_version = "revision-v2".into();
                profiles.observe(update).unwrap();
            }
            "cohort" => {
                assert!(profiles.begin_readmission(
                    &input.update.key,
                    &input.update.runtime_version,
                    200
                ));
            }
            "journal" => {
                conn.execute("UPDATE provider_receipt_invocation SET completion_json=replace(completion_json,'0.01','0.02')",[]).unwrap();
            }
            _ => {}
        }
        profiles.flush_dirty(&mut conn).unwrap();
        let recovered = PlanProfiles::with_window(if retirement == "window" { 2 } else { 50 });
        recovered.warm_from_db(&conn).unwrap();
        let result: serde_json::Value = serde_json::from_str(
            &provider_publication::publish(&mut conn, &recovered, ID).unwrap(),
        )
        .unwrap();
        assert_eq!(
            result["status"],
            if retirement == "none" {
                "recorded"
            } else {
                "abstained"
            }
        );
        assert_eq!(
            count(&conn, "multi_call_comparison_spend"),
            i64::from(retirement == "none")
        );
        assert_eq!(
            count(&conn, "multi_call_comparison_execution"),
            i64::from(retirement == "none")
        );
        assert_eq!(count(&conn, "provider_receipt_invocation"), 1);
        assert_eq!(
            provider_publication::recover_pending(&mut conn, &recovered).unwrap(),
            0
        );
    }
}

#[test]
fn corrupt_or_legacy_input_is_quarantined_without_touching_spend_or_profiles() {
    for kind in ["legacy", "corrupt"] {
        let mut conn = Connection::open_in_memory().unwrap();
        let profiles = PlanProfiles::new();
        let input = fixture(&mut conn, &profiles);
        if kind == "legacy" {
            provider_receipts::reserve_publication(&conn, ID, "receipt").unwrap();
        } else {
            provider_publication::reserve(&mut conn, ID, "receipt", &input).unwrap();
            conn.execute(
                "UPDATE provider_receipt_publication_input SET input_json='{}'",
                [],
            )
            .unwrap();
        }
        assert!(provider_publication::publish(&mut conn, &profiles, ID).is_err());
        assert!(provider_publication::reserve(&mut conn, ID, "receipt", &input).is_err());
        assert!(profiles.is_empty());
        assert_eq!(count(&conn, "multi_call_comparison_spend"), 0);
    }
}

#[test]
fn failed_publication_preserves_existing_profile_and_unrelated_dirty_work() {
    let mut conn = Connection::open_in_memory().unwrap();
    let profiles = PlanProfiles::new();
    let mut input = fixture(&mut conn, &profiles);
    let mut old = input.update.clone();
    old.runtime_version.target_model_version = "revision-v0".into();
    old.divergence = Some(0.0);
    profiles.observe(old.clone()).unwrap();
    profiles.flush_dirty(&mut conn).unwrap();
    let mut unrelated = input.update.clone();
    unrelated.key.execution_plan_id = ExecutionPlanId::parse("9".repeat(64)).unwrap();
    profiles.observe(unrelated.clone()).unwrap();
    input.predecessor = profiles.publication_boundary(&input.update.key);
    provider_publication::reserve(&mut conn, ID, "receipt", &input).unwrap();
    conn.execute_batch("CREATE TRIGGER fail_write BEFORE INSERT ON multi_call_comparison_execution BEGIN SELECT RAISE(ABORT,'interrupted'); END;").unwrap();
    assert!(provider_publication::publish(&mut conn, &profiles, ID).is_err());
    assert_eq!(
        profiles
            .get_for_reporting(&input.update.key)
            .unwrap()
            .n_observations,
        1
    );
    let retained = profiles.get_for_reporting(&input.update.key).unwrap();
    assert_eq!(retained.runtime_version, old.runtime_version);
    assert_eq!(retained.paired_observations, 1);
    assert_eq!(
        profiles
            .get_for_reporting(&unrelated.key)
            .unwrap()
            .n_observations,
        1
    );
    assert_eq!(profiles.dirty_len(), 1);
    conn.execute_batch("DROP TRIGGER fail_write;").unwrap();
    provider_publication::publish(&mut conn, &profiles, ID).unwrap();
    assert_eq!(
        profiles
            .get_for_reporting(&input.update.key)
            .unwrap()
            .n_observations,
        2
    );
    assert_eq!(profiles.dirty_len(), 1);
    profiles.flush_dirty(&mut conn).unwrap();
    // Runtime drift keeps lifetime counts but retires the old execution/pair.
    let current = profiles.get_for_reporting(&input.update.key).unwrap();
    assert_eq!(current.runtime_version, input.update.runtime_version);
    assert_eq!(current.window_observations, 1);
    assert_eq!(current.paired_observations, 0);
    assert_eq!(current.n_paired_observations, 1);
    assert_eq!(count(&conn, "execution_plan_observation"), 2);
    let cost: f64 = conn
        .query_row(
            "SELECT total_cost_usd FROM multi_call_comparison_spend",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(cost, 0.01);
}

#[test]
fn pending_recovery_does_not_scan_completed_publication_history() {
    let mut conn = Connection::open_in_memory().unwrap();
    let profiles = PlanProfiles::new();
    let input = fixture(&mut conn, &profiles);
    provider_publication::reserve(&mut conn, ID, "receipt", &input).unwrap();
    provider_publication::publish(&mut conn, &profiles, ID).unwrap();
    conn.execute_batch("WITH RECURSIVE history(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM history WHERE n<10000) INSERT INTO provider_receipt_profile_publication SELECT printf('%032x',n),'historical-digest','published' FROM history;").unwrap();
    let mut query=conn.prepare("EXPLAIN QUERY PLAN SELECT p.batch_id FROM provider_receipt_profile_publication p JOIN provider_receipt_publication_input i USING(batch_id) WHERE p.result_json IS NULL ORDER BY p.rowid").unwrap();
    let plan = query
        .query_map([], |row| row.get::<_, String>(3))
        .unwrap()
        .collect::<rusqlite::Result<Vec<_>>>()
        .unwrap();
    assert!(
        plan.iter()
            .any(|step| step.contains("provider_receipt_pending_publication")),
        "{plan:?}"
    );
    assert!(!plan.iter().any(|step| step == "SCAN p"), "{plan:?}");
    drop(query);
    assert_eq!(
        provider_publication::recover_pending(&mut conn, &profiles).unwrap(),
        0
    );
    assert_eq!(
        profiles
            .get_for_reporting(&input.update.key)
            .unwrap()
            .n_observations,
        1
    );
}
