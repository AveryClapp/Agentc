use agentc_optimizer::execution_plan::{
    CachePolicy, ExecutionPlanSpec, RewriteOrdering, ValidationPolicy,
};
use agentc_optimizer::multi_call::{
    CallPosition, InferenceCostSource, MultiCallBranch, MultiCallPlanSpec,
    EXACT_MAP_COMPARISON_VERSION, PREPARED_PROVIDER_BATCH_VERSION,
};
use agentc_optimizer::plan_profile::{CallSiteVersion, PlanProfileKey, PlanRuntimeVersion};
use agentc_optimizer::provider_economics::{self, Phase};
use agentc_optimizer::provider_receipts::{self, Completion, Invocation, Role, Status};
use agentc_optimizer::schema::ensure_cost_model_schema;
use rusqlite::Connection;
use serde_json::{json, Value};

struct Fixture {
    conn: Connection,
    spec: MultiCallPlanSpec,
    id: String,
}
impl Fixture {
    fn new(phase: Phase) -> Self {
        let mut conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        let spec = MultiCallPlanSpec {
            application_version: "a".repeat(64),
            callback_version: "callback-v1".into(),
            item_count: 1,
            max_workers: 1,
            comparison_version: Some(EXACT_MAP_COMPARISON_VERSION.into()),
            execution_contract: Some(PREPARED_PROVIDER_BATCH_VERSION.into()),
            branches: vec![MultiCallBranch {
                position: CallPosition { item: 0, call: 0 },
                call_site_version: CallSiteVersion::parse("c".repeat(64)).unwrap(),
                plan: ExecutionPlanSpec {
                    schema_version: 1,
                    provider_protocol: "openai.chat.completions.v1".into(),
                    requested_model_id: "model".into(),
                    target_model_id: "model".into(),
                    target_model_revision: Some("revision-v1".into()),
                    rewrite_ordering: RewriteOrdering::Ordered,
                    rewrites: vec![],
                    output_budget: Some(16),
                    cache_policy: CachePolicy {
                        stable_name: "provider-call".into(),
                        implementation_version: "1".into(),
                        parameters: json!({}),
                    },
                    validation_policy: ValidationPolicy {
                        stable_name: "none".into(),
                        implementation_version: "1".into(),
                        parameters: json!({}),
                    },
                },
            }],
        };
        let id = "b".repeat(32);
        assert!(provider_economics::enroll_batch(&mut conn, &id, &spec, phase).unwrap());
        Self { conn, spec, id }
    }
    fn command(&mut self, value: Value) -> anyhow::Result<String> {
        provider_receipts::handle_json(&mut self.conn, &value.to_string())
    }
    fn invocation(&self, ordinal: u32, role: Role) -> Invocation {
        let branch = &self.spec.branches[0];
        let key = PlanProfileKey {
            call_site_version: branch.call_site_version.clone(),
            execution_plan_id: branch.plan.execution_plan_id().unwrap(),
        };
        let runtime = PlanRuntimeVersion {
            provider_protocol: "openai.chat.completions.v1".into(),
            target_model_id: "model".into(),
            target_model_version: "revision-v1".into(),
            price_table_version: "prices-v1".into(),
        };
        Invocation {
            invocation_id: format!("{ordinal:032x}"),
            position: branch.position,
            ordinal,
            role,
            provider_protocol: runtime.provider_protocol.clone(),
            requested_model_id: "model".into(),
            request_sha256: Some("e".repeat(64)),
            logical_request_sha256: Some("e".repeat(64)),
            transport_sha256: Some("f".repeat(64)),
            logical_plan_key: Some(key.clone()),
            logical_runtime: Some(runtime.clone()),
            dispatch_plan_key: Some(key),
            dispatch_runtime: Some(runtime),
            parent_plan_key: None,
            transport_verified: true,
            tariff_declaration: None,
        }
    }
    fn begin(&mut self, call: &Invocation) {
        self.command(json!({"operation":"begin_call","batch_id":self.id,"invocation":call}))
            .unwrap();
    }
    fn finish(&mut self, call: &Invocation, completion: &Completion) {
        self.command(json!({"operation":"finish_call","batch_id":self.id,"invocation_id":call.invocation_id,"completion":completion})).unwrap();
    }
    fn seal(&mut self, failed: bool, calls: Vec<CallPosition>) {
        self.command(json!({"operation":"seal_batch","batch_id":self.id,"logical_calls":calls,"persistence_failed":failed})).unwrap();
    }
    fn metrics(&mut self, wall: f64) {
        self.command(
            json!({"operation":"finish_batch_economics","batch_id":self.id,"wall_latency_ms":wall}),
        )
        .unwrap();
    }
    fn summary(&self) -> provider_economics::Economics {
        provider_economics::summarize_cohort(
            &self.conn,
            &self.spec.profile_key().unwrap().call_site_version,
        )
        .unwrap()
    }
    fn tariff_summary(&self) -> agentc_optimizer::provider_tariff::Acquisition {
        agentc_optimizer::provider_tariff::summarize_acquisition(
            &self.conn,
            &self.spec.profile_key().unwrap().call_site_version,
        )
        .unwrap()
    }
}
fn completion() -> Completion {
    Completion {
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
    }
}

fn tariff_call(fixture: &Fixture, ordinal: u32, role: Role) -> (Invocation, Completion) {
    use agentc_optimizer::provider_tariff::{self, Declaration};
    let scope = agentc_core::storage::content_hash("billing-scope-v1\0https://api.openai.com/v1");
    let mut call = fixture.invocation(ordinal, role);
    call.requested_model_id = "gpt-4o-mini-2024-07-18".into();
    call.tariff_declaration = Some(Declaration {
        version: provider_tariff::VERSION.into(),
        snapshot_sha256: provider_tariff::snapshot_sha256(),
        endpoint_sha256: Some(scope.clone()),
        request_scope_supported: true,
    });
    let mut done = completion();
    done.model_version = Some(call.requested_model_id.clone());
    done.cost_source = InferenceCostSource::Estimated;
    done.billing_scope_sha256 = Some(scope);
    done.billing_usage=Some(serde_json::from_value(json!({"version":"provider-billing-usage-v1","usage":{
        "provider_protocol":"openai.chat.completions.v1","input_tokens":2,"output_tokens":1,"total_tokens":3,
        "input_details":null,"output_details":null,"service_tier":"default","unrecognized_fields":false}})).unwrap());
    (call, done)
}

