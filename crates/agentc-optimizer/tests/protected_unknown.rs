//! No-network interface proof for the opt-in UNKNOWN protected experiment.
//! All costs/outcomes below are synthetic test values, never provider evidence.

use std::sync::Arc;

use agentc_optimizer::{
    ffi::{self, PASS_THROUGH_JSON},
    model_catalog::{
        default_model_catalog, ModelCatalog, RequestRequirements, PROTECTED_ATTEMPT_KEY,
        ROUTE_CONTEXT_KEY,
    },
    rules::{ContextCompressRule, ModelDowngradeRule, StateDropRule},
    schema::ensure_cost_model_schema,
    Budget, Call, CostModel, CostModelUpdate, DepSource, ExplorationController, ExplorationPolicy,
    Message, Optimizer, OptimizerConfig, Outcome, Parameters, Plan, PlanGuard, PlanProfileKey,
    PlanProfileUpdate, PlanProfiles, PlanRuntimeVersion,
};
use rusqlite::Connection;
use serde_json::{json, Value};

fn fixture(compose: bool, warm: bool) -> (Arc<ModelCatalog>, Arc<CostModel>, Optimizer, Call) {
    let catalog = Arc::new(default_model_catalog().unwrap());
    let cm = Arc::new(CostModel::new());
    let budget = Arc::new(Budget::new());
    let opt = Optimizer::with_budget(
        cm.clone(),
        vec![
            Box::new(StateDropRule::default()),
            Box::new(ModelDowngradeRule::from_catalog(
                catalog.clone(),
                budget.clone(),
            )),
        ],
        OptimizerConfig {
            compose,
            ..OptimizerConfig::default()
        },
        budget,
    );
    let call = Call {
        call_site_id: "protected-unknown-test".into(),
        trace_id: [0; 16],
        span_id: [0; 8],
        model: "gpt-4o-2024-11-20".into(),
        messages: vec![
            Message {
                role: "system".into(),
                content: "fixed schema instruction".into(),
            },
            Message {
                role: "user".into(),
                content: "discard this private stale state".into(),
            },
            Message {
                role: "assistant".into(),
                content: "current draft".into(),
            },
            Message {
                role: "user".into(),
                content: "private question".into(),
            },
        ],
        parameters: Parameters {
            max_output_tokens: Some(512),
            extra: json!({
                "response_format": {"type":"json_schema", "json_schema":{"name":"answer","strict":true,"schema":{"type":"object","properties":{"result":{"type":["string","null"]}},"required":["result"],"additionalProperties":false}}},
                "message_deps":[{"kind":"literal"},{"kind":"state","key":"old"},{"kind":"literal"},{"kind":"literal"}],
                "window_state_reads":[],
                "agentc_route_context":{
                    "provider_protocol":"openai.chat.completions.v1", "provider_namespace":"openai",
                    "image_input":false,"tool_calling":false,"structured_outputs":true,"streaming":false,
                    "protected_attempt":{
                        "policy":"opaque-provider-attempt-v1", "mode":"protected-source-first-v1",
                        "catalog_version":catalog.catalog_version,
                        "endpoint_cohort_sha256":"a".repeat(64),
                        "application_contract_sha256":"b".repeat(64),
                        "rule_contract_sha256":"d".repeat(64), "max_request_json_bytes":65536
                    }
                }
            }),
            ..Parameters::default()
        },
        tools: vec![],
        input_deps: vec![DepSource::Literal; 4],
        occurrence_ix: 0,
    };
    if warm {
        warm_site(&cm, &call);
    }
    (catalog, cm, opt, call)
}

fn warm_site(cm: &CostModel, call: &Call) {
    for n in 0..3 {
        cm.observe(CostModelUpdate {
            call_site_id: call.call_site_id.clone(),
            input_tokens: 100,
            output_tokens: 40,
            latency_ms: 100.0,
            cost_usd: 0.01,
            output_is_structured: true,
            output_is_short: true,
            now_us: Some(n),
        });
    }
}

fn primary(
    opt: &Optimizer,
    catalog: &ModelCatalog,
    profiles: &PlanProfiles,
    call: &Call,
) -> String {
    ffi::optimize_profiled_plan(
        opt,
        Some(catalog),
        profiles,
        &PlanGuard::new(),
        &serde_json::to_string(call).unwrap(),
        2_000_000,
    )
}

