use agentc_optimizer::execution_plan::{
    CachePolicy, ExecutionPlanSpec, RewriteOrdering, ValidationPolicy,
};
use agentc_optimizer::multi_call::InferenceCostSource;
use agentc_optimizer::multi_call::{CallPosition, MultiCallBranch, MultiCallPlanSpec};
use agentc_optimizer::plan_profile::{CallSiteVersion, PlanRuntimeVersion};
use agentc_optimizer::provider_graph::{Comparison, Dependency, Results, SourceContract, Spec};
use agentc_optimizer::provider_graph_preparation::{self as preparation, Begin};
use agentc_optimizer::provider_graph_quality::Policy;
use agentc_optimizer::provider_graph_roles::{
    self as roles, Activation, Binding, Dispatch, Prepared, Program, Slot,
};
use agentc_optimizer::provider_receipts::{self as receipts, Completion, Invocation, Role, Status};
use rusqlite::Connection;
use serde_json::json;

fn position(call: u32) -> CallPosition {
    CallPosition { item: 0, call }
}
fn slot(call: u32, ordinal: u32) -> Slot {
    Slot {
        position: position(call),
        ordinal,
    }
}
fn plan(model: &str) -> ExecutionPlanSpec {
    ExecutionPlanSpec {
        schema_version: 1,
        provider_protocol: "openai.chat.completions.v1".into(),
        requested_model_id: "strong".into(),
        target_model_id: model.into(),
        target_model_revision: Some(format!("{model}-r1")),
        rewrite_ordering: RewriteOrdering::Ordered,
        rewrites: vec![],
        cache_policy: CachePolicy {
            stable_name: "provider-call".into(),
            implementation_version: "1".into(),
            parameters: json!({}),
        },
        output_budget: Some(16),
        validation_policy: ValidationPolicy {
            stable_name: "none".into(),
            implementation_version: "1".into(),
            parameters: json!({}),
        },
    }
}

fn prepared() -> Prepared {
    let branches: Vec<_> = (0..2)
        .map(|call| MultiCallBranch {
            position: position(call),
            call_site_version: CallSiteVersion::parse(format!("{:064x}", call + 1)).unwrap(),
            plan: plan("cheap"),
        })
        .collect();
    let graph = Spec {
        version: agentc_optimizer::provider_graph::VERSION.into(),
        calls: MultiCallPlanSpec {
            application_version: "a".repeat(64),
            callback_version: "role-graph-tests-v1".into(),
            item_count: 1,
            max_workers: 1,
            branches: branches.clone(),
            comparison_version: None,
            execution_contract: None,
        },
        dependencies: vec![Dependency {
            producer: position(0),
            consumer: position(1),
        }],
        results: Results::CallbackItems { items: vec![0] },
    };
    let mut dispatches = vec![];
    for (ordinal, role) in [
        Role::Primary,
        Role::Fallback,
        Role::Validation,
        Role::Shadow,
        Role::Exploration,
    ]
    .into_iter()
    .enumerate()
    {
        dispatches.push(Dispatch {
            slot: slot(0, ordinal as u32),
            role,
            call_site_version: branches[0].call_site_version.clone(),
            plan: if role == Role::Primary {
                branches[0].plan.clone()
            } else {
                plan("strong")
            },
            activation: if role == Role::Fallback {
                Activation::OnFailure {
                    predecessor: slot(0, 0),
                }
            } else {
                Activation::Always
            },
        });
    }
    dispatches.push(Dispatch {
        slot: slot(1, 0),
        role: Role::Primary,
        call_site_version: branches[1].call_site_version.clone(),
        plan: branches[1].plan.clone(),
        activation: Activation::Always,
    });
    let bindings = dispatches
        .iter()
        .map(|d| Binding {
            slot: d.slot,
            runtime: PlanRuntimeVersion {
                provider_protocol: d.plan.provider_protocol.clone(),
                target_model_id: d.plan.target_model_id.clone(),
                target_model_version: format!("{}-r1", d.plan.target_model_id),
                price_table_version: "prices-v1".into(),
            },
            request_sha256: format!("{:064x}", d.slot.position.call * 10 + d.slot.ordinal + 10),
            transport_sha256: "f".repeat(64),
        })
        .collect();
    Prepared {
        source_contracts: vec![
            SourceContract {
                position: position(0),
                sha256: "7".repeat(64),
            },
            SourceContract {
                position: position(1),
                sha256: "6".repeat(64),
            },
        ],
        program: Program {
            version: roles::VERSION.into(),
            graph,
            dispatches,
        },
        bindings,
        logical_requests: vec![
            SourceContract {
                position: position(0),
                sha256: "e".repeat(64),
            },
            SourceContract {
                position: position(1),
                sha256: "d".repeat(64),
            },
        ],
    }
}

struct Fixture {
    conn: Connection,
    prepared: Prepared,
    batch: String,
    lease: String,
}
impl Fixture {
    fn new() -> Self {
        Self::with_conn(Connection::open_in_memory().unwrap())
    }
    fn with_conn(conn: Connection) -> Self {
        Self::with_prepared(conn, prepared())
    }
    fn with_prepared(conn: Connection, prepared: Prepared) -> Self {
        let mut f = Self {
            conn,
            prepared,
            batch: "b".repeat(32),
            lease: String::new(),
        };
        agentc_optimizer::schema::ensure_cost_model_schema(&f.conn).unwrap();
        receipts::handle_json(&mut f.conn,&json!({"operation":"begin_batch","batch":{"version":receipts::VERSION,"batch_id":f.batch,"application_version":"a".repeat(64),"callback_version":"role-graph-tests-v1","item_count":1}}).to_string()).unwrap();
        f.lease = preparation::begin(
            &mut f.conn,
            Begin {
                batch_id: f.batch.clone(),
                max_workers: 1,
                input_sha256: Some("c".repeat(64)),
                template_execution_id: None,
                comparison_pair_id: None,
                role_program: Some(f.prepared.clone()),
            },
            Policy::default(),
            20,
            1,
        )
        .unwrap()
        .lease_id;
        f
    }
    fn invocation(&self, s: Slot, ordinal: u32) -> Invocation {
        let d = self
            .prepared
            .program
            .dispatches
            .iter()
            .find(|d| d.slot == s)
            .unwrap();
        let b = self.prepared.bindings.iter().find(|b| b.slot == s).unwrap();
        let owner = self
            .prepared
            .program
            .dispatches
            .iter()
            .find(|d| {
                d.slot
                    == Slot {
                        position: s.position,
                        ordinal: 0,
                    }
            })
            .unwrap();
        let runtime = &self
            .prepared
            .bindings
            .iter()
            .find(|b| b.slot == owner.slot)
            .unwrap()
            .runtime;
        Invocation {
            invocation_id: format!(
                "{}{:012x}",
                &self.batch[12..],
                s.position.item * 100 + s.position.call * 10 + ordinal
            ),
            position: s.position,
            ordinal,
            role: d.role,
            provider_protocol: d.plan.provider_protocol.clone(),
            requested_model_id: d.plan.target_model_id.clone(),
            request_sha256: Some(b.request_sha256.clone()),
            transport_sha256: Some(b.transport_sha256.clone()),
            logical_request_sha256: Some(
                self.prepared
                    .logical_requests
                    .iter()
                    .find(|r| r.position == s.position)
                    .unwrap()
                    .sha256
                    .clone(),
            ),
            logical_plan_key: Some(owner.key().unwrap()),
            logical_runtime: Some(runtime.clone()),
            dispatch_plan_key: Some(d.key().unwrap()),
            dispatch_runtime: Some(b.runtime.clone()),
            parent_plan_key: if d.role == Role::Primary {
                None
            } else {
                Some(owner.key().unwrap())
            },
            transport_verified: true,
            tariff_declaration: None,
        }
    }
    fn begin(&self, s: Slot, ordinal: u32) -> anyhow::Result<Invocation> {
        let call = self.invocation(s, ordinal);
        receipts::begin_call(&self.conn, &self.batch, &call)?;
        Ok(call)
    }
    fn done(&self, call: &Invocation, status: Status) {
        receipts::finish_call(
            &self.conn,
            &self.batch,
            &call.invocation_id,
            &Completion {
                status,
                model_version: if status == Status::Completed {
                    Some(
                        call.dispatch_runtime
                            .as_ref()
                            .unwrap()
                            .target_model_version
                            .clone(),
                    )
                } else {
                    None
                },
                input_tokens: Some(2),
                output_tokens: Some(1),
                cost_usd: Some(0.01),
                cost_source: InferenceCostSource::ProviderReported,
                latency_ms: 2.,
                http_attempts: Some(1),
                exception_type: if status == Status::Completed {
                    None
                } else {
                    Some("ProviderError".into())
                },
                billing_scope_sha256: None,
                provider_generation_sha256: None,
                provider_request_id_sha256: None,
                billing_usage: None,
            },
        )
        .unwrap();
    }
    fn run(&self, s: Slot, ordinal: u32, status: Status) {
        let call = self.begin(s, ordinal).unwrap();
        self.done(&call, status);
    }
    fn seal(&mut self) {
        receipts::handle_json(&mut self.conn,&json!({"operation":"seal_batch","batch_id":self.batch,"logical_calls":[position(0),position(1)]}).to_string()).unwrap();
        receipts::handle_json(&mut self.conn,&json!({"operation":"finish_batch_economics","batch_id":self.batch,"wall_latency_ms":100.}).to_string()).unwrap();
    }
    fn finish(&mut self, now: i64) -> anyhow::Result<roles::Execution> {
        roles::finish(
            &mut self.conn,
            &self.lease,
            Comparison {
                version: agentc_optimizer::provider_graph::COMPARISON_VERSION.into(),
                input_sha256: "c".repeat(64),
                output_sha256: "9".repeat(64),
            },
            now,
        )
    }
    fn complete(&mut self, failed: bool) {
        self.run(
            slot(0, 0),
            0,
            if failed {
                Status::Failed
            } else {
                Status::Completed
            },
        );
        let mut ordinal = 1;
        if failed {
            self.run(slot(0, 1), ordinal, Status::Completed);
            ordinal += 1;
        }
        for n in 2..5 {
            self.run(slot(0, n), ordinal, Status::Completed);
            ordinal += 1;
        }
        self.run(slot(1, 0), 0, Status::Completed);
        self.seal();
    }
}

#[test]
fn fallback_and_all_auxiliary_roles_select_results_and_release_dependencies() {
    let mut f = Fixture::new();
    f.run(slot(0, 0), 0, Status::Failed);
    f.run(slot(0, 1), 1, Status::Completed);
    assert!(f.begin(slot(1, 0), 0).is_err());
    for n in 2..5 {
        f.run(slot(0, n), n, Status::Completed);
    }
    f.run(slot(1, 0), 0, Status::Completed);
    f.seal();
    let e = f.finish(10).unwrap();
    assert_eq!(e.selected_results[0].slot, slot(0, 1));
    assert_eq!(e.selected_results[1].slot, slot(1, 0));
    assert_eq!(e.acquisition.physical_invocations, 6);
    assert_eq!(e.acquisition.roles.len(), 5);
    assert!((e.acquisition.known_cost_usd - 0.06).abs() < 1e-12);
    assert!(!e.acquisition.cost_complete);
    assert_eq!(e.acquisition.unknown_cost_invocations, 1);
    assert_eq!(f.finish(20).unwrap(), e);
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_role_execution",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        1
    );
    assert_eq!(
        f.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_execution", [], |r| r
                .get::<_, u64>(
                0
            ))
            .unwrap(),
        0
    );
    assert!(f.begin(slot(1, 0), 1).is_err());
}

#[test]
fn skipped_fallback_preserves_real_sdk_ordinals_and_cannot_be_forced() {
    let mut f = Fixture::new();
    let pending = f.begin(slot(0, 0), 0).unwrap();
    assert!(f.begin(slot(0, 1), 1).is_err());
    assert!(f.begin(slot(1, 0), 0).is_err());
    f.done(&pending, Status::Completed);
    assert!(f.begin(slot(0, 1), 1).is_err());
    for n in 2..5 {
        f.run(slot(0, n), n - 1, Status::Completed);
    }
    f.run(slot(1, 0), 0, Status::Completed);
    f.seal();
    let e = f.finish(10).unwrap();
    assert_eq!(e.selected_results[0].slot, slot(0, 0));
    assert_eq!(e.acquisition.physical_invocations, 5);
    assert!(e.acquisition.cost_complete);
    assert_eq!(e.acquisition.roles.get("fallback"), None);
}

