use agentc_optimizer::execution_plan::{
    CachePolicy, ExecutionPlanSpec, RewriteOrdering, ValidationPolicy,
};
use agentc_optimizer::multi_call::{
    CallPosition, InferenceCostSource, MultiCallBranch, MultiCallPlanSpec, MultiCallRuntimeBranch,
    MultiCallTotals, EXACT_MAP_COMPARISON_VERSION, PREPARED_PROVIDER_BATCH_VERSION,
};
use agentc_optimizer::plan_profile::{
    CallSiteVersion, PlanProfileKey, PlanProfileUpdate, PlanProfiles, PlanRuntimeVersion,
};
use agentc_optimizer::provider_economics::{self, Phase};
use agentc_optimizer::provider_execution;
use agentc_optimizer::provider_interval_profiles::{self, Observation, Window};
use agentc_optimizer::provider_publication::{
    self, ComparisonInput, DeferredEvidence, PublicationInput, ScalarProvenance,
};
use agentc_optimizer::provider_receipts::{self, Completion, Invocation, Role, Status};
use agentc_optimizer::provider_tariff::{self, Declaration, Interval};
use agentc_optimizer::schema::ensure_cost_model_schema;
use rusqlite::Connection;
use serde_json::json;

struct Fixture {
    conn: Connection,
    profiles: PlanProfiles,
    spec: MultiCallPlanSpec,
    protocol: String,
    model: String,
    endpoint: String,
    input_fingerprint: String,
    output_fingerprint: String,
    observed_at_base: i64,
    wall_latency_ms: f64,
    provider_reported: bool,
}