fn lease(
    opt: &Optimizer,
    catalog: &ModelCatalog,
    profiles: &PlanProfiles,
    controller: &ExplorationController,
    conn: &mut Connection,
    call: &Call,
) -> Value {
    let reference = primary(opt, catalog, profiles, call);
    serde_json::from_str(&ffi::reserve_profiled_exploration(
        opt,
        Some(catalog),
        profiles,
        &PlanGuard::new(),
        controller,
        conn,
        &serde_json::to_string(call).unwrap(),
        &reference,
        2_000_000,
    ))
    .unwrap()
}

fn db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    ensure_cost_model_schema(&conn).unwrap();
    conn
}

fn reference_only(encoded: &str) -> Value {
    let value: Value = serde_json::from_str(encoded).unwrap();
    assert_eq!(value["kind"], "pass_through");
    assert!(value.get("call").is_none());
    value
}

#[test]
fn unknown_default_stays_unprofiled_and_catalog_stays_proven_only() {
    let (catalog, _, opt, mut call) = fixture(true, true);
    assert!(RequestRequirements::from_call(&call).is_none());
    assert!(catalog.compatible_targets(&call).is_empty());
    assert_eq!(opt.candidate_plans(&call, &catalog).len(), 1);
    call.parameters.extra[ROUTE_CONTEXT_KEY]
        .as_object_mut()
        .unwrap()
        .remove(PROTECTED_ATTEMPT_KEY);
    assert_eq!(
        primary(&opt, &catalog, &PlanProfiles::new(), &call),
        PASS_THROUGH_JSON
    );
    assert!(catalog.compatible_targets(&call).is_empty());
    call.parameters.extra[ROUTE_CONTEXT_KEY]["input_tokens_upper_bound"] = json!(100);
    assert!(RequestRequirements::from_call(&call).is_some());
    assert!(!catalog.compatible_targets(&call).is_empty());
    assert!(
        reference_only(&primary(&opt, &catalog, &PlanProfiles::new(), &call))
            .get("agentc_observation_context")
            .is_some()
    );
}

#[test]
fn protected_leases_real_native_solo_route_and_joint_without_numeric_capacity() {
    let (catalog, _, opt, call) = fixture(true, true);
    let profiles = PlanProfiles::new();
    let controller = ExplorationController::new();
    let mut conn = db();
    let mut witnessed = std::collections::HashSet::new();
    for _ in 0..3 {
        let envelope = lease(&opt, &catalog, &profiles, &controller, &mut conn, &call);
        assert_eq!(envelope["kind"], "pass_through");
        assert!(envelope.get("call").is_none());
        let context = &envelope["agentc_exploration_context"];
        let candidate: Plan = serde_json::from_value(context["candidate_plan"].clone()).unwrap();
        let selected = match &candidate {
            Plan::Rewritten { call, .. } | Plan::Composed { call, .. } => call,
            _ => panic!("protected single call"),
        };
        assert_eq!(selected.parameters.max_output_tokens, Some(512));
        assert_eq!(
            selected.parameters.extra["response_format"],
            call.parameters.extra["response_format"]
        );
        assert!(selected.parameters.extra[ROUTE_CONTEXT_KEY]
            .get("input_tokens_upper_bound")
            .is_none());
        assert!(selected.parameters.extra[ROUTE_CONTEXT_KEY]
            .get("input_tokens_upper_bound_basis")
            .is_none());
        if candidate.rule_names().contains(&"StateDrop".into()) {
            assert_eq!(
                serde_json::to_value(&selected.messages).unwrap(),
                json!([call.messages[0], call.messages[2], call.messages[3]])
            );
        } else {
            assert_eq!(
                serde_json::to_value(&selected.messages).unwrap(),
                serde_json::to_value(&call.messages).unwrap()
            );
        }
        witnessed.insert((
            candidate.rule_names().contains(&"StateDrop".into()),
            selected.model != call.model,
        ));
        let token = context["lease_token"].as_str().unwrap();
        assert!(!token.contains("private"));
        let outcome = Outcome {
            call_site_id: Some(call.call_site_id.clone()),
            executed_model_id: Some(selected.model.clone()),
            input_tokens: 80,
            output_tokens: 12,
            cost_usd: 0.001,
            latency_ms: 50.0,
            ..Outcome::default()
        };
        let observed = ffi::complete_profiled_exploration(
            &profiles,
            &controller,
            &mut conn,
            token,
            &serde_json::to_string(&outcome).unwrap(),
            0.0,
            2_000_001,
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            profiles
                .get_for_reporting(observed.plan_observation.as_ref().unwrap().key())
                .unwrap()
                .paired_observations,
            1
        );
        assert!(ffi::complete_profiled_exploration(
            &profiles,
            &controller,
            &mut conn,
            token,
            &serde_json::to_string(&outcome).unwrap(),
            0.0,
            2_000_001
        )
        .unwrap()
        .is_none());
    }
    assert_eq!(
        witnessed,
        [(true, false), (false, true), (true, true)]
            .into_iter()
            .collect()
    );
}