fn completed_tariff_fixture() -> Fixture {
    let mut fixture = Fixture::new(Phase::Acquisition);
    fixture
        .command(json!({"operation":"tariff_policy"}))
        .unwrap();
    let (call, done) = tariff_call(&fixture, 0, Role::Primary);
    fixture.begin(&call);
    fixture.finish(&call, &done);
    fixture.seal(false, vec![call.position]);
    fixture.metrics(10.0);
    fixture
}

#[test]
fn tariff_acquisition_and_deployment_use_the_original_predispatch_owner_and_phase() {
    use agentc_optimizer::provider_tariff::{self, Declaration};
    for phase in [Phase::Acquisition, Phase::Deployment] {
        let mut fixture = Fixture::new(phase);
        fixture
            .command(json!({"operation":"tariff_policy"}))
            .unwrap();
        let scope =
            agentc_core::storage::content_hash("billing-scope-v1\0https://api.openai.com/v1");
        let mut call = fixture.invocation(0, Role::Primary);
        call.requested_model_id = "gpt-4o-mini-2024-07-18".into();
        call.tariff_declaration = Some(Declaration {
            version: provider_tariff::VERSION.into(),
            snapshot_sha256: provider_tariff::snapshot_sha256(),
            endpoint_sha256: Some(scope.clone()),
            request_scope_supported: true,
        });
        fixture.begin(&call);
        let mut done = completion();
        done.model_version = Some(call.requested_model_id.clone());
        done.cost_source = InferenceCostSource::Estimated;
        done.billing_scope_sha256 = Some(scope);
        done.billing_usage=Some(serde_json::from_value(json!({"version":"provider-billing-usage-v1","usage":{
            "provider_protocol":"openai.chat.completions.v1","input_tokens":2,"output_tokens":1,"total_tokens":3,
            "input_details":null,"output_details":null,"service_tier":"default","unrecognized_fields":false}})).unwrap());
        fixture.finish(&call, &done);
        fixture.seal(false, vec![call.position]);
        fixture.metrics(10.0);
        let first = provider_tariff::report(&mut fixture.conn, &fixture.id).unwrap();
        assert_eq!(first["lower_nanodollars"], 750);
        assert_eq!(first["upper_nanodollars"], 900);
        assert_eq!(first["acquisition"]["phase_classification_complete"], true);
        let expected = if phase == Phase::Acquisition {
            (750, 900, 1)
        } else {
            (0, 0, 0)
        };
        assert_eq!(first["acquisition"]["lower_nanodollars"], expected.0);
        assert_eq!(first["acquisition"]["upper_nanodollars"], expected.1);
        assert_eq!(first["acquisition"]["charged_invocations"], expected.2);
        let summary = fixture.tariff_summary();
        assert!(summary.coverage_complete);
        assert_eq!(summary.lower_nanodollars, expected.0);
        assert_eq!(summary.upper_nanodollars, Some(expected.1));
        assert_eq!(summary.charged_invocations, expected.2);
        fixture.finish(&call, &done);
        assert_eq!(
            provider_tariff::report(&mut fixture.conn, &fixture.id).unwrap(),
            first
        );
        assert_eq!(fixture.summary().known_cost_usd, 0.0);
        assert!(!fixture.summary().cost_complete);
        let mut auxiliary = call.clone();
        auxiliary.invocation_id = "1".repeat(32);
        auxiliary.ordinal = 1;
        auxiliary.role = Role::Validation;
        fixture.begin(&auxiliary);
        fixture.finish(&auxiliary, &done);
        let with_aux = provider_tariff::report(&mut fixture.conn, &fixture.id).unwrap();
        assert_eq!(
            with_aux["acquisition"]["lower_nanodollars"],
            expected.0 + 750
        );
        assert_eq!(
            with_aux["acquisition"]["upper_nanodollars"],
            expected.1 + 900
        );
        assert_eq!(
            with_aux["acquisition"]["charged_invocations"],
            expected.2 + 1
        );
        assert_eq!(
            fixture.tariff_summary().upper_nanodollars,
            Some(expected.1 + 900)
        );
        let pending = "2".repeat(32);
        assert!(provider_economics::enroll_batch(
            &mut fixture.conn,
            &pending,
            &fixture.spec,
            Phase::Acquisition
        )
        .unwrap());
        let pending_summary = fixture.tariff_summary();
        assert_eq!(pending_summary.owned_batches, 2);
        assert_eq!(pending_summary.coverage_batches, 2);
        assert!(!pending_summary.coverage_complete);
        assert_eq!(pending_summary.upper_nanodollars, None);
        assert_eq!(pending_summary.lower_nanodollars, expected.0 + 750);
        let reserved = provider_tariff::report(&mut fixture.conn, &fixture.id).unwrap();
        assert!(
            reserved["acquisition"]["upper_nanodollars"].is_null(),
            "pending owner batch without SDK rows must remain held: {reserved}"
        );
        assert_eq!(
            reserved["acquisition"]["observed_charged_lower_nanodollars"],
            with_aux["acquisition"]["observed_charged_lower_nanodollars"]
        );
        fixture.command(json!({"operation":"seal_batch","batch_id":pending,"logical_calls":[],"persistence_failed":false})).unwrap();
        fixture.command(json!({"operation":"finish_batch_economics","batch_id":pending,"wall_latency_ms":0.0})).unwrap();
        assert_eq!(
            fixture.tariff_summary().upper_nanodollars,
            Some(expected.1 + 900)
        );
        assert_eq!(
            provider_tariff::report(&mut fixture.conn, &fixture.id).unwrap()["acquisition"]
                ["upper_nanodollars"],
            with_aux["acquisition"]["upper_nanodollars"]
        );
        let lost = "3".repeat(32);
        provider_economics::enroll_batch(
            &mut fixture.conn,
            &lost,
            &fixture.spec,
            Phase::Acquisition,
        )
        .unwrap();
        fixture.command(json!({"operation":"seal_batch","batch_id":lost,"logical_calls":[call.position],"persistence_failed":true})).unwrap();
        fixture
            .command(
                json!({"operation":"finish_batch_economics","batch_id":lost,"wall_latency_ms":0.0}),
            )
            .unwrap();
        assert!(
            provider_tariff::report(&mut fixture.conn, &fixture.id).unwrap()["acquisition"]
                ["upper_nanodollars"]
                .is_null()
        );
        assert_eq!(fixture.tariff_summary().upper_nanodollars, None);
    }
}

