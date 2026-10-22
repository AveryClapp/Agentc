use agentc_optimizer::execution_plan::{
    CachePolicy, ExecutionPlanSpec, RewriteOrdering, ValidationPolicy,
};
use agentc_optimizer::multi_call::{
    observe_multi_call, CallPosition, InferenceCostSource, MultiCallBranch, MultiCallOutcome,
    MultiCallPlanSpec, MultiCallRuntimeBranch, EXACT_MAP_COMPARISON_VERSION,
};
use agentc_optimizer::multi_call_comparison::{
    compare_executions, record_acquisition, register_execution, replay_pairs,
};
use agentc_optimizer::plan_guard::{PlanGuard, PlanGuardDecision};
use agentc_optimizer::plan_profile::{CallSiteVersion, PlanProfiles, PlanRuntimeVersion};
use agentc_optimizer::schema::ensure_cost_model_schema;
use agentc_optimizer::Outcome;
use rusqlite::Connection;

struct Fixture {
    conn: Connection,
    profiles: PlanProfiles,
    guard: PlanGuard,
    spec: MultiCallPlanSpec,
    versions: Vec<MultiCallRuntimeBranch>,
    outcomes: Vec<MultiCallOutcome>,
    time: i64,
}

impl Fixture {
    fn new(window: u32) -> Self {
        let conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        let mut branches = Vec::new();
        let mut versions = Vec::new();
        let mut outcomes = Vec::new();
        for item in 0..2 {
            let position = CallPosition { item, call: 0 };
            branches.push(MultiCallBranch {
                position,
                call_site_version: CallSiteVersion::parse(format!("{item}").repeat(64)).unwrap(),
                plan: ExecutionPlanSpec {
                    schema_version: 1,
                    provider_protocol: "openai.chat.completions.v1".into(),
                    requested_model_id: format!("model-{item}"),
                    target_model_id: format!("model-{item}"),
                    target_model_revision: Some("declared-v1".into()),
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
            });
            versions.push(MultiCallRuntimeBranch {
                position,
                runtime: PlanRuntimeVersion {
                    provider_protocol: "openai.chat.completions.v1".into(),
                    target_model_id: format!("model-{item}"),
                    target_model_version: format!("provider-{item}-v1"),
                    price_table_version: "prices-v1".into(),
                },
            });
            outcomes.push(MultiCallOutcome {
                position,
                cost_source: InferenceCostSource::ProviderReported,
                outcome: Outcome {
                    cost_source: Default::default(),
                    physical_invocations: Vec::new(),
                    input_tokens: 2,
                    output_tokens: 1,
                    cost_usd: 0.01,
                    latency_ms: 5.0,
                    inference_cost_complete: Some(true),
                    ..Outcome::default()
                },
            });
        }
        Self {
            conn,
            profiles: PlanProfiles::with_window(window),
            guard: PlanGuard::new(),
            spec: MultiCallPlanSpec {
                application_version: "release-v1".into(),
                callback_version: "callback-v1".into(),
                item_count: 2,
                max_workers: 2,
                branches,
                comparison_version: Some(EXACT_MAP_COMPARISON_VERSION.into()),
                execution_contract: None,
            },
            versions,
            outcomes,
            time: 1,
        }
    }

    fn run(&mut self, workers: u32, input: &str, output: &str) -> String {
        let mut spec = self.spec.clone();
        spec.max_workers = workers;
        let (observation, _) = observe_multi_call(
            &self.profiles,
            &spec,
            &self.versions,
            &self.outcomes,
            if workers == 1 { 30.0 } else { 20.0 },
            Some(self.time),
        )
        .unwrap();
        self.time += 1;
        self.profiles.flush_dirty(&mut self.conn).unwrap();
        record_acquisition(&mut self.conn, &self.profiles, &observation).unwrap();
        record_acquisition(&mut self.conn, &self.profiles, &observation).unwrap();
        if spec.execution_contract.is_some() {
            self.account(&spec, &observation, if workers == 1 { 30.0 } else { 20.0 });
        }
        register_execution(
            &mut self.conn,
            &self.profiles,
            &spec,
            &observation,
            input.repeat(64),
            output.repeat(64),
        )
        .unwrap()
    }

    fn account(
        &mut self,
        spec: &MultiCallPlanSpec,
        observation: &agentc_optimizer::PlanObservationToken,
        wall: f64,
    ) {
        use agentc_optimizer::{provider_economics, provider_receipts};
        use provider_receipts::{Completion, Invocation, Role, Status};
        let id = format!("{:032x}", self.time);
        provider_economics::enroll_batch(
            &mut self.conn,
            &id,
            spec,
            provider_economics::Phase::Acquisition,
        )
        .unwrap();
        for (branch, outcome) in spec.branches.iter().zip(&self.outcomes) {
            let runtime = self.versions[branch.position.item as usize].runtime.clone();
            let key = agentc_optimizer::PlanProfileKey {
                call_site_version: branch.call_site_version.clone(),
                execution_plan_id: branch.plan.execution_plan_id().unwrap(),
            };
            let invocation = Invocation {
                invocation_id: format!("{:016x}{:016x}", self.time, branch.position.item),
                position: branch.position,
                ordinal: 0,
                role: Role::Primary,
                provider_protocol: runtime.provider_protocol.clone(),
                requested_model_id: runtime.target_model_id.clone(),
                request_sha256: Some("e".repeat(64)),
                logical_request_sha256: Some("e".repeat(64)),
                transport_sha256: Some("f".repeat(64)),
                logical_plan_key: Some(key.clone()),
                logical_runtime: Some(runtime.clone()),
                dispatch_plan_key: Some(key),
                dispatch_runtime: Some(runtime.clone()),
                parent_plan_key: None,
                transport_verified: true,
                tariff_declaration: None,
            };
            let completion = Completion {
                status: Status::Completed,
                model_version: Some(runtime.target_model_version),
                input_tokens: Some(outcome.outcome.input_tokens),
                output_tokens: Some(outcome.outcome.output_tokens),
                cost_usd: Some(outcome.outcome.cost_usd),
                cost_source: InferenceCostSource::ProviderReported,
                latency_ms: outcome.outcome.latency_ms,
                http_attempts: Some(1),
                exception_type: None,
                billing_scope_sha256: None,
                provider_generation_sha256: None,
                provider_request_id_sha256: None,
                billing_usage: None,
            };
            for command in [
                serde_json::json!({"operation":"begin_call","batch_id":id,"invocation":invocation}),
                serde_json::json!({"operation":"finish_call","batch_id":id,"invocation_id":invocation.invocation_id,"completion":completion}),
            ] {
                provider_receipts::handle_json(&mut self.conn, &command.to_string()).unwrap();
            }
        }
        provider_receipts::handle_json(&mut self.conn,&serde_json::json!({"operation":"seal_batch","batch_id":id,"logical_calls":spec.branches.iter().map(|branch|branch.position).collect::<Vec<_>>()} ).to_string()).unwrap();
        provider_receipts::handle_json(&mut self.conn,&serde_json::json!({"operation":"finish_batch_economics","batch_id":id,"wall_latency_ms":wall}).to_string()).unwrap();
        provider_economics::cover_acquisition(&self.conn, &id, observation).unwrap();
    }

    fn pair(
        &mut self,
        reference: &str,
        candidate: &str,
    ) -> agentc_optimizer::multi_call_comparison::ComparisonResult {
        compare_executions(
            &mut self.conn,
            &self.profiles,
            &self.guard,
            reference,
            candidate,
            self.time,
        )
        .unwrap()
    }

    fn count(&self, table: &str) -> i64 {
        self.conn
            .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .unwrap()
    }

    fn profile(&self) -> agentc_optimizer::PlanProfile {
        self.profiles
            .get_for_reporting(&self.spec.profile_key().unwrap())
            .unwrap()
    }
}

#[test]
fn pair_binds_two_real_executions_and_charges_both_once() {
    let mut fixture = Fixture::new(50);
    let reference = fixture.run(1, "a", "b");
    let candidate = fixture.run(2, "a", "b");
    let pair = fixture.pair(&reference, &candidate);
    assert_eq!(pair.divergence, 0.0);
    assert_eq!(pair.acquisition_cost_usd, 0.04);
    assert_eq!(pair.acquisition_latency_ms, 50.0);
    assert_eq!(fixture.profile().paired_observations, 1);
    assert_eq!(
        fixture.pair(&reference, &candidate).status,
        "already_recorded"
    );
    assert_eq!(fixture.profile().n_paired_observations, 1);
    assert_eq!(
        replay_pairs(&mut fixture.conn, &fixture.profiles, &fixture.guard).unwrap(),
        0
    );
    assert_eq!(fixture.count("multi_call_comparison_pair"), 1);
    let total: (i64, f64) = fixture
        .conn
        .query_row(
            "SELECT completed_pairs,total_cost_usd FROM multi_call_comparison_acquisition",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(total, (1, 0.04));
    let spend: (i64, f64) = fixture
        .conn
        .query_row(
            "SELECT SUM(completed_executions),SUM(total_cost_usd) FROM multi_call_comparison_spend",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(spend, (2, 0.04));
}

#[test]
fn any_ordered_result_difference_is_a_whole_batch_breach() {
    let mut fixture = Fixture::new(50);
    let reference = fixture.run(1, "a", "b");
    let candidate = fixture.run(2, "a", "c");
    assert_eq!(fixture.pair(&reference, &candidate).divergence, 1.0);
    assert_eq!(fixture.profile().divergence_upper_p95, Some(1.0));
    assert!(matches!(
        fixture
            .guard
            .decision(&fixture.spec.profile_key().unwrap(), fixture.time),
        PlanGuardDecision::Disabled(_)
    ));
}

#[test]
fn mismatched_inputs_versions_schedules_unknown_receipts_and_reuse_abstain() {
    let mut fixture = Fixture::new(50);
    let reference = fixture.run(1, "a", "b");
    let candidate = fixture.run(2, "a", "b");
    let other_inputs = fixture.run(2, "c", "b");
    for (left, right) in [
        (&reference, &other_inputs),
        (&candidate, &reference),
        (&reference, &reference),
        (&reference, &"f".repeat(64)),
    ] {
        assert!(compare_executions(
            &mut fixture.conn,
            &fixture.profiles,
            &fixture.guard,
            left,
            right,
            fixture.time
        )
        .is_err());
    }
    assert_eq!(fixture.count("multi_call_comparison_pair"), 0);
    fixture.pair(&reference, &candidate);
    let next_candidate = fixture.run(2, "a", "b");
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &reference,
        &next_candidate,
        fixture.time
    )
    .is_err());
    fixture.versions[1].runtime.target_model_version = "second-branch-v2".into();
    let changed = fixture.run(2, "a", "b");
    let fresh_reference = fixture.run(1, "a", "b");
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &reference,
        &changed,
        fixture.time
    )
    .is_err());
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &fresh_reference,
        &candidate,
        fixture.time
    )
    .is_err());
    assert_eq!(fixture.profile().paired_observations, 0);
}