#[test]
fn all_primary_entry_points_return_reference_even_with_favorable_warm_profiles_and_restart() {
    for compose in [true, false] {
        let (catalog, _, opt, call) = fixture(compose, true);
        let profiles = PlanProfiles::new();
        let mut conn = db();
        let reference = primary(&opt, &catalog, &profiles, &call);
        let reference_value = reference_only(&reference);
        assert!(reference_value.get("agentc_observation_context").is_some());
        // Use an actually issued native ModelDowngrade candidate, including
        // its f32-promoted .03 threshold, not a lookalike .05 identity.
        let (_, _, issuing_opt, _) = fixture(true, true);
        let controller = ExplorationController::new();
        let mut issued = None;
        for _ in 0..3 {
            let envelope = lease(
                &issuing_opt,
                &catalog,
                &PlanProfiles::new(),
                &controller,
                &mut conn,
                &call,
            );
            let context = &envelope["agentc_exploration_context"];
            let candidate: Plan =
                serde_json::from_value(context["candidate_plan"].clone()).unwrap();
            let token = context["lease_token"].as_str().unwrap();
            ffi::fail_profiled_exploration(&controller, &mut conn, token, 2_000_001).unwrap();
            if candidate.rule_names() == vec!["ModelDowngrade".to_string()] {
                issued = Some((
                    candidate,
                    context["candidate_plan"]["agentc_observation_context"].clone(),
                ));
            }
        }
        let (candidate, native_context) = issued.expect("real native ModelDowngrade candidate");
        let threshold = opt.divergence_threshold_for_plan(&candidate);
        assert_eq!(threshold.to_bits(), f64::from(0.03_f32).to_bits());
        let candidate_json = serde_json::to_string(&candidate).unwrap();
        let attached: Value = serde_json::from_str(&ffi::attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&call).unwrap(),
            &candidate_json,
            threshold,
        ))
        .unwrap();
        assert_eq!(attached["agentc_observation_context"], native_context);
        for (value, cost) in [(&reference_value, 0.01), (&attached, 0.001)] {
            let context = &value["agentc_observation_context"];
            let key: PlanProfileKey = serde_json::from_value(context["key"].clone()).unwrap();
            let runtime: PlanRuntimeVersion =
                serde_json::from_value(context["runtime_version"].clone()).unwrap();
            for n in 0..25 {
                profiles
                    .observe(PlanProfileUpdate {
                        key: key.clone(),
                        runtime_version: runtime.clone(),
                        input_tokens: 100,
                        output_tokens: 12,
                        latency_ms: 50.0,
                        cost_usd: cost,
                        output_is_structured: true,
                        output_is_short: true,
                        divergence: Some(0.0),
                        dispatch_fallback: false,
                        observed_at_us: Some(1_000_000 + n),
                    })
                    .unwrap();
            }
        }
        profiles.flush_dirty(&mut conn).unwrap();
        let restarted = PlanProfiles::new();
        restarted.warm_from_db(&conn).unwrap();
        for current in [&profiles, &restarted] {
            let key: PlanProfileKey =
                serde_json::from_value(native_context["key"].clone()).unwrap();
            let runtime: PlanRuntimeVersion =
                serde_json::from_value(native_context["runtime_version"].clone()).unwrap();
            assert_eq!(current.get(&key, &runtime).unwrap().paired_observations, 25);
            reference_only(&primary(&opt, &catalog, current, &call));
            assert!(matches!(opt.plan(&call), Plan::PassThrough));
            reference_only(&ffi::optimize_plan(
                &opt,
                &serde_json::to_string(&call).unwrap(),
            ));
            assert_eq!(opt.candidate_plans(&call, &catalog).len(), 1);
            reference_only(&ffi::guard_and_attach_observation_context(
                Some(&catalog),
                &PlanGuard::new(),
                &serde_json::to_string(&call).unwrap(),
                &candidate_json,
                threshold,
                2_000_000,
            ));
        }
    }
}