#[test]
fn tariff_summary_retains_undeclared_failed_and_structurally_invalid_late_work() {
    use agentc_optimizer::provider_tariff;
    for case in ["undeclared", "gap", "foreign_key", "failed"] {
        let mut fixture = completed_tariff_fixture();
        let (mut call, mut done) = tariff_call(&fixture, 1, Role::Validation);
        match case {
            "undeclared" => call.tariff_declaration = None,
            "gap" => call.ordinal = 2,
            "foreign_key" => {
                call.logical_plan_key.as_mut().unwrap().call_site_version =
                    CallSiteVersion::parse("d".repeat(64)).unwrap()
            }
            "failed" => {
                done.status = Status::Failed;
                done.exception_type = Some("TimeoutError".into());
                done.billing_usage = None;
            }
            _ => unreachable!(),
        }
        fixture.begin(&call);
        assert_eq!(fixture.tariff_summary().upper_nanodollars, None, "{case}");
        fixture.finish(&call, &done);
        let before = fixture.tariff_summary();
        assert_eq!(before.upper_nanodollars, None, "{case}: {before:?}");
        assert!(before.lower_nanodollars >= 750);
        let owner = fixture.spec.profile_key().unwrap().call_site_version;
        provider_tariff::recover(&mut fixture.conn, owner.as_str()).unwrap();
        let after = fixture.tariff_summary();
        assert_eq!(after.upper_nanodollars, None, "{case}: {after:?}");
        assert_eq!(before.lower_nanodollars, after.lower_nanodollars);
        if case == "undeclared" || case == "failed" {
            assert_eq!(after.unknown_invocations, 1);
        } else {
            assert!(!after.coverage_complete);
            assert_eq!(after.unknown_invocations, 0);
            assert_eq!(after.lower_nanodollars, 1500);
        }
    }
}

#[test]
fn tariff_coverage_migration_holds_until_explicit_recovery_and_survives_restart() {
    use agentc_optimizer::provider_tariff;
    let mut fixture = completed_tariff_fixture();
    let owner = fixture.spec.profile_key().unwrap().call_site_version;
    let skipped = "2".repeat(32);
    provider_economics::enroll_batch(
        &mut fixture.conn,
        &skipped,
        &fixture.spec,
        Phase::Acquisition,
    )
    .unwrap();
    fixture.command(json!({"operation":"seal_batch","batch_id":skipped,"logical_calls":[],"persistence_failed":false})).unwrap();
    fixture
        .command(
            json!({"operation":"finish_batch_economics","batch_id":skipped,"wall_latency_ms":0.0}),
        )
        .unwrap();
    let expected = fixture.tariff_summary();
    assert_eq!(expected.upper_nanodollars, Some(900));
    assert_eq!(expected.owned_batches, 2);
    let raw: (String, String) = fixture
        .conn
        .query_row(
            "SELECT invocation_json,completion_json FROM provider_receipt_invocation",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    // Simulate a store written before materialized coverage existed.
    fixture
        .conn
        .execute_batch(
            "DROP TABLE provider_tariff_coverage; DROP TABLE provider_tariff_coverage_cohort;",
        )
        .unwrap();
    for _ in 0..3 {
        let held = fixture.tariff_summary();
        assert_eq!(held.upper_nanodollars, None);
        assert_eq!(held.lower_nanodollars, 0);
        assert_eq!(held.observed_charged_lower_nanodollars, 750);
        assert_eq!(held.coverage_batches, 0);
    }
    provider_tariff::recover(&mut fixture.conn, owner.as_str()).unwrap();
    assert_eq!(fixture.tariff_summary(), expected);
    provider_tariff::recover(&mut fixture.conn, owner.as_str()).unwrap();
    assert_eq!(fixture.tariff_summary(), expected);
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("cost.db");
    fixture
        .conn
        .execute("VACUUM INTO ?1", [path.to_str().unwrap()])
        .unwrap();
    fixture.conn = Connection::open(&path).unwrap();
    assert_eq!(fixture.tariff_summary(), expected);
    let after: (String, String) = fixture
        .conn
        .query_row(
            "SELECT invocation_json,completion_json FROM provider_receipt_invocation",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(raw, after);
    let result: Value = serde_json::from_str(
        &fixture
            .command(json!({"operation":"tariff_acquisition","call_site_version":owner}))
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        result["acquisition"],
        serde_json::to_value(expected).unwrap()
    );
}

#[test]
fn tariff_coverage_write_failure_rolls_back_owner_and_original_seal() {
    let mut fixture = Fixture::new(Phase::Acquisition);
    let pending = "2".repeat(32);
    fixture.conn.execute_batch("CREATE TRIGGER coverage_fault BEFORE UPDATE ON provider_tariff_coverage_cohort BEGIN SELECT RAISE(ABORT,'coverage fault'); END;").unwrap();
    assert!(provider_economics::enroll_batch(
        &mut fixture.conn,
        &pending,
        &fixture.spec,
        Phase::Acquisition
    )
    .is_err());
    assert_eq!(fixture.summary().owned_batches, 1);
    assert_eq!(fixture.tariff_summary().coverage_batches, 1);
    assert!(!fixture
        .conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM provider_receipt_batch WHERE batch_id=?1)",
            [&pending],
            |r| r.get::<_, bool>(0)
        )
        .unwrap());
    assert!(fixture.command(json!({"operation":"seal_batch","batch_id":fixture.id,"logical_calls":[],"persistence_failed":false})).is_err());
    assert!(!fixture
        .conn
        .query_row(
            "SELECT sealed FROM provider_receipt_batch WHERE batch_id=?1",
            [&fixture.id],
            |r| r.get::<_, bool>(0)
        )
        .unwrap());
    assert!(!fixture.tariff_summary().coverage_complete);
    fixture
        .conn
        .execute_batch("DROP TRIGGER coverage_fault;")
        .unwrap();
    fixture.seal(false, vec![]);
    fixture.metrics(0.0);
    let expected = fixture.tariff_summary();
    assert!(expected.coverage_complete);
    assert_eq!(expected.owned_invocations, 0);
    assert_eq!(expected.upper_nanodollars, Some(0));
    fixture.seal(false, vec![]);
    fixture.metrics(0.0);
    assert_eq!(fixture.tariff_summary(), expected);
}

#[test]
fn tariff_summary_rejects_stale_snapshots_and_works_in_existing_transactions() {
    let fixture = completed_tariff_fixture();
    let owner = fixture.spec.profile_key().unwrap().call_site_version;
    let expected = fixture.tariff_summary();
    let transaction = fixture.conn.unchecked_transaction().unwrap();
    assert_eq!(
        agentc_optimizer::provider_tariff::summarize_acquisition(&transaction, &owner).unwrap(),
        expected
    );
    transaction.commit().unwrap();
    fixture
        .conn
        .execute(
            "UPDATE provider_tariff_coverage_cohort SET snapshot_sha256=?1 WHERE owner=?2",
            [&"0".repeat(64), owner.as_str()],
        )
        .unwrap();
    let stale = fixture.tariff_summary();
    assert!(!stale.coverage_complete);
    assert_eq!(stale.upper_nanodollars, None);
    assert_eq!(stale.lower_nanodollars, 0);
    assert_eq!(stale.observed_charged_lower_nanodollars, 750);
    assert_eq!(stale.bounded_observed_upper_nanodollars, 900);
}

