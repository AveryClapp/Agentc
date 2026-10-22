use std::collections::BTreeMap;

use agentc_optimizer::execution_plan::{
    CachePolicy, ExecutionPlanSpec, RewriteApplication, RewriteOrdering, ValidationPolicy,
};
use agentc_optimizer::multi_call::{
    CallPosition, InferenceCostSource, MultiCallBranch, MultiCallOutcome, MultiCallPlanSpec,
    MultiCallRuntimeBranch,
};
use agentc_optimizer::plan_profile::{
    CallSiteVersion, PlanProfileKey, PlanProfiles, PlanRuntimeVersion,
};
use agentc_optimizer::provider_graph::{
    self, Comparison, Dependency, Execution, PublishRequest, Results, Spec,
};
use agentc_optimizer::provider_graph_quality::{self as quality, Policy, Request};
use agentc_optimizer::{
    provider_graph_comparison as comparison, provider_graph_preparation as preparation,
    provider_receipts, Outcome,
};
use rusqlite::Connection;
use serde_json::json;

fn spec(reference: bool) -> Spec {
    Spec {
        version: provider_graph::VERSION.into(),
        calls: MultiCallPlanSpec {
            application_version: "a".repeat(64),
            callback_version: "whole-results-fixture-v1".into(),
            item_count: 2,
            max_workers: if reference { 1 } else { 2 },
            comparison_version: None,
            execution_contract: None,
            branches: [(0, 0), (0, 1), (1, 0)]
                .into_iter()
                .enumerate()
                .map(|(i, (item, call))| {
                    let alternate = !reference && i != 1;
                    MultiCallBranch {
                        position: CallPosition { item, call },
                        call_site_version: CallSiteVersion::parse(format!("{:064x}", i + 1))
                            .unwrap(),
                        plan: ExecutionPlanSpec {
                            schema_version: 1,
                            provider_protocol: "openai.chat.completions.v1".into(),
                            requested_model_id: "strong".into(),
                            target_model_id: if alternate { "cheap" } else { "strong" }.into(),
                            target_model_revision: Some("alias".into()),
                            rewrite_ordering: RewriteOrdering::Ordered,
                            rewrites: if alternate {
                                vec![RewriteApplication {
                                    stable_name: "OutputBudget".into(),
                                    implementation_version: "1".into(),
                                    parameters: json!({"max_output_tokens":64}),
                                }]
                            } else {
                                vec![]
                            },
                            cache_policy: CachePolicy {
                                stable_name: "provider-call".into(),
                                implementation_version: "1".into(),
                                parameters: json!({}),
                            },
                            output_budget: Some(if alternate { 64 } else { 128 }),
                            validation_policy: ValidationPolicy {
                                stable_name: if alternate {
                                    "sampled-reference"
                                } else {
                                    "none"
                                }
                                .into(),
                                implementation_version: "1".into(),
                                parameters: json!({}),
                            },
                        },
                    }
                })
                .collect(),
        },
        dependencies: vec![Dependency {
            producer: CallPosition { item: 0, call: 0 },
            consumer: CallPosition { item: 0, call: 1 },
        }],
        results: Results::CallbackItems { items: vec![0, 1] },
    }
}

struct Store {
    conn: Connection,
    profiles: PlanProfiles,
    prepared_pair: Option<comparison::Pair>,
    invocation_cost_usd: f64,
}

#[test]
fn primary_graph_admission_uses_whole_result_quality_costs_and_owned_resource_time() {
    use agentc_optimizer::{
        execution_plan::SelectionPolicy, provider_graph_admission as admission,
        provider_graph_economics::Kind,
    };
    let mut s = Store::new(20);
    let mut last = None;
    for n in 0..20 {
        s.invocation_cost_usd = 0.01;
        let r = s.seed(
            spec(true),
            2000 + n * 2,
            InferenceCostSource::ProviderReported,
            "r1",
            "c",
            "d",
        );
        s.command(json!({"operation":"finish_batch_economics","batch_id":r.batch_id,"wall_latency_ms":20.}));
        s.invocation_cost_usd = 0.001;
        let c = s.seed(
            spec(false),
            2001 + n * 2,
            InferenceCostSource::ProviderReported,
            "r1",
            "c",
            "d",
        );
        s.command(json!({"operation":"finish_batch_economics","batch_id":c.batch_id,"wall_latency_ms":20.}));
        last = Some(s.pair(&r, &c, c.observed_at_us + 1).unwrap().pair);
    }
    let p = last.unwrap();
    let request = admission::Request {
        kind: Kind::Primary,
        source_pair_ids: vec![p.pair_id.clone()],
        window_size: 20,
        remaining_batches: 1000,
        policy: admission::Policy {
            selection: SelectionPolicy {
                now_us: p.observed_at_us,
                ..SelectionPolicy::default()
            },
            quality: Policy::default(),
            money: admission::Money::BilledUsd,
            max_workers: 2,
            max_physical_invocations: 3,
        },
    };
    let d = admission::select(&s.conn, request.clone()).unwrap();
    assert!(!d.selected_reference);
    assert_eq!(d.selected_key, p.key);
    assert_eq!(d.acquisition.costs.physical_invocations, 120);
    let e = d.alternatives[0].estimate.as_ref().unwrap();
    assert_eq!(e.quality.pairs.len(), 20);
    assert_eq!(e.declared_workers, 2);
    assert_eq!(e.declared_physical_invocations, 3);
    // Existing financial snapshots and execution timing are separate records;
    // an altered mutable resource projection cannot replace original timing.
    s.conn.execute("UPDATE provider_graph_acquisition SET wall_latency_ms=0 WHERE batch_id=(SELECT batch_id FROM provider_graph_execution WHERE execution_id=?1)",[&p.candidate_execution_id]).unwrap();
    assert!(admission::select(&s.conn, request).is_err());
}

fn priced_primary_pairs(s: &mut Store) -> comparison::Pair {
    let mut last = None;
    for n in 0..20 {
        s.invocation_cost_usd = 0.01;
        let r = s.seed(
            spec(true),
            3000 + n * 2,
            InferenceCostSource::ProviderReported,
            "r1",
            "c",
            "d",
        );
        s.command(json!({"operation":"finish_batch_economics","batch_id":r.batch_id,"wall_latency_ms":20.}));
        s.invocation_cost_usd = 0.001;
        let c = s.seed(
            spec(false),
            3001 + n * 2,
            InferenceCostSource::ProviderReported,
            "r1",
            "c",
            "d",
        );
        s.command(json!({"operation":"finish_batch_economics","batch_id":c.batch_id,"wall_latency_ms":20.}));
        last = Some(s.pair(&r, &c, c.observed_at_us + 1).unwrap().pair);
    }
    last.unwrap()
}

fn primary_deployment_request(
    s: &Store,
    pair: &comparison::Pair,
    number: u64,
) -> agentc_optimizer::provider_graph_deployment::Begin {
    use agentc_optimizer::{
        provider_graph_admission as admission, provider_graph_deployment as deploy,
        provider_graph_economics::Kind,
    };
    let candidate = provider_graph::load(&s.conn, &pair.candidate_execution_id).unwrap();
    let batch = format!("{number:032x}");
    provider_receipts::begin_batch(
        &s.conn,
        &provider_receipts::Batch {
            version: provider_receipts::VERSION.into(),
            batch_id: batch.clone(),
            application_version: Some(candidate.spec.calls.application_version.clone()),
            callback_version: candidate.spec.calls.callback_version.clone(),
            item_count: 2,
        },
    )
    .unwrap();
    deploy::Begin {
        campaign: None,
        dynamic_primary_nodes: false,
        preparation: preparation::Begin {
            batch_id: batch,
            max_workers: 2,
            input_sha256: Some("e".repeat(64)),
            template_execution_id: Some(candidate.execution_id.clone()),
            comparison_pair_id: None,
            role_program: None,
        },
        admission: admission::Request {
            kind: Kind::Primary,
            source_pair_ids: vec![pair.pair_id.clone()],
            window_size: 20,
            remaining_batches: 1000,
            policy: admission::Policy {
                selection: agentc_optimizer::execution_plan::SelectionPolicy {
                    now_us: 0,
                    max_profile_age_us: i64::MAX,
                    ..Default::default()
                },
                quality: Policy::default(),
                money: admission::Money::BilledUsd,
                max_workers: 2,
                max_physical_invocations: 3,
            },
        },
        primary_bindings: (0..3)
            .map(|i| {
                let call = prepared_invocation(s, &candidate, i, number * 16 + i as u64);
                deploy::PrimaryBinding {
                    node: prepared_node(&candidate, i),
                    request_sha256: call.request_sha256.unwrap(),
                    transport_sha256: call.transport_sha256.unwrap(),
                }
            })
            .collect(),
    }
}

fn run_primary_deployment(s: &Store, pair: &comparison::Pair, batch: &str, number: u64) {
    let candidate = provider_graph::load(&s.conn, &pair.candidate_execution_id).unwrap();
    for i in 0..3 {
        let call = prepared_invocation(s, &candidate, i, number * 16 + i as u64);
        provider_receipts::begin_call(&s.conn, batch, &call).unwrap();
        complete_primary_invocation(s, batch, &call);
    }
}

fn complete_primary_invocation(s: &Store, batch: &str, call: &provider_receipts::Invocation) {
    provider_receipts::finish_call(
        &s.conn,
        batch,
        &call.invocation_id,
        &provider_receipts::Completion {
            status: provider_receipts::Status::Completed,
            model_version: Some(
                call.dispatch_runtime
                    .as_ref()
                    .unwrap()
                    .target_model_version
                    .clone(),
            ),
            input_tokens: Some(2),
            output_tokens: Some(1),
            cost_usd: Some(0.001),
            cost_source: InferenceCostSource::ProviderReported,
            latency_ms: 5.,
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

#[test]
fn guarded_primary_completion_preserves_its_domain_bindings_receipts_and_phase_costs() {
    use agentc_optimizer::{
        provider_graph_deployment as deploy, provider_graph_economics as finance,
    };
    let mut s = Store::new(20);
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let request = primary_deployment_request(&s, &pair, 4000);
    let batch = request.preparation.batch_id.clone();
    let admitted = deploy::begin(&mut s.conn, request.clone(), now).unwrap();
    let lease = admitted.lease.unwrap();
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_prepared_node WHERE lease_id=?1",
                [&lease.lease_id],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        3
    );
    assert_eq!(
        deploy::begin(&mut s.conn, request.clone(), now + 1)
            .unwrap()
            .lease
            .unwrap(),
        lease
    );
    run_primary_deployment(&s, &pair, &batch, 4000);
    s.command(json!({"operation":"seal_batch","batch_id":batch,"logical_calls":spec(false).calls.branches.iter().map(|b|b.position).collect::<Vec<_>>()}));
    s.command(json!({"operation":"finish_batch_economics","batch_id":batch,"wall_latency_ms":20.}));
    let comparison = Comparison {
        version: provider_graph::COMPARISON_VERSION.into(),
        input_sha256: "e".repeat(64),
        output_sha256: "d".repeat(64),
    };
    let e = deploy::finish_primary(
        &mut s.conn,
        &s.profiles,
        &lease.lease_id,
        comparison.clone(),
        now + 2,
    )
    .unwrap();
    assert_eq!(e.key, pair.key);
    assert_eq!(e.preparation_id.as_deref(), Some(lease.lease_id.as_str()));
    assert_eq!(e.totals.call_count, 3);
    assert_eq!(e.totals.wall_latency_ms, 20.);
    assert_eq!(
        deploy::finish_primary(
            &mut s.conn,
            &s.profiles,
            &lease.lease_id,
            comparison,
            now + 3
        )
        .unwrap(),
        e
    );
    let driver = admitted.decision.acquisition.economics.driver_id;
    let costs = finance::summarize_acquisition(&s.conn, &driver).unwrap();
    assert_eq!(costs.costs, admitted.decision.acquisition.costs);
    assert_eq!(costs.deployment.unwrap().costs.physical_invocations, 3);
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_pair", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        20
    );
    let role_table:bool=s.conn.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_graph_role_execution')",[],|r|r.get(0)).unwrap();
    if role_table {
        assert_eq!(
            s.conn
                .query_row(
                    "SELECT COUNT(*) FROM provider_graph_role_execution",
                    [],
                    |r| r.get::<_, u64>(0)
                )
                .unwrap(),
            0
        );
    }
}

#[test]
fn guarded_primary_preparation_rejects_incomplete_duplicate_and_changed_nodes_atomically() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let mut s = Store::new(20);
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    for (n, kind) in [
        "missing",
        "duplicate",
        "source",
        "runtime",
        "template",
        "input",
    ]
    .into_iter()
    .enumerate()
    {
        let mut request = primary_deployment_request(&s, &pair, 4100 + n as u64);
        let batch = request.preparation.batch_id.clone();
        match kind {
            "missing" => {
                request.primary_bindings.pop();
            }
            "duplicate" => request.primary_bindings[1] = request.primary_bindings[0].clone(),
            "source" => request.primary_bindings[0].node.source_contract_sha256 = "8".repeat(64),
            "runtime" => {
                request.primary_bindings[0].node.runtime.price_table_version = "retired".into()
            }
            "template" => {
                request.preparation.template_execution_id =
                    Some(pair.reference_execution_id.clone())
            }
            "input" => request.preparation.input_sha256 = None,
            _ => unreachable!(),
        }
        assert!(deploy::begin(&mut s.conn, request, now).is_err());
        assert_eq!(
            s.conn
                .query_row(
                    "SELECT COUNT(*) FROM provider_graph_preparation WHERE batch_id=?1",
                    [batch],
                    |r| r.get::<_, u64>(0)
                )
                .unwrap(),
            0
        );
    }
}

#[test]
fn guarded_primary_sdk_reservation_binds_actual_request_transport_role_and_dependencies() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let mut s = Store::new(20);
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let request = primary_deployment_request(&s, &pair, 4200);
    let batch = request.preparation.batch_id.clone();
    deploy::begin(&mut s.conn, request, now)
        .unwrap()
        .lease
        .unwrap();
    let e = provider_graph::load(&s.conn, &pair.candidate_execution_id).unwrap();
    let original = prepared_invocation(&s, &e, 0, 42000);
    for field in ["request", "transport", "logical", "role"] {
        let mut call = original.clone();
        match field {
            "request" => call.request_sha256 = Some("8".repeat(64)),
            "transport" => call.transport_sha256 = Some("8".repeat(64)),
            "logical" => call.logical_request_sha256 = Some("8".repeat(64)),
            "role" => call.role = provider_receipts::Role::Shadow,
            _ => unreachable!(),
        }
        assert!(provider_receipts::begin_call(&s.conn, &batch, &call).is_err());
    }
    assert!(
        provider_receipts::begin_call(&s.conn, &batch, &prepared_invocation(&s, &e, 1, 42001))
            .is_err()
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                [batch],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn guarded_primary_approval_survives_restart_and_missing_custody_holds_before_sdk() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let file = tempfile::NamedTempFile::new().unwrap();
    let mut s = Store::new(20);
    s.conn = Connection::open(file.path()).unwrap();
    agentc_optimizer::schema::ensure_cost_model_schema(&s.conn).unwrap();
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let request = primary_deployment_request(&s, &pair, 4300);
    let batch = request.preparation.batch_id.clone();
    let original = deploy::begin(&mut s.conn, request.clone(), now)
        .unwrap()
        .lease
        .unwrap();
    drop(s.conn);
    s.conn = Connection::open(file.path()).unwrap();
    assert_eq!(
        deploy::begin(&mut s.conn, request, now + 1)
            .unwrap()
            .lease
            .unwrap(),
        original
    );
    s.conn
        .execute(
            "DELETE FROM provider_graph_deployment WHERE lease_id=?1",
            [&original.lease_id],
        )
        .unwrap();
    let e = provider_graph::load(&s.conn, &pair.candidate_execution_id).unwrap();
    assert!(
        provider_receipts::begin_call(&s.conn, &batch, &prepared_invocation(&s, &e, 0, 43000))
            .is_err()
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                [batch],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn guarded_primary_cold_readmission_requires_twenty_complete_prepared_pairs_after_boundary() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let mut s = Store::new(20);
    let mut pair = priced_primary_pairs(&mut s);
    for n in 0..2 {
        s.invocation_cost_usd = 0.01;
        let r = s.seed(
            spec(true),
            4400 + n * 2,
            InferenceCostSource::ProviderReported,
            "r1",
            "c",
            "d",
        );
        s.command(json!({"operation":"finish_batch_economics","batch_id":r.batch_id,"wall_latency_ms":20.}));
        s.invocation_cost_usd = 0.001;
        let c = s.seed(
            spec(false),
            4401 + n * 2,
            InferenceCostSource::ProviderReported,
            "r1",
            "c",
            "f",
        );
        s.command(json!({"operation":"finish_batch_economics","batch_id":c.batch_id,"wall_latency_ms":20.}));
        pair = s.pair(&r, &c, c.observed_at_us + 1).unwrap().pair;
    }
    let boundary = quality_window(&s, &pair, pair.observed_at_us)
        .disabled
        .unwrap()
        .reenable_at_us;
    s.prepared_pair = Some(pair.clone());
    for n in 0..20_u32 {
        let now = boundary + 10 + i64::from(n) * 5;
        s.invocation_cost_usd = 0.01;
        let r = s.seed_at(
            spec(true),
            4500 + u64::from(n) * 2,
            InferenceCostSource::ProviderReported,
            "r1",
            ("c", "d"),
            (now, now + 1),
        );
        s.command(json!({"operation":"finish_batch_economics","batch_id":r.batch_id,"wall_latency_ms":20.}));
        s.invocation_cost_usd = 0.001;
        let c = s.seed_at(
            spec(false),
            4501 + u64::from(n) * 2,
            InferenceCostSource::ProviderReported,
            "r1",
            ("c", "d"),
            (now + 2, now + 3),
        );
        s.command(json!({"operation":"finish_batch_economics","batch_id":c.batch_id,"wall_latency_ms":20.}));
        s.pair(&r, &c, now + 4).unwrap();
        if n == 18 {
            let request = primary_deployment_request(&s, &pair, 4600);
            assert!(deploy::begin(&mut s.conn, request, now + 4)
                .unwrap()
                .lease
                .is_none());
        }
    }
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_quality_state WHERE disabled_at IS NOT NULL",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        1
    );
    let request = primary_deployment_request(&s, &pair, 4601);
    let admitted = deploy::begin(&mut s.conn, request, boundary + 111).unwrap();
    assert!(admitted.lease.is_some() && !admitted.decision.selected_reference);
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_quality_state WHERE disabled_at IS NOT NULL",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}
fn dynamic_primary_request(
    s: &Store,
    pair: &comparison::Pair,
    number: u64,
) -> agentc_optimizer::provider_graph_deployment::Begin {
    let mut r = primary_deployment_request(s, pair, number);
    r.dynamic_primary_nodes = true;
    r.primary_bindings.clear();
    r
}

#[test]
fn template_planning_requires_current_policy_ready_dependencies_and_an_unused_position() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let mut s = Store::new(20);
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let request = dynamic_primary_request(&s, &pair, 5300);
    let batch = request.preparation.batch_id.clone();
    let lease = deploy::begin(&mut s.conn, request, now)
        .unwrap()
        .lease
        .unwrap();
    let pos = CallPosition { item: 0, call: 0 };
    let consumer = CallPosition { item: 0, call: 1 };
    assert_eq!(
        preparation::template_for_dispatch(
            &s.conn,
            &lease.lease_id,
            pos,
            lease.policy,
            20,
            now + 1
        )
        .unwrap(),
        lease.template.clone().unwrap()
    );
    assert!(preparation::template_for_dispatch(
        &s.conn,
        &lease.lease_id,
        consumer,
        lease.policy,
        20,
        now + 1
    )
    .is_err());
    let mut changed = lease.policy;
    changed.divergence_threshold = 0.9;
    assert!(preparation::template_for_dispatch(
        &s.conn,
        &lease.lease_id,
        pos,
        changed,
        20,
        now + 1
    )
    .is_err());
    assert!(preparation::template_for_dispatch(
        &s.conn,
        &lease.lease_id,
        pos,
        lease.policy,
        25,
        now + 1
    )
    .is_err());
    let (binding, call) = dynamic_binding(&s, &pair, 0, 5300);
    deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &binding,
        lease.policy,
        20,
        now + 1,
    )
    .unwrap();
    provider_receipts::begin_call(&s.conn, &batch, &call).unwrap();
    assert!(preparation::template_for_dispatch(
        &s.conn,
        &lease.lease_id,
        pos,
        lease.policy,
        20,
        now + 1
    )
    .is_err());
    assert!(preparation::template_for_dispatch(
        &s.conn,
        &lease.lease_id,
        consumer,
        lease.policy,
        20,
        now + 1
    )
    .is_err());
    complete_primary_invocation(&s, &batch, &call);
    assert!(preparation::template_for_dispatch(
        &s.conn,
        &lease.lease_id,
        consumer,
        lease.policy,
        20,
        now + 1
    )
    .is_ok());
    s.command(json!({"operation":"seal_batch","batch_id":batch,"logical_calls":spec(false).calls.branches.iter().map(|b|b.position).collect::<Vec<_>>()}));
    assert!(preparation::template_for_dispatch(
        &s.conn,
        &lease.lease_id,
        consumer,
        lease.policy,
        20,
        now + 1
    )
    .is_err());
}