#[test]
fn changed_application_and_comparison_contracts_cannot_reuse_evidence() {
    let mut fixture = Fixture::new(50);
    let reference = fixture.run(1, "a", "b");
    fixture.spec.application_version = "release-v2".into();
    let candidate = fixture.run(2, "a", "b");
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &reference,
        &candidate,
        fixture.time
    )
    .is_err());
    let paired_key = fixture.spec.profile_key().unwrap();
    fixture.spec.comparison_version = None;
    assert_ne!(
        paired_key.execution_plan_id,
        fixture.spec.profile_key().unwrap().execution_plan_id
    );
    fixture.spec.comparison_version = Some("unknown-comparator".into());
    assert!(fixture.spec.profile_key().is_err());
}

#[test]
fn unpaired_and_incompatible_runs_keep_their_acquisition_spend_after_retirement() {
    let mut fixture = Fixture::new(2);
    let reference = fixture.run(1, "a", "b");
    let wrong_input = fixture.run(2, "c", "b");
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &reference,
        &wrong_input,
        fixture.time
    )
    .is_err());
    fixture.run(2, "a", "b");
    fixture.versions[1].runtime.target_model_version = "provider-second-v2".into();
    fixture.run(2, "a", "b");
    assert_eq!(fixture.profile().window_observations, 1);
    let total: (i64, f64) = fixture
        .conn
        .query_row(
            "SELECT SUM(completed_executions),SUM(total_cost_usd) FROM multi_call_comparison_spend",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(total, (4, 0.08));
    assert_eq!(fixture.count("multi_call_comparison_acquisition"), 0);
}