#[test]
fn malformed_opt_ins_and_numeric_mixtures_never_fall_into_default_primary_or_exploration() {
    let (catalog, _, opt, call) = fixture(true, true);
    let mutations = [
        ("mode", json!("candidate-first")),
        ("policy", json!("future-unknown")),
        ("endpoint_cohort_sha256", json!("bad")),
        ("application_contract_sha256", json!(null)),
        ("catalog_version", json!("changed")),
        ("max_request_json_bytes", json!(65536.0)),
        ("max_request_json_bytes", json!(0)),
        ("max_request_json_bytes", json!(65537)),
        ("max_request_json_bytes", json!(10)),
        ("primary_allowed", json!(true)),
    ];
    for (field, value) in mutations {
        let mut changed = call.clone();
        changed.parameters.extra[ROUTE_CONTEXT_KEY][PROTECTED_ATTEMPT_KEY][field] = value;
        let profiles = PlanProfiles::new();
        let mut conn = db();
        assert_eq!(
            primary(&opt, &catalog, &profiles, &changed),
            PASS_THROUGH_JSON
        );
        assert!(lease(
            &opt,
            &catalog,
            &profiles,
            &ExplorationController::new(),
            &mut conn,
            &changed
        )
        .get("agentc_exploration_context")
        .is_none());
        assert!(matches!(opt.plan(&changed), Plan::PassThrough));
    }
    for field in ["input_tokens_upper_bound", "input_tokens_upper_bound_basis"] {
        let mut changed = call.clone();
        changed.parameters.extra[ROUTE_CONTEXT_KEY][field] = json!(64000);
        assert_eq!(
            primary(&opt, &catalog, &PlanProfiles::new(), &changed),
            PASS_THROUGH_JSON
        );
        assert!(catalog.compatible_targets(&changed).is_empty());
    }
}

#[test]
fn policy_schema_and_source_cohorts_do_not_pool_but_prompt_values_do() {
    let (catalog, _, opt, call) = fixture(true, true);
    let profiles = PlanProfiles::new();
    let key = reference_only(&primary(&opt, &catalog, &profiles, &call))
        ["agentc_observation_context"]["key"]
        .clone();
    let mut text = call.clone();
    text.messages[3].content = "another private question".into();
    assert_eq!(
        reference_only(&primary(&opt, &catalog, &profiles, &text))["agentc_observation_context"]
            ["key"],
        key
    );
    for field in [
        "endpoint_cohort_sha256",
        "application_contract_sha256",
        "rule_contract_sha256",
    ] {
        let mut changed = call.clone();
        changed.parameters.extra[ROUTE_CONTEXT_KEY][PROTECTED_ATTEMPT_KEY][field] =
            json!("c".repeat(64));
        let changed_key = reference_only(&primary(&opt, &catalog, &profiles, &changed))
            ["agentc_observation_context"]["key"]
            .clone();
        assert_ne!(changed_key["call_site_version"], key["call_site_version"]);
        assert_ne!(changed_key["execution_plan_id"], key["execution_plan_id"]);
    }
    let mut changed = call.clone();
    changed.parameters.extra["response_format"]["json_schema"]["name"] = json!("other");
    assert_ne!(
        reference_only(&primary(&opt, &catalog, &profiles, &changed))["agentc_observation_context"]
            ["key"],
        key
    );
    let mut changed_catalog = (*catalog).clone();
    changed_catalog
        .targets
        .iter_mut()
        .find(|t| t.model_id == call.model)
        .unwrap()
        .model_version = "new-observed-source".into();
    assert_ne!(
        reference_only(&primary(&opt, &changed_catalog, &profiles, &call))
            ["agentc_observation_context"]["key"],
        key
    );
}