#[test]
fn policies_are_identity_bearing_and_all_conditional_runtimes_are_bound() {
    let original = prepared();
    let mut other = original.clone();
    other.program.dispatches.reverse();
    other.bindings.reverse();
    other.logical_requests.reverse();
    assert_eq!(
        original.program.key().unwrap(),
        other.program.key().unwrap()
    );
    assert_eq!(original.identity().unwrap(), other.identity().unwrap());
    assert_eq!(original.runtime().unwrap(), other.runtime().unwrap());
    other = original.clone();
    other.bindings[1].runtime.price_table_version = "prices-v2".into();
    assert_eq!(
        original.program.key().unwrap(),
        other.program.key().unwrap()
    );
    assert_ne!(original.runtime().unwrap(), other.runtime().unwrap());
    other = original.clone();
    other.program.dispatches[2].role = Role::Shadow;
    assert_ne!(
        original.program.key().unwrap(),
        other.program.key().unwrap()
    );
    other = original.clone();
    other.program.dispatches[1].activation = Activation::Always;
    assert!(other.validate().is_err());
    other = original.clone();
    other.program.dispatches[2].plan.cache_policy.stable_name = "memoized-response".into();
    assert!(other.validate().is_err());
    other = original.clone();
    other.bindings.pop();
    assert!(other.validate().is_err());
}

#[test]
fn changed_dispatch_plan_request_transport_or_owner_is_held_before_reservation() {
    let f = Fixture::new();
    let base = f.invocation(slot(0, 0), 0);
    let mut variants = vec![];
    let mut c = base.clone();
    c.request_sha256 = Some("0".repeat(64));
    variants.push(c);
    let mut c = base.clone();
    c.transport_sha256 = Some("0".repeat(64));
    variants.push(c);
    let mut c = base.clone();
    c.logical_request_sha256 = Some("0".repeat(64));
    variants.push(c);
    let mut c = base.clone();
    c.dispatch_runtime.as_mut().unwrap().price_table_version = "prices-v2".into();
    variants.push(c);
    let mut c = base.clone();
    c.requested_model_id = "other".into();
    variants.push(c);
    let mut c = base.clone();
    c.dispatch_plan_key = None;
    c.dispatch_runtime = None;
    variants.push(c);
    let mut c = base.clone();
    c.logical_plan_key = None;
    c.logical_runtime = None;
    variants.push(c);
    let mut c = base.clone();
    c.transport_verified = false;
    variants.push(c);
    for call in variants {
        assert!(receipts::begin_call(&f.conn, &f.batch, &call).is_err());
    }
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn model_drift_retry_and_cancellation_stop_further_dispatch_without_erasing_charges() {
    for fault in ["runtime", "retry", "cancelled"] {
        let f = Fixture::new();
        let call = f.begin(slot(0, 0), 0).unwrap();
        let status = if fault == "cancelled" {
            "cancelled"
        } else {
            "completed"
        };
        let completion = json!({"status":status,"model_version":if fault=="runtime" {"cheap-r2"} else {"cheap-r1"},"input_tokens":2,"output_tokens":1,"cost_usd":0.01,"cost_source":"provider_reported","latency_ms":2.,"http_attempts":if fault=="retry" {2} else {1},"exception_type":if fault=="cancelled" {Some("CancelledError")} else {None}});
        receipts::finish_call(
            &f.conn,
            &f.batch,
            &call.invocation_id,
            &serde_json::from_value(completion).unwrap(),
        )
        .unwrap();
        assert!(f.begin(slot(0, 2), 1).is_err());
        let q = agentc_optimizer::provider_graph_acquisition::report_batch(&f.conn, &f.batch)
            .unwrap()
            .unwrap();
        assert_eq!(q.physical_invocations, 1);
        assert_eq!(q.known_cost_usd, 0.01);
    }
}

#[test]
fn completed_original_program_survives_restart_and_missing_or_tampered_custody_is_held() {
    let file = tempfile::NamedTempFile::new().unwrap();
    let mut f = Fixture::with_conn(Connection::open(file.path()).unwrap());
    f.complete(true);
    let original = f.finish(10).unwrap();
    let lease = f.lease.clone();
    drop(f);
    let conn = Connection::open(file.path()).unwrap();
    assert_eq!(
        roles::load(&conn, &original.execution_id).unwrap(),
        original
    );
    conn.execute("UPDATE provider_graph_role_preparation SET prepared_json=json_set(prepared_json,'$.bindings[0].request_sha256',?1)",["0".repeat(64)]).unwrap();
    assert!(preparation::load(&conn, &lease).is_err());
    assert!(roles::load(&conn, &original.execution_id).is_err());
    conn.execute("DELETE FROM provider_graph_role_execution", [])
        .unwrap();
    conn.execute("DELETE FROM provider_graph_role_preparation", [])
        .unwrap();
    assert!(preparation::load(&conn, &lease).is_err());
}

#[test]
fn failed_or_partial_auxiliary_work_cannot_publish_a_complete_execution() {
    let mut f = Fixture::new();
    f.run(slot(0, 0), 0, Status::Completed);
    let call = f.begin(slot(0, 2), 1).unwrap();
    assert!(f.finish(10).is_err());
    f.done(&call, Status::Failed);
    assert!(f.begin(slot(0, 3), 2).is_err());
    f.seal();
    assert!(f.finish(10).is_err());
    assert_eq!(
        agentc_optimizer::provider_graph_acquisition::report_batch(&f.conn, &f.batch)
            .unwrap()
            .unwrap()
            .physical_invocations,
        2
    );
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_role_execution",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn original_role_preparation_is_idempotent_and_cannot_be_downgraded_or_rebound() {
    let mut f = Fixture::new();
    let request = Begin {
        batch_id: f.batch.clone(),
        max_workers: 1,
        input_sha256: Some("c".repeat(64)),
        template_execution_id: None,
        comparison_pair_id: None,
        role_program: Some(f.prepared.clone()),
    };
    let retry = preparation::begin(&mut f.conn, request, Policy::default(), 20, 100).unwrap();
    assert_eq!(retry.started_at_us, 1);
    assert_eq!(retry.lease_id, f.lease);
    let request = Begin {
        batch_id: f.batch.clone(),
        max_workers: 1,
        input_sha256: Some("c".repeat(64)),
        template_execution_id: None,
        comparison_pair_id: None,
        role_program: None,
    };
    assert!(preparation::begin(&mut f.conn, request, Policy::default(), 20, 100).is_err());
    let batch = "8".repeat(32);
    receipts::handle_json(&mut f.conn,&json!({"operation":"begin_batch","batch":{"version":receipts::VERSION,"batch_id":batch,"application_version":"a".repeat(64),"callback_version":"different-driver-v1","item_count":1}}).to_string()).unwrap();
    let request = Begin {
        batch_id: batch.clone(),
        max_workers: 1,
        input_sha256: Some("c".repeat(64)),
        template_execution_id: None,
        comparison_pair_id: None,
        role_program: Some(f.prepared),
    };
    assert!(preparation::begin(&mut f.conn, request, Policy::default(), 20, 100).is_err());
    for table in ["provider_graph_preparation", "provider_graph_acquisition"] {
        assert_eq!(
            f.conn
                .query_row(
                    &format!("SELECT COUNT(*) FROM {table} WHERE batch_id=?1"),
                    [&batch],
                    |r| r.get::<_, u64>(0)
                )
                .unwrap(),
            0
        );
    }
}

#[test]
fn incomplete_foreground_seal_cannot_publish_even_when_all_physical_calls_finished() {
    let mut f = Fixture::new();
    f.complete(false);
    f.conn
        .execute(
            "UPDATE provider_receipt_batch SET expected_calls_json=?1 WHERE batch_id=?2",
            rusqlite::params![serde_json::to_string(&vec![position(0)]).unwrap(), f.batch],
        )
        .unwrap();
    assert!(f.finish(10).is_err());
    assert_eq!(
        agentc_optimizer::provider_graph_acquisition::report_batch(&f.conn, &f.batch)
            .unwrap()
            .unwrap()
            .physical_invocations,
        5
    );
}

fn typed_program() -> Prepared {
    let old = prepared();
    let mut typed = old.clone();
    let source_slots = [slot(0, 0), slot(0, 2), slot(0, 1), slot(1, 0)];
    typed.program.dispatches.clear();
    typed.bindings.clear();
    for (index, source_slot) in source_slots.into_iter().enumerate() {
        let target = if index == 3 {
            slot(1, 0)
        } else {
            slot(0, index as u32)
        };
        let mut d = old
            .program
            .dispatches
            .iter()
            .find(|d| d.slot == source_slot)
            .unwrap()
            .clone();
        d.slot = target;
        d.activation = match index {
            1 => Activation::Validate {
                subject: slot(0, 0),
                contract_sha256: "8".repeat(64),
            },
            2 => Activation::OnValidationRejection {
                predecessor: slot(0, 0),
                validator: slot(0, 1),
            },
            _ => Activation::Always,
        };
        let mut b = old
            .bindings
            .iter()
            .find(|b| b.slot == source_slot)
            .unwrap()
            .clone();
        b.slot = target;
        typed.program.dispatches.push(d);
        typed.bindings.push(b);
    }
    typed.validate().unwrap();
    typed
}

fn typed_fixture() -> Fixture {
    Fixture::with_prepared(Connection::open_in_memory().unwrap(), typed_program())
}

#[test]
fn typed_validation_accepts_primary_or_selects_declared_rejection_fallback() {
    for decision in [
        roles::VerdictDecision::Accepted,
        roles::VerdictDecision::Rejected,
    ] {
        let mut f = typed_fixture();
        assert_ne!(
            f.prepared.program.key().unwrap(),
            prepared().program.key().unwrap()
        );
        let primary = f.begin(slot(0, 0), 0).unwrap();
        f.done(&primary, Status::Completed);
        assert!(f.begin(slot(1, 0), 0).is_err());
        let validator = f.begin(slot(0, 1), 1).unwrap();
        f.done(&validator, Status::Completed);
        assert!(f.begin(slot(1, 0), 0).is_err());
        assert!(f.begin(slot(0, 2), 2).is_err());
        let verdict =
            roles::record_validation(&mut f.conn, &f.lease, &validator.invocation_id, decision)
                .unwrap()
                .unwrap();
        assert_eq!(verdict.subject_invocation_id, primary.invocation_id);
        assert_eq!(verdict.contract_sha256, "8".repeat(64));
        assert_eq!(
            roles::record_validation(&mut f.conn, &f.lease, &validator.invocation_id, decision)
                .unwrap()
                .unwrap(),
            verdict
        );
        let expected = if decision == roles::VerdictDecision::Rejected {
            assert!(f.begin(slot(1, 0), 0).is_err());
            let fallback = f.begin(slot(0, 2), 2).unwrap();
            f.done(&fallback, Status::Completed);
            2
        } else {
            assert!(f.begin(slot(0, 2), 2).is_err());
            0
        };
        let consumer = f.begin(slot(1, 0), 0).unwrap();
        f.done(&consumer, Status::Completed);
        f.seal();
        let e = f.finish(10).unwrap();
        assert!(e.validation_sha256.is_some());
        assert_eq!(e.selected_results[0].slot, slot(0, expected));
        assert_eq!(
            e.acquisition.physical_invocations,
            if expected == 2 { 4 } else { 3 }
        );
        assert_eq!(f.finish(20).unwrap(), e);
        assert_eq!(roles::load(&f.conn, &e.execution_id).unwrap(), e);
    }
}

#[test]
fn missing_or_unknown_typed_verdict_cannot_release_result_or_authorize_fallback() {
    let mut f = typed_fixture();
    let primary = f.begin(slot(0, 0), 0).unwrap();
    f.done(&primary, Status::Completed);
    let validator = f.begin(slot(0, 1), 1).unwrap();
    f.done(&validator, Status::Completed);
    assert!(f.begin(slot(1, 0), 0).is_err());
    assert!(f.begin(slot(0, 2), 2).is_err());
    roles::record_validation(
        &mut f.conn,
        &f.lease,
        &validator.invocation_id,
        roles::VerdictDecision::Unknown,
    )
    .unwrap();
    assert!(f.begin(slot(1, 0), 0).is_err());
    assert!(f.begin(slot(0, 2), 2).is_err());
    f.seal();
    assert!(f.finish(10).is_err());
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        2
    );
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_role_execution",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn typed_validation_schedule_rejects_changed_subjects_contracts_and_fallback_links() {
    for case in 0..6 {
        let mut p = typed_program();
        match case {
            0 => {
                p.program.dispatches[1].activation = Activation::Validate {
                    subject: slot(1, 0),
                    contract_sha256: "8".repeat(64),
                }
            }
            1 => {
                p.program.dispatches[1].activation = Activation::Validate {
                    subject: slot(0, 0),
                    contract_sha256: "invalid".into(),
                }
            }
            2 => p.program.dispatches[1].role = Role::Shadow,
            3 => {
                p.program.dispatches[2].activation = Activation::OnValidationRejection {
                    predecessor: slot(0, 0),
                    validator: slot(0, 0),
                }
            }
            4 => {
                p.program.dispatches[2].activation = Activation::OnValidationRejection {
                    predecessor: slot(0, 1),
                    validator: slot(0, 1),
                }
            }
            _ => p.program.dispatches[2].role = Role::Exploration,
        }
        assert!(p.validate().is_err(), "{case}");
    }
    let mut p = typed_program();
    p.program.dispatches[2].role = Role::Validation;
    p.program.dispatches[2].activation = p.program.dispatches[1].activation.clone();
    assert!(p.validate().is_err());
}