impl Fixture {
    fn new(anthropic: bool) -> Self {
        let conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        provider_publication::ensure_schema(&conn).unwrap();
        provider_tariff::register(&conn).unwrap();
        let (protocol, model, url) = if anthropic {
            (
                "anthropic.messages.v1",
                "claude-haiku-4-5-20251001",
                "https://api.anthropic.com",
            )
        } else {
            (
                "openai.chat.completions.v1",
                "gpt-4o-mini-2024-07-18",
                "https://api.openai.com/v1",
            )
        };
        let spec = MultiCallPlanSpec {
            application_version: "a".repeat(64),
            callback_version: "interval-profile-fixture".into(),
            item_count: 1,
            max_workers: 1,
            comparison_version: Some(EXACT_MAP_COMPARISON_VERSION.into()),
            execution_contract: Some(PREPARED_PROVIDER_BATCH_VERSION.into()),
            branches: vec![MultiCallBranch {
                position: CallPosition { item: 0, call: 0 },
                call_site_version: CallSiteVersion::parse("c".repeat(64)).unwrap(),
                plan: ExecutionPlanSpec {
                    schema_version: 1,
                    provider_protocol: protocol.into(),
                    requested_model_id: model.into(),
                    target_model_id: model.into(),
                    target_model_revision: Some(model.into()),
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
        Self {
            conn,
            profiles: PlanProfiles::with_window(2),
            spec,
            protocol: protocol.into(),
            model: model.into(),
            endpoint: agentc_core::storage::content_hash(&format!("billing-scope-v1\0{url}")),
            input_fingerprint: "2".repeat(64),
            output_fingerprint: "3".repeat(64),
            observed_at_base: 100,
            wall_latency_ms: 10.0,
            provider_reported: false,
        }
    }

    fn two_items(anthropic: bool) -> Self {
        let mut fixture = Self::new(anthropic);
        let mut second = fixture.spec.branches[0].clone();
        second.position.item = 1;
        second.call_site_version = CallSiteVersion::parse("d".repeat(64)).unwrap();
        fixture.spec.branches.push(second);
        fixture.spec.item_count = 2;
        fixture
    }

    fn reserve(
        &mut self,
        n: u32,
        supported: bool,
        revision: &str,
        size: usize,
    ) -> (String, PlanRuntimeVersion) {
        let id = format!("{n:032x}");
        provider_economics::enroll_batch(&mut self.conn, &id, &self.spec, Phase::Acquisition)
            .unwrap();
        let mut runtimes = Vec::new();
        let mut requests = Vec::new();
        let mut provenance = Vec::new();
        for branch in &self.spec.branches {
            let key = PlanProfileKey {
                call_site_version: branch.call_site_version.clone(),
                execution_plan_id: branch.plan.execution_plan_id().unwrap(),
            };
            let runtime = PlanRuntimeVersion {
                provider_protocol: self.protocol.clone(),
                target_model_id: self.model.clone(),
                target_model_version: self.model.clone(),
                price_table_version: revision.into(),
            };
            let invocation = Invocation {
                invocation_id: format!(
                    "{:032x}",
                    u64::from(n) * 1000 + u64::from(branch.position.item)
                ),
                position: branch.position,
                ordinal: 0,
                role: Role::Primary,
                provider_protocol: self.protocol.clone(),
                requested_model_id: self.model.clone(),
                request_sha256: Some("e".repeat(64)),
                logical_request_sha256: Some("e".repeat(64)),
                transport_sha256: Some("f".repeat(64)),
                logical_plan_key: Some(key.clone()),
                logical_runtime: Some(runtime.clone()),
                dispatch_plan_key: Some(key),
                dispatch_runtime: Some(runtime.clone()),
                parent_plan_key: None,
                transport_verified: true,
                tariff_declaration: Some(Declaration {
                    version: provider_tariff::VERSION.into(),
                    snapshot_sha256: provider_tariff::snapshot_sha256(),
                    endpoint_sha256: Some(self.endpoint.clone()),
                    request_scope_supported: supported,
                }),
            };
            let usage = if self.protocol.starts_with("openai") {
                json!({"provider_protocol":self.protocol,"input_tokens":2,"output_tokens":1,"total_tokens":3,"input_details":null,"output_details":null,"service_tier":"default","unrecognized_fields":false})
            } else {
                json!({"provider_protocol":self.protocol,"input_tokens":2,"output_tokens":1,"cache_creation_input_tokens":3,"cache_read_input_tokens":4,"cache_creation":null,"server_tool_use":null,"service_tier":"standard","inference_geo":"global","unrecognized_fields":false})
            };
            let completion = Completion {
                status: Status::Completed,
                model_version: Some(self.model.clone()),
                input_tokens: Some(2),
                output_tokens: Some(1),
                cost_usd: Some(0.01),
                cost_source: if self.provider_reported {
                    InferenceCostSource::ProviderReported
                } else {
                    InferenceCostSource::Estimated
                },
                latency_ms: 10.0,
                http_attempts: Some(1),
                exception_type: None,
                billing_scope_sha256: Some(self.endpoint.clone()),
                provider_generation_sha256: None,
                provider_request_id_sha256: None,
                billing_usage: Some(
                    serde_json::from_value(
                        json!({"version":"provider-billing-usage-v1","usage":usage}),
                    )
                    .unwrap(),
                ),
            };
            for command in [
                json!({"operation":"begin_call","batch_id":id,"invocation":invocation}),
                json!({"operation":"finish_call","batch_id":id,"invocation_id":invocation.invocation_id,"completion":completion}),
            ] {
                provider_receipts::handle_json(&mut self.conn, &command.to_string()).unwrap();
            }
            runtimes.push(MultiCallRuntimeBranch {
                position: branch.position,
                runtime,
            });
            requests.push((branch.position, "e".repeat(64)));
            provenance.push(ScalarProvenance {
                position: branch.position,
                cost_source: if self.provider_reported {
                    InferenceCostSource::ProviderReported
                } else {
                    InferenceCostSource::Estimated
                },
                observation: None,
            });
        }
        let positions: Vec<_> = self.spec.branches.iter().map(|b| b.position).collect();
        for command in [
            json!({"operation":"seal_batch","batch_id":id,"logical_calls":positions,"persistence_failed":false}),
            json!({"operation":"finish_batch_economics","batch_id":id,"wall_latency_ms":self.wall_latency_ms}),
        ] {
            provider_receipts::handle_json(&mut self.conn, &command.to_string()).unwrap();
        }
        let count = self.spec.branches.len() as u32;
        let runtime = agentc_optimizer::multi_call::profile_runtime(&self.spec, &runtimes).unwrap();
        let key = self.spec.profile_key().unwrap();
        let mut input = PublicationInput {
            version: "provider-profile-publication-v1".into(),
            journal_sha256: provider_publication::journal_fingerprint(&self.conn, &id).unwrap(),
            predecessor: self.profiles.publication_boundary(&key),
            retained_window_size: size,
            update: PlanProfileUpdate {
                cost_evidence: agentc_optimizer::cost_evidence::CostEvidence {
                    source: if self.provider_reported {
                        agentc_optimizer::cost_evidence::CostSource::ProviderReported
                    } else {
                        agentc_optimizer::cost_evidence::CostSource::Estimated
                    },
                    physical_invocations: vec![],
                },
                key: key.clone(),
                runtime_version: runtime.clone(),
                input_tokens: 2 * count,
                output_tokens: count,
                latency_ms: self.wall_latency_ms,
                cost_usd: 0.01 * f64::from(count),
                output_is_structured: false,
                output_is_short: true,
                divergence: None,
                dispatch_fallback: false,
                observed_at_us: Some(self.observed_at_base + i64::from(n)),
            },
            totals: MultiCallTotals {
                input_tokens: 2 * count,
                output_tokens: count,
                cost_usd: 0.01 * f64::from(count),
                wall_latency_ms: self.wall_latency_ms,
                summed_call_latency_ms: 10.0 * f64::from(count),
                call_count: count,
            },
            charge_acquisition: true,
            original_evidence: None,
            comparison: Some(ComparisonInput {
                reference_plan_id: {
                    let mut reference = self.spec.clone();
                    reference.max_workers = 1;
                    reference.profile_key().unwrap().execution_plan_id
                },
                max_workers: self.spec.max_workers,
                input_fingerprint: self.input_fingerprint.clone(),
                output_fingerprint: self.output_fingerprint.clone(),
            }),
            deferred_evidence: Some(DeferredEvidence {
                spec: self.spec.clone(),
                runtimes,
                request_fingerprints: requests,
                scalar_provenance: provenance,
            }),
        };
        if self.provider_reported {
            input.original_evidence = input.deferred_evidence.take();
        }
        provider_publication::reserve(&mut self.conn, &id, &format!("receipt-{n}"), &input)
            .unwrap();
        (id, runtime)
    }

    fn publish(&mut self, id: &str) -> Observation {
        provider_interval_profiles::publish(&mut self.conn, id)
            .unwrap()
            .unwrap()
    }
    fn window(&self, runtime: &PlanRuntimeVersion, size: usize) -> Option<Window> {
        provider_interval_profiles::window(
            &self.conn,
            &self.spec.profile_key().unwrap(),
            runtime,
            size,
        )
        .unwrap()
    }
}

fn original_pair(
    f: &mut Fixture,
    n: u32,
    revision: &str,
    size: usize,
    at: i64,
) -> provider_execution::Pair {
    f.spec.max_workers = 1;
    f.wall_latency_ms = 20.0;
    let (reference, _) = f.reserve(n, true, revision, size);
    f.publish(&reference);
    let r = provider_execution::register(&f.conn, &reference)
        .unwrap()
        .unwrap();
    if f.provider_reported {
        provider_publication::publish(&mut f.conn, &f.profiles, &reference).unwrap();
    }
    f.spec.max_workers = 2;
    f.wall_latency_ms = 10.0;
    let (candidate, _) = f.reserve(n + 1, true, revision, size);
    f.publish(&candidate);
    let c = provider_execution::register(&f.conn, &candidate)
        .unwrap()
        .unwrap();
    if f.provider_reported {
        provider_publication::publish(&mut f.conn, &f.profiles, &candidate).unwrap();
    }
    provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, at).unwrap()
}

fn selected_workers(
    f: &mut Fixture,
    guard: &agentc_optimizer::plan_guard::PlanGuard,
    runtime: &PlanRuntimeVersion,
    tariff: bool,
    now_us: i64,
) -> anyhow::Result<u32> {
    let policy = agentc_optimizer::SelectionPolicy {
        objective: agentc_optimizer::execution_plan::SelectionObjective::Latency,
        now_us,
        ..Default::default()
    };
    selected_workers_with_policy(f, guard, runtime, tariff, &policy)
}

fn selected_workers_with_policy(
    f: &mut Fixture,
    guard: &agentc_optimizer::plan_guard::PlanGuard,
    runtime: &PlanRuntimeVersion,
    tariff: bool,
    policy: &agentc_optimizer::SelectionPolicy,
) -> anyhow::Result<u32> {
    f.spec.max_workers = 1;
    if tariff {
        agentc_optimizer::provider_tariff_admission::select(
            &mut f.conn,
            guard,
            agentc_optimizer::provider_tariff_admission::Request {
                reference: &f.spec,
                runtime,
                max_workers: 2,
                remaining_batches: 100000,
                window_size: f.profiles.retained_window_size(),
                policy,
                tariff: &agentc_optimizer::provider_tariff_admission::Policy {
                    quantity: provider_tariff::QUANTITY.into(),
                    snapshot_sha256: provider_tariff::snapshot_sha256(),
                    max_expected_nanodollars: None,
                },
            },
        )
        .map(|s| s.max_workers)
    } else {
        agentc_optimizer::prepared_batch::select_prepared_schedule(
            &mut f.conn,
            &f.profiles,
            guard,
            agentc_optimizer::prepared_batch::PreparedScheduleRequest {
                reference: &f.spec,
                runtime,
                max_workers: 2,
                remaining_batches: 100000,
                policy,
            },
        )
        .map(|s| s.max_workers)
    }
}

#[test]
fn original_runtime_changes_retire_legacy_disables_and_require_fresh_pairs_on_return() {
    for tariff in [false, true] {
        let mut f = Fixture::two_items(false);
        f.provider_reported = true;
        f.profiles = PlanProfiles::with_window(50);
        let old = original_pair(&mut f, 1, "r1", 50, 200);
        let guard = agentc_optimizer::plan_guard::PlanGuard::new();
        for id in ["a", "b"] {
            guard
                .record_validation_rejection(
                    &old.key,
                    &old.runtime_version,
                    0.0,
                    &id.repeat(32),
                    250,
                )
                .unwrap();
        }
        guard.flush_dirty(&mut f.conn).unwrap();
        f.observed_at_base = 1000;
        let mut current = old.clone();
        for count in 1..=20 {
            current = original_pair(&mut f, 2 * count + 1, "r2", 50, 1200 + i64::from(count));
            assert_eq!(
                selected_workers(&mut f, &guard, &current.runtime_version, tariff, 1300).unwrap(),
                if count < 20 { 1 } else { 2 },
                "tariff={tariff}, new runtime pair {count}"
            );
        }
        assert_eq!(
            guard.get(&current.key).unwrap().runtime_version,
            current.runtime_version
        );
        assert!(guard.get(&current.key).unwrap().disabled.is_none());
        assert!(agentc_optimizer::provider_quality::window(
            &f.conn,
            &old.key,
            &old.runtime_version,
            50,
            1300
        )
        .unwrap()
        .is_none());
        for count in 1..=20 {
            current = original_pair(&mut f, 2 * count + 41, "r1", 50, 1400 + i64::from(count));
            assert_eq!(
                selected_workers(&mut f, &guard, &current.runtime_version, tariff, 1500).unwrap(),
                if count < 20 { 1 } else { 2 },
                "tariff={tariff}, returning runtime pair {count}"
            );
        }
        let quality = agentc_optimizer::provider_quality::window(
            &f.conn,
            &current.key,
            &current.runtime_version,
            50,
            1500,
        )
        .unwrap()
        .unwrap();
        assert_eq!(quality.pairs.len(), 20);
        assert_eq!(quality.lifetime_pairs, 41);
        assert_eq!(
            provider_tariff::summarize_acquisition(&f.conn, &old.key.call_site_version)
                .unwrap()
                .upper_nanodollars,
            Some(147600)
        );
        assert!(
            (provider_economics::summarize_cohort(&f.conn, &old.key.call_site_version)
                .unwrap()
                .known_cost_usd
                - 1.64)
                .abs()
                < 1e-9
        );
        let restored = agentc_optimizer::plan_guard::PlanGuard::new();
        restored.warm_from_db(&f.conn).unwrap();
        assert_eq!(
            selected_workers(&mut f, &restored, &current.runtime_version, tariff, 1500).unwrap(),
            2
        );
    }
}

#[test]
fn legacy_validation_cooldown_requires_twenty_fresh_original_pairs() {
    for tariff in [false, true] {
        let mut f = Fixture::two_items(false);
        f.provider_reported = true;
        f.profiles = PlanProfiles::with_window(50);
        let old = original_pair(&mut f, 1, "r1", 50, 200);
        let guard = agentc_optimizer::plan_guard::PlanGuard::new();
        for id in ["a", "b"] {
            guard
                .record_validation_rejection(
                    &old.key,
                    &old.runtime_version,
                    0.0,
                    &id.repeat(32),
                    250,
                )
                .unwrap();
        }
        guard.flush_dirty(&mut f.conn).unwrap();
        let reenable = 250 + agentc_optimizer::plan_guard::PLAN_DISABLE_COOLDOWN_US;
        f.observed_at_base = reenable;
        for count in 1..=20 {
            original_pair(
                &mut f,
                count * 2 + 1,
                "r1",
                50,
                reenable + 200 + i64::from(count),
            );
            assert_eq!(
                selected_workers(&mut f, &guard, &old.runtime_version, tariff, reenable + 300)
                    .unwrap(),
                if count < 20 { 1 } else { 2 },
                "tariff={tariff}, pair {count}"
            );
        }
        assert!(guard.get(&old.key).unwrap().disabled.is_none());
        assert_eq!(guard.get(&old.key).unwrap().validation_rejection_count(), 0);
        let quality = agentc_optimizer::provider_quality::window(
            &f.conn,
            &old.key,
            &old.runtime_version,
            50,
            reenable + 300,
        )
        .unwrap()
        .unwrap();
        assert_eq!(quality.pairs.len(), 20);
        assert!(quality.evidence_boundary_us >= reenable);
    }
}

#[test]
fn original_readmission_constraints_and_failed_guard_flush_preserve_the_boundary() {
    for tariff in [false, true] {
        let mut f = Fixture::two_items(false);
        f.provider_reported = true;
        f.profiles = PlanProfiles::with_window(50);
        let old = original_pair(&mut f, 1, "r1", 50, 200);
        let guard = agentc_optimizer::plan_guard::PlanGuard::new();
        for id in ["a", "b"] {
            guard
                .record_validation_rejection(
                    &old.key,
                    &old.runtime_version,
                    0.0,
                    &id.repeat(32),
                    250,
                )
                .unwrap();
        }
        guard.flush_dirty(&mut f.conn).unwrap();
        let reenable = 250 + agentc_optimizer::plan_guard::PLAN_DISABLE_COOLDOWN_US;
        f.observed_at_base = reenable;
        for count in 1..=20 {
            original_pair(
                &mut f,
                count * 2 + 1,
                "r1",
                50,
                reenable + 200 + i64::from(count),
            );
        }
        let capped = agentc_optimizer::SelectionPolicy {
            objective: agentc_optimizer::execution_plan::SelectionObjective::Latency,
            now_us: reenable + 300,
            max_expected_latency_ms: Some(1.0),
            ..Default::default()
        };
        assert_eq!(
            selected_workers_with_policy(&mut f, &guard, &old.runtime_version, tariff, &capped)
                .unwrap(),
            1
        );
        assert!(guard.get(&old.key).unwrap().disabled.is_some());
        f.conn.execute_batch("CREATE TRIGGER guard_flush_fault BEFORE DELETE ON execution_plan_disabled BEGIN SELECT RAISE(ABORT,'guard flush fault'); END;").unwrap();
        assert!(
            selected_workers(&mut f, &guard, &old.runtime_version, tariff, reenable + 300).is_err()
        );
        assert_eq!(guard.dirty_len(), 1);
        assert_eq!(
            f.conn
                .query_row("SELECT COUNT(*) FROM execution_plan_disabled", [], |r| r
                    .get::<_, u64>(0))
                .unwrap(),
            1
        );
        let quality = agentc_optimizer::provider_quality::window(
            &f.conn,
            &old.key,
            &old.runtime_version,
            50,
            reenable + 300,
        )
        .unwrap()
        .unwrap();
        assert_eq!(quality.pairs.len(), 20);
        assert_eq!(quality.evidence_boundary_us, reenable);
        f.conn
            .execute_batch("DROP TRIGGER guard_flush_fault;")
            .unwrap();
        let restored = agentc_optimizer::plan_guard::PlanGuard::new();
        restored.warm_from_db(&f.conn).unwrap();
        assert_eq!(
            selected_workers(
                &mut f,
                &restored,
                &old.runtime_version,
                tariff,
                reenable + 300
            )
            .unwrap(),
            2
        );
        assert!(restored.get(&old.key).unwrap().disabled.is_none());
        assert_eq!(
            f.conn
                .query_row("SELECT COUNT(*) FROM execution_plan_disabled", [], |r| r
                    .get::<_, u64>(0))
                .unwrap(),
            0
        );
        let restored_again = agentc_optimizer::plan_guard::PlanGuard::new();
        restored_again.warm_from_db(&f.conn).unwrap();
        assert_eq!(
            selected_workers(
                &mut f,
                &restored_again,
                &old.runtime_version,
                tariff,
                reenable + 300
            )
            .unwrap(),
            2
        );
        assert_eq!(
            provider_tariff::summarize_acquisition(&f.conn, &old.key.call_site_version)
                .unwrap()
                .upper_nanodollars,
            Some(75600)
        );
    }
}

#[test]
fn original_quality_window_size_changes_require_new_pairs_and_preserve_exposure() {
    for tariff in [false, true] {
        let mut f = Fixture::two_items(false);
        f.provider_reported = true;
        f.profiles = PlanProfiles::with_window(50);
        let guard = agentc_optimizer::plan_guard::PlanGuard::new();
        let mut last = None;
        for n in (1..=39).step_by(2) {
            let pair = original_pair(&mut f, n, "r1", 50, 200 + i64::from(n));
            let token = |id: &str| {
                f.conn
                    .query_row(
                        "SELECT token FROM provider_original_usd_comparison WHERE execution_id=?1",
                        [id],
                        |row| row.get::<_, String>(0),
                    )
                    .unwrap()
            };
            let (reference, candidate) = (
                token(&pair.reference_execution_id),
                token(&pair.candidate_execution_id),
            );
            agentc_optimizer::multi_call_comparison::compare_executions(
                &mut f.conn,
                &f.profiles,
                &guard,
                &reference,
                &candidate,
                pair.observed_at_us,
            )
            .unwrap();
            last = Some(pair);
        }
        let old = last.unwrap();
        assert_eq!(
            selected_workers(&mut f, &guard, &old.runtime_version, tariff, 300).unwrap(),
            2
        );
        f.profiles = PlanProfiles::with_window(25);
        f.profiles.warm_from_db(&f.conn).unwrap();
        assert_eq!(
            f.profiles
                .get(&old.key, &old.runtime_version)
                .unwrap()
                .paired_observations,
            20
        );
        assert_eq!(
            selected_workers(&mut f, &guard, &old.runtime_version, tariff, 300).unwrap(),
            1
        );
        for count in 1..=20 {
            original_pair(&mut f, count * 2 + 39, "r1", 25, 400 + i64::from(count));
            assert_eq!(
                selected_workers(&mut f, &guard, &old.runtime_version, tariff, 500).unwrap(),
                if count < 20 { 1 } else { 2 }
            );
        }
        let quality = agentc_optimizer::provider_quality::window(
            &f.conn,
            &old.key,
            &old.runtime_version,
            25,
            500,
        )
        .unwrap()
        .unwrap();
        assert_eq!(quality.pairs.len(), 20);
        assert_eq!(quality.lifetime_pairs, 40);
        assert!(agentc_optimizer::provider_quality::window(
            &f.conn,
            &old.key,
            &old.runtime_version,
            50,
            500
        )
        .unwrap()
        .is_none());
        // A new window contract does not retire the runtime's damage budget.
        f.spec.max_workers = 1;
        let (r, _) = f.reserve(81, true, "r1", 25);
        let r = provider_execution::register(&f.conn, &r).unwrap().unwrap();
        f.publish(&r.batch_id);
        f.spec.max_workers = 2;
        f.output_fingerprint = "4".repeat(64);
        let (c, _) = f.reserve(82, true, "r1", 25);
        let c = provider_execution::register(&f.conn, &c).unwrap().unwrap();
        f.publish(&c.batch_id);
        provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 600).unwrap();
        f.spec.max_workers = 2;
        let (next, _) = f.reserve(83, true, "r1", 30);
        f.publish(&next);
        let changed = agentc_optimizer::provider_quality::window(
            &f.conn,
            &old.key,
            &old.runtime_version,
            30,
            700,
        )
        .unwrap()
        .unwrap();
        assert!(changed.pairs.is_empty());
        assert_eq!(changed.lifetime_pairs, 41);
        assert_eq!(changed.divergence_exposure, 1.0);
        assert!(changed.disabled.is_some());
        assert_eq!(
            provider_tariff::summarize_acquisition(&f.conn, &old.key.call_site_version)
                .unwrap()
                .upper_nanodollars,
            Some(149400)
        );
    }
}

#[test]
fn usd_mapping_upgrade_preserves_retirement_proofs_without_pinning_rolling_bindings() {
    let mut f = Fixture::two_items(false);
    f.provider_reported = true;
    f.profiles = PlanProfiles::with_window(2);
    let old = original_pair(&mut f, 1, "r1", 2, 200);
    let mappings = |conn: &Connection| {
        conn.prepare(
            "SELECT token,execution_id FROM provider_original_usd_comparison ORDER BY token",
        )
        .unwrap()
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
        .unwrap()
        .collect::<rusqlite::Result<Vec<_>>>()
        .unwrap()
    };
    let saved = mappings(&f.conn);
    assert_eq!(saved.len(), 2);
    f.conn.execute_batch("ALTER TABLE provider_original_usd_comparison RENAME TO old_mapping;
        CREATE TABLE provider_original_usd_comparison(token TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE,
            FOREIGN KEY(execution_id) REFERENCES provider_original_execution(execution_id),
            FOREIGN KEY(token) REFERENCES multi_call_comparison_execution(token));
        INSERT INTO provider_original_usd_comparison SELECT * FROM old_mapping;
        DROP TABLE old_mapping;").unwrap();
    // A nested caller can roll back the entire upgrade.
    {
        let tx = f.conn.unchecked_transaction().unwrap();
        provider_execution::ensure_schema(&tx).unwrap();
        assert_eq!(mappings(&tx), saved);
    }
    assert_eq!(f.conn.query_row("SELECT COUNT(*) FROM pragma_foreign_key_list('provider_original_usd_comparison') WHERE \"table\"='multi_call_comparison_execution'", [], |r|r.get::<_, u64>(0)).unwrap(), 1);
    provider_execution::ensure_schema(&f.conn).unwrap();
    provider_execution::ensure_schema(&f.conn).unwrap();
    assert_eq!(mappings(&f.conn), saved);
    for n in [3, 5] {
        original_pair(&mut f, n, "r1", 2, 200 + i64::from(n));
    }
    assert_eq!(mappings(&f.conn).len(), 6);
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM multi_call_comparison_execution",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        4
    );
    assert_eq!(
        f.conn
            .query_row("SELECT COUNT(*) FROM pragma_foreign_key_check", [], |r| r
                .get::<_, u64>(
                0
            ))
            .unwrap(),
        0
    );
    assert!(provider_execution::compare(
        &mut f.conn,
        &old.reference_execution_id,
        &old.candidate_execution_id,
        300
    )
    .is_err());
    assert_eq!(
        provider_tariff::summarize_acquisition(&f.conn, &old.key.call_site_version)
            .unwrap()
            .upper_nanodollars,
        Some(10800)
    );
}

#[test]
fn original_quality_window_preserves_lifetime_and_independent_exposure_after_eviction() {
    let mut f = Fixture::two_items(false);
    let first = original_pair(&mut f, 1, "r1", 2, 200);
    for n in [3, 5] {
        original_pair(&mut f, n, "r1", 2, 200 + i64::from(n));
    }
    let quality = agentc_optimizer::provider_quality::window(
        &f.conn,
        &first.key,
        &first.runtime_version,
        2,
        210,
    )
    .unwrap()
    .unwrap();
    assert_eq!(quality.lifetime_pairs, 3);
    assert_eq!(quality.pairs.len(), 2);
    assert!(!quality.pairs.iter().any(|p| p.pair_id == first.pair_id));
    assert_eq!(quality.divergence_upper_p95, Some(0.0));
    assert_eq!(quality.last_execution_at_us, Some(105));
    assert_eq!(quality.divergence_exposure, 0.0);
    assert!(quality.disabled.is_none());
    assert_eq!(
        f.conn
            .query_row("SELECT COUNT(*) FROM execution_plan_profile", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        0
    );
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("quality.db");
    f.conn
        .execute("VACUUM INTO ?1", [path.to_str().unwrap()])
        .unwrap();
    f.conn = Connection::open(path).unwrap();
    assert_eq!(
        agentc_optimizer::provider_quality::window(
            &f.conn,
            &first.key,
            &first.runtime_version,
            2,
            210
        )
        .unwrap()
        .unwrap(),
        quality
    );
}

#[test]
fn original_quality_projection_fault_rolls_back_the_pair_and_all_derived_state() {
    let mut f = Fixture::two_items(false);
    let (reference, _) = f.reserve(1, true, "r1", 2);
    let r = provider_execution::register(&f.conn, &reference)
        .unwrap()
        .unwrap();
    f.spec.max_workers = 2;
    let (candidate, _) = f.reserve(2, true, "r1", 2);
    let c = provider_execution::register(&f.conn, &candidate)
        .unwrap()
        .unwrap();
    agentc_optimizer::provider_quality::ensure_schema(&f.conn).unwrap();
    f.conn.execute_batch("CREATE TRIGGER quality_fault BEFORE INSERT ON provider_original_quality_event BEGIN SELECT RAISE(ABORT,'quality fault'); END;").unwrap();
    assert!(
        provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 200).is_err()
    );
    for table in [
        "provider_original_execution_pair",
        "provider_original_quality_event",
        "provider_original_quality_state",
    ] {
        assert_eq!(
            f.conn
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                    .get::<_, u64>(0))
                .unwrap(),
            0
        );
    }
    f.conn.execute_batch("DROP TRIGGER quality_fault;").unwrap();
    let pair =
        provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 201).unwrap();
    assert_eq!(
        provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 202).unwrap(),
        pair
    );
    assert_eq!(
        agentc_optimizer::provider_quality::window(
            &f.conn,
            &pair.key,
            &pair.runtime_version,
            2,
            202
        )
        .unwrap()
        .unwrap()
        .lifetime_pairs,
        1
    );
}