fn dynamic_binding(
    s: &Store,
    pair: &comparison::Pair,
    index: usize,
    number: u64,
) -> (
    agentc_optimizer::provider_graph_deployment::PrimaryBinding,
    provider_receipts::Invocation,
) {
    let template = provider_graph::load(&s.conn, &pair.candidate_execution_id).unwrap();
    let mut call = prepared_invocation(s, &template, index, number * 16 + index as u64);
    let mut node = prepared_node(&template, index);
    // These request values arise during the new workflow, not in its template.
    node.logical_request_sha256 = format!("{:064x}", number * 100 + index as u64);
    call.logical_request_sha256 = Some(node.logical_request_sha256.clone());
    call.request_sha256 = Some(format!("{:064x}", number * 200 + index as u64));
    (
        agentc_optimizer::provider_graph_deployment::PrimaryBinding {
            node,
            request_sha256: call.request_sha256.clone().unwrap(),
            transport_sha256: call.transport_sha256.clone().unwrap(),
        },
        call,
    )
}

#[test]
fn dynamic_primary_nodes_wait_for_producers_and_publish_original_new_requests() {
    use agentc_optimizer::{
        provider_graph_deployment as deploy, provider_graph_economics as finance,
    };
    let mut s = Store::new(20);
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let request = dynamic_primary_request(&s, &pair, 4700);
    let batch = request.preparation.batch_id.clone();
    let admitted = deploy::begin(&mut s.conn, request.clone(), now).unwrap();
    let lease = admitted.lease.unwrap();
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_deployment_node",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    assert_eq!(
        deploy::begin(&mut s.conn, request, now + 1)
            .unwrap()
            .lease
            .unwrap(),
        lease
    );
    let (consumer, _) = dynamic_binding(&s, &pair, 1, 4700);
    assert!(deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &consumer,
        lease.policy,
        20,
        now + 1
    )
    .is_err());
    for index in 0..3 {
        let (binding, call) = dynamic_binding(&s, &pair, index, 4700);
        assert!(provider_receipts::begin_call(&s.conn, &batch, &call).is_err());
        deploy::approve_primary_node(
            &mut s.conn,
            &lease.lease_id,
            &binding,
            lease.policy,
            20,
            now + 1,
        )
        .unwrap();
        deploy::approve_primary_node(
            &mut s.conn,
            &lease.lease_id,
            &binding,
            lease.policy,
            20,
            now + 1,
        )
        .unwrap();
        provider_receipts::begin_call(&s.conn, &batch, &call).unwrap();
        complete_primary_invocation(&s, &batch, &call);
    }
    s.command(json!({"operation":"seal_batch","batch_id":batch,"logical_calls":spec(false).calls.branches.iter().map(|b|b.position).collect::<Vec<_>>()}));
    s.command(json!({"operation":"finish_batch_economics","batch_id":batch,"wall_latency_ms":20.}));
    let comparison = Comparison {
        version: provider_graph::COMPARISON_VERSION.into(),
        input_sha256: "e".repeat(64),
        output_sha256: "d".repeat(64),
    };
    let e = deploy::finish_primary(
        &mut s.conn,
        &s.profiles,
        &lease.lease_id,
        comparison.clone(),
        now + 2,
    )
    .unwrap();
    assert_eq!(
        deploy::finish_primary(
            &mut s.conn,
            &s.profiles,
            &lease.lease_id,
            comparison,
            now + 3
        )
        .unwrap(),
        e
    );
    let costs =
        finance::summarize_acquisition(&s.conn, &admitted.decision.acquisition.economics.driver_id)
            .unwrap();
    assert_eq!(costs.costs, admitted.decision.acquisition.costs);
    assert_eq!(costs.deployment.unwrap().costs.physical_invocations, 3);
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_pair", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        20
    );
}

#[test]
fn dynamic_primary_bindings_reject_rebinding_changed_controls_and_unbound_dispatch() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let mut s = Store::new(20);
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let request = dynamic_primary_request(&s, &pair, 4800);
    let batch = request.preparation.batch_id.clone();
    let lease = deploy::begin(&mut s.conn, request, now)
        .unwrap()
        .lease
        .unwrap();
    let (binding, call) = dynamic_binding(&s, &pair, 0, 4800);
    assert!(preparation::approve_node(
        &mut s.conn,
        &lease.lease_id,
        &binding.node,
        lease.policy,
        20,
        now + 1
    )
    .is_err());
    for case in 0..5 {
        let mut changed = binding.clone();
        match case {
            0 => changed.node.source_contract_sha256 = "f".repeat(64),
            1 => changed.node.runtime.target_model_version = "another".into(),
            2 => changed.node.position.call = 9,
            3 => changed.node.plan.output_budget = Some(999),
            _ => changed.request_sha256 = "invalid".into(),
        }
        assert!(deploy::approve_primary_node(
            &mut s.conn,
            &lease.lease_id,
            &changed,
            lease.policy,
            20,
            now + 1
        )
        .is_err());
    }
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_prepared_node WHERE lease_id=?1",
                [&lease.lease_id],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &binding,
        lease.policy,
        20,
        now + 1,
    )
    .unwrap();
    for case in 0..3 {
        let mut changed = binding.clone();
        match case {
            0 => changed.request_sha256 = "e".repeat(64),
            1 => changed.transport_sha256 = "e".repeat(64),
            _ => changed.node.logical_request_sha256 = "e".repeat(64),
        }
        assert!(deploy::approve_primary_node(
            &mut s.conn,
            &lease.lease_id,
            &changed,
            lease.policy,
            20,
            now + 1
        )
        .is_err());
    }
    for case in 0..3 {
        let mut changed = call.clone();
        match case {
            0 => changed.request_sha256 = Some("e".repeat(64)),
            1 => changed.transport_sha256 = Some("e".repeat(64)),
            _ => changed.logical_request_sha256 = Some("e".repeat(64)),
        }
        assert!(provider_receipts::begin_call(&s.conn, &batch, &changed).is_err());
    }
    provider_receipts::begin_call(&s.conn, &batch, &call).unwrap();
    assert!(deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &binding,
        lease.policy,
        20,
        now + 1
    )
    .is_err());
}

#[test]
fn dynamic_primary_binding_and_node_roll_back_together() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let mut s = Store::new(20);
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let request = dynamic_primary_request(&s, &pair, 4900);
    let lease = deploy::begin(&mut s.conn, request, now)
        .unwrap()
        .lease
        .unwrap();
    let (binding, _) = dynamic_binding(&s, &pair, 0, 4900);
    s.conn.execute_batch("CREATE TRIGGER reject_dynamic_binding BEFORE INSERT ON provider_graph_deployment_node BEGIN SELECT RAISE(ABORT,'fixture'); END;").unwrap();
    assert!(deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &binding,
        lease.policy,
        20,
        now + 1
    )
    .is_err());
    for table in [
        "provider_graph_deployment_node",
        "provider_graph_prepared_node",
    ] {
        assert_eq!(
            s.conn
                .query_row(
                    &format!("SELECT COUNT(*) FROM {table} WHERE lease_id=?1"),
                    [&lease.lease_id],
                    |r| r.get::<_, u64>(0)
                )
                .unwrap(),
            0
        );
    }
    s.conn
        .execute_batch("DROP TRIGGER reject_dynamic_binding")
        .unwrap();
    deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &binding,
        lease.policy,
        20,
        now + 1,
    )
    .unwrap();
}

#[test]
fn dynamic_primary_restart_checks_original_bindings_and_missing_prior_custody() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("dynamic.db");
    let mut s = Store {
        conn: Connection::open(&path).unwrap(),
        profiles: PlanProfiles::with_window(20),
        prepared_pair: None,
        invocation_cost_usd: 0.01,
    };
    agentc_optimizer::schema::ensure_cost_model_schema(&s.conn).unwrap();
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let request = dynamic_primary_request(&s, &pair, 5000);
    let batch = request.preparation.batch_id.clone();
    let lease = deploy::begin(&mut s.conn, request, now)
        .unwrap()
        .lease
        .unwrap();
    let (binding, call) = dynamic_binding(&s, &pair, 0, 5000);
    deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &binding,
        lease.policy,
        20,
        now + 1,
    )
    .unwrap();
    drop(s.conn);
    s.conn = Connection::open(&path).unwrap();
    assert_eq!(preparation::load(&s.conn, &lease.lease_id).unwrap(), lease);
    provider_receipts::begin_call(&s.conn, &batch, &call).unwrap();
    complete_primary_invocation(&s, &batch, &call);
    let (next, _) = dynamic_binding(&s, &pair, 1, 5000);
    s.conn
        .execute(
            "DELETE FROM provider_graph_deployment_node WHERE lease_id=?1",
            [&lease.lease_id],
        )
        .unwrap();
    s.conn
        .execute(
            "DELETE FROM provider_graph_prepared_node WHERE lease_id=?1",
            [&lease.lease_id],
        )
        .unwrap();
    assert!(preparation::load(&s.conn, &lease.lease_id).is_err());
    assert!(deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &next,
        lease.policy,
        20,
        now + 2
    )
    .is_err());
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                [&batch],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        1
    );
}

#[test]
fn dynamic_primary_request_cannot_change_mode_policy_or_retired_evidence() {
    use agentc_optimizer::provider_graph_deployment as deploy;
    let mut s = Store::new(20);
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let mut mixed = primary_deployment_request(&s, &pair, 5100);
    mixed.dynamic_primary_nodes = true;
    assert!(deploy::begin(&mut s.conn, mixed, now).is_err());
    let request = dynamic_primary_request(&s, &pair, 5101);
    let lease = deploy::begin(&mut s.conn, request.clone(), now)
        .unwrap()
        .lease
        .unwrap();
    let mut changed = request;
    changed.dynamic_primary_nodes = false;
    assert!(deploy::begin(&mut s.conn, changed, now + 1).is_err());
    let (binding, _) = dynamic_binding(&s, &pair, 0, 5101);
    let mut policy = lease.policy;
    policy.divergence_threshold = 0.9;
    assert!(deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &binding,
        policy,
        20,
        now + 1
    )
    .is_err());
    s.seed(
        spec(false),
        5200,
        InferenceCostSource::ProviderReported,
        "r2",
        "c",
        "d",
    );
    assert!(deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &binding,
        lease.policy,
        20,
        5201
    )
    .is_err());
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_deployment_node",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

impl Store {
    fn new(window: u32) -> Self {
        let conn = Connection::open_in_memory().unwrap();
        agentc_optimizer::schema::ensure_cost_model_schema(&conn).unwrap();
        Self {
            conn,
            profiles: PlanProfiles::with_window(window),
            prepared_pair: None,
            invocation_cost_usd: 0.01,
        }
    }
    fn command(&mut self, value: serde_json::Value) {
        provider_receipts::handle_json(&mut self.conn, &value.to_string()).unwrap();
    }
    fn seed(
        &mut self,
        graph: Spec,
        number: u64,
        source: InferenceCostSource,
        revision: &str,
        input: &str,
        output: &str,
    ) -> Execution {
        self.seed_at(
            graph,
            number,
            source,
            revision,
            (input, output),
            (number as i64, number as i64),
        )
    }
    fn seed_at(
        &mut self,
        graph: Spec,
        number: u64,
        source: InferenceCostSource,
        revision: &str,
        results: (&str, &str),
        times: (i64, i64),
    ) -> Execution {
        let (input, output) = results;
        let batch = format!("{number:032x}");
        self.command(json!({"operation":"begin_batch","batch":{"version":provider_receipts::VERSION,"batch_id":batch,"application_version":graph.calls.application_version,"callback_version":graph.calls.callback_version,"item_count":2}}));
        let lease = agentc_optimizer::provider_graph_preparation::begin(
            &mut self.conn,
            agentc_optimizer::provider_graph_preparation::Begin {
                role_program: None,
                batch_id: batch.clone(),
                max_workers: graph.calls.max_workers,
                input_sha256: Some(input.repeat(64)),
                template_execution_id: self.prepared_pair.as_ref().map(|p| {
                    if graph.profile_key().unwrap() == p.reference_key {
                        p.reference_execution_id.clone()
                    } else {
                        assert_eq!(graph.profile_key().unwrap(), p.key);
                        p.candidate_execution_id.clone()
                    }
                }),
                comparison_pair_id: self.prepared_pair.as_ref().map(|p| p.pair_id.clone()),
            },
            Policy::default(),
            self.profiles.retained_window_size(),
            times.0,
        )
        .unwrap();
        let mut versions = vec![];
        let mut outcomes = vec![];
        let mut requests = BTreeMap::new();
        for (i, b) in graph.calls.branches.iter().enumerate() {
            let id = format!("{:032x}", number * 16 + i as u64);
            let key = PlanProfileKey {
                call_site_version: b.call_site_version.clone(),
                execution_plan_id: b.plan.execution_plan_id().unwrap(),
            };
            let runtime = PlanRuntimeVersion {
                provider_protocol: b.plan.provider_protocol.clone(),
                target_model_id: b.plan.target_model_id.clone(),
                target_model_version: format!("{}-{revision}", b.plan.target_model_id),
                price_table_version: "prices-v1".into(),
            };
            let hash = format!("{:064x}", i + 1);
            if lease.template.is_some() {
                preparation::approve_node(
                    &mut self.conn,
                    &lease.lease_id,
                    &preparation::Node {
                        position: b.position,
                        key: key.clone(),
                        plan: b.plan.clone(),
                        runtime: runtime.clone(),
                        source_contract_sha256: format!(
                            "{:064x}",
                            b.position.item * 2 + b.position.call + 1
                        ),
                        logical_request_sha256: hash.clone(),
                    },
                    Policy::default(),
                    self.profiles.retained_window_size(),
                    times.0,
                )
                .unwrap();
            }
            self.command(json!({"operation":"begin_call","batch_id":batch,"invocation":{"invocation_id":id,"position":b.position,"ordinal":0,"role":"primary","provider_protocol":b.plan.provider_protocol,"requested_model_id":b.plan.target_model_id,"request_sha256":hash,"logical_request_sha256":hash,"transport_sha256":"f".repeat(64),"logical_plan_key":key,"logical_runtime":runtime,"dispatch_plan_key":key,"dispatch_runtime":runtime,"parent_plan_key":null,"transport_verified":true}}));
            self.command(json!({"operation":"finish_call","batch_id":batch,"invocation_id":id,"completion":{"status":"completed","model_version":runtime.target_model_version,"input_tokens":2,"output_tokens":1,"cost_usd":self.invocation_cost_usd,"cost_source":source,"latency_ms":5.,"http_attempts":1,"exception_type":null}}));
            requests.insert(b.position, hash);
            versions.push(MultiCallRuntimeBranch {
                position: b.position,
                runtime,
            });
            outcomes.push(MultiCallOutcome {
                position: b.position,
                cost_source: source.clone(),
                outcome: Outcome {
                    cost_source: source.clone(),
                    physical_invocations: vec![
                        agentc_optimizer::cost_evidence::PhysicalInvocation {
                            batch_id: batch.clone(),
                            invocation_id: id,
                        },
                    ],
                    input_tokens: 2,
                    output_tokens: 1,
                    latency_ms: 5.,
                    cost_usd: self.invocation_cost_usd,
                    inference_cost_complete: Some(true),
                    ..Default::default()
                },
            });
        }
        self.command(json!({"operation":"seal_batch","batch_id":batch,"logical_calls":graph.calls.branches.iter().map(|b|b.position).collect::<Vec<_>>()}));
        provider_graph::publish(
            &mut self.conn,
            &self.profiles,
            PublishRequest {
                batch_id: &batch,
                spec: &graph,
                versions: &versions,
                outcomes: &outcomes,
                requests: &requests,
                wall_latency_ms: 20.,
                input_sha256: &format!("{number:064x}"),
                observed_at_us: times.1,
                comparison: Some(Comparison {
                    version: provider_graph::COMPARISON_VERSION.into(),
                    input_sha256: input.repeat(64),
                    output_sha256: output.repeat(64),
                }),
                source_contracts: &graph
                    .calls
                    .branches
                    .iter()
                    .map(|b| provider_graph::SourceContract {
                        position: b.position,
                        sha256: format!("{:064x}", b.position.item * 2 + b.position.call + 1),
                    })
                    .collect::<Vec<_>>(),
                preparation_id: Some(&lease.lease_id),
            },
        )
        .unwrap()
    }
    fn pair(
        &mut self,
        r: &Execution,
        c: &Execution,
        at: i64,
    ) -> anyhow::Result<comparison::Outcome> {
        comparison::compare(
            &mut self.conn,
            &r.execution_id,
            &c.execution_id,
            at,
            self.profiles.retained_window_size(),
        )
    }
}

#[test]
fn whole_graph_pair_is_atomic_one_to_one_and_independent_of_money_or_constituent_quality() {
    let mut s = Store::new(20);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    comparison::ensure_schema(&s.conn).unwrap();
    s.conn.execute_batch("CREATE TRIGGER fail_graph_pair BEFORE INSERT ON provider_graph_pair BEGIN SELECT RAISE(ABORT,'fixture'); END;").unwrap();
    assert!(s.pair(&r, &c, 10).is_err());
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_pair", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
    s.conn
        .execute_batch("DROP TRIGGER fail_graph_pair")
        .unwrap();
    let paired = s.pair(&r, &c, 10).unwrap();
    assert_eq!(paired.status, "recorded");
    assert_eq!(paired.pair.divergence, 0.);
    assert_eq!(paired.pair.acquisition_cost_usd, None);
    assert_ne!(paired.pair.reference_runtime, paired.pair.runtime_version);
    assert_eq!(s.pair(&r, &c, 100).unwrap().pair, paired.pair);
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM execution_plan_profile", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
    let c2 = s.seed(
        spec(false),
        3,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    assert!(s.pair(&r, &c2, 100).is_err());
    assert!(s.pair(&c, &r, 100).is_err());
}

#[test]
fn final_result_divergence_does_not_depend_on_billing_or_individual_node_scores() {
    let mut s = Store::new(20);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::ProviderReported,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::ProviderReported,
        "r1",
        "a",
        "c",
    );
    let paired = s.pair(&r, &c, 10).unwrap().pair;
    assert_eq!(paired.divergence, 1.);
    assert!((paired.acquisition_cost_usd.unwrap() - 0.06).abs() < 1e-9);
    assert_eq!(paired.acquisition_latency_ms, 40.);
    assert_eq!(
        s.conn
            .query_row(
                "SELECT SUM(n_paired_observations) FROM execution_plan_profile",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        0
    );
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM pragma_foreign_key_check", [], |r| r
                .get::<_, i64>(
                0
            ))
            .unwrap(),
        0
    );
}

#[test]
fn inputs_callbacks_models_results_and_original_reference_are_required() {
    for case in 0..6 {
        let mut s = Store::new(20);
        let mut rs = spec(true);
        let mut cs = spec(false);
        match case {
            0 => {}
            1 => cs.calls.callback_version = "other-callback".into(),
            2 => cs.calls.application_version = "b".repeat(64),
            3 => cs.calls.branches[0].plan.requested_model_id = "another-original".into(),
            4 => cs.results = Results::CallbackItems { items: vec![1, 0] },
            _ => rs.calls.max_workers = 2,
        }
        let r = s.seed(rs, 1, InferenceCostSource::ProviderReported, "r1", "a", "b");
        let c = s.seed(
            cs,
            2,
            InferenceCostSource::ProviderReported,
            "r1",
            if case == 0 { "c" } else { "a" },
            "b",
        );
        assert!(s.pair(&r, &c, 10).is_err(), "{case}");
        assert!(s.pair(&r, &c, 1).is_err());
    }
}

#[test]
fn unpriced_runtime_changes_window_retirement_and_returning_versions_cannot_revive_old_pairs() {
    let mut s = Store::new(2);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::ProviderReported,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::ProviderReported,
        "r1",
        "a",
        "b",
    );
    s.seed(
        spec(false),
        3,
        InferenceCostSource::Estimated,
        "r2",
        "a",
        "b",
    );
    assert!(s.pair(&r, &c, 10).is_err());
    s.seed(
        spec(false),
        4,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    assert!(s.pair(&r, &c, 10).is_err());
    let new = s.seed(
        spec(false),
        5,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    assert!(comparison::compare(&mut s.conn, &r.execution_id, &new.execution_id, 10, 20).is_err());
    assert_eq!(
        s.pair(&r, &new, 10).unwrap().pair.acquisition_cost_usd,
        None
    );
}

#[test]
fn immutable_receipts_vectors_result_bindings_and_committed_pairs_are_checked_after_restart() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("graphs.db");
    let mut s = Store {
        conn: Connection::open(&path).unwrap(),
        profiles: PlanProfiles::with_window(20),
        prepared_pair: None,
        invocation_cost_usd: 0.01,
    };
    agentc_optimizer::schema::ensure_cost_model_schema(&s.conn).unwrap();
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::ProviderReported,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::ProviderReported,
        "r1",
        "a",
        "b",
    );
    let original = s.pair(&r, &c, 10).unwrap().pair;
    drop(s);
    let mut conn = Connection::open(&path).unwrap();
    assert_eq!(
        comparison::compare(&mut conn, &r.execution_id, &c.execution_id, 100, 20)
            .unwrap()
            .pair,
        original
    );
    conn.execute(
        "UPDATE provider_graph_pair SET pair_json=json_set(pair_json,'$.divergence',1)",
        [],
    )
    .unwrap();
    assert!(comparison::compare(&mut conn, &r.execution_id, &c.execution_id, 100, 20).is_err());
    conn.execute("DELETE FROM provider_graph_quality_event", [])
        .unwrap();
    conn.execute("DELETE FROM provider_graph_pair", []).unwrap();
    conn.execute("UPDATE provider_receipt_invocation SET completion_json=json_set(completion_json,'$.model_version','tampered') WHERE batch_id=?1",[&c.batch_id]).unwrap();
    assert!(comparison::compare(&mut conn, &r.execution_id, &c.execution_id, 100, 20).is_err());
}