#[test]
fn typed_verdict_commit_is_atomic_idempotent_and_cannot_be_rebound_or_added_late() {
    let mut f = typed_fixture();
    let primary = f.begin(slot(0, 0), 0).unwrap();
    f.done(&primary, Status::Completed);
    assert!(roles::record_validation(
        &mut f.conn,
        &f.lease,
        &primary.invocation_id,
        roles::VerdictDecision::Accepted
    )
    .is_err());
    let validator = f.begin(slot(0, 1), 1).unwrap();
    assert!(roles::record_validation(
        &mut f.conn,
        &f.lease,
        &validator.invocation_id,
        roles::VerdictDecision::Accepted
    )
    .is_err());
    f.done(&validator, Status::Completed);
    f.conn.execute_batch("CREATE TRIGGER fail_verdict BEFORE INSERT ON provider_graph_validation_verdict BEGIN SELECT RAISE(ABORT,'fixture'); END;").unwrap();
    assert!(roles::record_validation(
        &mut f.conn,
        &f.lease,
        &validator.invocation_id,
        roles::VerdictDecision::Accepted
    )
    .is_err());
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_validation_verdict",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    f.conn.execute_batch("DROP TRIGGER fail_verdict").unwrap();
    let v = roles::record_validation(
        &mut f.conn,
        &f.lease,
        &validator.invocation_id,
        roles::VerdictDecision::Accepted,
    )
    .unwrap()
    .unwrap();
    assert!(roles::record_validation(
        &mut f.conn,
        &f.lease,
        &validator.invocation_id,
        roles::VerdictDecision::Rejected
    )
    .is_err());
    f.seal();
    assert_eq!(
        roles::record_validation(
            &mut f.conn,
            &f.lease,
            &validator.invocation_id,
            roles::VerdictDecision::Accepted
        )
        .unwrap()
        .unwrap(),
        v
    );
    let mut fresh = typed_fixture();
    let p = fresh.begin(slot(0, 0), 0).unwrap();
    fresh.done(&p, Status::Completed);
    let v = fresh.begin(slot(0, 1), 1).unwrap();
    fresh.done(&v, Status::Completed);
    fresh.seal();
    assert!(roles::record_validation(
        &mut fresh.conn,
        &fresh.lease,
        &v.invocation_id,
        roles::VerdictDecision::Accepted
    )
    .is_err());
}

#[test]
fn typed_verdict_restart_and_completed_execution_reads_revalidate_original_custody() {
    for fault in [
        "delete",
        "contract",
        "subject_receipt",
        "decision",
        "selection",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("verdict.db");
        let mut f = Fixture::with_prepared(Connection::open(&path).unwrap(), typed_program());
        let p = f.begin(slot(0, 0), 0).unwrap();
        f.done(&p, Status::Completed);
        let v = f.begin(slot(0, 1), 1).unwrap();
        f.done(&v, Status::Completed);
        roles::record_validation(
            &mut f.conn,
            &f.lease,
            &v.invocation_id,
            roles::VerdictDecision::Accepted,
        )
        .unwrap();
        drop(f.conn);
        f.conn = Connection::open(&path).unwrap();
        let consumer = f.begin(slot(1, 0), 0).unwrap();
        f.done(&consumer, Status::Completed);
        f.seal();
        let mut e = f.finish(10).unwrap();
        assert_eq!(roles::load(&f.conn, &e.execution_id).unwrap(), e);
        if fault == "selection" {
            let old = e.execution_id.clone();
            e.selected_results.push(e.selected_results[0].clone());
            let mut identity = serde_json::to_value(&e).unwrap();
            identity.as_object_mut().unwrap().remove("execution_id");
            identity.as_object_mut().unwrap().remove("observed_at_us");
            e.execution_id = agentc_core::storage::content_hash(
                &agentc_core::storage::canonical_json(&identity),
            );
            let raw = agentc_core::storage::canonical_json(&serde_json::to_value(&e).unwrap());
            f.conn.execute("UPDATE provider_graph_role_execution SET execution_id=?2,execution_sha256=?3,execution_json=?4 WHERE execution_id=?1",rusqlite::params![old,e.execution_id,agentc_core::storage::content_hash(&raw),raw]).unwrap();
        } else if fault == "delete" {
            f.conn
                .execute(
                    "DELETE FROM provider_graph_validation_verdict WHERE invocation_id=?1",
                    [&v.invocation_id],
                )
                .unwrap();
        } else {
            let raw:String=f.conn.query_row("SELECT verdict_json FROM provider_graph_validation_verdict WHERE invocation_id=?1",[&v.invocation_id],|r|r.get(0)).unwrap();
            let mut verdict: roles::ValidationVerdict = serde_json::from_str(&raw).unwrap();
            match fault {
                "contract" => verdict.contract_sha256 = "9".repeat(64),
                "subject_receipt" => verdict.subject_receipt_sha256 = "9".repeat(64),
                _ => verdict.decision = roles::VerdictDecision::Rejected,
            }
            let raw = agentc_core::storage::canonical_json(&serde_json::to_value(verdict).unwrap());
            f.conn.execute("UPDATE provider_graph_validation_verdict SET verdict_sha256=?2,verdict_json=?3 WHERE invocation_id=?1",rusqlite::params![v.invocation_id,agentc_core::storage::content_hash(&raw),raw]).unwrap();
        }
        assert!(roles::load(&f.conn, &e.execution_id).is_err(), "{fault}");
        assert!(f.finish(20).is_err(), "{fault}");
    }
}

#[test]
fn failed_or_cancelled_validator_cannot_claim_a_typed_acceptance() {
    for status in [Status::Failed, Status::Cancelled] {
        let mut f = typed_fixture();
        let p = f.begin(slot(0, 0), 0).unwrap();
        f.done(&p, Status::Completed);
        let v = f.begin(slot(0, 1), 1).unwrap();
        f.done(&v, status);
        assert!(roles::record_validation(
            &mut f.conn,
            &f.lease,
            &v.invocation_id,
            roles::VerdictDecision::Accepted
        )
        .is_err());
        assert!(f.begin(slot(1, 0), 0).is_err());
        assert!(f.begin(slot(0, 2), 2).is_err());
        assert_eq!(
            f.conn
                .query_row(
                    "SELECT COUNT(*) FROM provider_graph_validation_verdict",
                    [],
                    |r| r.get::<_, u64>(0)
                )
                .unwrap(),
            0
        );
    }
}

fn reference_program() -> Prepared {
    let mut p = prepared();
    for branch in &mut p.program.graph.calls.branches {
        branch.plan = plan("strong");
    }
    p.program.dispatches.retain(|d| d.role == Role::Primary);
    for d in &mut p.program.dispatches {
        d.plan = plan("strong");
    }
    p.bindings.retain(|b| b.slot.ordinal == 0);
    for b in &mut p.bindings {
        b.runtime.target_model_id = "strong".into();
        b.runtime.target_model_version = "strong-r1".into();
    }
    p
}

impl Fixture {
    fn reset(
        &mut self,
        p: Prepared,
        tag: u32,
        start: i64,
        pair: Option<&str>,
        policy: Policy,
    ) -> anyhow::Result<()> {
        let batch = format!("{tag:032x}");
        let calls = &p.program.graph.calls;
        receipts::handle_json(&mut self.conn,&json!({"operation":"begin_batch","batch":{"version":receipts::VERSION,"batch_id":batch,"application_version":calls.application_version,"callback_version":calls.callback_version,"item_count":calls.item_count}}).to_string())?;
        let lease = preparation::begin(
            &mut self.conn,
            Begin {
                batch_id: batch.clone(),
                max_workers: calls.max_workers,
                input_sha256: Some("c".repeat(64)),
                template_execution_id: None,
                comparison_pair_id: pair.map(str::to_owned),
                role_program: Some(p.clone()),
            },
            policy,
            20,
            start,
        )?;
        self.prepared = p;
        self.batch = batch;
        self.lease = lease.lease_id;
        Ok(())
    }
    fn publish_program(
        &mut self,
        p: Prepared,
        tag: u32,
        start: i64,
        pair: Option<&str>,
        different: bool,
    ) -> roles::Execution {
        self.reset(p, tag, start, pair, Policy::default()).unwrap();
        let mut dispatches = self.prepared.program.dispatches.clone();
        dispatches.sort_by_key(|d| d.slot);
        let mut ordinals = std::collections::BTreeMap::<CallPosition, u32>::new();
        for d in dispatches {
            if d.role == Role::Fallback {
                continue;
            }
            let ordinal = ordinals.entry(d.slot.position).or_default();
            self.run(d.slot, *ordinal, Status::Completed);
            *ordinal += 1;
        }
        self.seal();
        roles::finish(
            &mut self.conn,
            &self.lease,
            Comparison {
                version: agentc_optimizer::provider_graph::COMPARISON_VERSION.into(),
                input_sha256: "c".repeat(64),
                output_sha256: if different { "8" } else { "9" }.repeat(64),
            },
            start + 1,
        )
        .unwrap()
    }
    fn source_pair(
        &mut self,
        different: bool,
    ) -> agentc_optimizer::provider_graph_comparison::Pair {
        let r = self.publish_program(reference_program(), 11, 1, None, false);
        let c = self.publish_program(prepared(), 12, 3, None, different);
        agentc_optimizer::provider_graph_role_comparison::compare_with_policy(
            &mut self.conn,
            &r.execution_id,
            &c.execution_id,
            5,
            20,
            Policy::default(),
        )
        .unwrap()
        .pair
    }
}