#[test]
fn tariff_admission_uses_original_quality_and_conservative_complete_economics() {
    use agentc_optimizer::provider_tariff_admission::{self, Policy, Request};
    use agentc_optimizer::{
        execution_plan::SelectionObjective, plan_guard::PlanGuard, SelectionPolicy,
    };
    for reported in [false, true] {
        let mut f = Fixture::two_items(false);
        f.provider_reported = reported;
        f.profiles = PlanProfiles::with_window(50);
        let mut last = None;
        for n in (1..=39).step_by(2) {
            last = Some(original_pair(&mut f, n, "r1", 50, 200 + i64::from(n)));
        }
        let last = last.unwrap();
        f.spec.max_workers = 1;
        let policy = SelectionPolicy {
            objective: SelectionObjective::Latency,
            now_us: 250,
            ..Default::default()
        };
        let tariff = Policy {
            quantity: provider_tariff::QUANTITY.into(),
            snapshot_sha256: provider_tariff::snapshot_sha256(),
            max_expected_nanodollars: Some(4000),
        };
        let guard = PlanGuard::new();
        let select = |f: &mut Fixture, policy: &SelectionPolicy, tariff: &Policy, remaining| {
            provider_tariff_admission::select(
                &mut f.conn,
                &guard,
                Request {
                    reference: &f.spec,
                    runtime: &last.runtime_version,
                    max_workers: 2,
                    remaining_batches: remaining,
                    window_size: 50,
                    policy,
                    tariff,
                },
            )
            .unwrap()
        };
        let schedule = select(&mut f, &policy, &tariff, 10000);
        assert_eq!(schedule.max_workers, 2);
        assert!(!schedule.selected_reference);
        let estimate = schedule.alternatives[0].estimate.as_ref().unwrap();
        assert_eq!(estimate.quality.pairs.len(), 20);
        assert!(estimate.net_savings_lower_nanodollars < 0);
        assert!(estimate.net_latency_savings_ms > 0.0);
        assert_eq!(estimate.expected_upper_nanodollars, 1808);
        assert_eq!(
            schedule.acquisition_accounting.upper_nanodollars,
            Some(72000)
        );
        let mut cost = policy.clone();
        cost.objective = SelectionObjective::Cost;
        assert!(select(&mut f, &cost, &tariff, 10000).selected_reference);
        assert!(select(&mut f, &policy, &tariff, 1).selected_reference);
        let mut cap = tariff.clone();
        cap.max_expected_nanodollars = Some(1807);
        assert!(select(&mut f, &policy, &cap, 10000).selected_reference);
        let mut stale = policy.clone();
        stale.now_us = 1000;
        stale.max_profile_age_us = 100;
        assert!(select(&mut f, &stale, &tariff, 10000).selected_reference);
        if reported {
            assert_eq!(
                f.conn
                    .query_row("SELECT COUNT(*) FROM multi_call_comparison_pair", [], |r| r
                        .get::<_, u64>(0))
                    .unwrap(),
                0
            );
            let paid = agentc_optimizer::prepared_batch::select_prepared_schedule(
                &mut f.conn,
                &f.profiles,
                &guard,
                agentc_optimizer::prepared_batch::PreparedScheduleRequest {
                    reference: &f.spec,
                    runtime: &last.runtime_version,
                    max_workers: 2,
                    remaining_batches: 10000,
                    policy: &policy,
                },
            )
            .unwrap();
            assert_eq!(paid.max_workers, 2);
            assert_eq!(
                f.conn
                    .query_row(
                        "SELECT SUM(n_paired_observations) FROM execution_plan_profile",
                        [],
                        |r| r.get::<_, u64>(0)
                    )
                    .unwrap(),
                0
            );
        }
        let (unknown, _) = f.reserve(41, false, "r1", 50);
        f.publish(&unknown);
        assert!(select(&mut f, &policy, &tariff, 10000).selected_reference);
    }
}