#[test]
fn capabilities_hotness_and_failure_call_cap_remain_fail_closed() {
    let (catalog, cm, opt, call) = fixture(true, false);
    let profiles = PlanProfiles::new();
    let mut conn = db();
    assert!(lease(
        &opt,
        &catalog,
        &profiles,
        &ExplorationController::new(),
        &mut conn,
        &call
    )
    .get("agentc_exploration_context")
    .is_none());
    warm_site(&cm, &call);
    for field in ["image_input", "streaming", "tool_calling"] {
        let mut changed = call.clone();
        changed.parameters.extra[ROUTE_CONTEXT_KEY][field] = json!(true);
        assert_eq!(
            primary(&opt, &catalog, &profiles, &changed),
            PASS_THROUGH_JSON
        );
    }
    let mut too_large = call.clone();
    too_large.parameters.max_output_tokens = Some(u32::MAX);
    assert_eq!(
        primary(&opt, &catalog, &profiles, &too_large),
        PASS_THROUGH_JSON
    );
    let controller = ExplorationController::with_policy(ExplorationPolicy {
        max_calls_per_site: 1,
        ..ExplorationPolicy::default()
    })
    .unwrap();
    let envelope = lease(&opt, &catalog, &profiles, &controller, &mut conn, &call);
    let token = envelope["agentc_exploration_context"]["lease_token"]
        .as_str()
        .unwrap();
    let parsed: Value = serde_json::from_str(token).unwrap();
    let key: PlanProfileKey = serde_json::from_value(parsed["lease"]["key"].clone()).unwrap();
    ffi::fail_profiled_exploration(&controller, &mut conn, token, 2_000_001).unwrap();
    assert!(profiles.get_for_reporting(&key).is_none());
    assert!(
        lease(&opt, &catalog, &profiles, &controller, &mut conn, &call)
            .get("agentc_exploration_context")
            .is_none()
    );
    let snapshot = controller
        .snapshot(&conn, &key.call_site_version, 2_000_002)
        .unwrap();
    assert_eq!(snapshot.failed_calls, 1);
    assert_eq!(snapshot.calls_in_window, 1);
}

#[test]
fn inconsistent_schema_missing_caps_and_unsupported_parameters_abstain() {
    let (catalog, _, opt, call) = fixture(true, true);
    let mut rejected = Vec::new();
    for cap in [None, Some(0)] {
        let mut changed = call.clone();
        changed.parameters.max_output_tokens = cap;
        rejected.push(changed);
    }
    let mut changed = call.clone();
    changed.parameters.extra[ROUTE_CONTEXT_KEY]["structured_outputs"] = json!(false);
    rejected.push(changed); // bd-a1cq: actual schema still present.
    let mut changed = call.clone();
    changed
        .parameters
        .extra
        .as_object_mut()
        .unwrap()
        .remove("response_format");
    rejected.push(changed);
    let mut changed = call.clone();
    changed.parameters.extra["response_format"] = json!(null);
    rejected.push(changed);
    for field in ["seed", "reasoning_effort", "agentc_native_messages_opaque"] {
        let mut changed = call.clone();
        changed.parameters.extra[field] = json!(true);
        rejected.push(changed);
    }
    for field in ["provider_protocol", "provider_namespace"] {
        let mut changed = call.clone();
        changed.parameters.extra[ROUTE_CONTEXT_KEY][field] = json!("unknown");
        rejected.push(changed);
    }
    for changed in rejected {
        let profiles = PlanProfiles::new();
        let mut conn = db();
        assert_eq!(
            primary(&opt, &catalog, &profiles, &changed),
            PASS_THROUGH_JSON
        );
        assert!(lease(
            &opt,
            &catalog,
            &profiles,
            &ExplorationController::new(),
            &mut conn,
            &changed
        )
        .get("agentc_exploration_context")
        .is_none());
    }
    // No schema plus a false capability declaration is a valid plain-text request.
    let mut plain = call.clone();
    plain
        .parameters
        .extra
        .as_object_mut()
        .unwrap()
        .remove("response_format");
    plain.parameters.extra[ROUTE_CONTEXT_KEY]["structured_outputs"] = json!(false);
    assert!(
        reference_only(&primary(&opt, &catalog, &PlanProfiles::new(), &plain))
            .get("agentc_observation_context")
            .is_some()
    );
}

