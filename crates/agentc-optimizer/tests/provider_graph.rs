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
use agentc_optimizer::provider_graph::{self, Dependency, PublishRequest, Results, Spec};
use agentc_optimizer::{provider_receipts, Outcome};
use rusqlite::Connection;
use serde_json::json;

fn position(item: u32, call: u32) -> CallPosition {
    CallPosition { item, call }
}

fn spec() -> Spec {
    let branches = (0..2)
        .flat_map(|item| {
            (0..2).map(move |call| MultiCallBranch {
                position: position(item, call),
                call_site_version: CallSiteVersion::parse(format!("{:064x}", item * 2 + call + 1))
                    .unwrap(),
                plan: ExecutionPlanSpec {
                    schema_version: 1,
                    provider_protocol: "openai.chat.completions.v1".into(),
                    requested_model_id: "strong".into(),
                    target_model_id: "cheap".into(),
                    target_model_revision: Some("cheap-r1".into()),
                    rewrite_ordering: RewriteOrdering::Ordered,
                    rewrites: vec![
                        RewriteApplication {
                            stable_name: "PromptDedup".into(),
                            implementation_version: "1".into(),
                            parameters: json!({}),
                        },
                        RewriteApplication {
                            stable_name: "OutputBudget".into(),
                            implementation_version: "2".into(),
                            parameters: json!({"max_output_tokens":16}),
                        },
                    ],
                    cache_policy: CachePolicy {
                        stable_name: "provider-call".into(),
                        implementation_version: "1".into(),
                        parameters: json!({}),
                    },
                    output_budget: Some(16),
                    validation_policy: ValidationPolicy {
                        stable_name: "sampled-reference".into(),
                        implementation_version: "1".into(),
                        parameters: json!({"divergence_threshold":0.0}),
                    },
                },
            })
        })
        .collect();
    Spec {
        version: provider_graph::VERSION.into(),
        calls: MultiCallPlanSpec {
            application_version: "a".repeat(64),
            callback_version: "graph-fixture-v1".into(),
            item_count: 2,
            max_workers: 2,
            branches,
            comparison_version: None,
            execution_contract: None,
        },
        dependencies: vec![
            Dependency {
                producer: position(0, 0),
                consumer: position(0, 1),
            },
            Dependency {
                producer: position(1, 0),
                consumer: position(1, 1),
            },
        ],
        results: Results::Nodes {
            positions: vec![position(0, 1), position(1, 1)],
        },
    }
}

struct Fixture {
    conn: Connection,
    profiles: PlanProfiles,
    spec: Spec,
    versions: Vec<MultiCallRuntimeBranch>,
    outcomes: Vec<MultiCallOutcome>,
    requests: BTreeMap<CallPosition, String>,
    batch: String,
}