#[test]
fn original_damage_requires_twenty_fresh_pairs_after_cooldown_without_erasing_spend() {
    use agentc_optimizer::provider_tariff_admission::{self, Policy, Request};
    use agentc_optimizer::{
        execution_plan::SelectionObjective, plan_guard::PlanGuard, SelectionPolicy,
    };
    let mut f = Fixture::two_items(false);
    f.profiles = PlanProfiles::with_window(50);
    f.spec.max_workers = 1;
    let (reference, _) = f.reserve(1, true, "r1", 50);
    let r = provider_execution::register(&f.conn, &reference)
        .unwrap()
        .unwrap();
    f.publish(&reference);
    f.spec.max_workers = 2;
    f.output_fingerprint = "4".repeat(64);
    let (candidate, _) = f.reserve(2, true, "r1", 50);
    let c = provider_execution::register(&f.conn, &candidate)
        .unwrap()
        .unwrap();
    f.publish(&candidate);
    let bad =
        provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 200).unwrap();
    f.output_fingerprint = "3".repeat(64);
    let reenable = 200 + agentc_optimizer::plan_guard::PLAN_DISABLE_COOLDOWN_US;
    let q = agentc_optimizer::provider_quality::window(
        &f.conn,
        &bad.key,
        &bad.runtime_version,
        50,
        reenable,
    )
    .unwrap()
    .unwrap();
    assert!(q.disabled.is_some());
    assert_eq!(q.divergence_exposure, 0.0);
    assert!(q.pairs.is_empty());
    f.observed_at_base = reenable;
    let guard = PlanGuard::new();
    let tariff = Policy {
        quantity: provider_tariff::QUANTITY.into(),
        snapshot_sha256: provider_tariff::snapshot_sha256(),
        max_expected_nanodollars: None,
    };
    for count in 1..=20 {
        original_pair(
            &mut f,
            count * 2 + 1,
            "r1",
            50,
            reenable + 200 + i64::from(count),
        );
        f.spec.max_workers = 1;
        let policy = SelectionPolicy {
            objective: SelectionObjective::Latency,
            now_us: reenable + 300,
            ..Default::default()
        };
        let schedule = provider_tariff_admission::select(
            &mut f.conn,
            &guard,
            Request {
                reference: &f.spec,
                runtime: &bad.runtime_version,
                max_workers: 2,
                remaining_batches: 100000,
                window_size: 50,
                policy: &policy,
                tariff: &tariff,
            },
        )
        .unwrap();
        assert_eq!(schedule.selected_reference, count < 20, "{count}");
    }
    let q = agentc_optimizer::provider_quality::window(
        &f.conn,
        &bad.key,
        &bad.runtime_version,
        50,
        reenable + 300,
    )
    .unwrap()
    .unwrap();
    assert_eq!(q.lifetime_pairs, 21);
    assert_eq!(q.pairs.len(), 20);
    assert!(q.disabled.is_none());
    assert_eq!(
        provider_tariff::summarize_acquisition(&f.conn, &bad.key.call_site_version)
            .unwrap()
            .upper_nanodollars,
        Some(75600)
    );
}