#[test]
fn source_and_target_catalog_facts_are_checked_independently() {
    let (catalog, cm, _, call) = fixture(true, true);
    let budget = Arc::new(Budget::new());
    let opt = Optimizer::with_budget(
        cm,
        vec![Box::new(ModelDowngradeRule::from_catalog(
            catalog.clone(),
            budget.clone(),
        ))],
        OptimizerConfig::default(),
        budget,
    );
    for source in [true, false] {
        for mutation in 0..6 {
            let mut changed = (*catalog).clone();
            let target = changed
                .targets
                .iter_mut()
                .find(|target| {
                    target.model_id
                        == if source {
                            call.model.as_str()
                        } else {
                            "gpt-4o-mini-2024-07-18"
                        }
                })
                .unwrap();
            match mutation {
                0 => target.capabilities.text_input = false,
                1 => target.capabilities.structured_outputs = false,
                2 => target.max_output_tokens = 511,
                3 => target.adapter_protocol = "other-protocol".into(),
                4 => target.provider_namespace = "other-namespace".into(),
                _ => target.routing_group = "other-group".into(),
            }
            let profiles = PlanProfiles::new();
            let mut conn = db();
            let result = lease(
                &opt,
                &changed,
                &profiles,
                &ExplorationController::new(),
                &mut conn,
                &call,
            );
            assert!(
                result.get("agentc_exploration_context").is_none(),
                "source={source}, mutation={mutation}"
            );
        }
    }
}

#[test]
fn native_context_compress_route_preserves_exact_deletion_and_schema() {
    let (catalog, cm, _, mut call) = fixture(true, true);
    call.messages[1].content = "dead native CC bytes ".repeat(450);
    call.messages.insert(
        2,
        Message {
            role: "user".into(),
            content: "more dead context".into(),
        },
    );
    call.parameters.extra["message_deps"] = json!([{"kind":"literal"},{"kind":"literal"},{"kind":"literal"},{"kind":"literal"},{"kind":"literal"}]);
    call.input_deps = vec![DepSource::Literal; 5];
    // Even with no overlap signal, the current assistant/observation exchange
    // survives materialization and joint routing; the prefix document can go.
    call.parameters.extra["attention_scores"] = json!([1.0, 0.0, 0.0, 0.0, 0.0]);
    let budget = Arc::new(Budget::new());
    let opt = Optimizer::with_budget(
        cm,
        vec![
            Box::new(ContextCompressRule::default()),
            Box::new(ModelDowngradeRule::from_catalog(
                catalog.clone(),
                budget.clone(),
            )),
        ],
        OptimizerConfig::default(),
        budget,
    );
    let profiles = PlanProfiles::new();
    let controller = ExplorationController::new();
    let mut conn = db();
    let mut routed_cc = false;
    for _ in 0..3 {
        let envelope = lease(&opt, &catalog, &profiles, &controller, &mut conn, &call);
        let context = &envelope["agentc_exploration_context"];
        let candidate: Plan = serde_json::from_value(context["candidate_plan"].clone()).unwrap();
        if candidate.rule_names().contains(&"ContextCompress".into()) {
            let selected = match &candidate {
                Plan::Rewritten { call, .. } | Plan::Composed { call, .. } => call,
                _ => panic!("single call"),
            };
            assert_eq!(
                serde_json::to_value(&selected.messages).unwrap(),
                // Even an explicitly Literal first user message is retained
                // conservatively; only the later dead context is removed.
                json!([call.messages[0], call.messages[1], call.messages[3], call.messages[4]])
            );
            assert_eq!(
                selected.parameters.extra["response_format"],
                call.parameters.extra["response_format"]
            );
            assert_eq!(selected.parameters.max_output_tokens, Some(512));
            assert!(selected.parameters.extra[ROUTE_CONTEXT_KEY]
                .get("input_tokens_upper_bound")
                .is_none());
            routed_cc |= selected.model != call.model;
        }
        ffi::fail_profiled_exploration(
            &controller,
            &mut conn,
            context["lease_token"].as_str().unwrap(),
            2_000_001,
        )
        .unwrap();
    }
    assert!(routed_cc);
}