fn quality_window(s: &Store, pair: &comparison::Pair, now: i64) -> quality::Window {
    quality::window(
        &s.conn,
        Request::from_pair(
            pair,
            s.profiles.retained_window_size(),
            now,
            Policy::default(),
        ),
    )
    .unwrap()
    .unwrap()
}

fn prepare_template(s: &mut Store, e: &Execution, number: u64, now: i64) -> preparation::Lease {
    prepare_comparison_template(s, e, number, now, None)
}

fn prepare_comparison_template(
    s: &mut Store,
    e: &Execution,
    number: u64,
    now: i64,
    pair: Option<&comparison::Pair>,
) -> preparation::Lease {
    let batch = format!("{number:032x}");
    provider_receipts::begin_batch(
        &s.conn,
        &provider_receipts::Batch {
            version: provider_receipts::VERSION.into(),
            batch_id: batch.clone(),
            application_version: Some(e.spec.calls.application_version.clone()),
            callback_version: e.spec.calls.callback_version.clone(),
            item_count: e.spec.calls.item_count,
        },
    )
    .unwrap();
    preparation::begin(
        &mut s.conn,
        preparation::Begin {
            role_program: None,
            batch_id: batch,
            max_workers: e.spec.calls.max_workers,
            input_sha256: e.comparison.as_ref().map(|c| c.input_sha256.clone()),
            template_execution_id: Some(e.execution_id.clone()),
            comparison_pair_id: pair.map(|p| p.pair_id.clone()),
        },
        Policy::default(),
        s.profiles.retained_window_size(),
        now,
    )
    .unwrap()
}

fn prepared_node(e: &Execution, index: usize) -> preparation::Node {
    let b = &e.spec.calls.branches[index];
    preparation::Node {
        position: b.position,
        key: PlanProfileKey {
            call_site_version: b.call_site_version.clone(),
            execution_plan_id: b.plan.execution_plan_id().unwrap(),
        },
        plan: b.plan.clone(),
        runtime: e.runtime_branches[index].runtime.clone(),
        source_contract_sha256: e.source_contracts[index].sha256.clone(),
        logical_request_sha256: format!("{:064x}", index + 1),
    }
}

fn prepared_invocation(
    s: &Store,
    e: &Execution,
    index: usize,
    number: u64,
) -> provider_receipts::Invocation {
    let raw: String = s.conn.query_row(
        "SELECT invocation_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call LIMIT 1 OFFSET ?2",
        rusqlite::params![e.batch_id,index], |r| r.get(0),
    ).unwrap();
    let mut call: provider_receipts::Invocation = serde_json::from_str(&raw).unwrap();
    call.invocation_id = format!("{number:032x}");
    call
}

#[test]
fn completion_after_cooldown_cannot_make_an_old_workflow_fresh() {
    let mut s = Store::new(50);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "c",
    );
    let pair = s.pair(&r, &c, 2).unwrap().pair;
    let after = quality_window(&s, &pair, 2)
        .disabled
        .unwrap()
        .reenable_at_us;
    for (i, starts) in [
        (after - 1, after + 1),
        (after + 1, after - 1),
        (after - 1, after - 1),
    ]
    .into_iter()
    .enumerate()
    {
        let r = s.seed_at(
            spec(true),
            10 + 2 * i as u64,
            InferenceCostSource::Estimated,
            "r1",
            ("a", "b"),
            (starts.0, after + 10),
        );
        let c = s.seed_at(
            spec(false),
            11 + 2 * i as u64,
            InferenceCostSource::Estimated,
            "r1",
            ("a", "b"),
            (starts.1, after + 10),
        );
        let paired = s.pair(&r, &c, after + 10).unwrap();
        let q = paired.quality.unwrap();
        assert_eq!(q.lifetime_pairs, i as u64 + 2);
        assert!(q.pairs.is_empty());
        assert!(q.disabled.is_some());
        assert!(quality::finish_readmission_after_admission(&s.conn, &q, after + 10).is_err());
    }
    let r = s.seed_at(
        spec(true),
        30,
        InferenceCostSource::Estimated,
        "r1",
        ("a", "b"),
        (after + 1, after + 11),
    );
    let c = s.seed_at(
        spec(false),
        31,
        InferenceCostSource::Estimated,
        "r1",
        ("a", "b"),
        (after + 2, after + 11),
    );
    let q = s.pair(&r, &c, after + 11).unwrap().quality.unwrap();
    assert_eq!(q.pairs.len(), 1);
    assert_eq!(
        preparation::original_start(&s.conn, &r).unwrap(),
        Some(after + 1)
    );
}

#[test]
fn original_preparation_retries_preserve_start_and_corruption_holds_after_restart() {
    let mut s = Store::new(20);
    let e = s.seed_at(
        spec(false),
        1,
        InferenceCostSource::Estimated,
        "r1",
        ("a", "b"),
        (7, 100),
    );
    let id = e.preparation_id.as_ref().unwrap();
    let retry = preparation::begin(
        &mut s.conn,
        preparation::Begin {
            role_program: None,
            batch_id: e.batch_id.clone(),
            max_workers: 2,
            input_sha256: Some("a".repeat(64)),
            template_execution_id: None,
            comparison_pair_id: None,
        },
        Policy::default(),
        20,
        200,
    )
    .unwrap();
    assert_eq!(retry.started_at_us, 7);
    assert_eq!(&retry.lease_id, id);
    assert_eq!(preparation::original_start(&s.conn, &e).unwrap(), Some(7));
    assert!(preparation::begin(
        &mut s.conn,
        preparation::Begin {
            batch_id: e.batch_id.clone(),
            max_workers: 1,
            input_sha256: Some("a".repeat(64)),
            template_execution_id: None,
            comparison_pair_id: None,
            role_program: None,
        },
        Policy::default(),
        20,
        200
    )
    .is_err());
    s.conn.execute("UPDATE provider_graph_preparation SET lease_json=json_set(lease_json,'$.started_at_us',99)", []).unwrap();
    assert!(provider_graph::load(&s.conn, &e.execution_id).is_err());
}

#[test]
fn delayed_old_completions_do_not_evict_fresh_paired_start_evidence() {
    let mut s = Store::new(20);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "c",
    );
    let pair = s.pair(&r, &c, 2).unwrap().pair;
    let after = quality_window(&s, &pair, 2)
        .disabled
        .unwrap()
        .reenable_at_us;
    s.prepared_pair = Some(pair.clone());
    let mut last = pair;
    for n in 0..20 {
        let at = after + 10 + n;
        let r = s.seed_at(
            spec(true),
            10 + 2 * n as u64,
            InferenceCostSource::Estimated,
            "r1",
            ("a", "b"),
            (at, at),
        );
        let c = s.seed_at(
            spec(false),
            11 + 2 * n as u64,
            InferenceCostSource::Estimated,
            "r1",
            ("a", "b"),
            (at, at),
        );
        last = s.pair(&r, &c, at).unwrap().pair;
    }
    s.prepared_pair = None;
    for n in 0..21 {
        let at = after + 100 + n;
        let r = s.seed_at(
            spec(true),
            100 + 2 * n as u64,
            InferenceCostSource::Estimated,
            "r1",
            ("a", "b"),
            (1, at),
        );
        let c = s.seed_at(
            spec(false),
            101 + 2 * n as u64,
            InferenceCostSource::Estimated,
            "r1",
            ("a", "b"),
            (1, at),
        );
        s.pair(&r, &c, at).unwrap();
    }
    let q = quality_window(&s, &last, after + 200);
    assert_eq!(q.pairs.len(), 20);
    assert_eq!(q.prepared_pairs.len(), 20);
    assert_eq!(q.lifetime_pairs, 42);
    assert!(q.pairs.iter().all(|p| p.observed_at_us < after + 100));
    quality::finish_readmission_after_admission(&s.conn, &q, after + 200).unwrap();
}

#[test]
fn preparations_from_a_different_policy_cannot_warm_a_new_quality_cohort() {
    let mut s = Store::new(20);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let policy = Policy {
        divergence_threshold: 0.05,
        ..Policy::default()
    };
    let paired = comparison::compare_with_policy(
        &mut s.conn,
        &r.execution_id,
        &c.execution_id,
        3,
        20,
        policy,
    )
    .unwrap();
    let q = paired.quality.unwrap();
    assert_eq!(q.lifetime_pairs, 1);
    assert!(q.pairs.is_empty());
    assert_eq!(q.divergence_exposure, 0.);
}

#[test]
fn shared_preparation_holds_reference_changes_and_returning_epochs_before_dispatch() {
    for transition in 0..4 {
        let mut s = Store::new(20);
        let r = s.seed(
            spec(true),
            1,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "b",
        );
        let c = s.seed(
            spec(false),
            2,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "b",
        );
        let pair = s.pair(&r, &c, 2).unwrap().pair;
        let lease = prepare_comparison_template(&mut s, &c, 3, 3, Some(&pair));
        let node = prepared_node(&c, 0);
        preparation::approve_node(
            &mut s.conn,
            &lease.lease_id,
            &node,
            Policy::default(),
            20,
            3,
        )
        .unwrap();
        match transition {
            0 => {
                s.seed(
                    spec(true),
                    4,
                    InferenceCostSource::Estimated,
                    "r2",
                    "a",
                    "b",
                );
            }
            1 => {
                s.seed(
                    spec(true),
                    4,
                    InferenceCostSource::Estimated,
                    "r2",
                    "a",
                    "b",
                );
                s.seed(
                    spec(true),
                    5,
                    InferenceCostSource::Estimated,
                    "r1",
                    "a",
                    "b",
                );
            }
            2 => {
                s.profiles = PlanProfiles::with_window(21);
                s.seed(
                    spec(true),
                    4,
                    InferenceCostSource::Estimated,
                    "r1",
                    "a",
                    "b",
                );
            }
            _ => {
                quality::window(
                    &s.conn,
                    Request::from_pair(
                        &pair,
                        20,
                        4,
                        Policy {
                            divergence_threshold: 0.1,
                            ..Policy::default()
                        },
                    ),
                )
                .unwrap();
            }
        }
        assert!(preparation::approve_node(
            &mut s.conn,
            &lease.lease_id,
            &node,
            Policy::default(),
            20,
            6
        )
        .is_err());
        let call = prepared_invocation(&s, &c, 0, 100);
        assert!(provider_receipts::begin_call(&s.conn, &lease.batch_id, &call).is_err());
        assert_eq!(
            s.conn
                .query_row(
                    "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                    [&lease.batch_id],
                    |r| r.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }
}

#[test]
fn shared_preparation_custody_and_retry_survive_restart_and_reject_changed_source_pairs() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("prepared-pairs.db");
    let mut s = Store {
        conn: Connection::open(&path).unwrap(),
        profiles: PlanProfiles::with_window(20),
        prepared_pair: None,
        invocation_cost_usd: 0.01,
    };
    agentc_optimizer::schema::ensure_cost_model_schema(&s.conn).unwrap();
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let pair = s.pair(&r, &c, 2).unwrap().pair;
    let rl = prepare_comparison_template(&mut s, &r, 3, 3, Some(&pair));
    let cl = prepare_comparison_template(&mut s, &c, 4, 3, Some(&pair));
    assert_eq!(
        rl.comparison_context.as_ref().unwrap().generation_id,
        cl.comparison_context.as_ref().unwrap().generation_id
    );
    assert_eq!(
        rl.comparison_context.as_ref().unwrap().role,
        preparation::ComparisonRole::Reference
    );
    assert_eq!(
        cl.comparison_context.as_ref().unwrap().role,
        preparation::ComparisonRole::Candidate
    );
    drop(s);
    let mut conn = Connection::open(&path).unwrap();
    assert_eq!(preparation::load(&conn, &cl.lease_id).unwrap(), cl);
    let retry = preparation::begin(
        &mut conn,
        preparation::Begin {
            role_program: None,
            batch_id: cl.batch_id.clone(),
            max_workers: 2,
            input_sha256: Some("a".repeat(64)),
            template_execution_id: Some(c.execution_id.clone()),
            comparison_pair_id: Some(pair.pair_id.clone()),
        },
        Policy::default(),
        20,
        100,
    )
    .unwrap();
    assert_eq!(retry, cl);
    conn.execute(
        "UPDATE provider_graph_pair SET pair_json=json_set(pair_json,'$.divergence',1)",
        [],
    )
    .unwrap();
    assert!(preparation::load(&conn, &cl.lease_id).is_err());
    assert!(preparation::approve_node(
        &mut conn,
        &cl.lease_id,
        &prepared_node(&c, 0),
        Policy::default(),
        20,
        100
    )
    .is_err());
}

#[test]
fn shared_reference_change_after_sdk_completion_rolls_back_graph_publication() {
    let mut s = Store::new(20);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let pair = s.pair(&r, &c, 2).unwrap().pair;
    let lease = prepare_comparison_template(&mut s, &c, 3, 3, Some(&pair));
    let mut outcomes = vec![];
    let mut requests = BTreeMap::new();
    for (i, b) in c.spec.calls.branches.iter().enumerate() {
        let node = prepared_node(&c, i);
        preparation::approve_node(
            &mut s.conn,
            &lease.lease_id,
            &node,
            Policy::default(),
            20,
            3,
        )
        .unwrap();
        let call = prepared_invocation(&s, &c, i, 100 + i as u64);
        provider_receipts::begin_call(&s.conn, &lease.batch_id, &call).unwrap();
        let raw:String=s.conn.query_row("SELECT completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND item=?2 AND logical_call=?3",
            rusqlite::params![c.batch_id,b.position.item,b.position.call],|r|r.get(0)).unwrap();
        let completion: provider_receipts::Completion = serde_json::from_str(&raw).unwrap();
        provider_receipts::finish_call(&s.conn, &lease.batch_id, &call.invocation_id, &completion)
            .unwrap();
        requests.insert(b.position, node.logical_request_sha256);
        outcomes.push(MultiCallOutcome {
            position: b.position,
            cost_source: InferenceCostSource::Estimated,
            outcome: Outcome {
                cost_source: InferenceCostSource::Estimated,
                input_tokens: 2,
                output_tokens: 1,
                cost_usd: 0.01,
                latency_ms: 5.,
                inference_cost_complete: Some(true),
                physical_invocations: vec![agentc_optimizer::cost_evidence::PhysicalInvocation {
                    batch_id: lease.batch_id.clone(),
                    invocation_id: call.invocation_id,
                }],
                ..Default::default()
            },
        });
    }
    s.command(json!({"operation":"seal_batch","batch_id":lease.batch_id,"logical_calls":c.spec.calls.branches.iter().map(|b|b.position).collect::<Vec<_>>()}));
    s.seed(
        spec(true),
        4,
        InferenceCostSource::Estimated,
        "r2",
        "a",
        "b",
    );
    let published = provider_graph::publish(
        &mut s.conn,
        &s.profiles,
        PublishRequest {
            batch_id: &lease.batch_id,
            spec: &c.spec,
            versions: &c.runtime_branches,
            outcomes: &outcomes,
            requests: &requests,
            wall_latency_ms: 20.,
            input_sha256: &"f".repeat(64),
            observed_at_us: 5,
            comparison: c.comparison.clone(),
            source_contracts: &c.source_contracts,
            preparation_id: Some(&lease.lease_id),
        },
    );
    assert!(published.is_err());
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_execution WHERE batch_id=?1",
                [&lease.batch_id],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        0
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT execution_id FROM provider_graph_preparation WHERE lease_id=?1",
                [&lease.lease_id],
                |r| r.get::<_, Option<String>>(0)
            )
            .unwrap(),
        None
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                [&lease.batch_id],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        3
    );
    assert!(s.profiles.is_empty());
}

#[test]
fn twenty_fresh_observation_only_pairs_cannot_clear_a_joint_readmission_disable() {
    let mut s = Store::new(20);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "c",
    );
    let mut pair = s.pair(&r, &c, 2).unwrap().pair;
    let after = quality_window(&s, &pair, 2)
        .disabled
        .unwrap()
        .reenable_at_us;
    for n in 0..20 {
        let at = after + 10 + 2 * n;
        let r = s.seed_at(
            spec(true),
            10 + 2 * n as u64,
            InferenceCostSource::Estimated,
            "r1",
            ("a", "b"),
            (at, at),
        );
        let c = s.seed_at(
            spec(false),
            11 + 2 * n as u64,
            InferenceCostSource::Estimated,
            "r1",
            ("a", "b"),
            (at + 1, at + 1),
        );
        pair = s.pair(&r, &c, at + 1).unwrap().pair;
    }
    let q = quality_window(&s, &pair, after + 100);
    assert_eq!(q.pairs.len(), 20);
    assert_eq!(q.divergence_upper_p95, Some(0.));
    assert!(q.prepared_pairs.is_empty());
    assert!(quality::finish_readmission_after_admission(&s.conn, &q, after + 100).is_err());
    assert!(quality_window(&s, &pair, after + 100).disabled.is_some());
}

#[test]
fn prepared_dispatch_requires_exact_nodes_controls_roles_and_completed_dependencies() {
    let mut s = Store::new(20);
    let e = s.seed(
        spec(false),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let lease = prepare_template(&mut s, &e, 2, 2);
    let node = prepared_node(&e, 0);
    let call = prepared_invocation(&s, &e, 0, 100);
    assert!(provider_receipts::begin_call(&s.conn, &lease.batch_id, &call).is_err());
    for field in 0..4 {
        let mut changed = node.clone();
        match field {
            0 => changed.plan.output_budget = Some(32),
            1 => changed.runtime.price_table_version = "prices-v2".into(),
            2 => changed.source_contract_sha256 = "f".repeat(64),
            _ => changed.position.call = 10,
        }
        assert!(preparation::approve_node(
            &mut s.conn,
            &lease.lease_id,
            &changed,
            Policy::default(),
            20,
            2
        )
        .is_err());
    }
    preparation::approve_node(
        &mut s.conn,
        &lease.lease_id,
        &node,
        Policy::default(),
        20,
        2,
    )
    .unwrap();
    let mut changed = node.clone();
    changed.logical_request_sha256 = "a".repeat(64);
    assert!(preparation::approve_node(
        &mut s.conn,
        &lease.lease_id,
        &changed,
        Policy::default(),
        20,
        2
    )
    .is_err());
    for field in 0..6 {
        let mut changed = call.clone();
        match field {
            0 => changed.role = provider_receipts::Role::Validation,
            1 => changed.ordinal = 1,
            2 => changed.logical_request_sha256 = Some("a".repeat(64)),
            3 => changed.transport_verified = false,
            4 => changed.requested_model_id = "other".into(),
            _ => {
                changed
                    .logical_runtime
                    .as_mut()
                    .unwrap()
                    .price_table_version = "prices-v2".into()
            }
        }
        assert!(provider_receipts::begin_call(&s.conn, &lease.batch_id, &changed).is_err());
    }
    let consumer = prepared_node(&e, 1);
    preparation::approve_node(
        &mut s.conn,
        &lease.lease_id,
        &consumer,
        Policy::default(),
        20,
        2,
    )
    .unwrap();
    let consumer_call = prepared_invocation(&s, &e, 1, 101);
    assert!(provider_receipts::begin_call(&s.conn, &lease.batch_id, &consumer_call).is_err());
    provider_receipts::begin_call(&s.conn, &lease.batch_id, &call).unwrap();
    assert!(provider_receipts::begin_call(&s.conn, &lease.batch_id, &consumer_call).is_err());
    assert!(preparation::approve_node(
        &mut s.conn,
        &lease.lease_id,
        &node,
        Policy::default(),
        20,
        2
    )
    .is_err());
    let raw: String = s.conn.query_row("SELECT completion_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call LIMIT 1", [&e.batch_id], |r|r.get(0)).unwrap();
    let completion = serde_json::from_str(&raw).unwrap();
    provider_receipts::finish_call(&s.conn, &lease.batch_id, &call.invocation_id, &completion)
        .unwrap();
    provider_receipts::begin_call(&s.conn, &lease.batch_id, &consumer_call).unwrap();
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                [&lease.batch_id],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        2
    );
}