#[test]
fn original_intervals_publish_once_without_bills_quality_or_usd_profiles() {
    for anthropic in [false, true] {
        let mut fixture = Fixture::new(anthropic);
        let (id, runtime) = fixture.reserve(1, true, "r1", 2);
        let result: serde_json::Value = serde_json::from_str(
            &provider_publication::publish(&mut fixture.conn, &fixture.profiles, &id).unwrap(),
        )
        .unwrap();
        assert_eq!(result["status"], "pending_billing");
        let first = fixture.publish(&id);
        assert_eq!(fixture.publish(&id), first);
        let interval = if anthropic {
            Interval {
                lower_nanodollars: 11150,
                upper_nanodollars: 13400,
            }
        } else {
            Interval {
                lower_nanodollars: 750,
                upper_nanodollars: 900,
            }
        };
        assert_eq!(first.bound.as_ref().unwrap().interval, interval);
        let window = fixture.window(&runtime, 2).unwrap();
        assert_eq!(window.mean_interval, Some(interval));
        assert_eq!(window.lifetime_observations, 1);
        assert_eq!(
            first
                .original_comparison
                .as_ref()
                .unwrap()
                .output_fingerprint,
            "3".repeat(64)
        );
        for table in [
            "execution_plan_profile",
            "multi_call_comparison_execution",
            "multi_call_comparison_pair",
            "multi_call_comparison_spend",
            "provider_billing_settlement",
        ] {
            assert_eq!(
                fixture
                    .conn
                    .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                        .get::<_, u64>(0))
                    .unwrap(),
                0,
                "{table}"
            );
        }
        assert_eq!(fixture.conn.query_row("SELECT json_extract(completion_json,'$.cost_source') FROM provider_receipt_invocation",[],|r|r.get::<_,String>(0)).unwrap(),"estimated");
    }
}