#[test]
fn whole_role_result_pair_is_one_to_one_and_never_warms_primary_only_quality() {
    use agentc_optimizer::provider_graph_role_comparison as comparison;
    let mut f = Fixture::new();
    let pair = f.source_pair(false);
    assert_eq!(pair.divergence, 0.);
    assert!((pair.acquisition_cost_usd.unwrap() - 0.07).abs() < 1e-12);
    let q = comparison::window(
        &f.conn,
        agentc_optimizer::provider_graph_quality::Request::from_pair(
            &pair,
            20,
            5,
            Policy::default(),
        ),
    )
    .unwrap()
    .unwrap();
    assert_eq!(q.pairs.len(), 1);
    assert!(q.prepared_pairs.is_empty());
    assert_eq!(q.lifetime_pairs, 1);
    let retry = comparison::compare_with_policy(
        &mut f.conn,
        &pair.reference_execution_id,
        &pair.candidate_execution_id,
        6,
        20,
        Policy::default(),
    )
    .unwrap();
    assert_eq!(retry.status, "already_recorded");
    assert_eq!(retry.pair, pair);
    assert_eq!(retry.quality.unwrap().lifetime_pairs, 1);
    let c = f.publish_program(prepared(), 13, 7, None, false);
    assert!(comparison::compare_with_policy(
        &mut f.conn,
        &pair.reference_execution_id,
        &c.execution_id,
        9,
        20,
        Policy::default()
    )
    .is_err());
    assert_eq!(
        f.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_role_pair", [], |r| r
                .get::<_, u64>(
                0
            ))
            .unwrap(),
        1
    );
    assert!(agentc_optimizer::provider_graph_quality::window(
        &f.conn,
        agentc_optimizer::provider_graph_quality::Request::from_pair(
            &pair,
            20,
            9,
            Policy::default()
        )
    )
    .unwrap()
    .is_none());
    assert_eq!(
        f.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_pair", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_quality_event",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn prepared_role_pairs_bind_both_original_starts_and_the_shared_generation() {
    use agentc_optimizer::provider_graph_role_comparison as comparison;
    let mut f = Fixture::new();
    let source = f.source_pair(false);
    let r = f.publish_program(reference_program(), 21, 10, Some(&source.pair_id), false);
    let c = f.publish_program(prepared(), 22, 12, Some(&source.pair_id), false);
    let outcome = comparison::compare_with_policy(
        &mut f.conn,
        &r.execution_id,
        &c.execution_id,
        14,
        20,
        Policy::default(),
    )
    .unwrap();
    let q = outcome.quality.unwrap();
    assert_eq!(q.pairs.len(), 2);
    assert_eq!(q.prepared_pairs, vec![outcome.pair]);
    let r = preparation::load(&f.conn, &r.lease_id).unwrap();
    let c = preparation::load(&f.conn, &c.lease_id).unwrap();
    assert_eq!(
        r.role_comparison_context.as_ref().unwrap().generation,
        c.role_comparison_context.as_ref().unwrap().generation
    );
    assert_eq!(
        r.role_comparison_context.unwrap().role,
        preparation::ComparisonRole::Reference
    );
    assert_eq!(
        c.role_comparison_context.unwrap().role,
        preparation::ComparisonRole::Candidate
    );
}

#[test]
fn reference_or_candidate_epoch_changes_hold_the_next_role_dispatch() {
    for reference in [true, false] {
        let mut f = Fixture::new();
        let source = f.source_pair(false);
        f.reset(prepared(), 22, 10, Some(&source.pair_id), Policy::default())
            .unwrap();
        let key = if reference {
            &source.reference_key
        } else {
            &source.key
        };
        f.conn.execute("UPDATE provider_graph_role_execution_state SET observation_floor=source_sequence+1,source_sequence=source_sequence+1 WHERE call_site_version=?1 AND execution_plan_id=?2",rusqlite::params![key.call_site_version.as_str(),key.execution_plan_id.as_str()]).unwrap();
        assert!(f.begin(slot(0, 0), 0).is_err());
        assert_eq!(
            f.conn
                .query_row(
                    "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                    [&f.batch],
                    |r| r.get::<_, u64>(0)
                )
                .unwrap(),
            0
        );
    }
}

#[test]
fn changed_role_source_controls_and_non_original_references_are_rejected() {
    use agentc_optimizer::provider_graph_role_comparison as comparison;
    let mut f = Fixture::new();
    let source = f.source_pair(false);
    let mut changed = prepared();
    changed.source_contracts[0].sha256 = "0".repeat(64);
    assert!(f
        .reset(
            changed.clone(),
            22,
            10,
            Some(&source.pair_id),
            Policy::default()
        )
        .is_err());
    let c = f.publish_program(changed, 23, 11, None, false);
    let r = f.publish_program(reference_program(), 24, 13, None, false);
    assert!(comparison::compare_with_policy(
        &mut f.conn,
        &r.execution_id,
        &c.execution_id,
        15,
        20,
        Policy::default()
    )
    .is_err());
    let r = f.publish_program(prepared(), 25, 16, None, false);
    let c = f.publish_program(prepared(), 26, 18, None, false);
    assert!(comparison::compare_with_policy(
        &mut f.conn,
        &r.execution_id,
        &c.execution_id,
        20,
        20,
        Policy::default()
    )
    .is_err());
}

#[test]
fn role_pair_and_quality_event_roll_back_together_and_corruption_is_held() {
    use agentc_optimizer::provider_graph_role_comparison as comparison;
    let mut f = Fixture::new();
    let source = f.source_pair(false);
    let r = f.publish_program(reference_program(), 21, 10, None, false);
    let c = f.publish_program(prepared(), 22, 12, None, true);
    f.conn.execute_batch("CREATE TRIGGER reject_role_event BEFORE INSERT ON provider_graph_role_quality_event BEGIN SELECT RAISE(ABORT,'fixture'); END").unwrap();
    assert!(comparison::compare_with_policy(
        &mut f.conn,
        &r.execution_id,
        &c.execution_id,
        14,
        20,
        Policy::default()
    )
    .is_err());
    assert_eq!(
        f.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_role_pair", [], |r| r
                .get::<_, u64>(
                0
            ))
            .unwrap(),
        1
    );
    f.conn
        .execute_batch("DROP TRIGGER reject_role_event")
        .unwrap();
    let pair = comparison::compare_with_policy(
        &mut f.conn,
        &r.execution_id,
        &c.execution_id,
        14,
        20,
        Policy::default(),
    )
    .unwrap()
    .pair;
    f.conn
        .execute(
            "UPDATE provider_graph_role_quality_event SET excess=0 WHERE pair_id=?1",
            [&pair.pair_id],
        )
        .unwrap();
    assert!(comparison::window(
        &f.conn,
        agentc_optimizer::provider_graph_quality::Request::from_pair(
            &source,
            20,
            15,
            Policy::default()
        )
    )
    .is_err());
}

#[test]
fn role_quality_disable_requires_twenty_fresh_prepared_pairs_after_cooldown() {
    use agentc_optimizer::provider_graph_role_comparison as comparison;
    let mut f = Fixture::new();
    let source = f.source_pair(true);
    let q = comparison::window(
        &f.conn,
        agentc_optimizer::provider_graph_quality::Request::from_pair(
            &source,
            20,
            5,
            Policy::default(),
        ),
    )
    .unwrap()
    .unwrap();
    let boundary = q.disabled.unwrap().reenable_at_us;
    assert_eq!(q.divergence_exposure, 1.);
    let mut now = boundary + 1;
    for n in 0..20 {
        let r = f.publish_program(
            reference_program(),
            100 + n * 2,
            now,
            Some(&source.pair_id),
            false,
        );
        let c = f.publish_program(
            prepared(),
            101 + n * 2,
            now + 2,
            Some(&source.pair_id),
            false,
        );
        let q = comparison::compare_with_policy(
            &mut f.conn,
            &r.execution_id,
            &c.execution_id,
            now + 4,
            20,
            Policy::default(),
        )
        .unwrap()
        .quality
        .unwrap();
        if n < 19 {
            assert!(comparison::finish_readmission_after_admission(&f.conn, &q, now + 4).is_err());
        }
        now += 5;
    }
    let request = agentc_optimizer::provider_graph_quality::Request::from_pair(
        &source,
        20,
        now,
        Policy::default(),
    );
    let q = comparison::window(&f.conn, request).unwrap().unwrap();
    assert_eq!(q.prepared_pairs.len(), 20);
    assert_eq!(q.prepared_divergence_upper_p95, Some(0.));
    comparison::finish_readmission_after_admission(&f.conn, &q, now).unwrap();
    let after = comparison::window(&f.conn, request).unwrap().unwrap();
    assert!(after.disabled.is_none());
    assert_eq!(after.prepared_pairs.len(), 20);
    assert_eq!(after.evidence_boundary_us, boundary);
}