#[test]
fn prepared_epoch_changes_are_held_before_any_physical_dispatch() {
    for transition in 0..3 {
        let mut s = Store::new(20);
        let r = s.seed(
            spec(true),
            1,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "b",
        );
        let e = s.seed(
            spec(false),
            2,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "c",
        );
        let lease = prepare_template(&mut s, &e, 3, 3);
        let node = prepared_node(&e, 0);
        preparation::approve_node(
            &mut s.conn,
            &lease.lease_id,
            &node,
            Policy::default(),
            20,
            3,
        )
        .unwrap();
        match transition {
            0 => {
                s.seed(
                    spec(false),
                    4,
                    InferenceCostSource::Estimated,
                    "r2",
                    "a",
                    "b",
                );
            }
            1 => {
                s.profiles = PlanProfiles::with_window(21);
                s.seed(
                    spec(false),
                    4,
                    InferenceCostSource::Estimated,
                    "r1",
                    "a",
                    "b",
                );
            }
            _ => {
                assert!(s
                    .pair(&r, &e, 4)
                    .unwrap()
                    .quality
                    .unwrap()
                    .disabled
                    .is_some());
            }
        }
        assert!(preparation::approve_node(
            &mut s.conn,
            &lease.lease_id,
            &node,
            Policy::default(),
            20,
            5
        )
        .is_err());
        let call = prepared_invocation(&s, &e, 0, 100);
        assert!(provider_receipts::begin_call(&s.conn, &lease.batch_id, &call).is_err());
        assert_eq!(
            s.conn
                .query_row(
                    "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                    [&lease.batch_id],
                    |r| r.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }
}

#[test]
fn graph_quality_event_and_pair_roll_back_together_and_retry_counts_once() {
    let mut s = Store::new(20);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    comparison::ensure_schema(&s.conn).unwrap();
    quality::ensure_schema(&s.conn).unwrap();
    s.conn.execute_batch("CREATE TRIGGER fail_graph_quality BEFORE INSERT ON provider_graph_quality_event BEGIN SELECT RAISE(ABORT,'fixture'); END").unwrap();
    assert!(s.pair(&r, &c, 2).is_err());
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_pair", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_quality_state",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        0
    );
    s.conn
        .execute_batch("DROP TRIGGER fail_graph_quality")
        .unwrap();
    let pair = s.pair(&r, &c, 2).unwrap();
    assert_eq!(pair.quality.unwrap().lifetime_pairs, 1);
    let retry = s.pair(&r, &c, 3).unwrap();
    assert_eq!(retry.quality.unwrap().lifetime_pairs, 1);
}

#[test]
fn sparse_graph_pairs_outlive_execution_eviction_without_constituent_or_money_evidence() {
    let mut s = Store::new(20);
    let mut last = None;
    for n in 0..20 {
        let r = s.seed(
            spec(true),
            2 * n + 1,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "b",
        );
        let c = s.seed(
            spec(false),
            2 * n + 2,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "b",
        );
        last = Some(s.pair(&r, &c, 2 * n as i64 + 2).unwrap().pair);
    }
    for n in 41..102 {
        s.seed(
            spec(false),
            n,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "b",
        );
    }
    let last = last.unwrap();
    let window = quality_window(&s, &last, 102);
    assert_eq!(window.pairs.len(), 20);
    assert_eq!(window.divergence_upper_p95, Some(0.));
    assert_eq!(window.lifetime_pairs, 20);
    assert!(window.disabled.is_none());
    let retried = comparison::compare(
        &mut s.conn,
        &last.reference_execution_id,
        &last.candidate_execution_id,
        102,
        20,
    )
    .unwrap();
    assert_eq!(retried.status, "already_recorded");
    assert_eq!(retried.quality.unwrap().lifetime_pairs, 20);
    assert!(s.profiles.is_empty());
}

#[test]
fn baseline_and_candidate_runtime_returns_cannot_revive_graph_quality_or_clear_disable() {
    let mut s = Store::new(20);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "c",
    );
    let original = s.pair(&r, &c, 2).unwrap().pair;
    let old = quality_window(&s, &original, 2);
    assert!(old.disabled.is_some());
    let r2 = s.seed(
        spec(true),
        3,
        InferenceCostSource::Estimated,
        "r2",
        "a",
        "b",
    );
    assert!(quality::window(
        &s.conn,
        Request::from_pair(&original, 20, 3, Policy::default())
    )
    .unwrap()
    .is_none());
    let c2 = s.seed(
        spec(false),
        4,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let new = s.pair(&r2, &c2, 4).unwrap();
    assert_eq!(new.quality.unwrap().disabled, old.disabled);
    let r3 = s.seed(
        spec(true),
        5,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c3 = s.seed(
        spec(false),
        6,
        InferenceCostSource::Estimated,
        "r2",
        "a",
        "b",
    );
    let new = s.pair(&r3, &c3, 6).unwrap();
    let q = new.quality.unwrap();
    assert!(q.disabled.is_some());
    assert!(q.pairs.is_empty());
    assert_eq!(q.divergence_exposure, 1.);
    let c4 = s.seed(
        spec(false),
        7,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let r4 = s.seed(
        spec(true),
        8,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let new = s.pair(&r4, &c4, 8).unwrap().quality.unwrap();
    assert_eq!(new.disabled, old.disabled);
    assert_eq!(new.lifetime_pairs, 4);
}

#[test]
fn graph_readmission_requires_twenty_fresh_pairs_and_both_executions_after_cooldown() {
    let mut s = Store::new(50);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "c",
    );
    let pair = s.pair(&r, &c, 2).unwrap().pair;
    let disabled = quality_window(&s, &pair, 2).disabled.unwrap();
    let now = disabled.reenable_at_us;
    let q = quality_window(&s, &pair, now);
    assert_eq!(q.divergence_exposure, 0.);
    assert!(q.disabled.is_some());
    assert!(quality::finish_readmission_after_admission(&s.conn, &q, now).is_err());
    let old_ref = s.seed(
        spec(true),
        3,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let late = s.seed(
        spec(false),
        now as u64 + 1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    s.pair(&old_ref, &late, now + 1).unwrap();
    s.prepared_pair = Some(pair.clone());
    let mut last = pair;
    for n in 0..19 {
        let number = now as u64 + 10 + 2 * n;
        let r = s.seed(
            spec(true),
            number,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "b",
        );
        let c = s.seed(
            spec(false),
            number + 1,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "b",
        );
        last = s.pair(&r, &c, number as i64 + 1).unwrap().pair;
    }
    let q = quality_window(&s, &last, now + 50);
    assert_eq!(q.pairs.len(), 19);
    assert_eq!(q.prepared_pairs.len(), 19);
    assert!(quality::finish_readmission_after_admission(&s.conn, &q, now + 50).is_err());
    let r = s.seed(
        spec(true),
        now as u64 + 51,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        now as u64 + 52,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let last = s.pair(&r, &c, now + 52).unwrap().pair;
    let q = quality_window(&s, &last, now + 52);
    assert_eq!(q.pairs.len(), 20);
    assert_eq!(q.prepared_pairs.len(), 20);
    assert_eq!(q.divergence_upper_p95, Some(0.));
    quality::finish_readmission_after_admission(&s.conn, &q, now + 52).unwrap();
    let q = quality_window(&s, &last, now + 53);
    assert!(q.disabled.is_none());
    assert_eq!(q.prepared_pairs.len(), 20);
    assert_eq!(q.lifetime_pairs, 22);
    assert!(s.profiles.is_empty());
}

#[test]
fn original_policy_excess_survives_policy_loosenings_and_cold_cohorts() {
    let mut s = Store::new(20);
    let p = Policy {
        divergence_threshold: 0.05,
        ..Policy::default()
    };
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "c",
    );
    let pair =
        comparison::compare_with_policy(&mut s.conn, &r.execution_id, &c.execution_id, 2, 20, p)
            .unwrap()
            .pair;
    let q = quality::window(&s.conn, Request::from_pair(&pair, 20, 2, p))
        .unwrap()
        .unwrap();
    assert!((q.divergence_exposure - 0.95).abs() < 1e-9);
    assert!(q.disabled.is_none());
    let relaxed = Policy {
        divergence_threshold: 0.5,
        ..p
    };
    let q = quality::window(&s.conn, Request::from_pair(&pair, 20, 3, relaxed))
        .unwrap()
        .unwrap();
    assert!((q.divergence_exposure - 0.95).abs() < 1e-9);
    assert!(q.pairs.is_empty());
    let r = s.seed(
        spec(true),
        4,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        5,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "c",
    );
    let pair = comparison::compare_with_policy(
        &mut s.conn,
        &r.execution_id,
        &c.execution_id,
        5,
        20,
        relaxed,
    )
    .unwrap()
    .pair;
    let q = quality::window(&s.conn, Request::from_pair(&pair, 20, 5, relaxed))
        .unwrap()
        .unwrap();
    assert!(q.disabled.is_some());
    assert!((q.divergence_exposure - 1.45).abs() < 1e-9);
}

#[test]
fn missing_or_tampered_quality_projections_never_hide_exposure_or_fabricate_freshness() {
    for fault in ["time", "sequence", "policy", "event", "state"] {
        let mut s = Store::new(20);
        let r = s.seed(
            spec(true),
            1,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "b",
        );
        let c = s.seed(
            spec(false),
            2,
            InferenceCostSource::Estimated,
            "r1",
            "a",
            "c",
        );
        let pair = s.pair(&r, &c, 2).unwrap().pair;
        let sql=match fault {"time"=>"UPDATE provider_graph_quality_event SET observed_at=9999", "sequence"=>"UPDATE provider_graph_quality_event SET candidate_sequence=9999", "policy"=>"UPDATE provider_graph_quality_event SET policy_json=json_set(policy_json,'$.divergence_threshold',0.5)", "event"=>"DELETE FROM provider_graph_quality_event", _=>"DELETE FROM provider_graph_quality_state"};
        s.conn.execute_batch(sql).unwrap();
        assert!(
            quality::window(&s.conn, Request::from_pair(&pair, 20, 3, Policy::default())).is_err(),
            "{fault}"
        );
    }
}

#[test]
fn legacy_graph_pairs_recover_original_quality_once_without_callbacks_or_cost_projections() {
    let mut s = Store::new(20);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "c",
    );
    let pair = s.pair(&r, &c, 2).unwrap().pair;
    let before = quality_window(&s, &pair, 2);
    s.conn
        .execute_batch(
            "DROP TABLE provider_graph_quality_event;DROP TABLE provider_graph_quality_state",
        )
        .unwrap();
    let recovered = quality_window(&s, &pair, 3);
    assert_eq!(recovered.lifetime_pairs, before.lifetime_pairs);
    assert_eq!(recovered.disabled, before.disabled);
    assert_eq!(recovered.divergence_exposure, 1.);
    assert_eq!(quality_window(&s, &pair, 4).lifetime_pairs, 1);
    assert!(s.profiles.is_empty());
}

#[test]
fn graph_disable_persists_after_restart_and_readmission_rejects_stale_policy_or_runtime() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("graph-quality.db");
    let mut s = Store {
        conn: Connection::open(&path).unwrap(),
        profiles: PlanProfiles::with_window(20),
        prepared_pair: None,
        invocation_cost_usd: 0.01,
    };
    agentc_optimizer::schema::ensure_cost_model_schema(&s.conn).unwrap();
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::Estimated,
        "r1",
        "a",
        "c",
    );
    let pair = s.pair(&r, &c, 2).unwrap().pair;
    let before = quality_window(&s, &pair, 2);
    drop(s);
    let s = Store {
        conn: Connection::open(&path).unwrap(),
        profiles: PlanProfiles::with_window(20),
        prepared_pair: None,
        invocation_cost_usd: 0.01,
    };
    let restarted = quality_window(&s, &pair, 3);
    assert_eq!(restarted.disabled, before.disabled);
    assert_eq!(restarted.lifetime_pairs, 1);
    assert_eq!(restarted.divergence_exposure, 1.);
    let mut fabricated = restarted.clone();
    fabricated.policy.divergence_threshold = 1.;
    fabricated.divergence_exposure = 0.;
    fabricated.pairs = vec![pair.clone(); 20];
    fabricated.divergence_upper_p95 = Some(0.);
    assert!(quality::finish_readmission_after_admission(
        &s.conn,
        &fabricated,
        before.disabled.unwrap().reenable_at_us
    )
    .is_err());
    assert_eq!(quality_window(&s, &pair, 4).disabled, restarted.disabled);
}

fn sampling_fixture() -> (
    Store,
    comparison::Pair,
    agentc_optimizer::provider_graph_sampling::Begin,
) {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let mut s = Store::new(20);
    let r = s.seed(
        spec(true),
        1,
        InferenceCostSource::ProviderReported,
        "r1",
        "a",
        "b",
    );
    let c = s.seed(
        spec(false),
        2,
        InferenceCostSource::ProviderReported,
        "r1",
        "a",
        "b",
    );
    let pair = s.pair(&r, &c, 3).unwrap().pair;
    let batch = |n| provider_receipts::Batch {
        version: provider_receipts::VERSION.into(),
        batch_id: format!("{n:032x}"),
        application_version: Some(r.spec.calls.application_version.clone()),
        callback_version: r.spec.calls.callback_version.clone(),
        item_count: 2,
    };
    let request = sampling::Begin {
        source_pair_id: Some(pair.pair_id.clone()),
        deployment: None,
        reference_batch: batch(10),
        candidate_batch: batch(11),
        input_sha256: "e".repeat(64),
        policy: sampling::Policy {
            sampling_rate: 1.0,
            max_workflows: 2,
            max_samples: 1,
            max_physical_invocations: 6,
            max_lifetime_physical_invocations: None,
            commitments: None,
            refresh_interval_workflows: None,
            campaign: None,
            replay_contract_sha256: "f".repeat(64),
        },
    };
    (s, pair, request)
}

fn sampling_deployment_fixture() -> (
    Store,
    comparison::Pair,
    agentc_optimizer::provider_graph_sampling::Begin,
) {
    use agentc_optimizer::{
        provider_graph_admission as admission, provider_graph_sampling as sampling,
    };
    let mut s = Store::new(20);
    let mut pair = None;
    for n in 0..20 {
        s.invocation_cost_usd = 0.01;
        let r = s.seed(
            spec(true),
            100 + n * 2,
            InferenceCostSource::ProviderReported,
            "r1",
            "a",
            "b",
        );
        s.command(json!({"operation":"finish_batch_economics","batch_id":r.batch_id,"wall_latency_ms":20.}));
        s.invocation_cost_usd = 0.001;
        let c = s.seed(
            spec(false),
            101 + n * 2,
            InferenceCostSource::ProviderReported,
            "r1",
            "a",
            "b",
        );
        s.command(json!({"operation":"finish_batch_economics","batch_id":c.batch_id,"wall_latency_ms":20.}));
        pair = Some(s.pair(&r, &c, 1000 + n as i64).unwrap().pair);
    }
    let pair = pair.unwrap();
    let batch = |n| provider_receipts::Batch {
        version: provider_receipts::VERSION.into(),
        batch_id: format!("{n:032x}"),
        application_version: Some("a".repeat(64)),
        callback_version: "whole-results-fixture-v1".into(),
        item_count: 2,
    };
    let request = sampling::Begin {
        source_pair_id: Some(pair.pair_id.clone()),
        reference_batch: batch(1000),
        candidate_batch: batch(1001),
        input_sha256: "e".repeat(64),
        policy: sampling::Policy {
            sampling_rate: 0.,
            max_workflows: 1000,
            max_samples: 0,
            max_physical_invocations: 6,
            max_lifetime_physical_invocations: Some(12),
            commitments: None,
            refresh_interval_workflows: None,
            campaign: None,
            replay_contract_sha256: "f".repeat(64),
        },
        deployment: Some(sampling::Deployment {
            remaining_batches: 10000,
            policy: admission::Policy {
                selection: agentc_optimizer::execution_plan::SelectionPolicy {
                    now_us: 0,
                    max_profile_age_us: i64::MAX,
                    ..Default::default()
                },
                quality: Policy::default(),
                money: admission::Money::BilledUsd,
                max_workers: 2,
                max_physical_invocations: 3,
            },
        }),
    };
    (s, pair, request)
}

#[test]
fn sampling_deployment_is_guarded_before_dispatch_and_reuses_its_original_decision() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, pair, request) = sampling_deployment_fixture();
    let issued =
        sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 2000).unwrap();
    assert!(
        issued.ticket.deployed
            && !issued.ticket.sampled
            && issued.reference.is_none()
            && issued.candidate.is_none()
    );
    let lease = issued.deployment.as_ref().unwrap();
    assert!(
        lease.deployment_id.is_some()
            && lease.sampling_id.as_deref() == Some(&issued.ticket.ticket_id)
    );
    assert_eq!(
        lease.template.as_ref().unwrap().execution_id,
        pair.candidate_execution_id
    );
    assert_eq!(
        issued.decision.as_ref().unwrap().request.remaining_batches,
        1000
    );
    let retry = sampling::begin(&mut s.conn, request, Policy::default(), 20, 2001).unwrap();
    assert_eq!(
        serde_json::to_value(issued).unwrap(),
        serde_json::to_value(retry).unwrap()
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT workflows,samples FROM provider_graph_sampling_state",
                [],
                |r| Ok((r.get::<_, u64>(0)?, r.get::<_, u64>(1)?))
            )
            .unwrap(),
        (1, 0)
    );
}

#[test]
fn sampling_deployment_held_economics_uses_reference_and_cannot_loosen_its_policy() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, _, mut request) = sampling_deployment_fixture();
    request
        .deployment
        .as_mut()
        .unwrap()
        .policy
        .selection
        .max_expected_cost_usd = Some(0.);
    let issued =
        sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 2000).unwrap();
    assert!(!issued.ticket.deployed && issued.reference.is_some() && issued.deployment.is_none());
    request.reference_batch.batch_id = format!("{:032x}", 1002);
    request.candidate_batch.batch_id = format!("{:032x}", 1003);
    request
        .deployment
        .as_mut()
        .unwrap()
        .policy
        .selection
        .max_expected_cost_usd = None;
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 2001).is_err());
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_deployment", [], |r| {
                r.get::<_, u64>(0)
            })
            .unwrap(),
        0
    );
}

#[test]
fn sampling_deployment_failed_visible_work_remains_charged_and_cannot_replay() {
    use agentc_optimizer::{
        provider_graph_deployment as deploy, provider_graph_sampling as sampling,
    };
    let (mut s, pair, request) = sampling_deployment_fixture();
    let issued =
        sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 2000).unwrap();
    let lease = issued.deployment.as_ref().unwrap();
    let e = provider_graph::load(&s.conn, &pair.candidate_execution_id).unwrap();
    let call = prepared_invocation(&s, &e, 0, 20000);
    let binding = deploy::PrimaryBinding {
        node: prepared_node(&e, 0),
        request_sha256: call.request_sha256.clone().unwrap(),
        transport_sha256: call.transport_sha256.clone().unwrap(),
    };
    deploy::approve_primary_node(
        &mut s.conn,
        &lease.lease_id,
        &binding,
        Policy::default(),
        20,
        2000,
    )
    .unwrap();
    provider_receipts::begin_call(&s.conn, &lease.batch_id, &call).unwrap();
    assert!(sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 2001).is_err());
    s.command(json!({"operation":"seal_batch","batch_id":lease.batch_id,"logical_calls":[],"persistence_failed":true}));
    s.command(json!({"operation":"finish_batch_economics","batch_id":lease.batch_id,"wall_latency_ms":5.}));
    let done = sampling::finish(
        &mut s.conn,
        &issued.ticket.ticket_id,
        2001,
        Policy::default(),
        20,
    )
    .unwrap();
    assert_eq!(done.status, "incomplete");
    assert!(!done.sampled && done.pair_id.is_none());
    assert_eq!(
        done,
        sampling::finish(
            &mut s.conn,
            &issued.ticket.ticket_id,
            2002,
            Policy::default(),
            20
        )
        .unwrap()
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_sampling_invocation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        1
    );
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_pair", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        20
    );
    assert!(provider_receipts::begin_call(&s.conn, &lease.batch_id, &call).is_err());
    let mut next = request;
    next.reference_batch.batch_id = format!("{:032x}", 1002);
    next.candidate_batch.batch_id = format!("{:032x}", 1003);
    let next = sampling::begin(&mut s.conn, next, Policy::default(), 20, 2003).unwrap();
    assert!(
        !next.ticket.deployed,
        "unknown prior visible work cannot fund another deployment"
    );
}

#[test]
fn sampling_deployment_missing_joint_custody_holds_more_work() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    for deployment in [false, true] {
        let (mut s, _, request) = sampling_deployment_fixture();
        let issued =
            sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 2000).unwrap();
        let lease = issued.deployment.as_ref().unwrap();
        if deployment {
            s.conn
                .execute(
                    "DELETE FROM provider_graph_deployment WHERE lease_id=?1",
                    [&lease.lease_id],
                )
                .unwrap();
        } else {
            s.conn.execute_batch("PRAGMA foreign_keys=OFF").unwrap();
            s.conn
                .execute(
                    "DELETE FROM provider_graph_sampling WHERE ticket_id=?1",
                    [&issued.ticket.ticket_id],
                )
                .unwrap();
        }
        assert!(preparation::load(&s.conn, &lease.lease_id).is_err());
        assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 2001).is_err());
    }
}