#[test]
fn edited_binding_and_evicted_receipt_cannot_create_a_pair() {
    let mut fixture = Fixture::new(2);
    let reference = fixture.run(1, "a", "b");
    let candidate = fixture.run(2, "a", "b");
    fixture.conn.execute("UPDATE multi_call_comparison_execution SET binding_json=json_set(binding_json,'$.output_fingerprint',?1) WHERE token=?2",rusqlite::params!["c".repeat(64),candidate]).unwrap();
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &reference,
        &candidate,
        fixture.time
    )
    .is_err());
    let candidate = fixture.run(2, "a", "b");
    fixture.run(1, "a", "b");
    fixture.run(1, "a", "b");
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &reference,
        &candidate,
        fixture.time
    )
    .is_err());
    assert_eq!(fixture.count("multi_call_comparison_pair"), 0);
}

#[test]
fn journal_survives_failed_profile_flush_and_restart_repairs_without_recharging() {
    let mut fixture = Fixture::new(2);
    let reference = fixture.run(1, "a", "b");
    let candidate = fixture.run(2, "a", "b");
    fixture.conn.execute_batch("CREATE TRIGGER fail_pair_profile BEFORE INSERT ON execution_plan_divergence_observation BEGIN SELECT RAISE(ABORT,'simulated interrupted profile flush'); END;").unwrap();
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &reference,
        &candidate,
        fixture.time
    )
    .is_err());
    assert_eq!(fixture.count("multi_call_comparison_pair"), 1);
    assert_eq!(fixture.count("execution_plan_divergence_observation"), 0);
    fixture
        .conn
        .execute_batch("DROP TRIGGER fail_pair_profile;")
        .unwrap();
    fixture.profiles = PlanProfiles::with_window(2);
    fixture.profiles.warm_from_db(&fixture.conn).unwrap();
    fixture.guard = PlanGuard::new();
    fixture.guard.warm_from_db(&fixture.conn).unwrap();
    assert_eq!(
        replay_pairs(&mut fixture.conn, &fixture.profiles, &fixture.guard).unwrap(),
        1
    );
    assert_eq!(
        fixture.pair(&reference, &candidate).status,
        "already_recorded"
    );
    assert_eq!(fixture.profile().n_paired_observations, 1);
    let total: f64 = fixture
        .conn
        .query_row(
            "SELECT total_cost_usd FROM multi_call_comparison_acquisition",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(total, 0.04);
}