#[test]
fn role_evidence_revalidates_ancestry_journal_and_policy_between_operations() {
    use agentc_core::storage::{canonical_json, content_hash};
    use agentc_optimizer::provider_graph_quality::Request;
    use agentc_optimizer::provider_graph_role_comparison as comparison;
    let mut f = Fixture::new();
    let source = f.source_pair(false);
    let r = f.publish_program(reference_program(), 41, 10, Some(&source.pair_id), false);
    let c = f.publish_program(prepared(), 42, 12, Some(&source.pair_id), false);
    let paired = comparison::compare_with_policy(
        &mut f.conn,
        &r.execution_id,
        &c.execution_id,
        14,
        20,
        Policy::default(),
    )
    .unwrap();
    let request = Request::from_pair(&paired.pair, 20, 14, Policy::default());
    let original = comparison::window(&f.conn, request).unwrap().unwrap();
    assert_eq!(original.prepared_pairs.len(), 1);
    // Every fault follows a successful operation on this same connection.
    // Matching row hashes must not bypass execution, lease or source custody.
    for (table, id_column, json_column, hash_column, id, path, value) in [
        (
            "provider_graph_role_execution",
            "execution_id",
            "execution_json",
            "execution_sha256",
            c.execution_id.as_str(),
            "/journal_sha256",
            json!("0".repeat(64)),
        ),
        (
            "provider_graph_preparation",
            "lease_id",
            "lease_json",
            "lease_sha256",
            c.lease_id.as_str(),
            "/started_at_us",
            json!(11),
        ),
        (
            "provider_graph_role_preparation",
            "lease_id",
            "prepared_json",
            "prepared_sha256",
            c.lease_id.as_str(),
            "/bindings/0/request_sha256",
            json!("0".repeat(64)),
        ),
        (
            "provider_graph_role_pair",
            "pair_id",
            "pair_json",
            "pair_sha256",
            source.pair_id.as_str(),
            "/divergence",
            json!(1.),
        ),
    ] {
        let sql = format!("SELECT {json_column},{hash_column} FROM {table} WHERE {id_column}=?1");
        let (raw, hash): (String, String) = f
            .conn
            .query_row(&sql, [id], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap();
        let mut changed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        *changed.pointer_mut(path).unwrap() = value;
        let changed = canonical_json(&changed);
        let sql =
            format!("UPDATE {table} SET {json_column}=?2,{hash_column}=?3 WHERE {id_column}=?1");
        f.conn
            .execute(&sql, rusqlite::params![id, changed, content_hash(&changed)])
            .unwrap();
        assert!(
            comparison::load(&f.conn, &paired.pair.pair_id).is_err(),
            "{table}"
        );
        assert!(comparison::window(&f.conn, request).is_err(), "{table}");
        f.conn
            .execute(&sql, rusqlite::params![id, raw, hash])
            .unwrap();
        assert_eq!(
            comparison::window(&f.conn, request).unwrap().unwrap(),
            original
        );
    }
    let (invocation, completion): (String, String) = f.conn.query_row(
        "SELECT invocation_id,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY ordinal LIMIT 1",
        [&c.batch_id], |r| Ok((r.get(0)?, r.get(1)?)),
    ).unwrap();
    f.conn.execute("UPDATE provider_receipt_invocation SET completion_json=json_set(completion_json,'$.latency_ms',99) WHERE invocation_id=?1", [&invocation]).unwrap();
    assert!(comparison::window(&f.conn, request).is_err());
    f.conn
        .execute(
            "UPDATE provider_receipt_invocation SET completion_json=?2 WHERE invocation_id=?1",
            rusqlite::params![invocation, completion],
        )
        .unwrap();
    assert_eq!(
        comparison::window(&f.conn, request).unwrap().unwrap(),
        original
    );
    f.conn
        .execute(
            "UPDATE provider_graph_role_quality_event SET excess=1 WHERE pair_id=?1",
            [&paired.pair.pair_id],
        )
        .unwrap();
    assert!(comparison::window(&f.conn, request).is_err());
    f.conn
        .execute(
            "UPDATE provider_graph_role_quality_event SET excess=0 WHERE pair_id=?1",
            [&paired.pair.pair_id],
        )
        .unwrap();
    assert_eq!(
        comparison::window(&f.conn, request).unwrap().unwrap(),
        original
    );
    let runtime: String = f.conn.query_row("SELECT runtime_json FROM provider_graph_role_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2", rusqlite::params![source.key.call_site_version.as_str(),source.key.execution_plan_id.as_str()], |r| r.get(0)).unwrap();
    f.conn.execute("UPDATE provider_graph_role_execution_state SET runtime_json=json_set(runtime_json,'$.price_table_version','retired') WHERE call_site_version=?1 AND execution_plan_id=?2", rusqlite::params![source.key.call_site_version.as_str(),source.key.execution_plan_id.as_str()]).unwrap();
    assert!(comparison::window(&f.conn, request).unwrap().is_none());
    f.conn.execute("UPDATE provider_graph_role_execution_state SET runtime_json=?3 WHERE call_site_version=?1 AND execution_plan_id=?2", rusqlite::params![source.key.call_site_version.as_str(),source.key.execution_plan_id.as_str(),runtime]).unwrap();
    assert_eq!(
        comparison::window(&f.conn, request).unwrap().unwrap(),
        original
    );
}

#[test]
fn role_archive_reads_one_wal_snapshot_and_revalidates_after_commit() {
    use agentc_optimizer::provider_graph_role_comparison as comparison;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("roles.sqlite");
    let conn = Connection::open(&path).unwrap();
    conn.execute_batch("PRAGMA journal_mode=WAL").unwrap();
    let mut f = Fixture::with_conn(conn);
    let pair = f.source_pair(false);
    let writer = Connection::open(&path).unwrap();
    let tx = f.conn.transaction().unwrap();
    assert_eq!(comparison::load(&tx, &pair.pair_id).unwrap(), pair);
    writer.execute("UPDATE provider_receipt_invocation SET completion_json=json_set(completion_json,'$.latency_ms',99)", []).unwrap();
    assert_eq!(comparison::load(&tx, &pair.pair_id).unwrap(), pair);
    tx.commit().unwrap();
    assert!(comparison::load(&f.conn, &pair.pair_id).is_err());
}

#[derive(Clone, Copy)]
enum FinancialMode {
    Reported,
    FailedPrimary,
    EstimatedInvoice,
    UnknownTariff,
    ReportedCheap,
    AdverseCheap,
}

fn financial_fixture() -> Fixture {
    let conn = Connection::open_in_memory().unwrap();
    agentc_optimizer::schema::ensure_cost_model_schema(&conn).unwrap();
    Fixture {
        conn,
        prepared: prepared(),
        batch: String::new(),
        lease: String::new(),
    }
}

fn priced_program() -> Prepared {
    let mut p = prepared();
    let model = |id: &str| {
        if id == "strong" {
            "gpt-4o-2024-11-20"
        } else {
            "gpt-4o-mini-2024-07-18"
        }
        .to_owned()
    };
    for plan in p
        .program
        .graph
        .calls
        .branches
        .iter_mut()
        .map(|b| &mut b.plan)
        .chain(p.program.dispatches.iter_mut().map(|d| &mut d.plan))
    {
        plan.requested_model_id = model(&plan.requested_model_id);
        plan.target_model_id = model(&plan.target_model_id);
        plan.target_model_revision = Some(plan.target_model_id.clone());
    }
    for binding in &mut p.bindings {
        binding.runtime.target_model_id = model(&binding.runtime.target_model_id);
        binding.runtime.target_model_version = binding.runtime.target_model_id.clone();
    }
    p
}

fn publish_priced(f: &mut Fixture, tag: u32, mode: FinancialMode) -> roles::Execution {
    publish_financial_program(f, priced_program(), tag, mode, 100., None, None)
}

fn publish_financial_program(
    f: &mut Fixture,
    p: Prepared,
    tag: u32,
    mode: FinancialMode,
    wall: f64,
    source: Option<&str>,
    start: Option<i64>,
) -> roles::Execution {
    use agentc_optimizer::{
        provider_reconciliation as billing, provider_tariff as tariff, provider_usage as usage,
    };
    let snapshot = tariff::register(&f.conn).unwrap();
    let start = start.unwrap_or(i64::from(tag) * 10);
    f.reset(p, tag, start, source, Policy::default()).unwrap();
    let endpoint = if matches!(mode, FinancialMode::EstimatedInvoice) {
        billing::openrouter_scope()
    } else {
        agentc_core::storage::content_hash("billing-scope-v1\0https://api.openai.com/v1")
    };
    let mut ordinals = std::collections::BTreeMap::<CallPosition, u32>::new();
    for dispatch in f.prepared.program.dispatches.clone() {
        if dispatch.role == Role::Fallback && !matches!(mode, FinancialMode::FailedPrimary) {
            continue;
        }
        let ordinal = ordinals.entry(dispatch.slot.position).or_default();
        let mut call = f.invocation(dispatch.slot, *ordinal);
        call.tariff_declaration = Some(tariff::Declaration {
            version: tariff::VERSION.into(),
            snapshot_sha256: snapshot.clone(),
            endpoint_sha256: Some(endpoint.clone()),
            request_scope_supported: true,
        });
        receipts::begin_call(&f.conn, &f.batch, &call).unwrap();
        let failed = matches!(mode, FinancialMode::FailedPrimary) && dispatch.slot == slot(0, 0);
        let c = Completion {
            status: if failed {
                Status::Failed
            } else {
                Status::Completed
            },
            model_version: (!failed).then(|| {
                call.dispatch_runtime
                    .as_ref()
                    .unwrap()
                    .target_model_version
                    .clone()
            }),
            input_tokens: Some(2),
            output_tokens: Some(1),
            cost_usd: Some(if matches!(mode, FinancialMode::EstimatedInvoice) {
                0.003
            } else if matches!(
                mode,
                FinancialMode::ReportedCheap | FinancialMode::AdverseCheap
            ) && call.requested_model_id.contains("mini")
            {
                0.001
            } else {
                0.01
            }),
            cost_source: if matches!(mode, FinancialMode::EstimatedInvoice) {
                InferenceCostSource::Estimated
            } else {
                InferenceCostSource::ProviderReported
            },
            latency_ms: 2.,
            http_attempts: Some(1),
            exception_type: failed.then(|| "ProviderError".into()),
            billing_scope_sha256: Some(endpoint.clone()),
            provider_generation_sha256: Some(agentc_core::storage::content_hash(&format!(
                "generation:{}",
                call.invocation_id
            ))),
            provider_request_id_sha256: Some(agentc_core::storage::content_hash(&format!(
                "request:{}",
                call.invocation_id
            ))),
            billing_usage: (!failed).then(|| usage::BillingUsage {
                version: usage::VERSION.into(),
                usage: usage::Usage::Openai {
                    input_tokens: 2,
                    output_tokens: 1,
                    total_tokens: Some(3),
                    input_details: Some(usage::InputDetails {
                        cached_tokens: Some(0),
                        cache_write_tokens: None,
                        audio_tokens: None,
                        image_tokens: None,
                        text_tokens: None,
                    }),
                    output_details: None,
                    service_tier: (!matches!(mode, FinancialMode::UnknownTariff))
                        .then(|| "default".into()),
                    unrecognized_fields: false,
                },
            }),
        };
        receipts::finish_call(&f.conn, &f.batch, &call.invocation_id, &c).unwrap();
        *ordinal += 1;
    }
    receipts::handle_json(&mut f.conn,&json!({"operation":"seal_batch","batch_id":f.batch,"logical_calls":[position(0),position(1)]}).to_string()).unwrap();
    receipts::handle_json(
        &mut f.conn,
        &json!({"operation":"finish_batch_economics","batch_id":f.batch,"wall_latency_ms":wall})
            .to_string(),
    )
    .unwrap();
    roles::finish(
        &mut f.conn,
        &f.lease,
        Comparison {
            version: agentc_optimizer::provider_graph::COMPARISON_VERSION.into(),
            input_sha256: "c".repeat(64),
            output_sha256: if matches!(mode, FinancialMode::AdverseCheap) {
                "8"
            } else {
                "9"
            }
            .repeat(64),
        },
        start + 1,
    )
    .unwrap()
}

fn admission_program(reference: bool) -> Prepared {
    let mut p = priced_program();
    p.program.dispatches.retain(|d| d.role == Role::Primary);
    p.bindings.retain(|b| b.slot.ordinal == 0);
    if reference {
        for branch in &mut p.program.graph.calls.branches {
            branch.plan.target_model_id = branch.plan.requested_model_id.clone();
            branch.plan.target_model_revision = Some(branch.plan.target_model_id.clone());
        }
        for d in &mut p.program.dispatches {
            d.plan.target_model_id = d.plan.requested_model_id.clone();
            d.plan.target_model_revision = Some(d.plan.target_model_id.clone());
        }
        for b in &mut p.bindings {
            b.runtime.target_model_id = "gpt-4o-2024-11-20".into();
            b.runtime.target_model_version = b.runtime.target_model_id.clone();
        }
    }
    p
}

fn admission_request(
    pair: &agentc_optimizer::provider_graph_comparison::Pair,
    now: i64,
) -> agentc_optimizer::provider_graph_admission::Request {
    use agentc_optimizer::{
        execution_plan::SelectionPolicy, provider_graph_admission as admission,
        provider_graph_economics::Kind,
    };
    admission::Request {
        kind: Kind::Roles,
        source_pair_ids: vec![pair.pair_id.clone()],
        window_size: 20,
        remaining_batches: 1000,
        policy: admission::Policy {
            selection: SelectionPolicy {
                now_us: now,
                ..SelectionPolicy::default()
            },
            quality: Policy::default(),
            money: admission::Money::BilledUsd,
            max_workers: 1,
            max_physical_invocations: 2,
        },
    }
}

fn priced_pairs(f: &mut Fixture, count: u32) -> agentc_optimizer::provider_graph_comparison::Pair {
    use agentc_optimizer::provider_graph_role_comparison as comparison;
    let mut last = None;
    for n in 0..count {
        let r = publish_financial_program(
            f,
            admission_program(true),
            500 + n * 2,
            FinancialMode::ReportedCheap,
            100.,
            None,
            None,
        );
        let c = publish_financial_program(
            f,
            admission_program(false),
            501 + n * 2,
            FinancialMode::ReportedCheap,
            10.,
            None,
            None,
        );
        last = Some(
            comparison::compare_with_policy(
                &mut f.conn,
                &r.execution_id,
                &c.execution_id,
                c.observed_at_us + 1,
                20,
                Policy::default(),
            )
            .unwrap()
            .pair,
        );
    }
    last.unwrap()
}

fn deployment_request(
    f: &mut Fixture,
    pair: &agentc_optimizer::provider_graph_comparison::Pair,
    tag: u32,
) -> agentc_optimizer::provider_graph_deployment::Begin {
    let p = admission_program(false);
    f.batch = format!("{tag:032x}");
    f.prepared = p.clone();
    receipts::handle_json(&mut f.conn,&json!({"operation":"begin_batch","batch":{"version":receipts::VERSION,"batch_id":f.batch,"application_version":p.program.graph.calls.application_version,"callback_version":p.program.graph.calls.callback_version,"item_count":1}}).to_string()).unwrap();
    let mut admission = admission_request(pair, pair.observed_at_us + 1);
    admission.policy.selection.max_profile_age_us = i64::MAX;
    agentc_optimizer::provider_graph_deployment::Begin {
        campaign: None,
        dynamic_primary_nodes: false,
        primary_bindings: vec![],
        preparation: Begin {
            batch_id: f.batch.clone(),
            max_workers: 1,
            input_sha256: Some("c".repeat(64)),
            template_execution_id: None,
            comparison_pair_id: None,
            role_program: Some(p),
        },
        admission,
    }
}

fn complete_deployment(f: &mut Fixture, now: i64) -> roles::Execution {
    for call in 0..2 {
        let invocation = f.begin(slot(call, 0), 0).unwrap();
        receipts::finish_call(
            &f.conn,
            &f.batch,
            &invocation.invocation_id,
            &Completion {
                status: Status::Completed,
                model_version: Some(invocation.requested_model_id.clone()),
                input_tokens: Some(2),
                output_tokens: Some(1),
                cost_usd: Some(0.001),
                cost_source: InferenceCostSource::ProviderReported,
                latency_ms: 2.,
                http_attempts: Some(1),
                exception_type: None,
                billing_scope_sha256: None,
                provider_generation_sha256: None,
                provider_request_id_sha256: None,
                billing_usage: None,
            },
        )
        .unwrap();
    }
    f.seal();
    roles::finish(
        &mut f.conn,
        &f.lease,
        Comparison {
            version: agentc_optimizer::provider_graph::COMPARISON_VERSION.into(),
            input_sha256: "c".repeat(64),
            output_sha256: "9".repeat(64),
        },
        now,
    )
    .unwrap()
}

#[test]
fn guarded_deployment_rechecks_without_amortizing_its_own_normal_work_and_preserves_all_costs() {
    use agentc_optimizer::{
        provider_graph_admission as admission, provider_graph_deployment as deploy,
        provider_graph_economics as finance,
    };
    let mut f = financial_fixture();
    let pair = priced_pairs(&mut f, 20);
    let request = deployment_request(&mut f, &pair, 900);
    let now = pair.observed_at_us + 10;
    let outcome = deploy::begin(&mut f.conn, request.clone(), now).unwrap();
    let lease = outcome.lease.unwrap();
    f.lease = lease.lease_id.clone();
    assert!(lease.deployment_id.is_some());
    let driver = outcome.decision.acquisition.economics.driver_id;
    let pending = finance::summarize_acquisition(&f.conn, &driver).unwrap();
    assert_eq!(pending.economics.unfinished_batches, 1);
    assert!(pending.costs.billed_usd.is_none());
    let retry = deploy::begin(&mut f.conn, request.clone(), now + 1).unwrap();
    assert_eq!(retry.lease.unwrap(), lease);
    let before = outcome.decision.acquisition.costs.known_billed_usd;
    let e = complete_deployment(&mut f, now + 2);
    assert_eq!(e.prepared_id, f.prepared.identity().unwrap());
    let after = finance::summarize_acquisition(&f.conn, &driver).unwrap();
    assert_eq!(after.costs.known_billed_usd, before);
    let normal = after.deployment.unwrap();
    assert_eq!(normal.batches, vec![f.batch.clone()]);
    assert_eq!(normal.costs.physical_invocations, 2);
    assert!(
        normal.costs.known_billed_usd.lower_usd <= 0.002
            && normal.costs.known_billed_usd.upper_usd >= 0.002
    );
    let all =
        agentc_optimizer::provider_graph_acquisition::summarize_driver(&f.conn, &driver).unwrap();
    assert_eq!(all.physical_invocations, 82);
    let mut next = request.admission.clone();
    next.policy.selection.now_us = now + 3;
    next.policy.selection.max_expected_latency_ms = Some(106.);
    assert!(
        !admission::select(&f.conn, next.clone())
            .unwrap()
            .selected_reference
    );
    let (id,call,done):(String,String,String)=f.conn.query_row("SELECT invocation_id,invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 LIMIT 1",[&f.batch],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();
    let binding = agentc_core::storage::canonical_json(
        &json!({"version":"provider-billing-binding-v1","batch_id":f.batch,"invocation_id":id,"invocation":call,"completion":done}),
    );
    let raw = agentc_core::storage::canonical_json(&json!({"latency_ms":5.,"invoice":null}));
    f.conn
        .execute(
            "INSERT INTO provider_billing_attempt VALUES('deployment-resource',?1,?2,?3,NULL,?4)",
            rusqlite::params![
                id,
                f.batch,
                agentc_core::storage::content_hash(&binding),
                raw
            ],
        )
        .unwrap();
    assert!(finance::summarize_acquisition(&f.conn, &driver)
        .unwrap()
        .costs
        .billed_usd
        .is_none());
    f.conn.execute("UPDATE provider_billing_attempt SET result_json=result_input_json WHERE attempt_id='deployment-resource'",[]).unwrap();
    let accounted = finance::summarize_acquisition(&f.conn, &driver).unwrap();
    assert_eq!(accounted.costs.known_billed_usd, before);
    assert_eq!(
        accounted
            .deployment
            .unwrap()
            .economics
            .unwrap()
            .billing_verification_latency_ms,
        5.
    );
    let projection = finance::execution(&f.conn, finance::Kind::Roles, &e.execution_id).unwrap();
    assert!(projection.resource_latency_ms.unwrap().upper_ms >= 105.);
    let held = admission::select(&f.conn, next).unwrap();
    assert!(held.selected_reference);
    assert_eq!(
        held.alternatives[0].rejection.as_deref(),
        Some("expected_latency_limit")
    );
    let changed = deploy::Begin {
        campaign: None,
        dynamic_primary_nodes: false,
        primary_bindings: vec![],
        preparation: Begin {
            input_sha256: Some("8".repeat(64)),
            ..request.preparation
        },
        admission: request.admission,
    };
    assert!(deploy::begin(&mut f.conn, changed, now + 4).is_err());
}

#[test]
fn guarded_deployment_holds_unqualified_programs_and_new_unfinished_liabilities_before_reservation()
{
    use agentc_optimizer::provider_graph_deployment as deploy;
    let mut f = financial_fixture();
    let pair = priced_pairs(&mut f, 20);
    let now = pair.observed_at_us + 10;
    let mut changed = deployment_request(&mut f, &pair, 901);
    changed
        .preparation
        .role_program
        .as_mut()
        .unwrap()
        .source_contracts[0]
        .sha256 = "1".repeat(64);
    assert!(deploy::begin(&mut f.conn, changed, now).is_err());
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_preparation WHERE batch_id=?1",
                [&f.batch],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    let request = deployment_request(&mut f, &pair, 902);
    let lease = deploy::begin(&mut f.conn, request.clone(), now + 1)
        .unwrap()
        .lease
        .unwrap();
    f.lease = lease.lease_id;
    let candidate_batch = f.batch.clone();
    let candidate_lease = f.lease.clone();
    let other = deployment_request(&mut f, &pair, 903);
    assert!(deploy::begin(&mut f.conn, other, now + 2)
        .unwrap()
        .lease
        .is_none());
    f.batch = candidate_batch;
    f.lease = candidate_lease;
    let first = f.begin(slot(0, 0), 0).unwrap();
    f.done(&first, Status::Completed);
    // An ordinary new acquisition is not part of the active deployment's
    // frozen budget and must hold its next independent physical dispatch.
    let p = f.prepared.clone();
    f.reset(p, 904, now + 3, None, Policy::default()).unwrap();
    f.batch = request.preparation.batch_id;
    f.lease = lease_id_for_batch(&f.conn, &f.batch);
    let before = f
        .conn
        .query_row(
            "SELECT COUNT(*) FROM provider_receipt_invocation",
            [],
            |r| r.get::<_, u64>(0),
        )
        .unwrap();
    assert!(f.begin(slot(1, 0), 0).is_err());
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        before
    );
}

fn lease_id_for_batch(conn: &Connection, batch: &str) -> String {
    conn.query_row(
        "SELECT lease_id FROM provider_graph_preparation WHERE batch_id=?1",
        [batch],
        |r| r.get(0),
    )
    .unwrap()
}

#[test]
fn original_lifetime_resource_and_unsettled_lookup_times_cannot_be_repriced() {
    use agentc_optimizer::provider_graph_economics as finance;
    let mut f = financial_fixture();
    let pair = priced_pairs(&mut f, 20);
    let decision = agentc_optimizer::provider_graph_admission::select(
        &f.conn,
        admission_request(&pair, pair.observed_at_us),
    )
    .unwrap();
    let driver = decision.acquisition.economics.driver_id;
    let old:String=f.conn.query_row("SELECT batch_id FROM provider_graph_acquisition WHERE driver_id=?1 ORDER BY batch_id LIMIT 1",[&driver],|r|r.get(0)).unwrap();
    f.conn
        .execute(
            "UPDATE provider_graph_acquisition SET wall_latency_ms=0 WHERE batch_id=?1",
            [&old],
        )
        .unwrap();
    assert!(finance::summarize_acquisition(&f.conn, &driver).is_err());
    f.conn
        .execute(
            "UPDATE provider_graph_acquisition SET wall_latency_ms=100 WHERE batch_id=?1",
            [&old],
        )
        .unwrap();
    let (id,call,done):(String,String,String)=f.conn.query_row("SELECT invocation_id,invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 LIMIT 1",[&old],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();
    let binding = agentc_core::storage::canonical_json(
        &json!({"version":"provider-billing-binding-v1","batch_id":old,"invocation_id":id,"invocation":call,"completion":done}),
    );
    let raw = agentc_core::storage::canonical_json(&json!({"latency_ms":5.,"invoice":null}));
    f.conn
        .execute(
            "INSERT INTO provider_billing_attempt VALUES('resource-test',?1,?2,?3,?4,?4)",
            rusqlite::params![id, old, agentc_core::storage::content_hash(&binding), raw],
        )
        .unwrap();
    let good = finance::summarize_acquisition(&f.conn, &driver).unwrap();
    assert_eq!(good.economics.billing_verification_latency_ms, 5.);
    f.conn
        .execute(
            "UPDATE provider_billing_attempt SET result_json=?1 WHERE attempt_id='resource-test'",
            [agentc_core::storage::canonical_json(
                &json!({"latency_ms":0.,"invoice":null}),
            )],
        )
        .unwrap();
    assert!(finance::summarize_acquisition(&f.conn, &driver).is_err());
}

#[test]
fn guarded_deployment_custody_survives_restart_and_missing_approval_holds_before_dispatch() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let file = tempfile::NamedTempFile::new().unwrap();
    let mut f = financial_fixture();
    f.conn = Connection::open(file.path()).unwrap();
    agentc_optimizer::schema::ensure_cost_model_schema(&f.conn).unwrap();
    let pair = priced_pairs(&mut f, 20);
    let now = pair.observed_at_us + 10;
    let request = deployment_request(&mut f, &pair, 905);
    let original = deploy::begin(&mut f.conn, request.clone(), now)
        .unwrap()
        .lease
        .unwrap();
    drop(f.conn);
    f.conn = Connection::open(file.path()).unwrap();
    f.lease = original.lease_id.clone();
    let retry = deploy::begin(&mut f.conn, request, now + 1).unwrap();
    assert_eq!(retry.lease.unwrap(), original);
    f.conn
        .execute(
            "DELETE FROM provider_graph_deployment WHERE lease_id=?1",
            [&f.lease],
        )
        .unwrap();
    assert!(f.begin(slot(0, 0), 0).is_err());
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                [&f.batch],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn guarded_deployment_live_sdk_policy_and_retired_window_hold_original_approval() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let mut f = financial_fixture();
    let pair = priced_pairs(&mut f, 20);
    let now = pair.observed_at_us + 10;
    let mut request = deployment_request(&mut f, &pair, 906);
    let active = agentc_optimizer::execution_plan::SelectionPolicy::default();
    request.admission.policy.selection.max_profile_age_us = active.max_profile_age_us;
    deploy::validate_sdk_policy(&request, Policy::default(), &active, 20).unwrap();
    let lease = deploy::begin(&mut f.conn, request.clone(), now)
        .unwrap()
        .lease
        .unwrap();
    f.lease = lease.lease_id;
    let mut changed = active.clone();
    changed.objective = agentc_optimizer::execution_plan::SelectionObjective::Latency;
    assert!(
        deploy::verify_dispatch_policy(&f.conn, &f.batch, Policy::default(), &changed, 20).is_err()
    );
    changed = active;
    changed.max_profile_age_us /= 2;
    assert!(
        deploy::verify_dispatch_policy(&f.conn, &f.batch, Policy::default(), &changed, 20).is_err()
    );
    f.conn
        .execute(
            "UPDATE provider_graph_role_execution_state SET window_size=21",
            [],
        )
        .unwrap();
    assert!(f.begin(slot(0, 0), 0).is_err());
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                [&f.batch],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn persisted_tariff_certificate_roundtrips_signed_nanodollars_without_json_integer_narrowing() {
    use agentc_optimizer::{provider_graph_admission::Certificate, provider_tariff as tariff};
    let bound = tariff::Bound {
        quantity: tariff::QUANTITY.into(),
        snapshot_sha256: tariff::snapshot_sha256(),
        interval: tariff::Interval {
            lower_nanodollars: 0,
            upper_nanodollars: 0,
        },
    };
    for net in [1_i128, -1_i128, i128::from(u64::MAX), -i128::from(u64::MAX)] {
        let certificate = Certificate::PublishedTokenTariffUsd {
            reference: bound.clone(),
            candidate: bound.clone(),
            acquisition: bound.clone(),
            expected_upper_nanodollars: 0,
            net_savings_lower_nanodollars: net,
        };
        let raw = serde_json::to_string(&certificate).unwrap();
        assert_eq!(
            serde_json::from_str::<Certificate>(&raw).unwrap(),
            certificate
        );
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&raw).unwrap()
                ["net_savings_lower_nanodollars"],
            net.to_string()
        );
    }
}

