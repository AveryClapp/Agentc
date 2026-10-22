use agentc_optimizer::execution_plan::{
    CachePolicy, ExecutionPlanSpec, RewriteOrdering, ValidationPolicy,
};
use agentc_optimizer::multi_call::{
    aggregate_outcomes, observe_multi_call, profile_runtime, CallPosition, InferenceCostSource,
    MultiCallBranch, MultiCallOutcome, MultiCallPlanSpec, MultiCallRuntimeBranch,
    MULTI_CALL_RUNTIME_VERSION,
};
use agentc_optimizer::plan_profile::{CallSiteVersion, PlanProfiles, PlanRuntimeVersion};
use agentc_optimizer::schema::ensure_cost_model_schema;
use agentc_optimizer::Outcome;
use rusqlite::Connection;

fn fixture() -> (
    MultiCallPlanSpec,
    Vec<MultiCallRuntimeBranch>,
    Vec<MultiCallOutcome>,
) {
    let mut branches = Vec::new();
    let mut versions = Vec::new();
    let mut outcomes = Vec::new();
    for item in 0..2 {
        let position = CallPosition { item, call: 0 };
        let protocol = format!("protocol-{item}");
        let model = format!("model-{item}");
        branches.push(MultiCallBranch {
            position,
            call_site_version: CallSiteVersion::parse(format!("{item:x}").repeat(64)).unwrap(),
            plan: ExecutionPlanSpec {
                schema_version: 1,
                provider_protocol: protocol.clone(),
                requested_model_id: model.clone(),
                target_model_id: model.clone(),
                target_model_revision: Some("declared-v1".into()),
                rewrite_ordering: RewriteOrdering::Ordered,
                rewrites: vec![],
                cache_policy: CachePolicy {
                    stable_name: "provider-call".into(),
                    implementation_version: "1".into(),
                    parameters: serde_json::json!({}),
                },
                output_budget: Some(128),
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
                provider_protocol: protocol,
                target_model_id: model,
                target_model_version: format!("provider-revision-{item}"),
                price_table_version: format!("prices-{item}"),
            },
        });
        outcomes.push(MultiCallOutcome {
            position,
            cost_source: InferenceCostSource::ProviderReported,
            outcome: Outcome {
                cost_source: Default::default(),
                physical_invocations: Vec::new(),
                input_tokens: 10 + item,
                output_tokens: 20 + item,
                latency_ms: 60.0 + item as f64,
                cost_usd: 0.01 + item as f64 * 0.01,
                inference_cost_complete: Some(true),
                ..Outcome::default()
            },
        });
    }
    (
        MultiCallPlanSpec {
            application_version: "release-v1".into(),
            callback_version: "callback-v1".into(),
            item_count: 2,
            max_workers: 2,
            branches,
            comparison_version: None,
            execution_contract: None,
        },
        versions,
        outcomes,
    )
}

#[test]
fn completion_order_does_not_change_identity_runtime_or_metrics() {
    let (spec, versions, outcomes) = fixture();
    let mut reordered = spec.clone();
    reordered.branches.reverse();
    let mut reordered_versions = versions.clone();
    reordered_versions.reverse();
    let mut reordered_outcomes = outcomes.clone();
    reordered_outcomes.reverse();
    assert_eq!(
        spec.profile_key().unwrap(),
        reordered.profile_key().unwrap()
    );
    assert_eq!(
        profile_runtime(&spec, &versions).unwrap(),
        profile_runtime(&reordered, &reordered_versions).unwrap()
    );
    assert_eq!(
        aggregate_outcomes(&spec, &outcomes, 90.0).unwrap(),
        aggregate_outcomes(&reordered, &reordered_outcomes, 90.0).unwrap()
    );
}

#[test]
fn schedule_and_every_branch_affect_plan_identity_but_prices_do_not() {
    let (spec, versions, _) = fixture();
    let key = spec.profile_key().unwrap();
    let mut serial = spec.clone();
    serial.max_workers = 1;
    assert_ne!(
        key.execution_plan_id,
        serial.profile_key().unwrap().execution_plan_id
    );
    assert_eq!(
        key.call_site_version,
        serial.profile_key().unwrap().call_site_version
    );
    for item in 0..2 {
        let mut changed = spec.clone();
        changed.branches[item]
            .plan
            .target_model_id
            .push_str("-changed");
        assert_ne!(
            key.execution_plan_id,
            changed.profile_key().unwrap().execution_plan_id
        );
    }
    let runtime = profile_runtime(&spec, &versions).unwrap();
    let mut repriced = versions.clone();
    repriced[1].runtime.price_table_version = "new-second-branch-price".into();
    assert_ne!(runtime, profile_runtime(&spec, &repriced).unwrap());
    assert_eq!(key, spec.profile_key().unwrap());
}

#[test]
fn callback_release_and_leaf_cohorts_do_not_pool_evidence() {
    let (spec, _, _) = fixture();
    let key = spec.profile_key().unwrap();
    let mut changed = spec.clone();
    changed.application_version = "release-v2".into();
    assert_ne!(
        key.call_site_version,
        changed.profile_key().unwrap().call_site_version
    );
    changed = spec.clone();
    changed.callback_version = "callback-v2".into();
    assert_ne!(key, changed.profile_key().unwrap());
    changed = spec.clone();
    changed.branches[1].call_site_version = CallSiteVersion::parse("f".repeat(64)).unwrap();
    assert_ne!(
        key.call_site_version,
        changed.profile_key().unwrap().call_site_version
    );
}