#[test]
fn every_role_and_unprofiled_late_work_are_charged_once_without_comparison_totals() {
    let mut fixture = Fixture::new(Phase::Acquisition);
    for (ordinal, role) in [
        Role::Primary,
        Role::Validation,
        Role::Shadow,
        Role::Fallback,
    ]
    .into_iter()
    .enumerate()
    {
        let call = fixture.invocation(ordinal as u32, role);
        fixture.begin(&call);
        fixture.begin(&call);
        fixture.finish(&call, &completion());
        fixture.finish(&call, &completion());
    }
    fixture.seal(false, vec![CallPosition { item: 0, call: 0 }]);
    fixture.metrics(100.0);
    fixture.metrics(100.0);
    let first = fixture.summary();
    assert!(first.cost_complete);
    assert_eq!(first.charged_invocations, 4);
    assert!((first.known_cost_usd - 0.04).abs() < 1e-12);
    assert_eq!(first.acquisition_latency_ms, 100.0);
    let late = fixture.invocation(4, Role::Exploration);
    fixture.begin(&late);
    assert!(!fixture.summary().cost_complete);
    let mut done = completion();
    done.latency_ms = 0.0;
    fixture.finish(&late, &done);
    fixture.finish(&late, &done);
    assert_eq!(fixture.summary().acquisition_latency_ms, 100.0);
    let late = fixture.invocation(5, Role::Exploration);
    fixture.begin(&late);
    fixture.finish(&late, &completion());
    fixture.finish(&late, &completion());
    let all = fixture.summary();
    assert!(all.cost_complete);
    assert_eq!(all.charged_invocations, 6);
    assert!((all.known_cost_usd - 0.06).abs() < 1e-12);
    assert_eq!(all.acquisition_latency_ms, 110.0);
    assert_eq!(
        fixture
            .conn
            .query_row(
                "SELECT COUNT(*) FROM multi_call_comparison_spend",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn deployment_primary_is_not_charged_as_learning_but_auxiliary_and_unknown_work_are() {
    let mut fixture = Fixture::new(Phase::Deployment);
    let primary = fixture.invocation(0, Role::Primary);
    fixture.begin(&primary);
    assert_eq!(fixture.summary().unknown_cost_invocations, 1);
    fixture.finish(&primary, &completion());
    fixture.seal(false, vec![primary.position]);
    fixture.metrics(100.0);
    let ordinary = fixture.summary();
    assert!(ordinary.cost_complete);
    assert_eq!(ordinary.known_cost_usd, 0.0);
    assert_eq!(ordinary.acquisition_latency_ms, 0.0);
    for (ordinal, role) in [
        Role::Validation,
        Role::Shadow,
        Role::Exploration,
        Role::Fallback,
        Role::Primary,
    ]
    .into_iter()
    .enumerate()
    {
        let call = fixture.invocation(ordinal as u32 + 1, role);
        fixture.begin(&call);
        fixture.finish(&call, &completion());
    }
    let all = fixture.summary();
    assert!(all.cost_complete);
    assert_eq!(all.charged_invocations, 5);
    assert!((all.known_cost_usd - 0.05).abs() < 1e-12);
    assert_eq!(all.acquisition_latency_ms, 50.0);
}

#[test]
fn failures_estimates_unsupported_transport_and_missing_receipts_never_become_zero_cost() {
    for kind in [
        "failure",
        "estimate",
        "transport",
        "binding",
        "missing",
        "persistence",
    ] {
        let mut fixture = Fixture::new(Phase::Acquisition);
        let mut call = fixture.invocation(0, Role::Primary);
        let mut done = completion();
        match kind {
            "failure" => {
                done.status = Status::Failed;
                done.cost_source = InferenceCostSource::Unknown;
                done.cost_usd = None;
                done.exception_type = Some("ServerError".into());
            }
            "estimate" => {
                done.cost_source = InferenceCostSource::Estimated;
            }
            "transport" => {
                call.transport_verified = false;
                done.http_attempts = None;
            }
            "binding" => {
                call.logical_plan_key.as_mut().unwrap().execution_plan_id =
                    agentc_optimizer::ExecutionPlanId::parse("9".repeat(64)).unwrap();
            }
            _ => {}
        }
        if kind != "missing" {
            fixture.begin(&call);
            fixture.finish(&call, &done);
        }
        fixture.seal(kind == "persistence", vec![call.position]);
        fixture.metrics(100.0);
        let summary = fixture.summary();
        assert!(!summary.cost_complete, "{kind}");
        if kind == "estimate" {
            assert_eq!(summary.estimated_cost_usd, 0.01);
            assert_eq!(summary.known_cost_usd, 0.0);
        }
        if matches!(kind, "transport" | "binding" | "persistence") {
            assert_eq!(summary.known_cost_usd, 0.01);
        }
        fixture.seal(false, vec![call.position]);
        assert!(
            !fixture.summary().cost_complete,
            "sticky uncertainty: {kind}"
        );
    }
}

#[test]
fn predispatch_skips_have_known_cost_but_observer_loss_is_sticky() {
    let mut fixture = Fixture::new(Phase::Acquisition);
    fixture.seal(false, vec![]);
    fixture.metrics(1.0);
    assert!(fixture.summary().cost_complete);
    assert_eq!(fixture.summary().known_cost_usd, 0.0);
    fixture.seal(true, vec![]);
    assert!(!fixture.summary().cost_complete);
}

#[test]
fn owner_phase_and_foreground_metrics_are_immutable_and_survive_restart() {
    let mut fixture = Fixture::new(Phase::Acquisition);
    assert!(!provider_economics::enroll_batch(
        &mut fixture.conn,
        &fixture.id,
        &fixture.spec,
        Phase::Acquisition
    )
    .unwrap());
    assert!(provider_economics::enroll_batch(
        &mut fixture.conn,
        &fixture.id,
        &fixture.spec,
        Phase::Deployment
    )
    .is_err());
    let call = fixture.invocation(0, Role::Primary);
    fixture.begin(&call);
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("cost.db");
    fixture
        .conn
        .execute("VACUUM INTO ?1", [path.to_str().unwrap()])
        .unwrap();
    fixture.conn = Connection::open(&path).unwrap();
    assert!(!fixture.summary().cost_complete);
    assert_eq!(fixture.summary().unknown_cost_invocations, 1);
    fixture.finish(&call, &completion());
    fixture.seal(false, vec![call.position]);
    fixture.metrics(100.0);
    assert!(fixture.command(json!({"operation":"finish_batch_economics","batch_id":fixture.id,"wall_latency_ms":200.0})).is_err());
    let first = serde_json::to_value(fixture.summary()).unwrap();
    fixture.conn = Connection::open(&path).unwrap();
    fixture.finish(&call, &completion());
    fixture.metrics(100.0);
    assert_eq!(serde_json::to_value(fixture.summary()).unwrap(), first);
}

#[test]
fn receipt_and_economic_update_commit_together() {
    let mut fixture = Fixture::new(Phase::Acquisition);
    let call = fixture.invocation(0, Role::Primary);
    fixture.begin(&call);
    fixture.conn.execute_batch("CREATE TRIGGER fail_economics BEFORE UPDATE ON provider_economic_invocation BEGIN SELECT RAISE(ABORT,'storage failure'); END;").unwrap();
    assert!(fixture.command(json!({"operation":"finish_call","batch_id":fixture.id,"invocation_id":call.invocation_id,"completion":completion()})).is_err());
    assert!(fixture
        .conn
        .query_row(
            "SELECT completion_json IS NULL FROM provider_receipt_invocation",
            [],
            |r| r.get::<_, bool>(0)
        )
        .unwrap());
    assert_eq!(fixture.summary().known_cost_usd, 0.0);
    assert_eq!(fixture.summary().unknown_cost_invocations, 1);
    fixture
        .conn
        .execute_batch("DROP TRIGGER fail_economics;")
        .unwrap();
    fixture.finish(&call, &completion());
    fixture.seal(false, vec![call.position]);
    fixture.metrics(100.0);
    assert!(fixture.summary().cost_complete);
    assert_eq!(fixture.summary().known_cost_usd, 0.01);
}

#[test]
fn covered_comparison_spend_is_not_added_twice_and_uncovered_history_is_retained() {
    use agentc_optimizer::multi_call::{
        observe_multi_call, MultiCallOutcome, MultiCallRuntimeBranch,
    };
    use agentc_optimizer::multi_call_comparison::record_acquisition;
    use agentc_optimizer::{Outcome, PlanProfiles};
    let mut fixture = Fixture::new(Phase::Acquisition);
    let call = fixture.invocation(0, Role::Primary);
    fixture.begin(&call);
    fixture.finish(&call, &completion());
    fixture.seal(false, vec![call.position]);
    fixture.metrics(120.0);
    let versions = vec![MultiCallRuntimeBranch {
        position: call.position,
        runtime: call.logical_runtime.unwrap(),
    }];
    let outcomes = vec![MultiCallOutcome {
        position: call.position,
        cost_source: InferenceCostSource::ProviderReported,
        outcome: Outcome {
            input_tokens: 2,
            output_tokens: 1,
            cost_usd: 0.01,
            latency_ms: 100.0,
            inference_cost_complete: Some(true),
            ..Outcome::default()
        },
    }];
    let profiles = PlanProfiles::new();
    let (observation, _) = observe_multi_call(
        &profiles,
        &fixture.spec,
        &versions,
        &outcomes,
        100.0,
        Some(1),
    )
    .unwrap();
    profiles.flush_dirty(&mut fixture.conn).unwrap();
    record_acquisition(&mut fixture.conn, &profiles, &observation).unwrap();
    provider_economics::cover_acquisition(&fixture.conn, &fixture.id, &observation).unwrap();
    provider_economics::cover_acquisition(&fixture.conn, &fixture.id, &observation).unwrap();
    let covered = fixture.summary();
    assert!(covered.cost_complete);
    assert_eq!(covered.known_cost_usd, 0.01);
    assert_eq!(covered.acquisition_latency_ms, 120.0);
    let (uncovered, _) = observe_multi_call(
        &profiles,
        &fixture.spec,
        &versions,
        &outcomes,
        100.0,
        Some(2),
    )
    .unwrap();
    profiles.flush_dirty(&mut fixture.conn).unwrap();
    record_acquisition(&mut fixture.conn, &profiles, &uncovered).unwrap();
    let summary = fixture.summary();
    assert!(!summary.cost_complete);
    assert_eq!(summary.uncovered_profile_executions, 1);
    assert_eq!(summary.known_cost_usd, 0.02);
    assert_eq!(summary.acquisition_latency_ms, 220.0);
    assert!(profiles.begin_readmission(observation.key(), observation.runtime_version(), 3));
    profiles.flush_dirty(&mut fixture.conn).unwrap();
    assert_eq!(fixture.summary().known_cost_usd, 0.02);
}

#[test]
fn cohort_summary_does_not_scan_ordinary_deployment_history() {
    let fixture = Fixture::new(Phase::Deployment);
    let owner = fixture.spec.profile_key().unwrap().call_site_version;
    let mut query=fixture.conn.prepare("EXPLAIN QUERY PLAN SELECT owned_batches,known_cost_usd,unknown_cost_invocations FROM provider_economic_cohort WHERE owner_call_site_version=?1").unwrap();
    let plan = query
        .query_map([owner.as_str()], |row| row.get::<_, String>(3))
        .unwrap()
        .collect::<rusqlite::Result<Vec<_>>>()
        .unwrap();
    assert!(
        plan.iter()
            .any(|step| step.contains("SEARCH provider_economic_cohort")),
        "{plan:?}"
    );
    assert!(
        !plan.iter().any(|step| step.starts_with("SCAN")),
        "{plan:?}"
    );
}

fn billable_completion() -> Completion {
    let mut value = completion();
    value.cost_source = InferenceCostSource::Estimated;
    value.billing_scope_sha256 =
        Some(agentc_optimizer::provider_reconciliation::openrouter_scope());
    value.provider_generation_sha256 = Some("1".repeat(64));
    value.provider_request_id_sha256 = Some("2".repeat(64));
    value
}
impl Fixture {
    fn bill(&self, call: &Invocation, cost: f64) -> Value {
        let binding = agentc_optimizer::provider_reconciliation::bindings(&self.conn, &self.id)
            .unwrap()
            .into_iter()
            .find(|row| row.invocation_id == call.invocation_id)
            .unwrap();
        json!({"issuer":agentc_optimizer::provider_reconciliation::ISSUER,
            "receipt_sha256":binding.receipt_sha256,"billing_scope_sha256":binding.billing_scope_sha256,
            "generation_sha256":binding.generation_sha256,"request_id_sha256":binding.request_id_sha256,
            "model_id":"model","input_tokens":2,"output_tokens":1,"cost_usd":cost,
            "cancelled":false,"finish_reason":"stop"})
    }
    fn reserve_bill(
        &mut self,
        call: &Invocation,
        bill: &Value,
        attempt: u32,
    ) -> anyhow::Result<String> {
        self.command(
            json!({"operation":"begin_billing_lookup","batch_id":self.id,
            "invocation_id":call.invocation_id,"attempt_id":format!("{attempt:032x}"),
            "receipt_sha256":bill["receipt_sha256"]}),
        )
    }
    fn finish_bill(&mut self, bill: &Value, attempt: u32) -> anyhow::Result<String> {
        self.command(
            json!({"operation":"finish_billing_lookup","attempt_id":format!("{attempt:032x}"),
            "latency_ms":7.0,"invoice":bill}),
        )
    }
}

#[test]
fn verified_bill_settles_estimate_once_without_mutating_sdk_or_quality_evidence() {
    let mut fixture = Fixture::new(Phase::Acquisition);
    let call = fixture.invocation(0, Role::Primary);
    fixture.begin(&call);
    fixture.finish(&call, &billable_completion());
    fixture.seal(false, vec![call.position]);
    fixture.metrics(100.0);
    let raw: String = fixture
        .conn
        .query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let bill = fixture.bill(&call, 0.02);
    fixture.reserve_bill(&call, &bill, 10).unwrap();
    fixture.reserve_bill(&call, &bill, 10).unwrap();
    assert_eq!(fixture.summary().pending_billing_verifications, 1);
    fixture.finish_bill(&bill, 10).unwrap();
    fixture.finish_bill(&bill, 10).unwrap();
    let summary = fixture.summary();
    assert!(summary.cost_complete);
    assert_eq!(summary.known_cost_usd, 0.02);
    assert_eq!(summary.estimated_cost_usd, 0.01);
    assert_eq!(summary.acquisition_latency_ms, 107.0);
    assert_eq!(summary.pending_billing_verifications, 0);
    assert!(fixture.reserve_bill(&call, &bill, 10).is_err());
    let after: String = fixture
        .conn
        .query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(raw, after);
    for table in [
        "execution_plan_observation",
        "multi_call_comparison_spend",
        "provider_receipt_profile_publication",
    ] {
        assert_eq!(
            fixture
                .conn
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| row
                    .get::<_, u64>(0))
                .unwrap(),
            0
        );
    }
}

#[test]
fn failed_deployment_bill_remains_charged_and_known_lower_bound_cannot_decrease() {
    let mut fixture = Fixture::new(Phase::Deployment);
    let call = fixture.invocation(0, Role::Primary);
    let mut done = billable_completion();
    done.status = Status::Failed;
    done.cost_source = InferenceCostSource::ProviderReported;
    fixture.begin(&call);
    fixture.finish(&call, &done);
    fixture.seal(false, vec![call.position]);
    fixture.metrics(100.0);
    let mut bill = fixture.bill(&call, 0.005);
    fixture.reserve_bill(&call, &bill, 10).unwrap();
    assert!(fixture.finish_bill(&bill, 10).is_err());
    assert_eq!(fixture.summary().pending_billing_verifications, 1);
    assert_eq!(fixture.summary().known_cost_usd, 0.01);
    bill["cost_usd"] = json!(0.02);
    fixture.finish_bill(&bill, 10).unwrap();
    let summary = fixture.summary();
    assert!(summary.cost_complete);
    assert_eq!(summary.charged_invocations, 1);
    assert_eq!(summary.known_cost_usd, 0.02);
    assert_eq!(summary.acquisition_latency_ms, 17.0);
}

#[test]
fn mismatched_invoices_fail_atomically_and_failed_lookups_keep_original_hold() {
    for field in [
        "issuer",
        "receipt_sha256",
        "billing_scope_sha256",
        "generation_sha256",
        "request_id_sha256",
        "model_id",
        "input_tokens",
        "output_tokens",
        "cost_usd",
    ] {
        let mut fixture = Fixture::new(Phase::Acquisition);
        let call = fixture.invocation(0, Role::Primary);
        fixture.begin(&call);
        fixture.finish(&call, &billable_completion());
        fixture.seal(false, vec![call.position]);
        fixture.metrics(100.0);
        let mut bill = fixture.bill(&call, 0.02);
        fixture.reserve_bill(&call, &bill, 10).unwrap();
        bill[field] = match field {
            "input_tokens" | "output_tokens" => json!(99),
            "cost_usd" => json!(-1),
            _ => json!("9".repeat(64)),
        };
        assert!(fixture.finish_bill(&bill, 10).is_err(), "{field}");
        assert_eq!(fixture.summary().known_cost_usd, 0.0);
        assert_eq!(fixture.summary().pending_billing_verifications, 1);
        fixture.finish_bill(&Value::Null, 10).unwrap();
        fixture.finish_bill(&Value::Null, 10).unwrap();
        assert_eq!(fixture.summary().pending_billing_verifications, 0);
        assert_eq!(fixture.summary().acquisition_latency_ms, 107.0);
        assert_eq!(fixture.summary().unknown_cost_invocations, 1);
        assert!(!fixture.summary().cost_complete);
        assert_eq!(
            fixture
                .conn
                .query_row(
                    "SELECT COUNT(*) FROM provider_billing_settlement",
                    [],
                    |row| row.get::<_, u64>(0)
                )
                .unwrap(),
            0
        );
    }
}

#[test]
fn one_generation_bill_cannot_settle_two_physical_invocations() {
    let mut fixture = Fixture::new(Phase::Acquisition);
    let first = fixture.invocation(0, Role::Primary);
    let second = fixture.invocation(1, Role::Fallback);
    for call in [&first, &second] {
        fixture.begin(call);
        fixture.finish(call, &billable_completion());
    }
    fixture.seal(false, vec![first.position]);
    fixture.metrics(100.0);
    let bill = fixture.bill(&first, 0.02);
    fixture.reserve_bill(&first, &bill, 10).unwrap();
    fixture.finish_bill(&bill, 10).unwrap();
    let duplicate = fixture.bill(&second, 0.02);
    fixture.reserve_bill(&second, &duplicate, 11).unwrap();
    assert!(fixture.finish_bill(&duplicate, 11).is_err());
    assert_eq!(fixture.summary().known_cost_usd, 0.02);
    assert_eq!(fixture.summary().unknown_cost_invocations, 1);
}

#[test]
fn unknown_transport_multiple_sends_and_foreign_endpoint_cannot_request_billing_settlement() {
    for kind in ["transport", "attempts", "endpoint", "identity"] {
        let mut fixture = Fixture::new(Phase::Acquisition);
        let mut call = fixture.invocation(0, Role::Primary);
        let mut done = billable_completion();
        match kind {
            "transport" => {
                call.transport_verified = false;
                done.http_attempts = None;
            }
            "attempts" => done.http_attempts = Some(2),
            "endpoint" => done.billing_scope_sha256 = Some("9".repeat(64)),
            _ => done.provider_generation_sha256 = None,
        }
        fixture.begin(&call);
        fixture.finish(&call, &done);
        let bill = fixture.bill(&call, 0.02);
        assert!(fixture.reserve_bill(&call, &bill, 10).is_err(), "{kind}");
        assert_eq!(fixture.summary().pending_billing_verifications, 0);
        assert_eq!(fixture.summary().unknown_cost_invocations, 1);
    }
}

#[test]
fn billing_write_failure_and_restart_preserve_pending_hold_then_commit_once() {
    let mut fixture = Fixture::new(Phase::Acquisition);
    let call = fixture.invocation(0, Role::Primary);
    fixture.begin(&call);
    fixture.finish(&call, &billable_completion());
    fixture.seal(false, vec![call.position]);
    fixture.metrics(100.0);
    let bill = fixture.bill(&call, 0.02);
    fixture.reserve_bill(&call, &bill, 10).unwrap();
    fixture.conn.execute_batch("CREATE TRIGGER fail_billing BEFORE UPDATE ON provider_economic_cohort BEGIN SELECT RAISE(ABORT,'billing write failed'); END;").unwrap();
    assert!(fixture.finish_bill(&bill, 10).is_err());
    assert_eq!(
        fixture
            .conn
            .query_row(
                "SELECT COUNT(*) FROM provider_billing_settlement",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("cost.db");
    fixture
        .conn
        .execute("VACUUM INTO ?1", [path.to_str().unwrap()])
        .unwrap();
    fixture.conn = Connection::open(&path).unwrap();
    assert_eq!(fixture.summary().pending_billing_verifications, 1);
    assert!(!fixture.summary().cost_complete);
    fixture
        .conn
        .execute_batch("DROP TRIGGER fail_billing;")
        .unwrap();
    assert_eq!(
        agentc_optimizer::provider_reconciliation::recover_pending(&mut fixture.conn).unwrap(),
        1
    );
    assert_eq!(
        agentc_optimizer::provider_reconciliation::recover_pending(&mut fixture.conn).unwrap(),
        0
    );
    fixture.conn = Connection::open(&path).unwrap();
    fixture.finish_bill(&bill, 10).unwrap();
    assert!(fixture.summary().cost_complete);
    assert_eq!(fixture.summary().known_cost_usd, 0.02);
    assert_eq!(fixture.summary().acquisition_latency_ms, 107.0);
}

#[test]
fn settlement_does_not_clear_foreign_ownership_or_missing_receipt_coverage() {
    for kind in ["owner", "observer"] {
        let mut fixture = Fixture::new(Phase::Acquisition);
        let mut call = fixture.invocation(0, Role::Primary);
        if kind == "owner" {
            call.logical_plan_key.as_mut().unwrap().execution_plan_id =
                agentc_optimizer::ExecutionPlanId::parse("9".repeat(64)).unwrap();
        }
        fixture.begin(&call);
        fixture.finish(&call, &billable_completion());
        fixture.seal(kind == "observer", vec![call.position]);
        fixture.metrics(100.0);
        let bill = fixture.bill(&call, 0.02);
        fixture.reserve_bill(&call, &bill, 10).unwrap();
        fixture.finish_bill(&bill, 10).unwrap();
        assert_eq!(fixture.summary().known_cost_usd, 0.02);
        assert!(!fixture.summary().cost_complete, "{kind}");
    }
}

#[test]
fn invoice_cannot_reprice_completed_reported_cost_without_profile_invalidation() {
    let mut fixture = Fixture::new(Phase::Acquisition);
    let call = fixture.invocation(0, Role::Primary);
    let mut done = billable_completion();
    done.cost_source = InferenceCostSource::ProviderReported;
    fixture.begin(&call);
    fixture.finish(&call, &done);
    fixture.seal(false, vec![call.position]);
    fixture.metrics(100.0);
    let bill = fixture.bill(&call, 0.02);
    fixture.reserve_bill(&call, &bill, 10).unwrap();
    assert!(fixture.finish_bill(&bill, 10).is_err());
    assert_eq!(fixture.summary().known_cost_usd, 0.01);
    assert!(!fixture.summary().cost_complete);
    fixture.finish_bill(&Value::Null, 10).unwrap();
    assert!(fixture.summary().cost_complete);
    assert_eq!(fixture.summary().known_cost_usd, 0.01);
}

#[test]
fn invoice_uniqueness_survives_a_new_application_cohort_and_preserves_old_spend() {
    let mut fixture = Fixture::new(Phase::Acquisition);
    let first_owner = fixture.spec.profile_key().unwrap().call_site_version;
    let first = fixture.invocation(0, Role::Primary);
    fixture.begin(&first);
    fixture.finish(&first, &billable_completion());
    fixture.seal(false, vec![first.position]);
    fixture.metrics(100.0);
    let bill = fixture.bill(&first, 0.02);
    fixture.reserve_bill(&first, &bill, 10).unwrap();
    fixture.finish_bill(&bill, 10).unwrap();
    fixture.id = "8".repeat(32);
    fixture.spec.application_version = "9".repeat(64);
    provider_economics::enroll_batch(
        &mut fixture.conn,
        &fixture.id,
        &fixture.spec,
        Phase::Acquisition,
    )
    .unwrap();
    let mut second = fixture.invocation(0, Role::Primary);
    second.invocation_id = "9".repeat(32);
    fixture.begin(&second);
    fixture.finish(&second, &billable_completion());
    fixture.seal(false, vec![second.position]);
    fixture.metrics(100.0);
    let reused = fixture.bill(&second, 0.02);
    fixture.reserve_bill(&second, &reused, 11).unwrap();
    assert!(fixture.finish_bill(&reused, 11).is_err());
    assert_eq!(fixture.summary().known_cost_usd, 0.0);
    assert_eq!(fixture.summary().unknown_cost_invocations, 1);
    let old = provider_economics::summarize_cohort(&fixture.conn, &first_owner).unwrap();
    assert!(old.cost_complete);
    assert_eq!(old.known_cost_usd, 0.02);
    assert_eq!(old.acquisition_latency_ms, 107.0);
}

#[test]
fn recovered_historical_tariffs_cannot_supply_current_snapshot_lower_bounds() {
    use agentc_optimizer::provider_tariff;
    let mut fixture = completed_tariff_fixture();
    let owner = fixture.spec.profile_key().unwrap().call_site_version;
    let (raw, done): (String, String) = fixture
        .conn
        .query_row(
            "SELECT invocation_json,completion_json FROM provider_receipt_invocation",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    let mut historical = provider_tariff::snapshot();
    historical["observed_date"] = json!("2026-10-11");
    historical["nanodollars_per_token"]["gpt-4o-mini"]["default"] = json!([1500, 750, 6000]);
    let body = agentc_core::storage::canonical_json(&historical);
    let old_sha = agentc_core::storage::content_hash(&body);
    fixture
        .conn
        .execute(
            "INSERT INTO provider_tariff_policy VALUES(?1,?2)",
            [&old_sha, &body],
        )
        .unwrap();
    let mut inv: Invocation = serde_json::from_str(&raw).unwrap();
    inv.tariff_declaration.as_mut().unwrap().snapshot_sha256 = old_sha.clone();
    let raw = agentc_core::storage::canonical_json(&serde_json::to_value(&inv).unwrap());
    fixture
        .conn
        .execute(
            "UPDATE provider_receipt_invocation SET invocation_json=?1",
            [&raw],
        )
        .unwrap();
    let stored: String = fixture
        .conn
        .query_row(
            "SELECT result_json FROM provider_tariff_invocation",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let mut stored: Value = serde_json::from_str(&stored).unwrap();
    stored["bound"]["snapshot_sha256"] = json!(old_sha);
    stored["bound"]["interval"] = json!({"lower_nanodollars":7500,"upper_nanodollars":9000});
    stored["receipt_sha256"] = json!(agentc_core::storage::content_hash(
        &agentc_core::storage::canonical_json(&json!({
            "version":"provider-billing-binding-v1", "batch_id":fixture.id,
            "invocation_id":inv.invocation_id, "invocation":raw, "completion":done
        }))
    ));
    fixture.conn.execute("UPDATE provider_tariff_invocation SET result_json=?1,lower_nanodollars=7500,upper_nanodollars=9000", [stored.to_string()]).unwrap();
    fixture.conn.execute_batch("UPDATE provider_tariff_cohort SET lower_nanodollars=7500,upper_nanodollars=9000; DROP TABLE provider_tariff_coverage; DROP TABLE provider_tariff_coverage_cohort;").unwrap();
    provider_tariff::recover(&mut fixture.conn, owner.as_str()).unwrap();
    let summary = fixture.tariff_summary();
    assert_eq!(summary.upper_nanodollars, None);
    assert_eq!(
        summary.lower_nanodollars, 0,
        "historical charges cannot be labeled with current snapshot: {summary:?}"
    );
    assert_eq!(summary.observed_charged_lower_nanodollars, 7500);
    assert_eq!(summary.bounded_observed_upper_nanodollars, 9000);
    let diagnostic = provider_tariff::report(&mut fixture.conn, &fixture.id).unwrap();
    assert_eq!(diagnostic["lower_nanodollars"], 0);
    assert!(diagnostic["upper_nanodollars"].is_null());
    assert_eq!(diagnostic["acquisition"]["lower_nanodollars"], 0);
    assert!(diagnostic["acquisition"]["upper_nanodollars"].is_null());
    assert_eq!(
        diagnostic["acquisition"]["observed_charged_lower_nanodollars"],
        7500
    );
}

#[test]
fn structural_only_coverage_upgrade_is_atomic_and_requires_pricing_recovery() {
    use agentc_optimizer::provider_tariff;
    let mut fixture = completed_tariff_fixture();
    let owner = fixture.spec.profile_key().unwrap().call_site_version;
    let expected = fixture.tariff_summary();
    fixture.conn.execute_batch(
        "DROP TABLE provider_tariff_coverage; DROP TABLE provider_tariff_coverage_cohort;
         CREATE TABLE provider_tariff_coverage(batch_id TEXT PRIMARY KEY,owner TEXT NOT NULL,complete INTEGER NOT NULL);
         CREATE TABLE provider_tariff_coverage_cohort(owner TEXT PRIMARY KEY,batches INTEGER NOT NULL DEFAULT 0,incomplete_batches INTEGER NOT NULL DEFAULT 0,snapshot_sha256 TEXT NOT NULL);",
    ).unwrap();
    fixture
        .conn
        .execute(
            "INSERT INTO provider_tariff_coverage VALUES(?1,?2,1)",
            [&fixture.id, owner.as_str()],
        )
        .unwrap();
    fixture
        .conn
        .execute(
            "INSERT INTO provider_tariff_coverage_cohort VALUES(?1,1,0,?2)",
            [owner.as_str(), &provider_tariff::snapshot_sha256()],
        )
        .unwrap();
    fixture.conn.execute_batch("CREATE TRIGGER upgrade_fault BEFORE UPDATE ON provider_tariff_coverage_cohort BEGIN SELECT RAISE(ABORT,'upgrade fault'); END;").unwrap();
    assert!(provider_tariff::ensure_schema(&fixture.conn).is_err());
    assert!(fixture.conn.is_autocommit());
    for (table, column) in [
        ("provider_tariff_coverage", "pricing_compatible"),
        ("provider_tariff_coverage_cohort", "incompatible_batches"),
    ] {
        assert!(!fixture
            .conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM pragma_table_info(?1) WHERE name=?2)",
                [table, column],
                |r| r.get::<_, bool>(0)
            )
            .unwrap());
    }
    fixture
        .conn
        .execute_batch("DROP TRIGGER upgrade_fault;")
        .unwrap();
    let held = fixture.tariff_summary();
    assert!(!held.pricing_complete);
    assert_eq!(held.upper_nanodollars, None);
    assert_eq!(held.lower_nanodollars, 0);
    assert_eq!(held.observed_charged_lower_nanodollars, 750);
    provider_tariff::recover(&mut fixture.conn, owner.as_str()).unwrap();
    assert_eq!(fixture.tariff_summary(), expected);
    provider_tariff::recover(&mut fixture.conn, owner.as_str()).unwrap();
    assert_eq!(fixture.tariff_summary(), expected);
}