#[test]
fn repeated_deployment_requests_cannot_replenish_the_original_remaining_horizon() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let mut f = financial_fixture();
    let pair = priced_pairs(&mut f, 20);
    let now = pair.observed_at_us + 10;
    for (n, remaining) in [(0, 26), (1, 25)] {
        let mut request = deployment_request(&mut f, &pair, 910 + n);
        request.admission.remaining_batches = 26;
        let outcome = deploy::begin(&mut f.conn, request, now + i64::from(n) * 5).unwrap();
        assert_eq!(outcome.decision.request.remaining_batches, remaining);
        f.lease = outcome.lease.unwrap().lease_id;
        complete_deployment(&mut f, now + i64::from(n) * 5 + 1);
    }
    let mut request = deployment_request(&mut f, &pair, 912);
    request.admission.remaining_batches = 26;
    let held = deploy::begin(&mut f.conn, request, now + 11).unwrap();
    assert!(held.lease.is_none());
    assert_eq!(held.decision.request.remaining_batches, 24);
    assert_eq!(
        held.decision.alternatives[0].rejection.as_deref(),
        Some("nonpositive_net_cost_benefit")
    );
}

#[test]
fn graph_admission_requires_twenty_pairs_and_positive_whole_driver_amortized_benefit() {
    use agentc_optimizer::provider_graph_admission::{self as admission, Certificate, Money};
    let mut f = financial_fixture();
    let p = priced_pairs(&mut f, 19);
    let held = admission::select(&f.conn, admission_request(&p, p.observed_at_us)).unwrap();
    assert!(held.selected_reference);
    assert_eq!(
        held.alternatives[0].rejection.as_deref(),
        Some("insufficient_complete_graph_evidence")
    );
    let r = publish_financial_program(
        &mut f,
        admission_program(true),
        538,
        FinancialMode::ReportedCheap,
        100.,
        None,
        None,
    );
    let c = publish_financial_program(
        &mut f,
        admission_program(false),
        539,
        FinancialMode::ReportedCheap,
        10.,
        None,
        None,
    );
    let p = agentc_optimizer::provider_graph_role_comparison::compare_with_policy(
        &mut f.conn,
        &r.execution_id,
        &c.execution_id,
        c.observed_at_us + 1,
        20,
        Policy::default(),
    )
    .unwrap()
    .pair;
    let request = admission_request(&p, p.observed_at_us);
    let selected = admission::select(&f.conn, request.clone()).unwrap();
    assert!(!selected.selected_reference);
    assert_eq!(selected.selected_key, p.key);
    assert_eq!(selected.acquisition.costs.physical_invocations, 80);
    match selected.alternatives[0].estimate.as_ref().unwrap().money {
        Certificate::BilledUsd {
            expected_upper_usd,
            net_savings_lower_usd,
            ..
        } => {
            assert!((0.00244..0.002441).contains(&expected_upper_usd));
            assert!(net_savings_lower_usd > 0.01755 && net_savings_lower_usd <= 0.01756);
        }
        _ => panic!("wrong monetary quantity"),
    }
    let mut short = request.clone();
    short.remaining_batches = 1;
    assert_eq!(
        admission::select(&f.conn, short).unwrap().alternatives[0]
            .rejection
            .as_deref(),
        Some("nonpositive_net_cost_benefit")
    );
    let mut tariff = request;
    tariff.policy.money = Money::PublishedTokenTariffUsd {
        snapshot_sha256: agentc_optimizer::provider_tariff::snapshot_sha256(),
        max_expected_nanodollars: None,
    };
    let selected = admission::select(&f.conn, tariff.clone()).unwrap();
    assert!(!selected.selected_reference);
    match selected.alternatives[0].estimate.as_ref().unwrap().money {
        Certificate::PublishedTokenTariffUsd {
            expected_upper_nanodollars,
            net_savings_lower_nanodollars,
            ..
        } => {
            assert_eq!(expected_upper_nanodollars, 2436);
            assert_eq!(net_savings_lower_nanodollars, 27564);
        }
        _ => panic!("wrong monetary quantity"),
    }
    tariff.policy.selection.max_expected_cost_usd = Some(1.);
    assert!(admission::select(&f.conn, tariff).is_err());
}