impl Fixture {
    fn new() -> Self {
        let conn = Connection::open_in_memory().unwrap();
        agentc_optimizer::schema::ensure_cost_model_schema(&conn).unwrap();
        let mut f = Self {
            conn,
            profiles: PlanProfiles::with_window(20),
            spec: spec(),
            versions: vec![],
            outcomes: vec![],
            requests: BTreeMap::new(),
            batch: "b".repeat(32),
        };
        f.command(json!({"operation":"begin_batch","batch":{"version":provider_receipts::VERSION,"batch_id":f.batch,"application_version":f.spec.calls.application_version,"callback_version":f.spec.calls.callback_version,"item_count":2}}));
        for (index, branch) in f.spec.calls.branches.clone().iter().enumerate() {
            let id = format!("{index:032x}");
            let key = PlanProfileKey {
                call_site_version: branch.call_site_version.clone(),
                execution_plan_id: branch.plan.execution_plan_id().unwrap(),
            };
            let runtime = PlanRuntimeVersion {
                provider_protocol: branch.plan.provider_protocol.clone(),
                target_model_id: "cheap".into(),
                target_model_version: "cheap-r1".into(),
                price_table_version: "prices-v1".into(),
            };
            let request = format!("{:064x}", index + 10);
            f.command(json!({"operation":"begin_call","batch_id":f.batch,"invocation":{
                "invocation_id":id,"position":branch.position,"ordinal":0,"role":"primary","provider_protocol":branch.plan.provider_protocol,
                "requested_model_id":"cheap","request_sha256":request,"logical_request_sha256":request,"transport_sha256":"f".repeat(64),
                "logical_plan_key":key,"logical_runtime":runtime,"dispatch_plan_key":key,"dispatch_runtime":runtime,"parent_plan_key":null,"transport_verified":true
            }}));
            f.command(json!({"operation":"finish_call","batch_id":f.batch,"invocation_id":id,"completion":{
                "status":"completed","model_version":"cheap-r1","input_tokens":2,"output_tokens":1,"cost_usd":0.01,"cost_source":"provider_reported","latency_ms":5.0,"http_attempts":1,"exception_type":null
            }}));
            f.requests.insert(branch.position, request);
            f.versions.push(MultiCallRuntimeBranch {
                position: branch.position,
                runtime,
            });
            f.outcomes.push(MultiCallOutcome {
                position: branch.position,
                cost_source: InferenceCostSource::ProviderReported,
                outcome: Outcome {
                    cost_source: InferenceCostSource::ProviderReported,
                    physical_invocations: vec![
                        agentc_optimizer::cost_evidence::PhysicalInvocation {
                            batch_id: f.batch.clone(),
                            invocation_id: id,
                        },
                    ],
                    input_tokens: 2,
                    output_tokens: 1,
                    latency_ms: 5.0,
                    cost_usd: 0.01,
                    inference_cost_complete: Some(true),
                    ..Default::default()
                },
            });
        }
        f.command(json!({"operation":"seal_batch","batch_id":f.batch,"logical_calls":f.spec.calls.branches.iter().map(|b|b.position).collect::<Vec<_>>()}));
        f
    }
    fn command(&mut self, command: serde_json::Value) {
        provider_receipts::handle_json(&mut self.conn, &command.to_string()).unwrap();
    }
    fn publish(&mut self, now: i64) -> anyhow::Result<provider_graph::Execution> {
        provider_graph::publish(
            &mut self.conn,
            &self.profiles,
            PublishRequest {
                batch_id: &self.batch,
                spec: &self.spec,
                versions: &self.versions,
                outcomes: &self.outcomes,
                requests: &self.requests,
                wall_latency_ms: 20.0,
                input_sha256: &"d".repeat(64),
                observed_at_us: now,
                comparison: None,
                source_contracts: &[],
                preparation_id: None,
            },
        )
    }
}

#[test]
fn primary_graph_cost_projection_preserves_original_execution_without_minting_profiles() {
    use agentc_optimizer::provider_graph_economics::{self as finance, Kind};
    let mut f = Fixture::new();
    let e = f.publish(1).unwrap();
    let costs = finance::execution(&f.conn, Kind::Primary, &e.execution_id).unwrap();
    assert_eq!(costs.costs.physical_invocations, 4);
    let usd = costs.costs.billed_usd.unwrap();
    assert!(usd.lower_usd <= 0.04 && usd.upper_usd >= 0.04);
    assert!(costs.costs.published_token_tariff.is_none());
    let window = finance::window(&f.conn, Kind::Primary, &e.key, &e.runtime, 20)
        .unwrap()
        .unwrap();
    assert_eq!(window.observations[0].execution_id, e.execution_id);
    assert_eq!(window.lifetime_executions, 1);
    assert!(
        finance::window(&f.conn, Kind::Roles, &e.key, &e.runtime, 20)
            .unwrap()
            .is_none()
    );
    // Legacy primary publications without acquisition starts retain costs but
    // cannot manufacture a foreground resource projection.
    assert!(window.foreground_latency_mean_ms.is_none());
    let raw = provider_receipts::handle_json(&mut f.conn, &json!({"operation":"graph_cost_window","kind":"primary","key":e.key,"runtime":e.runtime,"window_size":20}).to_string()).unwrap();
    let response: serde_json::Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(response["admission"], "financial_projection_only");
    assert_eq!(response["window"]["lifetime_executions"], 1);
}