#[test]
fn sampling_deployment_counts_future_calibration_in_cost_and_latency_before_admission() {
    use agentc_optimizer::{
        provider_graph_admission as admission, provider_graph_sampling as sampling,
    };
    for future_samples in [1, 999] {
        let (mut s, _, mut request) = sampling_deployment_fixture();
        request.policy.sampling_rate = 0.001;
        request.policy.max_samples = future_samples;
        let issued = sampling::begin(&mut s.conn, request, Policy::default(), 20, 2000).unwrap();
        assert!(
            !issued.ticket.sampled,
            "fixture clock must issue its prospective unsampled workflow"
        );
        if future_samples == 999 {
            assert!(!issued.ticket.deployed && issued.reference.is_some());
        } else {
            assert!(issued.ticket.deployed && issued.reference.is_none());
            let decision = issued.decision.as_ref().unwrap();
            assert_eq!(decision.future_samples, 1);
            let e = decision
                .alternatives
                .iter()
                .find_map(|a| a.estimate.as_ref())
                .unwrap();
            let admission::Certificate::BilledUsd {
                candidate_upper_usd,
                acquisition_upper_usd,
                expected_upper_usd,
                ..
            } = &e.money
            else {
                panic!("expected billed evidence");
            };
            let r = e
                .reference_costs
                .observations
                .iter()
                .map(|o| o.costs.billed_usd.unwrap().upper_usd)
                .reduce(f64::max)
                .unwrap();
            assert!(
                *expected_upper_usd >= candidate_upper_usd + (acquisition_upper_usd + r) / 1000.
            );
            let c = e
                .candidate_costs
                .observations
                .iter()
                .map(|o| o.resource_latency_ms.unwrap().upper_ms)
                .reduce(f64::max)
                .unwrap();
            let r = e
                .reference_costs
                .observations
                .iter()
                .map(|o| o.resource_latency_ms.unwrap().upper_ms)
                .reduce(f64::max)
                .unwrap();
            assert!(
                e.expected_latency_upper_ms
                    >= c + (decision.acquisition.acquisition_latency_upper_ms + r) / 1000.
            );
        }
    }
}

#[test]
fn sampling_decides_before_both_legs_and_preserves_retry_and_lifetime_limits() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, _, request) = sampling_fixture();
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    assert!(first.ticket.sampled);
    assert_eq!(
        first.reference.as_ref().unwrap().started_at_us,
        first.candidate.as_ref().unwrap().started_at_us
    );
    let retry = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 5).unwrap();
    assert_eq!(
        serde_json::to_value(&first).unwrap(),
        serde_json::to_value(retry).unwrap()
    );
    let mut changed = request.clone();
    changed.policy.max_samples = 2;
    assert!(sampling::begin(&mut s.conn, changed, Policy::default(), 20, 5).is_err());
    let mut second = request.clone();
    second.reference_batch.batch_id = format!("{:032x}", 12);
    second.candidate_batch.batch_id = format!("{:032x}", 13);
    let next = sampling::begin(&mut s.conn, second, Policy::default(), 20, 6).unwrap();
    assert!(!next.ticket.sampled && next.candidate.is_none());
    assert_eq!(next.ticket.ordinal, 2);
    let mut third = request;
    third.reference_batch.batch_id = format!("{:032x}", 14);
    third.candidate_batch.batch_id = format!("{:032x}", 15);
    assert!(sampling::begin(&mut s.conn, third, Policy::default(), 20, 7).is_err());
    let counts:(u64,u64,u64)=s.conn.query_row("SELECT workflows,samples,(SELECT COUNT(*) FROM provider_graph_sampling) FROM provider_graph_sampling_state",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();
    assert_eq!(counts, (2, 1, 2));
}

#[test]
fn sampling_invalid_policy_or_uncovered_physical_graph_rolls_back_before_preparation() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    for case in 0..6 {
        let (mut s, _, mut request) = sampling_fixture();
        match case {
            0 => request.policy.sampling_rate = -0.1,
            1 => request.policy.sampling_rate = f64::NAN,
            2 => request.policy.max_physical_invocations = 5,
            3 => request.policy.replay_contract_sha256 = "missing".into(),
            4 => request.candidate_batch.callback_version = "changed".into(),
            _ => request.reference_batch.application_version = Some("0".repeat(64)),
        }
        assert!(
            sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).is_err(),
            "{case}"
        );
        let count: u64 = s
            .conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_sampling_state",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
        let count: u64 = s
            .conn
            .query_row("SELECT COUNT(*) FROM provider_graph_preparation", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(count, 2);
    }
}

#[test]
fn sampling_candidate_waits_for_visible_reference_and_dispatched_callbacks_cannot_retry() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, pair, request) = sampling_fixture();
    let outcome = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    let reference = provider_graph::load(&s.conn, &pair.reference_execution_id).unwrap();
    let candidate = provider_graph::load(&s.conn, &pair.candidate_execution_id).unwrap();
    for (lease, e, n) in [
        (outcome.reference.as_ref().unwrap(), &reference, 200),
        (outcome.candidate.as_ref().unwrap(), &candidate, 201),
    ] {
        preparation::approve_node(
            &mut s.conn,
            &lease.lease_id,
            &prepared_node(e, 0),
            Policy::default(),
            20,
            4,
        )
        .unwrap();
        let call = prepared_invocation(&s, e, 0, n);
        let result = provider_receipts::begin_call(&s.conn, &lease.batch_id, &call);
        if n == 200 {
            result.unwrap();
            complete_primary_invocation(&s, &lease.batch_id, &call);
        } else {
            assert!(result.is_err());
        }
    }
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 5).is_err());
    let n: u64 = s
        .conn
        .query_row(
            "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id IN (?1,?2)",
            rusqlite::params![
                outcome.reference.as_ref().unwrap().batch_id,
                outcome.candidate.unwrap().batch_id
            ],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 1);
}

#[test]
fn sampling_restart_custody_detects_deleted_ticket_or_replenished_lifetime_counter() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    for delete in [false, true] {
        let (mut s, _, request) = sampling_fixture();
        let outcome = sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).unwrap();
        assert!(preparation::load(&s.conn, &outcome.reference.as_ref().unwrap().lease_id).is_ok());
        if delete {
            s.conn
                .execute(
                    "DELETE FROM provider_graph_sampling WHERE ticket_id=?1",
                    [&outcome.ticket.ticket_id],
                )
                .unwrap();
        } else {
            s.conn
                .execute(
                    "UPDATE provider_graph_sampling_state SET workflows=0,samples=0",
                    [],
                )
                .unwrap();
        }
        assert!(preparation::load(&s.conn, &outcome.reference.as_ref().unwrap().lease_id).is_err());
        assert!(sampling::finish(
            &mut s.conn,
            &outcome.ticket.ticket_id,
            5,
            Policy::default(),
            20
        )
        .is_err());
    }
}

#[test]
fn sampling_failed_foregrounds_finish_once_without_fabricating_quality_or_reference_success() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, _, request) = sampling_fixture();
    let outcome = sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).unwrap();
    assert!(sampling::finish(
        &mut s.conn,
        &outcome.ticket.ticket_id,
        5,
        Policy::default(),
        20
    )
    .is_err());
    for lease in [
        outcome.reference.as_ref().unwrap(),
        outcome.candidate.as_ref().unwrap(),
    ] {
        s.command(json!({"operation":"seal_batch","batch_id":lease.batch_id,"logical_calls":[],"persistence_failed":true}));
        s.command(json!({"operation":"finish_batch_economics","batch_id":lease.batch_id,"wall_latency_ms":0.}));
    }
    for (quality, size, now) in [
        (Policy::default(), 50, 5),
        (
            Policy {
                divergence_threshold: 0.5,
                ..Policy::default()
            },
            20,
            5,
        ),
        (Policy::default(), 20, 3),
    ] {
        assert!(
            sampling::finish(&mut s.conn, &outcome.ticket.ticket_id, now, quality, size).is_err()
        );
    }
    let done = sampling::finish(
        &mut s.conn,
        &outcome.ticket.ticket_id,
        5,
        Policy::default(),
        20,
    )
    .unwrap();
    assert_eq!(done.status, "incomplete");
    assert!(done.pair_id.is_none());
    assert_eq!(
        done,
        sampling::finish(
            &mut s.conn,
            &outcome.ticket.ticket_id,
            6,
            Policy::default(),
            20
        )
        .unwrap()
    );
    let n: u64 = s
        .conn
        .query_row("SELECT COUNT(*) FROM provider_graph_pair", [], |r| r.get(0))
        .unwrap();
    assert_eq!(n, 1);
    let mut forged = serde_json::to_value(done).unwrap();
    forged["status"] = json!("reference_only");
    let raw = agentc_core::storage::canonical_json(&forged);
    let hash = agentc_core::storage::content_hash(&raw);
    s.conn
        .execute(
            "UPDATE provider_graph_sampling SET result_json=?2,result_sha256=?3 WHERE ticket_id=?1",
            rusqlite::params![outcome.ticket.ticket_id, raw, hash],
        )
        .unwrap();
    assert!(sampling::finish(
        &mut s.conn,
        &outcome.ticket.ticket_id,
        7,
        Policy::default(),
        20
    )
    .is_err());
}

#[test]
fn sampling_fractional_decisions_are_prospective_and_remain_fixed_on_retry() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, _, mut request) = sampling_fixture();
    request.policy.sampling_rate = 0.5;
    request.policy.max_workflows = 32;
    request.policy.max_samples = 32;
    let mut selected = 0;
    for n in 0..32 {
        request.reference_batch.batch_id = format!("{:032x}", 100 + 2 * n);
        request.candidate_batch.batch_id = format!("{:032x}", 101 + 2 * n);
        let issued =
            sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 10 + n).unwrap();
        selected += u64::from(issued.ticket.sampled);
        let retry =
            sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 10 + n).unwrap();
        assert_eq!(issued.ticket.sampled, retry.ticket.sampled);
        assert_eq!(issued.ticket.ticket_id, retry.ticket.ticket_id);
    }
    assert!(selected > 0 && selected < 32);
    let saved: u64 = s
        .conn
        .query_row(
            "SELECT samples FROM provider_graph_sampling_state",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(saved, selected);
}

fn cold_sampling_fixture() -> (
    Store,
    agentc_optimizer::provider_graph_sampling::Begin,
    agentc_optimizer::Optimizer,
    agentc_optimizer::model_catalog::ModelCatalog,
    agentc_optimizer::dag::Call,
) {
    use agentc_optimizer::{
        dag::{Call, DepSource, Message, Parameters},
        provider_graph_sampling as sampling,
    };
    let s = Store::new(20);
    let batch = |n| provider_receipts::Batch {
        version: provider_receipts::VERSION.into(),
        batch_id: format!("{n:032x}"),
        application_version: Some("a".repeat(64)),
        callback_version: "native-discovery-v1".into(),
        item_count: 2,
    };
    let request = sampling::Begin {
        source_pair_id: None,
        deployment: None,
        reference_batch: batch(10),
        candidate_batch: batch(11),
        input_sha256: "e".repeat(64),
        policy: sampling::Policy {
            sampling_rate: 1.0,
            max_workflows: 2,
            max_samples: 2,
            max_physical_invocations: 6,
            max_lifetime_physical_invocations: None,
            commitments: None,
            refresh_interval_workflows: None,
            campaign: None,
            replay_contract_sha256: "f".repeat(64),
        },
    };
    let opt = agentc_optimizer::Optimizer::empty(
        std::sync::Arc::new(agentc_optimizer::CostModel::new()),
        agentc_optimizer::OptimizerConfig::default(),
    );
    let catalog = agentc_optimizer::model_catalog::default_model_catalog().unwrap();
    let source = Call {
        call_site_id: "sdk.native-discovery".into(),
        trace_id: [0; 16],
        span_id: [0; 8],
        model: "gpt-4o-2024-11-20".into(),
        messages: vec![Message {
            role: "user".into(),
            content: "PRIVATE_NATIVE_DISCOVERY".into(),
        }],
        parameters: Parameters {
            max_output_tokens: Some(128),
            extra: json!({"agentc_route_context":{"provider_protocol":"openai.chat.completions.v1","provider_namespace":"openai","input_tokens_upper_bound":64,"image_input":false,"tool_calling":false,"structured_outputs":false,"streaming":false,"provider_configuration":{"version":"provider-request-config-v1","supported":true,"sha256":"a".repeat(64)},"sdk_response_cache":{"version":agentc_optimizer::wiring::SDK_RESPONSE_CACHE_VERSION,"supported":true,"message_sha256":"c".repeat(64),"shape_sha256":"d".repeat(64)}}}),
            ..Parameters::default()
        },
        tools: vec![],
        input_deps: vec![DepSource::Literal],
        occurrence_ix: 0,
    };
    (s, request, opt, catalog, source)
}

fn cold_invocation(node: &preparation::Node, number: u64) -> provider_receipts::Invocation {
    provider_receipts::Invocation {
        invocation_id: format!("{number:032x}"),
        position: node.position,
        ordinal: 0,
        role: provider_receipts::Role::Primary,
        provider_protocol: node.plan.provider_protocol.clone(),
        requested_model_id: node.plan.target_model_id.clone(),
        request_sha256: Some("f".repeat(64)),
        logical_request_sha256: Some(node.logical_request_sha256.clone()),
        transport_sha256: Some("d".repeat(64)),
        logical_plan_key: Some(node.key.clone()),
        logical_runtime: Some(node.runtime.clone()),
        dispatch_plan_key: Some(node.key.clone()),
        dispatch_runtime: Some(node.runtime.clone()),
        parent_plan_key: None,
        transport_verified: true,
        tariff_declaration: None,
    }
}

#[test]
fn cold_sampling_starts_without_evidence_and_bounds_the_unknown_graph_before_dispatch() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, request, _, _, _) = cold_sampling_fixture();
    let issued = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    assert!(
        issued.reference.as_ref().unwrap().template.is_none()
            && issued.candidate.as_ref().unwrap().template.is_none()
    );
    assert_eq!(
        issued.reference.as_ref().unwrap().started_at_us,
        issued.candidate.as_ref().unwrap().started_at_us
    );
    let retry = sampling::begin(&mut s.conn, request, Policy::default(), 20, 5).unwrap();
    assert_eq!(issued.ticket.ticket_id, retry.ticket.ticket_id);
    for table in [
        "provider_receipt_invocation",
        "provider_graph_execution",
        "provider_graph_pair",
    ] {
        let n: u64 = s
            .conn
            .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 0);
    }
}

#[test]
fn sampling_lifetime_physical_reserves_open_workflows_and_retry_never_replenishes() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, _, _, _) = cold_sampling_fixture();
    request.policy.max_workflows = 10;
    request.policy.max_samples = 10;
    request.policy.max_lifetime_physical_invocations = Some(6);
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    let retry = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 5).unwrap();
    assert_eq!(first.ticket.ticket_id, retry.ticket.ticket_id);
    request.reference_batch.batch_id = format!("{:032x}", 12);
    request.candidate_batch.batch_id = format!("{:032x}", 13);
    let error = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 6)
        .unwrap_err()
        .to_string();
    assert!(error.contains("lifetime physical allowance"), "{error}");
    request.policy.max_lifetime_physical_invocations = Some(12);
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 6).is_err());
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_sampling", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_receipt_batch", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        2
    );
}

fn billed_commitments(
    workflow: f64,
    lifetime: f64,
    open: u64,
    pending: u64,
) -> agentc_optimizer::provider_graph_sampling::Commitments {
    use agentc_optimizer::provider_graph_sampling::{CommitmentMoney, Commitments};
    Commitments {
        money: CommitmentMoney::BilledUsd {
            workflow_usd: workflow,
            lifetime_usd: lifetime,
        },
        max_open_workflows: open,
        max_pending_invocations: pending,
    }
}

#[test]
fn sampling_commitments_reserve_before_callbacks_and_policy_cannot_replenish() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, _, _, _) = cold_sampling_fixture();
    request.policy.commitments = Some(billed_commitments(1., 1., 1, 1));
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    let report = sampling::commitment_report(&s.conn, &first.ticket.ticket_id)
        .unwrap()
        .unwrap();
    assert_eq!(report.open_workflows, 1);
    assert_eq!(report.billed_committed_upper_usd, Some(1.));
    let retry = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 5).unwrap();
    assert_eq!(first.ticket.ticket_id, retry.ticket.ticket_id);
    request.reference_batch.batch_id = format!("{:032x}", 12);
    request.candidate_batch.batch_id = format!("{:032x}", 13);
    assert!(sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 6).is_err());
    request.policy.commitments = Some(billed_commitments(1., 3., 3, 3));
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 6).is_err());
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_sampling", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn sampling_commitments_release_verified_unused_money_after_both_legs_finish() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    request.policy.commitments = Some(billed_commitments(1., 1.1, 2, 2));
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    publish_cold_leg(
        &mut s,
        first.reference.as_ref().unwrap(),
        &opt,
        &catalog,
        &source,
        true,
        100,
    );
    publish_cold_leg(
        &mut s,
        first.candidate.as_ref().unwrap(),
        &opt,
        &catalog,
        &source,
        false,
        200,
    );
    request.reference_batch.batch_id = format!("{:032x}", 12);
    request.candidate_batch.batch_id = format!("{:032x}", 13);
    assert!(sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 5).is_err());
    sampling::finish(
        &mut s.conn,
        &first.ticket.ticket_id,
        6,
        Policy::default(),
        20,
    )
    .unwrap();
    let report = sampling::commitment_report(&s.conn, &first.ticket.ticket_id)
        .unwrap()
        .unwrap();
    assert_eq!(report.open_workflows, 0);
    assert_eq!(report.unknown_cost_workflows, 0);
    let charged = report.billed_committed_upper_usd.unwrap();
    assert!((0.004..0.0040000001).contains(&charged));
    let next = sampling::begin(&mut s.conn, request, Policy::default(), 20, 7).unwrap();
    let report = sampling::commitment_report(&s.conn, &next.ticket.ticket_id)
        .unwrap()
        .unwrap();
    assert_eq!(report.open_workflows, 1);
    assert!(report.billed_committed_upper_usd.unwrap() > 1.004);
}

fn commit_cold_call(
    s: &mut Store,
    lease: &preparation::Lease,
    opt: &agentc_optimizer::Optimizer,
    catalog: &agentc_optimizer::model_catalog::ModelCatalog,
    source: &agentc_optimizer::dag::Call,
    number: u64,
) -> provider_receipts::Invocation {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let pos = CallPosition { item: 0, call: 0 };
    let source = serde_json::to_string(source).unwrap();
    let raw = sampling::plan_discovery(
        &mut s.conn,
        opt,
        catalog,
        &lease.lease_id,
        pos,
        &source,
        true,
    )
    .unwrap();
    let node = agentc_optimizer::ffi::prepared_graph_node(catalog, pos, &source, &raw).unwrap();
    let call = cold_invocation(&node, number);
    provider_receipts::begin_call(&s.conn, &lease.batch_id, &call).unwrap();
    call
}

#[test]
fn sampling_commitments_sealing_pending_work_does_not_release_its_money_or_shared_slot() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    request.policy.sampling_rate = 0.;
    request.policy.max_samples = 0;
    request.policy.commitments = Some(billed_commitments(1., 1.5, 2, 1));
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    let lease = first.reference.as_ref().unwrap();
    let call = commit_cold_call(&mut s, lease, &opt, &catalog, &source, 100);
    provider_receipts::begin_call(&s.conn, &lease.batch_id, &call).unwrap();
    s.command(json!({"operation":"seal_batch","batch_id":lease.batch_id,"logical_calls":[],"persistence_failed":true}));
    sampling::finish(
        &mut s.conn,
        &first.ticket.ticket_id,
        5,
        Policy::default(),
        20,
    )
    .unwrap();
    let report = sampling::commitment_report(&s.conn, &first.ticket.ticket_id)
        .unwrap()
        .unwrap();
    assert_eq!(report.open_workflows, 0);
    assert_eq!(report.pending_invocations, 1);
    assert_eq!(report.unknown_cost_workflows, 1);
    assert_eq!(report.billed_committed_upper_usd, Some(1.));
    request.reference_batch.batch_id = format!("{:032x}", 12);
    request.candidate_batch.batch_id = format!("{:032x}", 13);
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 6).is_err());
}

#[test]
fn sampling_commitments_pending_calls_share_a_limit_across_independent_workflows() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    request.policy.sampling_rate = 0.;
    request.policy.max_samples = 0;
    request.policy.commitments = Some(billed_commitments(1., 3., 2, 1));
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    request.reference_batch.batch_id = format!("{:032x}", 12);
    request.candidate_batch.batch_id = format!("{:032x}", 13);
    let second = sampling::begin(&mut s.conn, request, Policy::default(), 20, 5).unwrap();
    let first_lease = first.reference.as_ref().unwrap();
    let second_lease = second.reference.as_ref().unwrap();
    let call = commit_cold_call(&mut s, first_lease, &opt, &catalog, &source, 100);
    let pos = CallPosition { item: 0, call: 0 };
    let source = serde_json::to_string(&source).unwrap();
    let raw = sampling::plan_discovery(
        &mut s.conn,
        &opt,
        &catalog,
        &second_lease.lease_id,
        pos,
        &source,
        true,
    )
    .unwrap();
    let node = agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &source, &raw).unwrap();
    let next = cold_invocation(&node, 200);
    assert!(provider_receipts::begin_call(&s.conn, &second_lease.batch_id, &next).is_err());
    complete_primary_invocation(&s, &first_lease.batch_id, &call);
    provider_receipts::begin_call(&s.conn, &second_lease.batch_id, &next).unwrap();
    let r = sampling::commitment_report(&s.conn, &second.ticket.ticket_id)
        .unwrap()
        .unwrap();
    assert_eq!(r.pending_invocations, 1);
}