#[test]
fn graph_admission_enforces_resource_money_freshness_and_pending_accounting_limits() {
    use agentc_optimizer::provider_graph_admission as admission;
    let mut f = financial_fixture();
    let p = priced_pairs(&mut f, 20);
    let request = admission_request(&p, p.observed_at_us);
    for (mut policy, reason) in [
        (
            {
                let mut p = request.policy.clone();
                p.max_physical_invocations = 1;
                p
            },
            "physical_resource_limit",
        ),
        (
            {
                let mut p = request.policy.clone();
                p.selection.max_expected_cost_usd = Some(0.002);
                p
            },
            "expected_cost_limit",
        ),
        (
            {
                let mut p = request.policy.clone();
                p.selection.max_expected_latency_ms = Some(11.);
                p
            },
            "expected_latency_limit",
        ),
    ] {
        policy.selection.now_us = request.policy.selection.now_us;
        let mut r = request.clone();
        r.policy = policy;
        assert_eq!(
            admission::select(&f.conn, r).unwrap().alternatives[0]
                .rejection
                .as_deref(),
            Some(reason)
        );
    }
    let mut stale = request.clone();
    stale.policy.selection.now_us += stale.policy.selection.max_profile_age_us + 1;
    assert_eq!(
        admission::select(&f.conn, stale).unwrap().alternatives[0]
            .rejection
            .as_deref(),
        Some("stale_or_preboundary_cost_evidence")
    );
    f.reset(
        admission_program(false),
        999,
        10000,
        None,
        Policy::default(),
    )
    .unwrap();
    let held = admission::select(&f.conn, request).unwrap();
    assert!(held.selected_reference);
    assert_eq!(
        held.alternatives[0].rejection.as_deref(),
        Some("incomplete_compatible_quantity_costs")
    );
    assert_eq!(held.acquisition.economics.unfinished_batches, 1);
}

#[test]
fn original_source_controls_cannot_be_mixed_into_graph_admission_windows() {
    use agentc_optimizer::provider_graph_admission as admission;
    let mut f = financial_fixture();
    let p = priced_pairs(&mut f, 20);
    let mut r = admission_program(true);
    let mut c = admission_program(false);
    r.source_contracts[0].sha256 = "0".repeat(64);
    c.source_contracts[0].sha256 = "0".repeat(64);
    let r = publish_financial_program(
        &mut f,
        r,
        900,
        FinancialMode::ReportedCheap,
        100.,
        None,
        None,
    );
    let c = publish_financial_program(
        &mut f,
        c,
        901,
        FinancialMode::ReportedCheap,
        10.,
        None,
        None,
    );
    agentc_optimizer::provider_graph_role_comparison::compare_with_policy(
        &mut f.conn,
        &r.execution_id,
        &c.execution_id,
        c.observed_at_us + 1,
        20,
        Policy::default(),
    )
    .unwrap();
    let d = admission::select(&f.conn, admission_request(&p, c.observed_at_us + 1)).unwrap();
    assert!(d.selected_reference);
    assert_eq!(
        d.alternatives[0].rejection.as_deref(),
        Some("incompatible_original_source_controls")
    );
}

#[test]
fn frozen_graph_worker_limit_holds_independent_dispatch_and_releases_after_completion() {
    let mut f = financial_fixture();
    let mut p = admission_program(false);
    let move_position = |p: &mut CallPosition| {
        if p.call == 1 {
            p.item = 1;
            p.call = 0;
        }
    };
    for b in &mut p.program.graph.calls.branches {
        move_position(&mut b.position);
    }
    for d in &mut p.program.dispatches {
        move_position(&mut d.slot.position);
    }
    for b in &mut p.bindings {
        move_position(&mut b.slot.position);
    }
    for s in p.source_contracts.iter_mut().chain(&mut p.logical_requests) {
        move_position(&mut s.position);
    }
    p.program.graph.calls.item_count = 2;
    p.program.graph.results = Results::CallbackItems { items: vec![0, 1] };
    p.program.graph.dependencies.clear();
    f.reset(p, 1001, 1, None, Policy::default()).unwrap();
    let first = f.begin(slot(0, 0), 0).unwrap();
    f.begin(slot(0, 0), 0).unwrap(); // Exact reservation retry does not occupy a second slot.
    let independent = Slot {
        position: CallPosition { item: 1, call: 0 },
        ordinal: 0,
    };
    assert!(f.begin(independent, 0).is_err());
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        1
    );
    f.done(&first, Status::Completed);
    f.begin(independent, 0).unwrap();
}

#[test]
fn latency_graph_admission_uses_latency_benefit_with_a_known_cost_limit() {
    use agentc_optimizer::{
        execution_plan::SelectionObjective, provider_graph_admission as admission,
    };
    let mut f = financial_fixture();
    let mut last = None;
    for n in 0..20 {
        let r = publish_financial_program(
            &mut f,
            admission_program(true),
            1100 + n * 2,
            FinancialMode::Reported,
            100.,
            None,
            None,
        );
        let c = publish_financial_program(
            &mut f,
            admission_program(false),
            1101 + n * 2,
            FinancialMode::Reported,
            10.,
            None,
            None,
        );
        last = Some(
            agentc_optimizer::provider_graph_role_comparison::compare_with_policy(
                &mut f.conn,
                &r.execution_id,
                &c.execution_id,
                c.observed_at_us + 1,
                20,
                Policy::default(),
            )
            .unwrap()
            .pair,
        );
    }
    let p = last.unwrap();
    let mut request = admission_request(&p, p.observed_at_us);
    assert!(
        admission::select(&f.conn, request.clone())
            .unwrap()
            .selected_reference
    );
    request.policy.selection.objective = SelectionObjective::Latency;
    request.policy.selection.max_expected_cost_usd = Some(0.021);
    let selected = admission::select(&f.conn, request.clone()).unwrap();
    assert!(!selected.selected_reference);
    assert!(
        selected.alternatives[0]
            .estimate
            .as_ref()
            .unwrap()
            .net_latency_savings_lower_ms
            > 87.
    );
    request.policy.selection.max_expected_cost_usd = Some(0.02);
    assert_eq!(
        admission::select(&f.conn, request).unwrap().alternatives[0]
            .rejection
            .as_deref(),
        Some("expected_cost_limit")
    );
}

#[test]
fn cold_graph_admission_requires_complete_fresh_costs_and_twenty_prepared_pairs_without_clearing_disable(
) {
    use agentc_optimizer::{
        provider_graph_admission as admission, provider_graph_role_comparison as comparison,
    };
    let mut f = financial_fixture();
    let r = publish_financial_program(
        &mut f,
        admission_program(true),
        1200,
        FinancialMode::ReportedCheap,
        100.,
        None,
        None,
    );
    let c = publish_financial_program(
        &mut f,
        admission_program(false),
        1201,
        FinancialMode::AdverseCheap,
        10.,
        None,
        None,
    );
    let source = comparison::compare_with_policy(
        &mut f.conn,
        &r.execution_id,
        &c.execution_id,
        c.observed_at_us + 1,
        20,
        Policy::default(),
    )
    .unwrap();
    let boundary = source.quality.unwrap().disabled.unwrap().reenable_at_us;
    let mut now = boundary + 1;
    for n in 0..20 {
        let r = publish_financial_program(
            &mut f,
            admission_program(true),
            1300 + n * 2,
            FinancialMode::ReportedCheap,
            100.,
            Some(&source.pair.pair_id),
            Some(now),
        );
        let c = publish_financial_program(
            &mut f,
            admission_program(false),
            1301 + n * 2,
            FinancialMode::ReportedCheap,
            10.,
            Some(&source.pair.pair_id),
            Some(now + 2),
        );
        comparison::compare_with_policy(
            &mut f.conn,
            &r.execution_id,
            &c.execution_id,
            now + 4,
            20,
            Policy::default(),
        )
        .unwrap();
        let d = admission::select(&f.conn, admission_request(&source.pair, now + 4)).unwrap();
        if n < 19 {
            assert!(d.selected_reference);
        } else {
            assert!(!d.selected_reference);
            assert!(
                d.alternatives[0]
                    .estimate
                    .as_ref()
                    .unwrap()
                    .readmission_required
            );
            assert_eq!(
                d.alternatives[0]
                    .estimate
                    .as_ref()
                    .unwrap()
                    .quality
                    .prepared_pairs
                    .len(),
                20
            );
        }
        now += 5;
    }
    assert_eq!(f.conn.query_row("SELECT COUNT(*) FROM provider_graph_role_quality_state WHERE disabled_at IS NOT NULL",[],|r|r.get::<_,u64>(0)).unwrap(),1);
    let request = deployment_request(&mut f, &source.pair, 990);
    let deployed =
        agentc_optimizer::provider_graph_deployment::begin(&mut f.conn, request, now + 1).unwrap();
    f.lease = deployed.lease.unwrap().lease_id;
    assert!(!deployed.decision.selected_reference);
    assert_eq!(f.conn.query_row("SELECT COUNT(*) FROM provider_graph_role_quality_state WHERE disabled_at IS NOT NULL",[],|r|r.get::<_,u64>(0)).unwrap(),0);
    let call = f.begin(slot(0, 0), 0).unwrap();
    f.done(&call, Status::Completed);
}