#[test]
fn graph_identity_canonicalizes_node_edge_order_and_separates_result_model_rewrite_and_dependencies(
) {
    let graph = spec();
    let key = graph.profile_key().unwrap();
    let mut reordered = graph.clone();
    reordered.calls.branches.reverse();
    reordered.dependencies.reverse();
    assert_eq!(key, reordered.profile_key().unwrap());
    assert_ne!(
        key.execution_plan_id,
        graph.calls.profile_key().unwrap().execution_plan_id
    );
    let mut scheduling = graph.clone();
    scheduling.calls.max_workers = 1;
    assert_eq!(
        key.call_site_version,
        scheduling.profile_key().unwrap().call_site_version
    );
    assert_ne!(
        key.execution_plan_id,
        scheduling.profile_key().unwrap().execution_plan_id
    );
    for mutation in 0..7 {
        let mut changed = graph.clone();
        match mutation {
            0 => changed.calls.branches[0].plan.target_model_id = "another-model".into(),
            1 => changed.calls.branches[1].plan.rewrites.reverse(),
            2 => changed.calls.branches[2].plan.rewrites[0].implementation_version = "new".into(),
            3 => changed.calls.branches[3].plan.output_budget = Some(8),
            4 => changed.dependencies.push(Dependency {
                producer: position(0, 0),
                consumer: position(1, 1),
            }),
            5 => {
                if let Results::Nodes { positions } = &mut changed.results {
                    positions.reverse();
                }
            }
            _ => changed.results = Results::CallbackItems { items: vec![0, 1] },
        }
        assert_ne!(
            key.execution_plan_id,
            changed.profile_key().unwrap().execution_plan_id,
            "{mutation}"
        );
    }
}

#[test]
fn invalid_dependency_and_result_contracts_cannot_create_graph_identity() {
    for case in 0..7 {
        let mut graph = spec();
        match case {
            0 => graph.dependencies.push(graph.dependencies[0]),
            1 => graph.dependencies.push(Dependency {
                producer: position(0, 0),
                consumer: position(0, 0),
            }),
            2 => graph.dependencies.push(Dependency {
                producer: position(9, 0),
                consumer: position(0, 1),
            }),
            3 => graph.dependencies.push(Dependency {
                producer: position(0, 1),
                consumer: position(0, 0),
            }),
            4 => graph.dependencies.clear(),
            5 => {
                graph.results = Results::Nodes {
                    positions: vec![position(9, 0)],
                }
            }
            _ => {
                graph.calls.execution_contract =
                    Some(agentc_optimizer::multi_call::PREPARED_PROVIDER_BATCH_VERSION.into())
            }
        }
        assert!(graph.profile_key().is_err(), "{case}");
    }
}

#[test]
fn graph_runtime_keeps_every_model_revision_and_price_and_checks_complete_coverage() {
    let f = Fixture::new();
    let original = f.spec.runtime(&f.versions).unwrap();
    for index in 0..f.versions.len() {
        let mut versions = f.versions.clone();
        versions[index].runtime.price_table_version = "new-price".into();
        assert_ne!(original, f.spec.runtime(&versions).unwrap());
        versions[index].runtime.target_model_id = "wrong-model".into();
        assert!(f.spec.runtime(&versions).is_err());
    }
    assert!(f.spec.runtime(&f.versions[1..]).is_err());
}