#[test]
fn protected_concurrency_forged_reference_and_disabled_guard_stay_closed() {
    let (catalog, cm, _, call) = fixture(true, true);
    let budget = Arc::new(Budget::new());
    let opt = Optimizer::with_budget(
        cm,
        vec![Box::new(ModelDowngradeRule::from_catalog(
            catalog.clone(),
            budget.clone(),
        ))],
        OptimizerConfig::default(),
        budget,
    );
    let profiles = PlanProfiles::new();
    let controller = ExplorationController::new();
    let mut conn = db();
    let reference = primary(&opt, &catalog, &profiles, &call);
    let mut forged: Value = serde_json::from_str(&reference).unwrap();
    forged["agentc_observation_context"]["call_site_id"] = json!("foreign-site");
    let forged = serde_json::to_string(&forged).unwrap();
    assert_eq!(
        ffi::reserve_profiled_exploration(
            &opt,
            Some(&catalog),
            &profiles,
            &PlanGuard::new(),
            &controller,
            &mut conn,
            &serde_json::to_string(&call).unwrap(),
            &forged,
            2_000_000
        ),
        forged
    );
    let envelope = lease(&opt, &catalog, &profiles, &controller, &mut conn, &call);
    assert!(
        lease(&opt, &catalog, &profiles, &controller, &mut conn, &call)
            .get("agentc_exploration_context")
            .is_none()
    );
    let context = &envelope["agentc_exploration_context"];
    let candidate: Plan = serde_json::from_value(context["candidate_plan"].clone()).unwrap();
    let outcome = Outcome {
        call_site_id: Some(call.call_site_id.clone()),
        executed_model_id: Some("gpt-4o-mini-2024-07-18".into()),
        input_tokens: 100,
        output_tokens: 10,
        cost_usd: 0.001,
        latency_ms: 1.0,
        ..Outcome::default()
    };
    let observed = ffi::complete_profiled_exploration(
        &profiles,
        &controller,
        &mut conn,
        context["lease_token"].as_str().unwrap(),
        &serde_json::to_string(&outcome).unwrap(),
        1.0,
        2_000_001,
    )
    .unwrap()
    .unwrap();
    let guard = PlanGuard::new();
    guard
        .record_sample(
            observed.plan_observation.as_ref().unwrap(),
            1.0,
            opt.divergence_threshold_for_plan(&candidate),
            Some(2_000_001),
        )
        .unwrap();
    let next = ffi::reserve_profiled_exploration(
        &opt,
        Some(&catalog),
        &profiles,
        &guard,
        &controller,
        &mut conn,
        &serde_json::to_string(&call).unwrap(),
        &reference,
        2_000_002,
    );
    // One .97 excess is below the unchanged 1.0 exposure budget; two exceed it.
    let next: Value = serde_json::from_str(&next).unwrap();
    let observed = ffi::complete_profiled_exploration(
        &profiles,
        &controller,
        &mut conn,
        next["agentc_exploration_context"]["lease_token"]
            .as_str()
            .unwrap(),
        &serde_json::to_string(&outcome).unwrap(),
        1.0,
        2_000_003,
    )
    .unwrap()
    .unwrap();
    guard
        .record_sample(
            observed.plan_observation.as_ref().unwrap(),
            1.0,
            opt.divergence_threshold_for_plan(&candidate),
            Some(2_000_003),
        )
        .unwrap();
    let blocked = ffi::reserve_profiled_exploration(
        &opt,
        Some(&catalog),
        &profiles,
        &guard,
        &controller,
        &mut conn,
        &serde_json::to_string(&call).unwrap(),
        &reference,
        2_000_004,
    );
    assert!(serde_json::from_str::<Value>(&blocked)
        .unwrap()
        .get("agentc_exploration_context")
        .is_none());
}