#[test]
fn sampling_commitments_observed_overrun_stays_readable_and_holds_new_dispatches() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    request.policy.sampling_rate = 0.;
    request.policy.max_samples = 0;
    request.policy.commitments = Some(billed_commitments(0.0001, 1., 2, 2));
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    let lease = first.reference.as_ref().unwrap();
    let call = commit_cold_call(&mut s, lease, &opt, &catalog, &source, 100);
    complete_primary_invocation(&s, &lease.batch_id, &call);
    let r = sampling::commitment_report(&s.conn, &first.ticket.ticket_id)
        .unwrap()
        .unwrap();
    assert!(r.budget_exceeded);
    assert!(r.billed_committed_upper_usd.unwrap() >= 0.001);
    request.reference_batch.batch_id = format!("{:032x}", 12);
    request.candidate_batch.batch_id = format!("{:032x}", 13);
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 5).is_err());
    s.command(json!({"operation":"seal_batch","batch_id":lease.batch_id,"logical_calls":[],"persistence_failed":true}));
    let done = sampling::finish(
        &mut s.conn,
        &first.ticket.ticket_id,
        6,
        Policy::default(),
        20,
    )
    .unwrap();
    assert_eq!(done.status, "incomplete");
    assert!(
        sampling::commitment_report(&s.conn, &first.ticket.ticket_id)
            .unwrap()
            .unwrap()
            .budget_exceeded
    );
}

#[test]
fn sampling_commitments_invalid_or_corrupted_policy_and_dispatch_hold_before_more_work() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    for c in [
        billed_commitments(-1., 2., 2, 1),
        billed_commitments(2., 1., 2, 1),
        billed_commitments(1., 2., 0, 1),
        billed_commitments(1., 2., 1, 0),
    ] {
        request.policy.commitments = Some(c);
        assert!(sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).is_err());
    }
    request.policy.commitments = Some(billed_commitments(1., 2., 2, 2));
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    commit_cold_call(
        &mut s,
        first.reference.as_ref().unwrap(),
        &opt,
        &catalog,
        &source,
        100,
    );
    s.conn
        .execute("DELETE FROM provider_graph_sampling_invocation", [])
        .unwrap();
    assert!(sampling::commitment_report(&s.conn, &first.ticket.ticket_id).is_err());
    request.reference_batch.batch_id = format!("{:032x}", 12);
    request.candidate_batch.batch_id = format!("{:032x}", 13);
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 5).is_err());
}

#[test]
fn sampling_commitments_joint_deployment_uses_the_same_pre_callback_money_reservation() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, _, mut request) = sampling_deployment_fixture();
    request.policy.max_lifetime_physical_invocations = None;
    request.policy.commitments = Some(billed_commitments(0.1, 0.15, 2, 2));
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 2000).unwrap();
    assert!(first.ticket.deployed);
    let r = sampling::commitment_report(&s.conn, &first.ticket.ticket_id)
        .unwrap()
        .unwrap();
    assert_eq!(r.open_workflows, 1);
    assert_eq!(r.billed_committed_upper_usd, Some(0.1));
    request.reference_batch.batch_id = format!("{:032x}", 1002);
    request.candidate_batch.batch_id = format!("{:032x}", 1003);
    assert!(
        sampling::begin(&mut s.conn, request, Policy::default(), 20, 2001)
            .unwrap_err()
            .to_string()
            .contains("monetary commitment")
    );
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_deployment", [], |r| {
                r.get::<_, u64>(0)
            })
            .unwrap(),
        1
    );
}

#[test]
fn sampling_commitments_two_connections_cannot_reserve_the_same_lifetime_money() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (_, mut request, _, _, _) = cold_sampling_fixture();
    request.policy.commitments = Some(billed_commitments(1., 1., 10, 10));
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("commitments.db");
    let conn = Connection::open(&path).unwrap();
    agentc_optimizer::schema::ensure_cost_model_schema(&conn).unwrap();
    sampling::ensure_schema(&conn).unwrap();
    drop(conn);
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let threads: Vec<_> = (0..2)
        .map(|n| {
            let path = path.clone();
            let barrier = barrier.clone();
            let mut request = request.clone();
            request.reference_batch.batch_id = format!("{:032x}", 10 + n * 2);
            request.candidate_batch.batch_id = format!("{:032x}", 11 + n * 2);
            std::thread::spawn(move || {
                let mut conn = Connection::open(path).unwrap();
                conn.busy_timeout(std::time::Duration::from_secs(10))
                    .unwrap();
                barrier.wait();
                sampling::begin(&mut conn, request, Policy::default(), 20, 4)
            })
        })
        .collect();
    let results: Vec<_> = threads.into_iter().map(|t| t.join().unwrap()).collect();
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    let error = results
        .into_iter()
        .find_map(Result::err)
        .unwrap()
        .to_string();
    assert!(error.contains("monetary commitment"), "{error}");
    let conn = Connection::open(path).unwrap();
    assert_eq!(
        conn.query_row("SELECT COUNT(*) FROM provider_graph_sampling", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn sampling_commitments_tariff_requires_its_original_snapshot_before_sdk_dispatch() {
    use agentc_optimizer::{provider_graph_sampling as sampling, provider_tariff as tariff};
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    let mut policy = sampling::Commitments {
        money: sampling::CommitmentMoney::PublishedTokenTariffUsd {
            snapshot_sha256: "a".repeat(64),
            workflow_nanodollars: 50000,
            lifetime_nanodollars: 100000,
        },
        max_open_workflows: 2,
        max_pending_invocations: 1,
    };
    request.policy.commitments = Some(policy.clone());
    assert!(sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).is_err());
    let sampling::CommitmentMoney::PublishedTokenTariffUsd {
        snapshot_sha256, ..
    } = &mut policy.money
    else {
        unreachable!();
    };
    *snapshot_sha256 = tariff::snapshot_sha256();
    request.policy.commitments = Some(policy);
    let first = sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).unwrap();
    let lease = first.reference.as_ref().unwrap();
    let pos = CallPosition { item: 0, call: 0 };
    let source = serde_json::to_string(&source).unwrap();
    let raw = sampling::plan_discovery(
        &mut s.conn,
        &opt,
        &catalog,
        &lease.lease_id,
        pos,
        &source,
        true,
    )
    .unwrap();
    let node = agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &source, &raw).unwrap();
    let call = cold_invocation(&node, 100);
    assert!(
        provider_receipts::begin_call(&s.conn, &lease.batch_id, &call)
            .unwrap_err()
            .to_string()
            .contains("pinned tariff declaration")
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    let r = sampling::commitment_report(&s.conn, &first.ticket.ticket_id)
        .unwrap()
        .unwrap();
    assert_eq!(r.tariff_committed_upper_nanodollars, Some(50000));
}

#[test]
fn sampling_lifetime_physical_releases_only_undispatched_slots_after_finish() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    request.policy.max_workflows = 10;
    request.policy.max_samples = 10;
    request.policy.max_lifetime_physical_invocations = Some(10);
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    publish_cold_leg(
        &mut s,
        first.reference.as_ref().unwrap(),
        &opt,
        &catalog,
        &source,
        true,
        100,
    );
    publish_cold_leg(
        &mut s,
        first.candidate.as_ref().unwrap(),
        &opt,
        &catalog,
        &source,
        false,
        200,
    );
    let mut next = request.clone();
    next.reference_batch.batch_id = format!("{:032x}", 12);
    next.candidate_batch.batch_id = format!("{:032x}", 13);
    // Four calls have dispatched, but the first workflow still owns six slots.
    assert!(sampling::begin(&mut s.conn, next.clone(), Policy::default(), 20, 6).is_err());
    sampling::finish(
        &mut s.conn,
        &first.ticket.ticket_id,
        6,
        Policy::default(),
        20,
    )
    .unwrap();
    let second = sampling::begin(&mut s.conn, next.clone(), Policy::default(), 20, 7).unwrap();
    assert_eq!(second.ticket.ordinal, 2);
    let retried = sampling::begin(&mut s.conn, next, Policy::default(), 20, 8).unwrap();
    assert_eq!(second.ticket.ticket_id, retried.ticket.ticket_id);
    request.reference_batch.batch_id = format!("{:032x}", 14);
    request.candidate_batch.batch_id = format!("{:032x}", 15);
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 9).is_err());
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_sampling_invocation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        4
    );
}

#[test]
fn sampling_lifetime_physical_failed_pending_work_remains_charged_and_retries_share_slots() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    request.policy.sampling_rate = 0.0;
    request.policy.max_workflows = 10;
    request.policy.max_samples = 0;
    request.policy.max_lifetime_physical_invocations = Some(6);
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    let pos = CallPosition { item: 0, call: 0 };
    let source = serde_json::to_string(&source).unwrap();
    let raw = sampling::plan_discovery(
        &mut s.conn,
        &opt,
        &catalog,
        &first.reference.as_ref().unwrap().lease_id,
        pos,
        &source,
        true,
    )
    .unwrap();
    let node = agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &source, &raw).unwrap();
    let call = cold_invocation(&node, 100);
    provider_receipts::begin_call(&s.conn, &first.reference.as_ref().unwrap().batch_id, &call)
        .unwrap();
    provider_receipts::begin_call(&s.conn, &first.reference.as_ref().unwrap().batch_id, &call)
        .unwrap();
    let mut conflict = call.clone();
    conflict.request_sha256 = Some("a".repeat(64));
    assert!(provider_receipts::begin_call(
        &s.conn,
        &first.reference.as_ref().unwrap().batch_id,
        &conflict
    )
    .is_err());
    s.command(json!({"operation":"seal_batch","batch_id":first.reference.as_ref().unwrap().batch_id,"logical_calls":[],"persistence_failed":true}));
    sampling::finish(
        &mut s.conn,
        &first.ticket.ticket_id,
        5,
        Policy::default(),
        20,
    )
    .unwrap();
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_sampling_invocation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        1
    );
    request.reference_batch.batch_id = format!("{:032x}", 12);
    request.candidate_batch.batch_id = format!("{:032x}", 13);
    assert!(sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 6).is_err());
    let mut completion = serde_json::to_value(provider_receipts::Completion {
        status: provider_receipts::Status::Failed,
        model_version: None,
        input_tokens: None,
        output_tokens: None,
        cost_usd: None,
        cost_source: InferenceCostSource::Estimated,
        latency_ms: 1.0,
        http_attempts: Some(1),
        exception_type: Some("FixtureError".into()),
        billing_scope_sha256: None,
        provider_generation_sha256: None,
        provider_request_id_sha256: None,
        billing_usage: None,
    })
    .unwrap();
    completion["status"] = json!("failed");
    s.command(json!({"operation":"finish_call","batch_id":first.reference.as_ref().unwrap().batch_id,"invocation_id":call.invocation_id,"completion":completion}));
    // Completion and missing billing do not return a dispatched slot.
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 7).is_err());
}

#[test]
fn sampling_lifetime_physical_missing_or_changed_dispatch_custody_holds_new_work() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    for kind in 0..4 {
        let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
        request.policy.max_workflows = 10;
        request.policy.max_samples = 10;
        request.policy.max_lifetime_physical_invocations = Some(12);
        let first =
            sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
        let pos = CallPosition { item: 0, call: 0 };
        let source = serde_json::to_string(&source).unwrap();
        let raw = sampling::plan_discovery(
            &mut s.conn,
            &opt,
            &catalog,
            &first.reference.as_ref().unwrap().lease_id,
            pos,
            &source,
            true,
        )
        .unwrap();
        let node =
            agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &source, &raw).unwrap();
        provider_receipts::begin_call(
            &s.conn,
            &first.reference.as_ref().unwrap().batch_id,
            &cold_invocation(&node, 100),
        )
        .unwrap();
        match kind {
            0 => {
                s.conn
                    .execute("DELETE FROM provider_graph_sampling_invocation", [])
                    .unwrap();
            }
            1 => {
                s.conn.execute_batch("PRAGMA foreign_keys=OFF").unwrap();
                s.conn
                    .execute("DELETE FROM provider_receipt_invocation", [])
                    .unwrap();
            }
            2 => {
                s.conn
                    .execute(
                        "UPDATE provider_receipt_invocation SET invocation_json='{}'",
                        [],
                    )
                    .unwrap();
            }
            _ => {
                s.conn
                    .execute("UPDATE provider_graph_sampling SET result_json='{}'", [])
                    .unwrap();
            }
        }
        request.reference_batch.batch_id = format!("{:032x}", 12);
        request.candidate_batch.batch_id = format!("{:032x}", 13);
        assert!(
            sampling::begin(&mut s.conn, request, Policy::default(), 20, 6).is_err(),
            "{kind}"
        );
        assert!(
            preparation::load(&s.conn, &first.reference.as_ref().unwrap().lease_id).is_err(),
            "{kind}"
        );
    }
}

#[test]
fn sampling_lifetime_physical_two_connections_cannot_reserve_the_same_last_allowance() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (s, mut request, _, _, _) = cold_sampling_fixture();
    request.policy.max_workflows = 10;
    request.policy.max_samples = 10;
    request.policy.max_lifetime_physical_invocations = Some(6);
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("sampling.db");
    let conn = Connection::open(&path).unwrap();
    agentc_optimizer::schema::ensure_cost_model_schema(&conn).unwrap();
    sampling::ensure_schema(&conn).unwrap();
    drop(conn);
    drop(s);
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let threads: Vec<_> = (0..2)
        .map(|n| {
            let barrier = barrier.clone();
            let path = path.clone();
            let mut request = request.clone();
            request.reference_batch.batch_id = format!("{:032x}", 10 + 2 * n);
            request.candidate_batch.batch_id = format!("{:032x}", 11 + 2 * n);
            std::thread::spawn(move || {
                let mut conn = Connection::open(path).unwrap();
                conn.busy_timeout(std::time::Duration::from_secs(10))
                    .unwrap();
                barrier.wait();
                sampling::begin(&mut conn, request, Policy::default(), 20, 4)
            })
        })
        .collect();
    let results: Vec<_> = threads.into_iter().map(|t| t.join().unwrap()).collect();
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    let error = results
        .into_iter()
        .find_map(Result::err)
        .unwrap()
        .to_string();
    assert!(error.contains("lifetime physical allowance"), "{error}");
    let conn = Connection::open(path).unwrap();
    assert_eq!(
        conn.query_row("SELECT COUNT(*) FROM provider_graph_sampling", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn sampling_lifetime_physical_policy_is_optional_and_validated_before_state_changes() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, _, _, _) = cold_sampling_fixture();
    let raw = serde_json::to_value(&request).unwrap();
    assert!(raw["policy"]
        .get("max_lifetime_physical_invocations")
        .is_none());
    assert_eq!(
        serde_json::from_value::<sampling::Begin>(raw)
            .unwrap()
            .policy,
        request.policy
    );
    for allowance in [0, 5, (1 << 53) + 1] {
        request.policy.max_lifetime_physical_invocations = Some(allowance);
        assert!(sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).is_err());
        assert_eq!(
            s.conn
                .query_row(
                    "SELECT COUNT(*) FROM provider_graph_sampling_state",
                    [],
                    |r| r.get::<_, u64>(0)
                )
                .unwrap(),
            0
        );
    }
}

#[test]
fn cold_sampling_native_node_is_immutable_and_sdk_dispatch_cannot_bypass_it() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, request, opt, catalog, mut source) = cold_sampling_fixture();
    let issued = sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).unwrap();
    let pos = CallPosition { item: 0, call: 0 };
    let original = serde_json::to_string(&source).unwrap();
    let raw = sampling::plan_discovery(
        &mut s.conn,
        &opt,
        &catalog,
        &issued.reference.as_ref().unwrap().lease_id,
        pos,
        &original,
        true,
    )
    .unwrap();
    let node = agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &original, &raw).unwrap();
    assert_eq!(
        raw,
        sampling::plan_discovery(
            &mut s.conn,
            &opt,
            &catalog,
            &issued.reference.as_ref().unwrap().lease_id,
            pos,
            &original,
            true
        )
        .unwrap()
    );
    source.parameters.max_output_tokens = Some(256);
    assert!(sampling::plan_discovery(
        &mut s.conn,
        &opt,
        &catalog,
        &issued.reference.as_ref().unwrap().lease_id,
        pos,
        &serde_json::to_string(&source).unwrap(),
        true
    )
    .is_err());
    let mut call = cold_invocation(&node, 100);
    call.logical_request_sha256 = Some("0".repeat(64));
    assert!(provider_receipts::begin_call(
        &s.conn,
        &issued.reference.as_ref().unwrap().batch_id,
        &call
    )
    .is_err());
    call.logical_request_sha256 = Some(node.logical_request_sha256.clone());
    provider_receipts::begin_call(&s.conn, &issued.reference.as_ref().unwrap().batch_id, &call)
        .unwrap();
    assert!(sampling::plan_discovery(
        &mut s.conn,
        &opt,
        &catalog,
        &issued.reference.as_ref().unwrap().lease_id,
        pos,
        &original,
        true
    )
    .is_err());
    let stored: String = s
        .conn
        .query_row(
            "SELECT node_json FROM provider_graph_prepared_node",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(!stored.contains("PRIVATE_"));
}

#[test]
fn cold_sampling_candidate_waits_for_reference_and_worker_limit_is_atomic() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, request, opt, catalog, source) = cold_sampling_fixture();
    let issued = sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).unwrap();
    let original = serde_json::to_string(&source).unwrap();
    let pos = CallPosition { item: 0, call: 0 };
    assert!(sampling::plan_discovery(
        &mut s.conn,
        &opt,
        &catalog,
        &issued.candidate.as_ref().unwrap().lease_id,
        pos,
        &original,
        false
    )
    .is_err());
    let mut calls = Vec::new();
    for item in 0..2 {
        let position = CallPosition { item, call: 0 };
        let raw = sampling::plan_discovery(
            &mut s.conn,
            &opt,
            &catalog,
            &issued.reference.as_ref().unwrap().lease_id,
            position,
            &original,
            true,
        )
        .unwrap();
        let node = agentc_optimizer::ffi::prepared_graph_node(&catalog, position, &original, &raw)
            .unwrap();
        calls.push(cold_invocation(&node, 100 + u64::from(item)));
    }
    provider_receipts::begin_call(
        &s.conn,
        &issued.reference.as_ref().unwrap().batch_id,
        &calls[0],
    )
    .unwrap();
    assert!(provider_receipts::begin_call(
        &s.conn,
        &issued.reference.as_ref().unwrap().batch_id,
        &calls[1]
    )
    .is_err());
    complete_primary_invocation(&s, &issued.reference.as_ref().unwrap().batch_id, &calls[0]);
    provider_receipts::begin_call(
        &s.conn,
        &issued.reference.as_ref().unwrap().batch_id,
        &calls[1],
    )
    .unwrap();
}

#[test]
fn cold_sampling_missing_node_and_global_counter_custody_hold_before_more_sdk_work() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    for erase_node in [true, false] {
        let (mut s, request, opt, catalog, source) = cold_sampling_fixture();
        let issued = sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).unwrap();
        let pos = CallPosition { item: 0, call: 0 };
        let original = serde_json::to_string(&source).unwrap();
        let raw = sampling::plan_discovery(
            &mut s.conn,
            &opt,
            &catalog,
            &issued.reference.as_ref().unwrap().lease_id,
            pos,
            &original,
            true,
        )
        .unwrap();
        let node =
            agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &original, &raw).unwrap();
        if erase_node {
            s.conn
                .execute("DELETE FROM provider_graph_prepared_node", [])
                .unwrap();
        } else {
            s.conn
                .execute(
                    "UPDATE provider_graph_sampling_state SET workflows=0,samples=0",
                    [],
                )
                .unwrap();
        }
        assert!(provider_receipts::begin_call(
            &s.conn,
            &issued.reference.as_ref().unwrap().batch_id,
            &cold_invocation(&node, 100)
        )
        .is_err());
        let n: u64 = s
            .conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 0);
    }
}