#[test]
fn unknown_intervals_advance_the_window_without_erasing_lifetime_liabilities() {
    let mut fixture = Fixture::new(false);
    let mut runtime = None;
    for n in 1..=4 {
        let (id, observed) = fixture.reserve(n, n != 2, "r1", 2);
        fixture.publish(&id);
        runtime = Some(observed);
        let window = fixture.window(runtime.as_ref().unwrap(), 2).unwrap();
        assert_eq!(window.lifetime_observations, u64::from(n));
        assert_eq!(window.observations.len(), n.min(2) as usize);
        assert_eq!(window.mean_interval.is_some(), n == 1 || n == 4);
    }
    let owner = fixture.spec.profile_key().unwrap().call_site_version;
    assert_eq!(
        provider_tariff::summarize_acquisition(&fixture.conn, &owner)
            .unwrap()
            .upper_nanodollars,
        None
    );
    assert!(fixture.window(runtime.as_ref().unwrap(), 1).is_none());
}

#[test]
fn runtime_retirement_and_late_publication_never_rewarm_an_old_window() {
    let mut fixture = Fixture::new(false);
    let (first, v1) = fixture.reserve(1, true, "r1", 2);
    fixture.publish(&first);
    let (late, _) = fixture.reserve(2, true, "r1", 2);
    let (new, v2) = fixture.reserve(3, true, "r2", 2);
    fixture.publish(&new);
    assert!(fixture.window(&v1, 2).is_none());
    fixture.publish(&late);
    assert_eq!(fixture.window(&v2, 2).unwrap().observations.len(), 1);
    let (returned, _) = fixture.reserve(4, true, "r1", 2);
    fixture.publish(&returned);
    let window = fixture.window(&v1, 2).unwrap();
    assert_eq!(window.observations.len(), 1);
    assert_eq!(window.observations[0].batch_id, returned);
    assert_eq!(window.lifetime_observations, 4);
    let tx = fixture.conn.unchecked_transaction().unwrap();
    assert!(
        provider_interval_profiles::window(&tx, &fixture.spec.profile_key().unwrap(), &v1, 2)
            .unwrap()
            .is_some()
    );
    tx.commit().unwrap();
}