#[test]
fn retirement_before_interrupted_flush_prevents_replay_from_counting_old_pairs() {
    let mut fixture = Fixture::new(2);
    for _ in 0..2 {
        let reference = fixture.run(1, "a", "b");
        let candidate = fixture.run(2, "a", "b");
        fixture.pair(&reference, &candidate);
    }
    let reference = fixture.run(1, "a", "b");
    let candidate = fixture.run(2, "a", "b");
    fixture.conn.execute_batch("CREATE TRIGGER fail_pair_profile BEFORE INSERT ON execution_plan_divergence_observation BEGIN SELECT RAISE(ABORT,'simulated flush failure'); END;").unwrap();
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &reference,
        &candidate,
        fixture.time
    )
    .is_err());
    assert_eq!(fixture.count("multi_call_comparison_pair"), 2);
    fixture
        .conn
        .execute_batch("DROP TRIGGER fail_pair_profile;")
        .unwrap();
    fixture.profiles = PlanProfiles::with_window(2);
    fixture.profiles.warm_from_db(&fixture.conn).unwrap();
    fixture.guard = PlanGuard::new();
    fixture.guard.warm_from_db(&fixture.conn).unwrap();
    assert_eq!(
        replay_pairs(&mut fixture.conn, &fixture.profiles, &fixture.guard).unwrap(),
        1
    );
    assert_eq!(
        replay_pairs(&mut fixture.conn, &fixture.profiles, &fixture.guard).unwrap(),
        0
    );
    assert_eq!(fixture.profile().n_paired_observations, 3);
    assert_eq!(fixture.profile().paired_observations, 2);
}