fn publish_cold_leg(
    s: &mut Store,
    lease: &preparation::Lease,
    opt: &agentc_optimizer::Optimizer,
    catalog: &agentc_optimizer::model_catalog::ModelCatalog,
    source: &agentc_optimizer::dag::Call,
    reference: bool,
    number: u64,
) -> Execution {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let mut nodes = Vec::new();
    let mut outcomes = Vec::new();
    let mut requests = BTreeMap::new();
    for item in 0..2 {
        let pos = CallPosition { item, call: 0 };
        let input = serde_json::to_string(source).unwrap();
        let raw = sampling::plan_discovery(
            &mut s.conn,
            opt,
            catalog,
            &lease.lease_id,
            pos,
            &input,
            reference,
        )
        .unwrap();
        let node = agentc_optimizer::ffi::prepared_graph_node(catalog, pos, &input, &raw).unwrap();
        let call = cold_invocation(&node, number + u64::from(item));
        provider_receipts::begin_call(&s.conn, &lease.batch_id, &call).unwrap();
        complete_primary_invocation(s, &lease.batch_id, &call);
        requests.insert(pos, node.logical_request_sha256.clone());
        outcomes.push(MultiCallOutcome {
            position: pos,
            cost_source: InferenceCostSource::ProviderReported,
            outcome: Outcome {
                cost_source: InferenceCostSource::ProviderReported,
                physical_invocations: vec![agentc_optimizer::cost_evidence::PhysicalInvocation {
                    batch_id: lease.batch_id.clone(),
                    invocation_id: call.invocation_id,
                }],
                input_tokens: 2,
                output_tokens: 1,
                latency_ms: 5.0,
                cost_usd: 0.001,
                inference_cost_complete: Some(true),
                ..Outcome::default()
            },
        });
        nodes.push(node);
    }
    let graph = Spec {
        version: provider_graph::VERSION.into(),
        calls: MultiCallPlanSpec {
            application_version: "a".repeat(64),
            callback_version: "native-discovery-v1".into(),
            item_count: 2,
            max_workers: 1,
            branches: nodes
                .iter()
                .map(|n| MultiCallBranch {
                    position: n.position,
                    call_site_version: n.key.call_site_version.clone(),
                    plan: n.plan.clone(),
                })
                .collect(),
            execution_contract: None,
            comparison_version: None,
        },
        dependencies: vec![],
        results: Results::CallbackItems { items: vec![0, 1] },
    };
    let versions = nodes
        .iter()
        .map(|n| MultiCallRuntimeBranch {
            position: n.position,
            runtime: n.runtime.clone(),
        })
        .collect::<Vec<_>>();
    let controls = nodes
        .iter()
        .map(|n| provider_graph::SourceContract {
            position: n.position,
            sha256: n.source_contract_sha256.clone(),
        })
        .collect::<Vec<_>>();
    s.command(json!({"operation":"seal_batch","batch_id":lease.batch_id,"logical_calls":nodes.iter().map(|n|n.position).collect::<Vec<_>>()}));
    provider_graph::publish(
        &mut s.conn,
        &s.profiles,
        PublishRequest {
            batch_id: &lease.batch_id,
            spec: &graph,
            versions: &versions,
            outcomes: &outcomes,
            requests: &requests,
            wall_latency_ms: 10.0,
            input_sha256: "e".repeat(64).as_str(),
            observed_at_us: lease.started_at_us + 1,
            comparison: Some(Comparison {
                version: provider_graph::COMPARISON_VERSION.into(),
                input_sha256: "e".repeat(64),
                output_sha256: "b".repeat(64),
            }),
            source_contracts: &controls,
            preparation_id: Some(&lease.lease_id),
        },
    )
    .unwrap()
}

fn refresh_sampling_fixture() -> (
    Store,
    agentc_optimizer::provider_graph_sampling::Begin,
    agentc_optimizer::Optimizer,
    agentc_optimizer::model_catalog::ModelCatalog,
    agentc_optimizer::dag::Call,
    agentc_optimizer::provider_graph_sampling::Finished,
) {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, _, catalog, source) = cold_sampling_fixture();
    request.policy.max_workflows = 10;
    request.policy.max_samples = 10;
    request.policy.refresh_interval_workflows = Some(2);
    request.policy.max_lifetime_physical_invocations = Some(24);
    let costs = std::sync::Arc::new(agentc_optimizer::CostModel::new());
    for _ in 0..20 {
        costs.observe(agentc_optimizer::cost_model::CostModelUpdate {
            call_site_id: source.call_site_id.clone(),
            input_tokens: 2,
            output_tokens: 1,
            latency_ms: 5.,
            cost_usd: 0.001,
            output_is_structured: false,
            output_is_short: true,
            now_us: Some(1),
        });
    }
    let opt = agentc_optimizer::Optimizer::new(
        costs,
        vec![
            Box::new(agentc_optimizer::ModelDowngradeRule::from_catalog(
                std::sync::Arc::new(catalog.clone()),
                std::sync::Arc::new(agentc_optimizer::Budget::new()),
            )),
            Box::new(agentc_optimizer::OutputBudgetRule::default()),
        ],
        agentc_optimizer::OptimizerConfig::default(),
    );
    let issued = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    finish_cold_sampling(&mut s, &issued, &opt, &catalog, &source, 100);
    let done = sampling::finish(
        &mut s.conn,
        &issued.ticket.ticket_id,
        6,
        Policy::default(),
        20,
    )
    .unwrap();
    assert_eq!(done.status, "paired");
    (s, request, opt, catalog, source, done)
}

fn finish_cold_sampling(
    s: &mut Store,
    issued: &agentc_optimizer::provider_graph_sampling::Outcome,
    opt: &agentc_optimizer::Optimizer,
    catalog: &agentc_optimizer::model_catalog::ModelCatalog,
    source: &agentc_optimizer::dag::Call,
    number: u64,
) {
    publish_cold_leg(
        s,
        issued.reference.as_ref().unwrap(),
        opt,
        catalog,
        source,
        true,
        number,
    );
    publish_cold_leg(
        s,
        issued.candidate.as_ref().unwrap(),
        opt,
        catalog,
        source,
        false,
        number + 10,
    );
}

fn next_sampling(request: &mut agentc_optimizer::provider_graph_sampling::Begin, number: u64) {
    request.reference_batch.batch_id = format!("{number:032x}");
    request.candidate_batch.batch_id = format!("{:032x}", number + 1);
}

#[test]
fn sampling_refresh_reuses_counters_and_retains_original_checkpoint_pairs() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, opt, catalog, source, first) = refresh_sampling_fixture();
    next_sampling(&mut request, 20);
    let warm = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 8).unwrap();
    assert!(warm.ticket.refresh.is_none());
    assert_eq!(warm.ticket.discovered_pair_id, first.pair_id);
    next_sampling(&mut request, 22);
    let fresh = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 10).unwrap();
    assert_eq!(fresh.ticket.ordinal, 3);
    assert_eq!(
        fresh.ticket.refresh.as_ref().unwrap().reason,
        sampling::RefreshReason::Interval
    );
    assert!(fresh.ticket.discovered_pair_id.is_none());
    assert!(fresh.reference.as_ref().unwrap().template.is_none());
    finish_cold_sampling(&mut s, &fresh, &opt, &catalog, &source, 200);
    let done = sampling::finish(
        &mut s.conn,
        &fresh.ticket.ticket_id,
        12,
        Policy::default(),
        20,
    )
    .unwrap();
    assert_eq!(done.status, "paired");
    assert_ne!(done.pair_id, first.pair_id);
    next_sampling(&mut request, 24);
    let reused = sampling::begin(&mut s.conn, request, Policy::default(), 20, 14).unwrap();
    assert_eq!(reused.ticket.ordinal, 4);
    assert_eq!(reused.ticket.discovered_pair_id, done.pair_id);
    assert_eq!(
        s.conn
            .query_row(
                "SELECT workflows,samples FROM provider_graph_sampling_state",
                [],
                |r| Ok((r.get::<_, u64>(0)?, r.get::<_, u64>(1)?))
            )
            .unwrap(),
        (4, 4)
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_sampling_template",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        2
    );
    comparison::load(&s.conn, first.pair_id.as_ref().unwrap()).unwrap();
    assert_eq!(
        sampling::finish(&mut s.conn, &first.ticket_id, 15, Policy::default(), 20).unwrap(),
        first
    );
}

#[test]
fn sampling_refresh_checkpoint_damage_holds_before_another_workflow() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    for damage in 0..3 {
        let (mut s, mut request, opt, catalog, source, _) = refresh_sampling_fixture();
        request.policy.refresh_interval_workflows = Some(2);
        next_sampling(&mut request, 20);
        sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 8).unwrap();
        next_sampling(&mut request, 22);
        let fresh =
            sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 10).unwrap();
        finish_cold_sampling(&mut s, &fresh, &opt, &catalog, &source, 200);
        sampling::finish(
            &mut s.conn,
            &fresh.ticket.ticket_id,
            12,
            Policy::default(),
            20,
        )
        .unwrap();
        match damage {
            0 => {
                s.conn
                    .execute(
                        "DELETE FROM provider_graph_sampling_template WHERE generation=1",
                        [],
                    )
                    .unwrap();
            }
            1 => {
                s.conn.execute("UPDATE provider_graph_sampling_template SET checkpoint_sha256='changed' WHERE generation=2",[]).unwrap();
            }
            _ => {
                s.conn.execute("UPDATE provider_graph_sampling_discovered SET pair_id=json_extract((SELECT checkpoint_json FROM provider_graph_sampling_template WHERE generation=1),'$.pair_id'),ticket_id=json_extract((SELECT checkpoint_json FROM provider_graph_sampling_template WHERE generation=1),'$.ticket_id')",[]).unwrap();
            }
        }
        next_sampling(&mut request, 24);
        assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 14).is_err());
        assert_eq!(
            s.conn
                .query_row("SELECT COUNT(*) FROM provider_graph_sampling", [], |r| r
                    .get::<_, u64>(0))
                .unwrap(),
            3
        );
    }
}

#[test]
fn sampling_refresh_late_completion_does_not_replace_a_newer_template() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, opt, catalog, source, first) = refresh_sampling_fixture();
    next_sampling(&mut request, 20);
    sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 8).unwrap();
    next_sampling(&mut request, 22);
    let a = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 10).unwrap();
    next_sampling(&mut request, 24);
    let b = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 11).unwrap();
    assert_eq!(
        a.ticket.refresh.as_ref().unwrap().source_pair_id,
        first.pair_id.unwrap()
    );
    assert_eq!(
        a.ticket.refresh.as_ref().unwrap().source_pair_id,
        b.ticket.refresh.as_ref().unwrap().source_pair_id
    );
    finish_cold_sampling(&mut s, &b, &opt, &catalog, &source, 200);
    let winner =
        sampling::finish(&mut s.conn, &b.ticket.ticket_id, 13, Policy::default(), 20).unwrap();
    finish_cold_sampling(&mut s, &a, &opt, &catalog, &source, 300);
    let late =
        sampling::finish(&mut s.conn, &a.ticket.ticket_id, 14, Policy::default(), 20).unwrap();
    assert_ne!(late.pair_id, winner.pair_id);
    next_sampling(&mut request, 26);
    let next = sampling::begin(&mut s.conn, request, Policy::default(), 20, 15).unwrap();
    assert_eq!(next.ticket.discovered_pair_id, winner.pair_id);
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_sampling_template",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        2
    );
}

#[test]
fn sampling_refresh_retired_execution_cohorts_are_relearned_without_resetting_limits() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    for window_change in [false, true] {
        let (mut s, mut request, _, _, _, first) = refresh_sampling_fixture();
        let p = comparison::load(&s.conn, first.pair_id.as_ref().unwrap()).unwrap();
        let candidate = provider_graph::load(&s.conn, &p.candidate_execution_id).unwrap();
        if window_change {
            s.profiles = PlanProfiles::with_window(21);
        }
        s.seed_at(
            candidate.spec,
            1000,
            InferenceCostSource::ProviderReported,
            "r2",
            ("e", "b"),
            (7, 8),
        );
        next_sampling(&mut request, 20);
        let fresh = sampling::begin(&mut s.conn, request, Policy::default(), 20, 9).unwrap();
        assert_eq!(fresh.ticket.ordinal, 2);
        assert_eq!(
            fresh.ticket.refresh.as_ref().unwrap().reason,
            sampling::RefreshReason::Retired
        );
        assert!(fresh.reference.as_ref().unwrap().template.is_none());
        assert!(fresh.ticket.sampled);
        comparison::load(&s.conn, &p.pair_id).unwrap();
    }
}

#[test]
fn sampling_refresh_damaged_execution_heads_cannot_authorize_rediscovery() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    for damage in 0..3 {
        let (mut s, mut request, _, _, _, _) = refresh_sampling_fixture();
        match damage {
            0 => {
                s.conn
                    .execute("DELETE FROM provider_graph_execution_state", [])
                    .unwrap();
            }
            1 => {
                s.conn
                    .execute(
                        "UPDATE provider_graph_execution_state SET window_size=21",
                        [],
                    )
                    .unwrap();
            }
            _ => {
                s.conn.execute("UPDATE provider_graph_execution_state SET runtime_json=json_set(runtime_json,'$.price_table_version','changed')",[]).unwrap();
            }
        }
        next_sampling(&mut request, 20);
        assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 8).is_err());
        assert_eq!(
            s.conn
                .query_row("SELECT COUNT(*) FROM provider_graph_sampling", [], |r| r
                    .get::<_, u64>(0))
                .unwrap(),
            1
        );
    }
}

#[test]
fn sampling_refresh_preserves_retired_family_cooldown_before_candidate_replay() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, _, _, _, first) = refresh_sampling_fixture();
    let p = comparison::load(&s.conn, first.pair_id.as_ref().unwrap()).unwrap();
    let r = provider_graph::load(&s.conn, &p.reference_execution_id).unwrap();
    let c = provider_graph::load(&s.conn, &p.candidate_execution_id).unwrap();
    let r = s.seed_at(
        r.spec,
        1000,
        InferenceCostSource::ProviderReported,
        "r2",
        ("e", "b"),
        (7, 8),
    );
    let c = s.seed_at(
        c.spec,
        1001,
        InferenceCostSource::ProviderReported,
        "r2",
        ("e", "d"),
        (9, 10),
    );
    let bad = s.pair(&r, &c, 11).unwrap();
    assert!(bad.quality.as_ref().unwrap().disabled.is_some());
    next_sampling(&mut request, 20);
    let fresh = sampling::begin(&mut s.conn, request, Policy::default(), 20, 12).unwrap();
    assert_eq!(
        fresh.ticket.refresh.as_ref().unwrap().reason,
        sampling::RefreshReason::Retired
    );
    assert!(!fresh.ticket.sampled);
    assert!(fresh.candidate.is_none());
    assert!(fresh.reference.as_ref().unwrap().template.is_none());
    assert_eq!(
        s.conn
            .query_row(
                "SELECT workflows,samples FROM provider_graph_sampling_state",
                [],
                |r| Ok((r.get::<_, u64>(0)?, r.get::<_, u64>(1)?))
            )
            .unwrap(),
        (2, 1)
    );
}

#[test]
fn sampling_refresh_without_an_alternative_keeps_original_checkpoint_and_charges() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, _, catalog, source, first) = refresh_sampling_fixture();
    next_sampling(&mut request, 20);
    sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 8).unwrap();
    next_sampling(&mut request, 22);
    let fresh = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 10).unwrap();
    let empty = agentc_optimizer::Optimizer::empty(
        std::sync::Arc::new(agentc_optimizer::CostModel::new()),
        agentc_optimizer::OptimizerConfig::default(),
    );
    finish_cold_sampling(&mut s, &fresh, &empty, &catalog, &source, 200);
    let done = sampling::finish(
        &mut s.conn,
        &fresh.ticket.ticket_id,
        12,
        Policy::default(),
        20,
    )
    .unwrap();
    assert_eq!(done.status, "no_alternative");
    assert_eq!(
        s.conn
            .query_row(
                "SELECT pair_id FROM provider_graph_sampling_discovered",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
        first.pair_id.unwrap()
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_sampling_invocation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        8
    );
    next_sampling(&mut request, 24);
    let retry = sampling::begin(&mut s.conn, request, Policy::default(), 20, 14).unwrap();
    assert_eq!(retry.ticket.ordinal, 4);
    assert!(retry.ticket.refresh.is_some());
}

#[test]
fn sampling_periodic_refresh_rejects_explicit_pairs_and_invalid_intervals() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    for invalid in [0, 11] {
        let (mut s, mut request, _, _, _) = cold_sampling_fixture();
        request.policy.refresh_interval_workflows = Some(invalid);
        assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).is_err());
    }
    let (mut s, mut request, _, _, _, first) = refresh_sampling_fixture();
    request.source_pair_id = first.pair_id;
    next_sampling(&mut request, 20);
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 8).is_err());
}

#[test]
fn sampling_refresh_deleted_checkpoint_holds_an_already_issued_sdk_dispatch() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut request, opt, catalog, source, _) = refresh_sampling_fixture();
    next_sampling(&mut request, 20);
    sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 8).unwrap();
    next_sampling(&mut request, 22);
    let fresh = sampling::begin(&mut s.conn, request, Policy::default(), 20, 10).unwrap();
    s.conn
        .execute("DELETE FROM provider_graph_sampling_template", [])
        .unwrap();
    assert!(sampling::plan_discovery(
        &mut s.conn,
        &opt,
        &catalog,
        &fresh.reference.as_ref().unwrap().lease_id,
        CallPosition { item: 0, call: 0 },
        &serde_json::to_string(&source).unwrap(),
        true
    )
    .is_err());
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        4
    );
}

#[test]
fn cold_sampling_without_an_alternative_preserves_limits_without_manufacturing_quality() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, request, opt, catalog, source) = cold_sampling_fixture();
    let issued = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    publish_cold_leg(
        &mut s,
        issued.reference.as_ref().unwrap(),
        &opt,
        &catalog,
        &source,
        true,
        100,
    );
    publish_cold_leg(
        &mut s,
        issued.candidate.as_ref().unwrap(),
        &opt,
        &catalog,
        &source,
        false,
        200,
    );
    let done = sampling::finish(
        &mut s.conn,
        &issued.ticket.ticket_id,
        6,
        Policy::default(),
        20,
    )
    .unwrap();
    assert_eq!(done.status, "no_alternative");
    assert!(done.pair_id.is_none());
    assert_eq!(
        done,
        sampling::finish(
            &mut s.conn,
            &issued.ticket.ticket_id,
            7,
            Policy::default(),
            20
        )
        .unwrap()
    );
    for table in [
        "provider_graph_pair",
        "provider_graph_quality_event",
        "provider_graph_sampling_discovered",
    ] {
        assert_eq!(
            s.conn
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                    .get::<_, u64>(0))
                .unwrap(),
            0
        );
    }
    let mut next = request;
    next.reference_batch.batch_id = format!("{:032x}", 12);
    next.candidate_batch.batch_id = format!("{:032x}", 13);
    let second = sampling::begin(&mut s.conn, next, Policy::default(), 20, 8).unwrap();
    assert_eq!(second.ticket.ordinal, 2);
    assert!(second.reference.as_ref().unwrap().template.is_none());
}

#[test]
fn cold_sampling_archival_reads_detect_deleted_native_instructions() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, request, opt, catalog, source) = cold_sampling_fixture();
    let issued = sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).unwrap();
    let e = publish_cold_leg(
        &mut s,
        issued.reference.as_ref().unwrap(),
        &opt,
        &catalog,
        &source,
        true,
        100,
    );
    assert!(provider_graph::load(&s.conn, &e.execution_id).is_ok());
    s.conn
        .execute(
            "DELETE FROM provider_graph_prepared_node WHERE lease_id=?1",
            [&issued.reference.as_ref().unwrap().lease_id],
        )
        .unwrap();
    assert!(provider_graph::load(&s.conn, &e.execution_id).is_err());
}

fn shared_campaign_policy() -> agentc_optimizer::provider_graph_campaign::Policy {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    campaign::Policy {
        campaign_id: "5".repeat(64),
        commitments: sampling::Commitments {
            money: sampling::CommitmentMoney::BilledUsd {
                workflow_usd: 0.05,
                lifetime_usd: 0.12,
            },
            max_open_workflows: 2,
            max_pending_invocations: 1,
        },
        max_workflows: 10,
        max_physical_invocations: 6,
        max_lifetime_physical_invocations: 24,
    }
}

#[test]
fn shared_campaign_sampling_binding_survives_a_different_explicit_pair_state() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    let (mut s, mut request, _, _, _) = cold_sampling_fixture();
    let pair = priced_primary_pairs(&mut s);
    let p = shared_campaign_policy();
    request.policy.campaign = Some(p.clone());
    sampling::begin(
        &mut s.conn,
        request.clone(),
        Policy::default(),
        20,
        pair.observed_at_us + 10,
    )
    .unwrap();
    next_sampling(&mut request, 40);
    request.source_pair_id = Some(pair.pair_id);
    for policy in [
        None,
        Some(campaign::Policy {
            campaign_id: "8".repeat(64),
            ..p.clone()
        }),
    ] {
        request.policy.campaign = policy;
        let error = sampling::begin(
            &mut s.conn,
            request.clone(),
            Policy::default(),
            20,
            pair.observed_at_us + 11,
        )
        .unwrap_err();
        assert!(
            error.to_string().contains("original campaign commitment"),
            "{error}"
        );
    }
    assert_eq!(
        campaign::report(&s.conn, &p.campaign_id).unwrap().workflows,
        1
    );
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_graph_sampling_state",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        1
    );
}