#[test]
fn failed_projection_is_atomic_and_restart_preserves_receipt_bindings() {
    let mut fixture = Fixture::new(false);
    let (id, runtime) = fixture.reserve(1, true, "r1", 2);
    provider_interval_profiles::ensure_schema(&fixture.conn).unwrap();
    provider_execution::ensure_schema(&fixture.conn).unwrap();
    fixture.conn.execute_batch("CREATE TRIGGER projection_fault BEFORE INSERT ON provider_tariff_plan_state BEGIN SELECT RAISE(ABORT,'projection fault'); END;").unwrap();
    assert!(provider_interval_profiles::publish(&mut fixture.conn, &id).is_err());
    assert_eq!(
        fixture
            .conn
            .query_row(
                "SELECT COUNT(*) FROM provider_tariff_plan_observation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    fixture
        .conn
        .execute_batch("DROP TRIGGER projection_fault;")
        .unwrap();
    assert_eq!(
        fixture
            .conn
            .query_row(
                "SELECT COUNT(*) FROM provider_original_execution",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    let first = fixture.publish(&id);
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("cost.db");
    fixture
        .conn
        .execute("VACUUM INTO ?1", [path.to_str().unwrap()])
        .unwrap();
    fixture.conn = Connection::open(&path).unwrap();
    assert_eq!(fixture.publish(&id), first);
    assert_eq!(
        fixture.window(&runtime, 2).unwrap().lifetime_observations,
        1
    );
    fixture.conn.execute("UPDATE provider_receipt_invocation SET completion_json=replace(completion_json,'estimated','unknown')",[]).unwrap();
    assert!(provider_interval_profiles::window(
        &fixture.conn,
        &fixture.spec.profile_key().unwrap(),
        &runtime,
        2
    )
    .is_err());
}

#[test]
fn original_pairs_are_independent_of_tariffs_bills_and_legacy_quality() {
    for anthropic in [false, true] {
        let mut f = Fixture::two_items(anthropic);
        let (reference, _) = f.reserve(1, true, "r1", 2);
        let r = provider_execution::register(&f.conn, &reference)
            .unwrap()
            .unwrap();
        assert_eq!(
            f.publish(&reference).original_execution_id,
            Some(r.execution_id.clone())
        );
        f.spec.max_workers = 2;
        let (candidate, _) = f.reserve(2, false, "r1", 2);
        let c = provider_execution::register(&f.conn, &candidate)
            .unwrap()
            .unwrap();
        assert!(f.publish(&candidate).bound.is_none());
        let pair = provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 200)
            .unwrap();
        assert_eq!(pair.divergence, 0.0);
        assert_eq!(
            provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 300)
                .unwrap(),
            pair
        );
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("original.db");
        f.conn
            .execute("VACUUM INTO ?1", [path.to_str().unwrap()])
            .unwrap();
        f.conn = Connection::open(path).unwrap();
        assert_eq!(
            provider_execution::register(&f.conn, &reference).unwrap(),
            Some(r.clone())
        );
        assert_eq!(
            provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 400)
                .unwrap(),
            pair
        );
        for table in [
            "execution_plan_profile",
            "multi_call_comparison_execution",
            "multi_call_comparison_pair",
            "provider_billing_settlement",
        ] {
            assert_eq!(
                f.conn
                    .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                        .get::<_, u64>(0))
                    .unwrap(),
                0
            );
        }
        let (another, _) = f.reserve(3, true, "r1", 2);
        let other = provider_execution::register(&f.conn, &another)
            .unwrap()
            .unwrap();
        assert!(provider_execution::compare(
            &mut f.conn,
            &r.execution_id,
            &other.execution_id,
            400
        )
        .is_err());
        assert_eq!(
            f.conn
                .query_row(
                    "SELECT COUNT(*) FROM provider_original_execution_pair",
                    [],
                    |r| r.get::<_, u64>(0)
                )
                .unwrap(),
            1
        );
    }
}