#[test]
fn aggregate_sums_physical_work_and_uses_driver_wall_time() {
    let (spec, _, outcomes) = fixture();
    let totals = aggregate_outcomes(&spec, &outcomes, 90.0).unwrap();
    assert_eq!(totals.input_tokens, 21);
    assert_eq!(totals.output_tokens, 41);
    assert!((totals.cost_usd - 0.03).abs() < 1e-12);
    assert_eq!(totals.summed_call_latency_ms, 121.0);
    assert_eq!(totals.wall_latency_ms, 90.0);
    assert_eq!(totals.call_count, 2);
    assert!(aggregate_outcomes(&spec, &outcomes, 50.0).is_err());
}

#[test]
fn multiple_calls_per_item_are_supported_with_no_missing_or_duplicate_slots() {
    let (mut spec, _, _) = fixture();
    let mut second = spec.branches[0].clone();
    second.position.call = 1;
    spec.branches.push(second);
    assert!(spec.profile_key().is_ok());
    spec.branches.last_mut().unwrap().position.call = 2;
    assert!(spec.profile_key().is_err());
    spec.branches.last_mut().unwrap().position.call = 0;
    assert!(spec.profile_key().is_err());
    spec.branches.pop();
    spec.branches.remove(0);
    assert!(spec.profile_key().is_err());
}

#[test]
fn incomplete_estimated_failed_and_duplicate_outcomes_do_not_mutate_profiles() {
    let (spec, versions, outcomes) = fixture();
    let profiles = PlanProfiles::new();
    let mut invalid = vec![outcomes[..1].to_vec()];
    let mut duplicate = outcomes.clone();
    duplicate[1].position = duplicate[0].position;
    invalid.push(duplicate);
    for source in [
        InferenceCostSource::Estimated,
        InferenceCostSource::Unknown,
        InferenceCostSource::NoInference,
    ] {
        let mut changed = outcomes.clone();
        changed[1].cost_source = source;
        invalid.push(changed);
    }
    let mut changed = outcomes.clone();
    changed[1].outcome.inference_cost_complete = Some(false);
    invalid.push(changed);
    let mut changed = outcomes.clone();
    changed[1].outcome.dispatch_fallback = true;
    invalid.push(changed);
    let mut changed = outcomes.clone();
    changed[1].outcome.additional_inference = Some(Default::default());
    invalid.push(changed);
    let mut changed = outcomes.clone();
    changed[1].outcome.input_tokens = u32::MAX;
    invalid.push(changed);
    for outcomes in invalid {
        assert!(
            observe_multi_call(&profiles, &spec, &versions, &outcomes, 90.0, Some(10)).is_err()
        );
        assert!(profiles.is_empty());
    }
}

#[test]
fn runtime_vector_covers_every_position_and_protocol_model_price_revision() {
    let (spec, versions, _) = fixture();
    assert!(profile_runtime(&spec, &versions[..1]).is_err());
    let mut changed = versions.clone();
    changed[1].position = changed[0].position;
    assert!(profile_runtime(&spec, &changed).is_err());
    for field in [
        "provider_protocol",
        "target_model_id",
        "target_model_version",
        "price_table_version",
    ] {
        let mut value = serde_json::to_value(&versions).unwrap();
        value[1]["runtime"][field] = serde_json::json!("");
        let changed: Vec<MultiCallRuntimeBranch> = serde_json::from_value(value).unwrap();
        assert!(profile_runtime(&spec, &changed).is_err());
    }
}

#[test]
fn vector_persists_and_second_branch_drift_resets_window_without_creating_pairs() {
    let (spec, versions, outcomes) = fixture();
    let profiles = PlanProfiles::with_window(2);
    let mut conn = Connection::open_in_memory().unwrap();
    ensure_cost_model_schema(&conn).unwrap();
    for time in 1..=3 {
        observe_multi_call(&profiles, &spec, &versions, &outcomes, 90.0, Some(time)).unwrap();
    }
    let key = spec.profile_key().unwrap();
    let runtime = profile_runtime(&spec, &versions).unwrap();
    assert_eq!(runtime.provider_protocol, MULTI_CALL_RUNTIME_VERSION);
    let decoded: Vec<MultiCallRuntimeBranch> =
        serde_json::from_str(&runtime.target_model_version).unwrap();
    assert_eq!(decoded, versions);
    profiles.flush_dirty(&mut conn).unwrap();
    let restored = PlanProfiles::with_window(2);
    assert_eq!(restored.warm_from_db(&conn).unwrap(), 1);
    let profile = restored.get(&key, &runtime).unwrap();
    assert_eq!(profile.window_observations, 2);
    assert_eq!(profile.n_observations, 3);
    assert_eq!(profile.paired_observations, 0);
    assert_eq!(profile.divergence_upper_p95, None);
    let mut drifted = versions.clone();
    drifted[1].runtime.target_model_version = "provider-revision-second-v2".into();
    observe_multi_call(&restored, &spec, &drifted, &outcomes, 91.0, Some(4)).unwrap();
    assert!(restored.get(&key, &runtime).is_none());
    let profile = restored
        .get(&key, &profile_runtime(&spec, &drifted).unwrap())
        .unwrap();
    assert_eq!(profile.n_observations, 4);
    assert_eq!(profile.window_observations, 1);
    assert_eq!(profile.paired_observations, 0);
    assert_eq!(profile.latency_ms.mean, 91.0);
    restored.flush_dirty(&mut conn).unwrap();
    let reloaded = PlanProfiles::with_window(2);
    reloaded.warm_from_db(&conn).unwrap();
    assert_eq!(
        reloaded
            .get(&key, &profile_runtime(&spec, &drifted).unwrap())
            .unwrap()
            .window_observations,
        1
    );
}