#[test]
fn shared_campaign_enrollment_blocks_dispatch_from_an_older_unbudgeted_lease() {
    use agentc_optimizer::provider_graph_sampling as sampling;
    let (mut s, mut learning, _, _, _) = cold_sampling_fixture();
    let pair = priced_primary_pairs(&mut s);
    let candidate = provider_graph::load(&s.conn, &pair.candidate_execution_id).unwrap();
    let lease = prepare_template(&mut s, &candidate, 8100, pair.observed_at_us + 10);
    preparation::approve_node(
        &mut s.conn,
        &lease.lease_id,
        &prepared_node(&candidate, 0),
        Policy::default(),
        20,
        pair.observed_at_us + 10,
    )
    .unwrap();
    let metadata: String = s
        .conn
        .query_row(
            "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
            [&lease.batch_id],
            |r| r.get(0),
        )
        .unwrap();
    let mut original: provider_receipts::Batch = serde_json::from_str(&metadata).unwrap();
    original.batch_id = format!("{:032x}", 8190);
    learning.reference_batch = original.clone();
    original.batch_id = format!("{:032x}", 8191);
    learning.candidate_batch = original;
    learning.policy.campaign = Some(shared_campaign_policy());
    sampling::begin(
        &mut s.conn,
        learning,
        Policy::default(),
        20,
        pair.observed_at_us + 11,
    )
    .unwrap();
    let call = prepared_invocation(&s, &candidate, 0, 8199);
    let result = provider_receipts::begin_call(&s.conn, &lease.batch_id, &call);
    assert!(
        result.is_err(),
        "older unbudgeted lease escaped its driver's campaign binding"
    );
    assert!(result
        .unwrap_err()
        .to_string()
        .contains("original campaign commitment"));
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                [&lease.batch_id],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    let batch = format!("{:032x}", 8290);
    let metadata: String = s
        .conn
        .query_row(
            "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
            [&lease.batch_id],
            |r| r.get(0),
        )
        .unwrap();
    let mut next: provider_receipts::Batch = serde_json::from_str(&metadata).unwrap();
    next.batch_id = batch.clone();
    provider_receipts::begin_batch(&s.conn, &next).unwrap();
    let error = preparation::begin(
        &mut s.conn,
        preparation::Begin {
            batch_id: batch,
            max_workers: 2,
            input_sha256: Some("e".repeat(64)),
            template_execution_id: Some(candidate.execution_id),
            comparison_pair_id: None,
            role_program: None,
        },
        Policy::default(),
        20,
        pair.observed_at_us + 12,
    )
    .unwrap_err();
    assert!(
        error.to_string().contains("original campaign commitment"),
        "{error}"
    );
    next.batch_id = format!("{:032x}", 8291);
    provider_receipts::begin_batch(&s.conn, &next).unwrap();
    let error = provider_receipts::begin_call(&s.conn, &next.batch_id, &call).unwrap_err();
    let stop: serde_json::Value = serde_json::from_str(
        &agentc_optimizer::provider_graph_campaign::binding_abstention(&error).unwrap(),
    )
    .unwrap();
    assert_eq!(stop["hard_stop"], "graph_campaign_binding");
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?1",
                [&next.batch_id],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn shared_campaign_reserves_independent_sampling_drivers_before_callbacks() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    let (mut s, mut request, _, _, _) = cold_sampling_fixture();
    let p = shared_campaign_policy();
    request.policy.campaign = Some(p.clone());
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    let retry = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 5).unwrap();
    assert_eq!(first.ticket.ticket_id, retry.ticket.ticket_id);
    next_sampling(&mut request, 20);
    request.reference_batch.callback_version = "independent-driver".into();
    request.candidate_batch.callback_version = "independent-driver".into();
    let second = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 6).unwrap();
    assert_ne!(first.ticket.driver_id, second.ticket.driver_id);
    let r = campaign::report(&s.conn, &p.campaign_id).unwrap();
    assert_eq!(
        (
            r.workflows,
            r.open_workflows,
            r.committed_physical_invocations
        ),
        (2, 2, 12)
    );
    assert!(r.billed_committed_upper_usd.unwrap() >= 0.1);
    next_sampling(&mut request, 22);
    request.reference_batch.callback_version = "third-driver".into();
    request.candidate_batch.callback_version = "third-driver".into();
    assert!(sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 7).is_err());
    request
        .policy
        .campaign
        .as_mut()
        .unwrap()
        .commitments
        .max_open_workflows = 3;
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 7).is_err());
    assert_eq!(
        campaign::report(&s.conn, &p.campaign_id).unwrap().workflows,
        2
    );
}

#[test]
fn shared_campaign_pending_call_limit_spans_independent_sampling_drivers() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    let p = shared_campaign_policy();
    request.policy.campaign = Some(p.clone());
    let first = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    next_sampling(&mut request, 20);
    request.reference_batch.callback_version = "other-driver".into();
    request.candidate_batch.callback_version = "other-driver".into();
    let second = sampling::begin(&mut s.conn, request, Policy::default(), 20, 5).unwrap();
    let mut calls = vec![];
    for (number, outcome) in [(100, &first), (200, &second)] {
        let lease = outcome.reference.as_ref().unwrap();
        let pos = CallPosition { item: 0, call: 0 };
        let input = serde_json::to_string(&source).unwrap();
        let raw = sampling::plan_discovery(
            &mut s.conn,
            &opt,
            &catalog,
            &lease.lease_id,
            pos,
            &input,
            true,
        )
        .unwrap();
        let node = agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &input, &raw).unwrap();
        calls.push((lease.batch_id.clone(), cold_invocation(&node, number)));
    }
    provider_receipts::begin_call(&s.conn, &calls[0].0, &calls[0].1).unwrap();
    provider_receipts::begin_call(&s.conn, &calls[0].0, &calls[0].1).unwrap();
    assert!(provider_receipts::begin_call(&s.conn, &calls[1].0, &calls[1].1).is_err());
    assert_eq!(
        campaign::report(&s.conn, &p.campaign_id)
            .unwrap()
            .pending_invocations,
        1
    );
    complete_primary_invocation(&s, &calls[0].0, &calls[0].1);
    provider_receipts::begin_call(&s.conn, &calls[1].0, &calls[1].1).unwrap();
    assert_eq!(
        campaign::report(&s.conn, &p.campaign_id)
            .unwrap()
            .physical_invocations,
        2
    );
}

#[test]
fn shared_campaign_releases_only_verified_completed_sampling_money_and_slots() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    let p = shared_campaign_policy();
    request.policy.campaign = Some(p.clone());
    let issued = sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).unwrap();
    finish_cold_sampling(&mut s, &issued, &opt, &catalog, &source, 100);
    let before = campaign::report(&s.conn, &p.campaign_id).unwrap();
    assert_eq!(before.held_workflows, 1);
    assert_eq!(before.committed_physical_invocations, 6);
    sampling::finish(
        &mut s.conn,
        &issued.ticket.ticket_id,
        6,
        Policy::default(),
        20,
    )
    .unwrap();
    let after = campaign::report(&s.conn, &p.campaign_id).unwrap();
    assert_eq!(
        (
            after.held_workflows,
            after.open_workflows,
            after.physical_invocations,
            after.committed_physical_invocations
        ),
        (0, 0, 4, 4)
    );
    assert!(
        after.billed_committed_upper_usd.unwrap() >= 0.004
            && after.billed_committed_upper_usd.unwrap() < 0.005
    );
}

#[test]
fn shared_campaign_deleted_enrollment_policy_or_slot_holds_more_work() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    for damage in 0..4 {
        let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
        let p = shared_campaign_policy();
        request.policy.campaign = Some(p.clone());
        let first =
            sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
        let pos = CallPosition { item: 0, call: 0 };
        let input = serde_json::to_string(&source).unwrap();
        let raw = sampling::plan_discovery(
            &mut s.conn,
            &opt,
            &catalog,
            &first.reference.as_ref().unwrap().lease_id,
            pos,
            &input,
            true,
        )
        .unwrap();
        let node = agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &input, &raw).unwrap();
        let call = cold_invocation(&node, 100);
        if damage == 3 {
            provider_receipts::begin_call(
                &s.conn,
                &first.reference.as_ref().unwrap().batch_id,
                &call,
            )
            .unwrap();
        }
        s.conn.execute_batch("PRAGMA foreign_keys=OFF").unwrap();
        match damage {
            0 => {
                s.conn
                    .execute("DELETE FROM provider_graph_campaign_workflow", [])
                    .unwrap();
            }
            1 => {
                s.conn
                    .execute("DELETE FROM provider_graph_campaign", [])
                    .unwrap();
            }
            2 => {
                s.conn
                    .execute(
                        "UPDATE provider_graph_campaign_workflow SET enrollment_sha256='changed'",
                        [],
                    )
                    .unwrap();
            }
            _ => {
                s.conn
                    .execute("DELETE FROM provider_graph_campaign_invocation", [])
                    .unwrap();
            }
        }
        assert!(campaign::report(&s.conn, &p.campaign_id).is_err());
        assert!(provider_receipts::begin_call(
            &s.conn,
            &first.reference.as_ref().unwrap().batch_id,
            &call
        )
        .is_err());
        next_sampling(&mut request, 20);
        assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 6).is_err());
        assert_eq!(
            s.conn
                .query_row("SELECT COUNT(*) FROM provider_graph_sampling", [], |r| r
                    .get::<_, u64>(0))
                .unwrap(),
            1
        );
    }
}

#[test]
fn shared_campaign_learning_and_independent_deployment_spend_one_allowance() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_deployment as deploy,
        provider_graph_sampling as sampling,
    };
    let (mut s, mut learning, opt, catalog, source) = cold_sampling_fixture();
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let mut p = shared_campaign_policy();
    p.commitments.money = sampling::CommitmentMoney::BilledUsd {
        workflow_usd: 0.05,
        lifetime_usd: 5.0,
    };
    p.commitments.max_open_workflows = 1;
    p.max_lifetime_physical_invocations = 1000;
    p.max_workflows = 1000;
    let mut request = primary_deployment_request(&s, &pair, 8000);
    request.campaign = Some(p.clone());
    learning.policy.campaign = Some(p.clone());
    let admitted = deploy::begin(&mut s.conn, request.clone(), now).unwrap();
    let lease = admitted
        .lease
        .unwrap_or_else(|| panic!("campaign admission held: {:?}", admitted.decision));
    let mut escaped = learning.clone();
    let metadata: String = s
        .conn
        .query_row(
            "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
            [&lease.batch_id],
            |r| r.get(0),
        )
        .unwrap();
    let mut original: provider_receipts::Batch = serde_json::from_str(&metadata).unwrap();
    original.batch_id = format!("{:032x}", 8990);
    escaped.reference_batch = original.clone();
    original.batch_id = format!("{:032x}", 8991);
    escaped.candidate_batch = original;
    escaped.policy.campaign = None;
    let error = sampling::begin(&mut s.conn, escaped, Policy::default(), 20, now + 1).unwrap_err();
    assert!(
        error.to_string().contains("original campaign commitment"),
        "{error}"
    );
    assert!(sampling::begin(
        &mut s.conn,
        learning.clone(),
        Policy::default(),
        20,
        now + 1
    )
    .is_err());
    run_primary_deployment(&s, &pair, &lease.batch_id, 8000);
    s.command(json!({"operation":"seal_batch","batch_id":lease.batch_id,"logical_calls":spec(false).calls.branches.iter().map(|b|b.position).collect::<Vec<_>>()}));
    s.command(json!({"operation":"finish_batch_economics","batch_id":lease.batch_id,"wall_latency_ms":20.}));
    assert_eq!(
        campaign::report(&s.conn, &p.campaign_id)
            .unwrap()
            .held_workflows,
        1
    );
    deploy::finish_primary(
        &mut s.conn,
        &s.profiles,
        &lease.lease_id,
        Comparison {
            version: provider_graph::COMPARISON_VERSION.into(),
            input_sha256: "e".repeat(64),
            output_sha256: "d".repeat(64),
        },
        now + 2,
    )
    .unwrap();
    let issued = sampling::begin(&mut s.conn, learning, Policy::default(), 20, now + 3).unwrap();
    finish_cold_sampling(&mut s, &issued, &opt, &catalog, &source, 9000);
    sampling::finish(
        &mut s.conn,
        &issued.ticket.ticket_id,
        now + 5,
        Policy::default(),
        20,
    )
    .unwrap();
    let r = campaign::report(&s.conn, &p.campaign_id).unwrap();
    assert_eq!(
        (
            r.workflows,
            r.held_workflows,
            r.physical_invocations,
            r.committed_physical_invocations
        ),
        (2, 0, 7, 7)
    );
    assert!(
        r.billed_committed_upper_usd.unwrap() >= 0.007
            && r.billed_committed_upper_usd.unwrap() < 0.008
    );
}

#[test]
fn shared_campaign_failed_and_sealed_pending_work_keeps_money_and_pending_holds() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    for pending in [false, true] {
        let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
        let p = shared_campaign_policy();
        request.policy.campaign = Some(p.clone());
        let issued = sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).unwrap();
        let reference = issued.reference.as_ref().unwrap();
        let pos = CallPosition { item: 0, call: 0 };
        let input = serde_json::to_string(&source).unwrap();
        let raw = sampling::plan_discovery(
            &mut s.conn,
            &opt,
            &catalog,
            &reference.lease_id,
            pos,
            &input,
            true,
        )
        .unwrap();
        let node = agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &input, &raw).unwrap();
        let call = cold_invocation(&node, 100);
        provider_receipts::begin_call(&s.conn, &reference.batch_id, &call).unwrap();
        if !pending {
            provider_receipts::finish_call(
                &s.conn,
                &reference.batch_id,
                &call.invocation_id,
                &provider_receipts::Completion {
                    status: provider_receipts::Status::Failed,
                    model_version: None,
                    input_tokens: None,
                    output_tokens: None,
                    cost_usd: None,
                    cost_source: InferenceCostSource::Estimated,
                    latency_ms: 1.,
                    http_attempts: Some(1),
                    exception_type: Some("fixture".into()),
                    billing_scope_sha256: None,
                    provider_generation_sha256: None,
                    provider_request_id_sha256: None,
                    billing_usage: None,
                },
            )
            .unwrap();
        }
        for lease in [
            issued.reference.as_ref().unwrap(),
            issued.candidate.as_ref().unwrap(),
        ] {
            s.command(json!({"operation":"seal_batch","batch_id":lease.batch_id,"logical_calls":if lease.batch_id==reference.batch_id {vec![pos]} else {vec![]}}));
            s.command(json!({"operation":"finish_batch_economics","batch_id":lease.batch_id,"wall_latency_ms":1.}));
        }
        let done = sampling::finish(
            &mut s.conn,
            &issued.ticket.ticket_id,
            6,
            Policy::default(),
            20,
        )
        .unwrap();
        assert_eq!(done.status, "incomplete");
        let r = campaign::report(&s.conn, &p.campaign_id).unwrap();
        assert_eq!(
            (
                r.held_workflows,
                r.unknown_cost_workflows,
                r.pending_invocations
            ),
            (1, 1, u64::from(pending))
        );
        assert!(r.billed_committed_upper_usd.unwrap() >= 0.05);
        assert_eq!(r.physical_invocations, 1);
    }
}

#[test]
fn shared_campaign_known_overrun_stays_readable_and_blocks_more_dispatches() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    let mut p = shared_campaign_policy();
    p.commitments.money = sampling::CommitmentMoney::BilledUsd {
        workflow_usd: 0.0005,
        lifetime_usd: 0.1,
    };
    request.policy.campaign = Some(p.clone());
    let issued = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    let reference = issued.reference.as_ref().unwrap();
    let input = serde_json::to_string(&source).unwrap();
    let mut calls = vec![];
    for item in 0..2 {
        let pos = CallPosition { item, call: 0 };
        let raw = sampling::plan_discovery(
            &mut s.conn,
            &opt,
            &catalog,
            &reference.lease_id,
            pos,
            &input,
            true,
        )
        .unwrap();
        let node = agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &input, &raw).unwrap();
        calls.push(cold_invocation(&node, 100 + u64::from(item)));
    }
    provider_receipts::begin_call(&s.conn, &reference.batch_id, &calls[0]).unwrap();
    complete_primary_invocation(&s, &reference.batch_id, &calls[0]);
    let r = campaign::report(&s.conn, &p.campaign_id).unwrap();
    assert!(r.budget_exceeded);
    assert!(r.billed_committed_upper_usd.unwrap() >= 0.001);
    assert!(provider_receipts::begin_call(&s.conn, &reference.batch_id, &calls[1]).is_err());
    next_sampling(&mut request, 20);
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 6).is_err());
    assert_eq!(
        campaign::report(&s.conn, &p.campaign_id).unwrap().workflows,
        1
    );
}

#[test]
fn shared_campaign_lifetime_physical_cap_cannot_be_replenished_by_a_different_driver() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    let mut p = shared_campaign_policy();
    p.max_lifetime_physical_invocations = 8;
    request.policy.campaign = Some(p.clone());
    let issued = sampling::begin(&mut s.conn, request.clone(), Policy::default(), 20, 4).unwrap();
    finish_cold_sampling(&mut s, &issued, &opt, &catalog, &source, 100);
    sampling::finish(
        &mut s.conn,
        &issued.ticket.ticket_id,
        6,
        Policy::default(),
        20,
    )
    .unwrap();
    next_sampling(&mut request, 20);
    request.reference_batch.callback_version = "other".into();
    request.candidate_batch.callback_version = "other".into();
    assert!(sampling::begin(&mut s.conn, request, Policy::default(), 20, 8).is_err());
    assert_eq!(
        campaign::report(&s.conn, &p.campaign_id)
            .unwrap()
            .committed_physical_invocations,
        4
    );
}

#[test]
fn shared_campaign_tariff_requires_original_snapshot_before_sdk_dispatch() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    let (mut s, mut request, opt, catalog, source) = cold_sampling_fixture();
    let mut p = shared_campaign_policy();
    p.commitments.money = sampling::CommitmentMoney::PublishedTokenTariffUsd {
        snapshot_sha256: agentc_optimizer::provider_tariff::snapshot_sha256(),
        workflow_nanodollars: 50000,
        lifetime_nanodollars: 120000,
    };
    request.policy.campaign = Some(p.clone());
    let issued = sampling::begin(&mut s.conn, request, Policy::default(), 20, 4).unwrap();
    let lease = issued.reference.as_ref().unwrap();
    let pos = CallPosition { item: 0, call: 0 };
    let input = serde_json::to_string(&source).unwrap();
    let raw = sampling::plan_discovery(
        &mut s.conn,
        &opt,
        &catalog,
        &lease.lease_id,
        pos,
        &input,
        true,
    )
    .unwrap();
    let node = agentc_optimizer::ffi::prepared_graph_node(&catalog, pos, &input, &raw).unwrap();
    assert!(
        provider_receipts::begin_call(&s.conn, &lease.batch_id, &cold_invocation(&node, 100))
            .is_err()
    );
    assert_eq!(
        campaign::report(&s.conn, &p.campaign_id)
            .unwrap()
            .physical_invocations,
        0
    );
}

#[test]
fn shared_campaign_two_writer_connections_cannot_reserve_the_same_remaining_money() {
    use agentc_optimizer::{
        provider_graph_campaign as campaign, provider_graph_sampling as sampling,
    };
    let (_, mut request, _, _, _) = cold_sampling_fixture();
    let mut p = shared_campaign_policy();
    p.commitments.money = sampling::CommitmentMoney::BilledUsd {
        workflow_usd: 1.,
        lifetime_usd: 1.,
    };
    request.policy.campaign = Some(p.clone());
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("campaign.db");
    let conn = Connection::open(&path).unwrap();
    agentc_optimizer::schema::ensure_cost_model_schema(&conn).unwrap();
    sampling::ensure_schema(&conn).unwrap();
    campaign::ensure_schema(&conn).unwrap();
    drop(conn);
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let threads: Vec<_> = (0..2)
        .map(|n| {
            let path = path.clone();
            let barrier = barrier.clone();
            let mut request = request.clone();
            next_sampling(&mut request, 10 + n * 2);
            request.reference_batch.callback_version = format!("driver-{n}");
            request.candidate_batch.callback_version = format!("driver-{n}");
            std::thread::spawn(move || {
                let mut conn = Connection::open(path).unwrap();
                conn.busy_timeout(std::time::Duration::from_secs(10))
                    .unwrap();
                barrier.wait();
                sampling::begin(&mut conn, request, Policy::default(), 20, 4)
            })
        })
        .collect();
    let results: Vec<_> = threads.into_iter().map(|t| t.join().unwrap()).collect();
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    let conn = Connection::open(path).unwrap();
    let r = campaign::report(&conn, &p.campaign_id).unwrap();
    assert_eq!(r.workflows, 1);
    assert_eq!(r.billed_committed_upper_usd, Some(1.));
}

#[test]
fn shared_campaign_cannot_amortize_learning_over_unfunded_future_workflows() {
    use agentc_optimizer::{
        provider_graph_deployment as deploy, provider_graph_sampling as sampling,
    };
    let mut s = Store::new(20);
    let pair = priced_primary_pairs(&mut s);
    let now = pair.observed_at_us + 10;
    let mut p = shared_campaign_policy();
    p.commitments.money = sampling::CommitmentMoney::BilledUsd {
        workflow_usd: 0.05,
        lifetime_usd: 0.075,
    };
    let mut request = primary_deployment_request(&s, &pair, 8000);
    request.campaign = Some(p);
    let outcome = deploy::begin(&mut s.conn, request, now).unwrap();
    assert!(outcome.decision.selected_reference && outcome.lease.is_none());
    assert_eq!(outcome.decision.request.remaining_batches, 1);
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_deployment", [], |r| {
                r.get::<_, u64>(0)
            })
            .unwrap(),
        0
    );
}