#[test]
fn complete_graph_publication_is_atomic_idempotent_and_restart_safe_without_paired_quality() {
    let mut f = Fixture::new();
    provider_graph::ensure_schema(&f.conn).unwrap();
    f.conn.execute_batch("CREATE TRIGGER graph_write_fault BEFORE INSERT ON provider_graph_execution BEGIN SELECT RAISE(ABORT,'graph fault'); END;").unwrap();
    assert!(f.publish(100).is_err());
    assert!(f.profiles.is_empty());
    assert_eq!(
        f.conn
            .query_row("SELECT COUNT(*) FROM execution_plan_profile", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        0
    );
    f.conn
        .execute_batch("DROP TRIGGER graph_write_fault;")
        .unwrap();
    let first = f.publish(100).unwrap();
    assert_eq!(first.totals.call_count, 4);
    assert_eq!(first.totals.cost_usd, 0.04);
    assert_eq!(f.publish(200).unwrap(), first);
    let profile = f.profiles.get(&first.key, &first.runtime).unwrap();
    assert_eq!(profile.n_observations, 1);
    assert_eq!(profile.paired_observations, 0);
    assert!(profile.divergence_upper_p95.is_none());
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("graph.db");
    f.conn
        .execute("VACUUM INTO ?1", [path.to_str().unwrap()])
        .unwrap();
    f.conn = Connection::open(path).unwrap();
    f.profiles = PlanProfiles::with_window(20);
    f.profiles.warm_from_db(&f.conn).unwrap();
    assert_eq!(f.publish(300).unwrap(), first);
    assert_eq!(
        f.profiles
            .get(&first.key, &first.runtime)
            .unwrap()
            .n_observations,
        1
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
}

#[test]
fn missing_or_tampered_physical_graph_evidence_never_publishes_a_profile() {
    for case in 0..7 {
        let mut f = Fixture::new();
        match case {
            0 => f.outcomes[0].outcome.physical_invocations.clear(),
            1 => f.outcomes[1].outcome.cost_usd = 0.0,
            2 => {
                f.requests.insert(position(0, 0), "e".repeat(64));
            }
            3 => {
                f.conn.execute("UPDATE provider_receipt_invocation SET invocation_json=replace(invocation_json,'cheap','strong') WHERE ordinal=0",[]).unwrap();
            }
            4 => {
                f.conn.execute("UPDATE provider_receipt_invocation SET completion_json=replace(completion_json,'provider_reported','estimated')",[]).unwrap();
            }
            5 => f.outcomes[0].outcome.latency_ms = 0.0,
            _ => {
                f.conn.execute("UPDATE provider_receipt_invocation SET invocation_json=replace(invocation_json,'prices-v1','wrong-price')",[]).unwrap();
            }
        }
        assert!(f.publish(100).is_err(), "{case}");
        assert!(f.profiles.is_empty());
    }
}

#[test]
fn unpriced_graph_custody_is_atomic_and_survives_retry_without_billed_profile() {
    let mut f = Fixture::new();
    for outcome in &mut f.outcomes {
        outcome.cost_source = InferenceCostSource::Estimated;
        outcome.outcome.cost_source = InferenceCostSource::Estimated;
    }
    f.conn.execute("UPDATE provider_receipt_invocation SET completion_json=replace(completion_json,'provider_reported','estimated')",[]).unwrap();
    provider_graph::ensure_schema(&f.conn).unwrap();
    f.conn.execute_batch("CREATE TRIGGER pending_graph_fault BEFORE INSERT ON provider_graph_execution BEGIN SELECT RAISE(ABORT,'pending graph fault'); END;").unwrap();
    assert!(f.publish(100).is_err());
    assert!(f.profiles.is_empty());
    assert_eq!(
        f.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_execution", [], |r| r
                .get::<_, u64>(
                0
            ))
            .unwrap(),
        0
    );
    f.conn
        .execute_batch("DROP TRIGGER pending_graph_fault;")
        .unwrap();
    let saved = f.publish(100).unwrap();
    assert_eq!(
        saved.cost_source,
        agentc_optimizer::cost_evidence::CostSource::Estimated
    );
    assert_eq!(f.publish(200).unwrap(), saved);
    assert!(f.profiles.is_empty());
    assert_eq!(
        f.conn
            .query_row("SELECT COUNT(*) FROM execution_plan_profile", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        0
    );
}