#[test]
fn original_pair_mismatches_and_missing_originals_fail_without_consuming_results() {
    for mismatch in ["input", "runtime", "reference", "time", "missing", "same"] {
        let mut f = Fixture::two_items(false);
        let (reference, _) = f.reserve(1, true, "r1", 2);
        let r = provider_execution::register(&f.conn, &reference)
            .unwrap()
            .unwrap();
        f.spec.max_workers = if mismatch == "reference" { 1 } else { 2 };
        if mismatch == "input" {
            f.input_fingerprint = "4".repeat(64);
        }
        let (candidate, _) = f.reserve(2, true, if mismatch == "runtime" { "r2" } else { "r1" }, 2);
        let c = provider_execution::register(&f.conn, &candidate)
            .unwrap()
            .unwrap();
        let candidate_id = match mismatch {
            "missing" => "f".repeat(64),
            "same" => r.execution_id.clone(),
            _ => c.execution_id,
        };
        assert!(
            provider_execution::compare(
                &mut f.conn,
                &r.execution_id,
                &candidate_id,
                if mismatch == "time" { 101 } else { 200 }
            )
            .is_err(),
            "{mismatch}"
        );
        assert_eq!(
            f.conn
                .query_row(
                    "SELECT COUNT(*) FROM provider_original_execution_pair",
                    [],
                    |r| r.get::<_, u64>(0)
                )
                .unwrap(),
            0
        );
    }
}

#[test]
fn original_divergent_pair_write_fault_is_atomic_and_receipt_tampering_is_rejected() {
    let mut f = Fixture::two_items(false);
    let (reference, _) = f.reserve(1, true, "r1", 2);
    let r = provider_execution::register(&f.conn, &reference)
        .unwrap()
        .unwrap();
    f.spec.max_workers = 2;
    f.output_fingerprint = "4".repeat(64);
    let (candidate, _) = f.reserve(2, true, "r1", 2);
    let c = provider_execution::register(&f.conn, &candidate)
        .unwrap()
        .unwrap();
    f.conn.execute_batch("CREATE TRIGGER pair_fault BEFORE INSERT ON provider_original_execution_pair BEGIN SELECT RAISE(ABORT,'pair fault'); END;").unwrap();
    assert!(
        provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 200).is_err()
    );
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_original_execution_pair",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    f.conn.execute_batch("DROP TRIGGER pair_fault;").unwrap();
    let pair =
        provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 200).unwrap();
    assert_eq!(pair.divergence, 1.0);
    f.conn.execute("UPDATE provider_receipt_invocation SET completion_json=replace(completion_json,'estimated','unknown') WHERE batch_id=?1", [&candidate]).unwrap();
    assert!(
        provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 300).is_err()
    );
    assert!(provider_execution::register(&f.conn, &candidate).is_err());
}

#[test]
fn original_source_order_window_and_runtime_retirement_cannot_be_rewarmed() {
    for changed in ["window", "runtime"] {
        let mut f = Fixture::two_items(false);
        let (reference, _) = f.reserve(1, true, "r1", 2);
        let r = provider_execution::register(&f.conn, &reference)
            .unwrap()
            .unwrap();
        f.spec.max_workers = 2;
        let (old, _) = f.reserve(2, true, "r1", 2);
        let (late, _) = f.reserve(3, true, "r1", 2);
        let (new, _) = f.reserve(
            4,
            true,
            if changed == "runtime" { "r2" } else { "r1" },
            if changed == "window" { 1 } else { 2 },
        );
        provider_execution::register(&f.conn, &new).unwrap();
        provider_execution::register(&f.conn, &late).unwrap();
        let old = provider_execution::register(&f.conn, &old)
            .unwrap()
            .unwrap();
        assert!(
            provider_execution::compare(&mut f.conn, &r.execution_id, &old.execution_id, 200)
                .is_err(),
            "{changed}"
        );
        assert_eq!(f.conn.query_row("SELECT lifetime_executions FROM provider_original_execution_state WHERE execution_plan_id=?1", [old.key.execution_plan_id.as_str()], |r|r.get::<_,u64>(0)).unwrap(),3);
    }
    let mut f = Fixture::two_items(false);
    let (reference, _) = f.reserve(1, true, "r1", 2);
    let r = provider_execution::register(&f.conn, &reference)
        .unwrap()
        .unwrap();
    f.spec.max_workers = 2;
    let (candidate, _) = f.reserve(2, true, "r1", 2);
    let c = provider_execution::register(&f.conn, &candidate)
        .unwrap()
        .unwrap();
    for n in 3..=4 {
        let (id, _) = f.reserve(n, true, "r1", 2);
        provider_execution::register(&f.conn, &id).unwrap();
    }
    assert!(
        provider_execution::compare(&mut f.conn, &r.execution_id, &c.execution_id, 200).is_err()
    );
}