#[test]
fn role_graph_financial_views_count_all_real_roles_and_separate_bill_from_tariff() {
    use agentc_optimizer::provider_graph_economics::{self as finance, Kind};
    let mut f = financial_fixture();
    let e = publish_priced(&mut f, 201, FinancialMode::Reported);
    let projection = finance::execution(&f.conn, Kind::Roles, &e.execution_id).unwrap();
    assert_eq!(projection.costs.physical_invocations, 5);
    let usd = projection.costs.billed_usd.unwrap();
    assert!(usd.lower_usd <= 0.05 && usd.upper_usd >= 0.05);
    let token = projection.costs.published_token_tariff.unwrap();
    assert_eq!(token.interval.lower_nanodollars, 46800);
    assert_eq!(token.interval.upper_nanodollars, 46800);
    assert_eq!(
        projection
            .costs
            .receipts
            .iter()
            .filter(|r| r.settlement_sha256.is_some())
            .count(),
        0
    );
    let window = finance::window(&f.conn, Kind::Roles, &e.key, &e.runtime, 20)
        .unwrap()
        .unwrap();
    assert_eq!(window.lifetime_executions, 1);
    assert_eq!(window.observations.len(), 1);
    assert_eq!(window.foreground_latency_mean_ms, Some(100.));
    let lifetime =
        finance::summarize_acquisition(&f.conn, &e_driver(&f.conn, &e.batch_id)).unwrap();
    assert!(lifetime.economics.cost_complete && lifetime.costs.billed_usd.is_some());
    assert_eq!(
        lifetime.costs.published_token_tariff.unwrap().interval,
        token.interval
    );
    assert!(
        finance::window(&f.conn, Kind::Primary, &e.key, &e.runtime, 20)
            .unwrap()
            .is_none()
    );
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_billing_settlement",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COALESCE(SUM(n_paired_observations),0) FROM execution_plan_profile",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn failed_role_charges_and_unknown_newest_usage_cannot_be_filtered_from_cost_windows() {
    use agentc_optimizer::provider_graph_economics::{self as finance, Kind};
    let mut f = financial_fixture();
    let first = publish_priced(&mut f, 211, FinancialMode::Reported);
    let unknown = publish_priced(&mut f, 212, FinancialMode::UnknownTariff);
    let window = finance::window(&f.conn, Kind::Roles, &first.key, &first.runtime, 20)
        .unwrap()
        .unwrap();
    assert_eq!(window.observations[0].execution_id, unknown.execution_id);
    assert_eq!(window.observations.len(), 2);
    assert!(window.published_token_tariff_mean.is_none());
    assert!(window.billed_usd_mean.is_some());
    let failed = publish_priced(&mut f, 213, FinancialMode::FailedPrimary);
    let costs = finance::execution(&f.conn, Kind::Roles, &failed.execution_id)
        .unwrap()
        .costs;
    assert_eq!(costs.physical_invocations, 6);
    assert_eq!(costs.unpriced_usd_invocations, 1);
    assert_eq!(costs.unpriced_tariff_invocations, 1);
    assert!(costs.billed_usd.is_none() && costs.published_token_tariff.is_none());
    assert!(costs.known_billed_usd.lower_usd <= 0.06 && costs.known_billed_usd.upper_usd >= 0.06);
    let driver = e_driver(&f.conn, &failed.batch_id);
    let lifetime = finance::summarize_acquisition(&f.conn, &driver).unwrap();
    assert_eq!(lifetime.costs.physical_invocations, 16);
    assert!(lifetime.costs.billed_usd.is_none() && lifetime.costs.published_token_tariff.is_none());
}

fn e_driver(conn: &Connection, batch: &str) -> String {
    conn.query_row(
        "SELECT driver_id FROM provider_graph_acquisition WHERE batch_id=?1",
        [batch],
        |r| r.get(0),
    )
    .unwrap()
}

#[test]
fn deleting_an_unknown_role_execution_cannot_reveal_a_priced_older_window() {
    use agentc_optimizer::provider_graph_economics::{self as finance, Kind};
    let mut f = financial_fixture();
    let good = publish_priced(&mut f, 214, FinancialMode::Reported);
    let unknown = publish_priced(&mut f, 215, FinancialMode::UnknownTariff);
    let w = finance::window(&f.conn, Kind::Roles, &good.key, &good.runtime, 20)
        .unwrap()
        .unwrap();
    assert!(w.published_token_tariff_mean.is_none());
    f.conn
        .execute(
            "DELETE FROM provider_graph_role_execution WHERE execution_id=?1",
            [&unknown.execution_id],
        )
        .unwrap();
    assert!(finance::window(&f.conn, Kind::Roles, &good.key, &good.runtime, 20).is_err());
}

#[test]
fn role_financial_retirement_and_unfinished_variants_preserve_lifetime_liabilities() {
    use agentc_optimizer::provider_graph_economics::{self as finance, Kind};
    let mut f = financial_fixture();
    let old = publish_priced(&mut f, 221, FinancialMode::Reported);
    let driver = e_driver(&f.conn, &old.batch_id);
    let mut changed = priced_program();
    for b in &mut changed.bindings {
        b.runtime.price_table_version = "prices-v2".into();
    }
    // A pending new variant cannot erase the older owned spend.
    f.reset(changed, 222, 2220, None, Policy::default())
        .unwrap();
    let lifetime = finance::summarize_acquisition(&f.conn, &driver).unwrap();
    assert_eq!(lifetime.economics.owned_batches, 2);
    assert_eq!(lifetime.costs.physical_invocations, 5);
    assert!(lifetime.costs.billed_usd.is_none() && lifetime.costs.published_token_tariff.is_none());
    assert!(lifetime.costs.known_billed_usd.upper_usd >= 0.05);
    f.conn.execute("UPDATE provider_graph_role_execution_state SET runtime_json=json_set(runtime_json,'$.price_table_version','retired')", []).unwrap();
    assert!(
        finance::window(&f.conn, Kind::Roles, &old.key, &old.runtime, 20)
            .unwrap()
            .is_none()
    );
    assert_eq!(
        finance::execution(&f.conn, Kind::Roles, &old.execution_id)
            .unwrap()
            .costs
            .physical_invocations,
        5
    );
    assert!(finance::window(&f.conn, Kind::Roles, &old.key, &old.runtime, 0).is_err());
}

#[test]
fn late_role_invoice_resolves_original_cost_slots_without_new_execution_or_quality() {
    use agentc_optimizer::{
        provider_graph_economics::{self as finance, Kind},
        provider_reconciliation as billing,
    };
    let mut f = financial_fixture();
    let e = publish_priced(&mut f, 231, FinancialMode::EstimatedInvoice);
    let before = finance::window(&f.conn, Kind::Roles, &e.key, &e.runtime, 20)
        .unwrap()
        .unwrap();
    assert!(before.billed_usd_mean.is_none() && before.published_token_tariff_mean.is_none());
    let execution_raw: String = f
        .conn
        .query_row(
            "SELECT execution_json FROM provider_graph_role_execution WHERE execution_id=?1",
            [&e.execution_id],
            |r| r.get(0),
        )
        .unwrap();
    let bindings = billing::bindings(&f.conn, &e.batch_id).unwrap();
    for (n, b) in bindings.iter().enumerate() {
        let attempt = format!("{:032x}", 1000 + n);
        let invoice = json!({"issuer":billing::ISSUER,"receipt_sha256":b.receipt_sha256,"billing_scope_sha256":b.billing_scope_sha256,"generation_sha256":b.generation_sha256,"request_id_sha256":b.request_id_sha256,
            "model_id":b.model_version,"input_tokens":2,"output_tokens":1,"cost_usd":0.02,"cancelled":false,"finish_reason":"stop"});
        receipts::handle_json(&mut f.conn, &json!({"operation":"begin_billing_lookup","batch_id":e.batch_id,"invocation_id":b.invocation_id,"attempt_id":attempt,"receipt_sha256":b.receipt_sha256}).to_string()).unwrap();
        let done = json!({"operation":"finish_billing_lookup","attempt_id":attempt,"latency_ms":7.,"invoice":invoice});
        receipts::handle_json(&mut f.conn, &done.to_string()).unwrap();
        receipts::handle_json(&mut f.conn, &done.to_string()).unwrap();
    }
    let after = finance::window(&f.conn, Kind::Roles, &e.key, &e.runtime, 20)
        .unwrap()
        .unwrap();
    assert_eq!(before.source_sequence, after.source_sequence);
    assert_eq!(before.lifetime_executions, after.lifetime_executions);
    assert_eq!(
        before.observations[0].execution_id,
        after.observations[0].execution_id
    );
    let usd = after.billed_usd_mean.unwrap();
    assert!(usd.lower_usd <= 0.1 && usd.upper_usd >= 0.1);
    assert!(after.published_token_tariff_mean.is_none());
    assert_eq!(
        f.conn
            .query_row(
                "SELECT execution_json FROM provider_graph_role_execution WHERE execution_id=?1",
                [&e.execution_id],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
        execution_raw
    );
    assert_eq!(
        f.conn
            .query_row(
                "SELECT COALESCE(SUM(n_paired_observations),0) FROM execution_plan_profile",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    // Rehashed, receipt-compatible repricing still lacks the authenticated input.
    f.conn.execute("UPDATE provider_billing_settlement SET invoice_json=json_set(invoice_json,'$.cost_usd',0.03) WHERE invocation_id=?1", [&bindings[0].invocation_id]).unwrap();
    assert!(finance::window(&f.conn, Kind::Roles, &e.key, &e.runtime, 20).is_err());
}

#[test]
fn shared_campaign_role_deployment_retains_frozen_work_and_releases_verified_unused_money() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_deployment as deploy,
        provider_graph_sampling as sampling,
    };
    let mut f = financial_fixture();
    let pair = priced_pairs(&mut f, 20);
    let now = pair.observed_at_us + 10;
    let p = campaign::Policy {
        campaign_id: "6".repeat(64),
        commitments: sampling::Commitments {
            money: sampling::CommitmentMoney::BilledUsd {
                workflow_usd: 0.01,
                lifetime_usd: 5.0,
            },
            max_open_workflows: 1,
            max_pending_invocations: 1,
        },
        max_workflows: 1000,
        max_physical_invocations: 2,
        max_lifetime_physical_invocations: 1000,
    };
    let mut request = deployment_request(&mut f, &pair, 900);
    request.campaign = Some(p.clone());
    let outcome = deploy::begin(&mut f.conn, request.clone(), now).unwrap();
    let lease = outcome.lease.unwrap();
    f.lease = lease.lease_id.clone();
    let before = campaign::report(&f.conn, &p.campaign_id).unwrap();
    assert_eq!(before.held_workflows, 1);
    assert_eq!(before.billed_committed_upper_usd, Some(0.01));
    complete_deployment(&mut f, now + 2);
    let after = campaign::report(&f.conn, &p.campaign_id).unwrap();
    assert_eq!(
        (
            after.physical_invocations,
            after.committed_physical_invocations,
            after.held_workflows
        ),
        (2, 2, 0)
    );
    assert!(
        after.billed_committed_upper_usd.unwrap() >= 0.002
            && after.billed_committed_upper_usd.unwrap() < 0.003
    );
    let mut changed = deployment_request(&mut f, &pair, 901);
    changed.campaign = Some(p.clone());
    changed
        .campaign
        .as_mut()
        .unwrap()
        .commitments
        .max_open_workflows = 2;
    assert!(deploy::begin(&mut f.conn, changed, now + 3).is_err());
    for campaign in [
        None,
        Some(campaign::Policy {
            campaign_id: "7".repeat(64),
            ..p.clone()
        }),
    ] {
        let mut omitted = deployment_request(&mut f, &pair, 902);
        omitted.campaign = campaign;
        let error = deploy::begin(&mut f.conn, omitted, now + 4).unwrap_err();
        assert!(
            error.to_string().contains("original campaign commitment"),
            "{error}"
        );
    }
    let mut next = deployment_request(&mut f, &pair, 903);
    next.campaign = Some(p.clone());
    assert!(deploy::begin(&mut f.conn, next, now + 5)
        .unwrap()
        .lease
        .is_some());
    assert_eq!(
        campaign::report(&f.conn, &p.campaign_id).unwrap().workflows,
        2
    );
}