#[test]
fn reference_consumption_survives_retirement_of_its_pair() {
    let mut fixture = Fixture::new(3);
    let references: Vec<_> = (0..3).map(|_| fixture.run(1, "a", "b")).collect();
    for reference in references.iter().rev() {
        let candidate = fixture.run(2, "a", "b");
        fixture.pair(reference, &candidate);
    }
    let reference = fixture.run(1, "a", "b");
    let candidate = fixture.run(2, "a", "b");
    fixture.pair(&reference, &candidate);
    assert_eq!(fixture.count("multi_call_comparison_pair"), 3);
    let candidate = fixture.run(2, "a", "b");
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &references[2],
        &candidate,
        fixture.time
    )
    .is_err());
    assert_eq!(fixture.profile().n_paired_observations, 4);
}

fn prepared_fixture() -> Fixture {
    let mut fixture = Fixture::new(50);
    fixture.spec.application_version = "a".repeat(64);
    fixture.spec.execution_contract =
        Some(agentc_optimizer::multi_call::PREPARED_PROVIDER_BATCH_VERSION.into());
    fixture
}

fn prepared_policy(now_us: i64) -> agentc_optimizer::SelectionPolicy {
    agentc_optimizer::SelectionPolicy {
        objective: agentc_optimizer::SelectionObjective::Latency,
        min_paired_observations: 20,
        now_us,
        max_profile_age_us: 1_000_000,
        ..Default::default()
    }
}

fn acquire_pairs(fixture: &mut Fixture, count: usize) {
    for _ in 0..count {
        let reference = fixture.run(1, "a", "b");
        let candidate = fixture.run(2, "a", "b");
        fixture.pair(&reference, &candidate);
    }
}

fn schedule(
    fixture: &mut Fixture,
    remaining: u64,
    policy: &agentc_optimizer::SelectionPolicy,
) -> agentc_optimizer::prepared_batch::PreparedSchedule {
    let mut reference = fixture.spec.clone();
    reference.max_workers = 1;
    let runtime = fixture.profile().runtime_version;
    agentc_optimizer::prepared_batch::select_prepared_schedule(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        agentc_optimizer::prepared_batch::PreparedScheduleRequest {
            reference: &reference,
            runtime: &runtime,
            max_workers: 2,
            remaining_batches: remaining,
            policy,
        },
    )
    .unwrap()
}

#[test]
fn prepared_scheduling_requires_twenty_real_pairs_and_charges_full_acquisition() {
    let mut fixture = prepared_fixture();
    acquire_pairs(&mut fixture, 19);
    let policy = prepared_policy(fixture.time);
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);
    acquire_pairs(&mut fixture, 1);
    let policy = prepared_policy(fixture.time);
    let selected = schedule(&mut fixture, 1000, &policy);
    assert!(!selected.selected_reference);
    assert_eq!(selected.max_workers, 2);
    assert!((selected.completed_acquisition_cost_usd - 0.8).abs() < 1e-12);
    assert_eq!(selected.completed_acquisition_latency_ms, 1000.0);
    assert!(schedule(&mut fixture, 1, &policy).selected_reference);
}

#[test]
fn prepared_selector_rejects_staleness_cost_latency_limits_and_disabled_guard() {
    let mut fixture = prepared_fixture();
    acquire_pairs(&mut fixture, 20);
    let mut policy = prepared_policy(fixture.time + 2);
    policy.max_profile_age_us = 1;
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);
    policy = prepared_policy(fixture.time);
    policy.max_expected_cost_usd = Some(0.019);
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);
    policy.max_expected_cost_usd = None;
    policy.max_expected_latency_ms = Some(19.0);
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);
    let reference = fixture.run(1, "a", "b");
    let bad = fixture.run(2, "a", "c");
    fixture.pair(&reference, &bad);
    let policy = prepared_policy(fixture.time);
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);
}

#[test]
fn arbitrary_callback_profiles_and_invalid_deployment_volume_cannot_admit_a_schedule() {
    let mut fixture = prepared_fixture();
    acquire_pairs(&mut fixture, 20);
    let mut reference = fixture.spec.clone();
    reference.max_workers = 1;
    let runtime = fixture.profile().runtime_version;
    let policy = prepared_policy(fixture.time);
    reference.execution_contract = None;
    assert!(agentc_optimizer::prepared_batch::select_prepared_schedule(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        agentc_optimizer::prepared_batch::PreparedScheduleRequest {
            reference: &reference,
            runtime: &runtime,
            max_workers: 2,
            remaining_batches: 1000,
            policy: &policy,
        }
    )
    .is_err());
    reference.execution_contract =
        Some(agentc_optimizer::multi_call::PREPARED_PROVIDER_BATCH_VERSION.into());
    assert!(agentc_optimizer::prepared_batch::select_prepared_schedule(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        agentc_optimizer::prepared_batch::PreparedScheduleRequest {
            reference: &reference,
            runtime: &runtime,
            max_workers: 2,
            remaining_batches: 0,
            policy: &policy,
        }
    )
    .is_err());
    fixture.versions[1].runtime.target_model_version = "changed-second-branch".into();
    fixture.run(2, "a", "b");
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);
}

#[test]
fn prepared_readmission_requires_a_fresh_cohort_and_retains_all_acquisition_spend() {
    let mut fixture = prepared_fixture();
    fixture.guard = PlanGuard::with_limits(0.1, 100, 10).unwrap();
    acquire_pairs(&mut fixture, 20);
    let delayed_reference = fixture.run(1, "a", "b");
    let delayed_candidate = fixture.run(2, "a", "b");
    let reference = fixture.run(1, "a", "b");
    let candidate = fixture.run(2, "a", "c");
    fixture.pair(&reference, &candidate);
    let policy = prepared_policy(fixture.time);
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);

    fixture.time = 200;
    let policy = prepared_policy(fixture.time);
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);
    assert_eq!(fixture.profile().paired_observations, 0);
    assert!(compare_executions(
        &mut fixture.conn,
        &fixture.profiles,
        &fixture.guard,
        &delayed_reference,
        &delayed_candidate,
        fixture.time,
    )
    .is_err());
    acquire_pairs(&mut fixture, 19);
    let policy = prepared_policy(fixture.time);
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);
    acquire_pairs(&mut fixture, 1);
    let policy = prepared_policy(fixture.time);
    let selected = schedule(&mut fixture, 1000, &policy);
    assert_eq!(selected.max_workers, 2, "{selected:?}");
    assert_eq!(fixture.profile().paired_observations, 20);
    assert!((selected.completed_acquisition_cost_usd - 1.68).abs() < 1e-12);
    assert!(matches!(
        fixture.guard.decision(&selected.selected_key, fixture.time),
        PlanGuardDecision::Allow
    ));
}

#[test]
fn prepared_limits_include_learning_and_unknown_owners_block_admission() {
    let mut fixture = prepared_fixture();
    acquire_pairs(&mut fixture, 20);
    let mut policy = prepared_policy(fixture.time);
    assert_eq!(schedule(&mut fixture, 1000, &policy).max_workers, 2);
    policy.max_expected_cost_usd = Some(0.02);
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);
    policy.max_expected_cost_usd = None;
    policy.max_expected_latency_ms = Some(20.0);
    assert!(schedule(&mut fixture, 1000, &policy).selected_reference);
    policy.max_expected_latency_ms = None;
    agentc_optimizer::provider_economics::enroll_batch(
        &mut fixture.conn,
        &"9".repeat(32),
        &fixture.spec,
        agentc_optimizer::provider_economics::Phase::Acquisition,
    )
    .unwrap();
    let pending = schedule(&mut fixture, 100000, &policy);
    assert!(pending.selected_reference);
    assert_eq!(
        pending.reason,
        agentc_optimizer::execution_plan::SelectionReason::AcquisitionCostsIncomplete
    );
    assert_eq!(pending.acquisition_accounting.incomplete_batches, 1);
    assert!((pending.completed_acquisition_cost_usd - 0.8).abs() < 1e-12);
}
