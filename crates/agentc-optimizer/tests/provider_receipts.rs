use agentc_optimizer::multi_call::{CallPosition, InferenceCostSource};
use agentc_optimizer::plan_profile::{CallSiteVersion, PlanProfileKey, PlanRuntimeVersion};
use agentc_optimizer::provider_receipts::{
    handle_json, Batch, Completion, Invocation, Role, Status, VERSION,
};
use agentc_optimizer::ExecutionPlanId;
use rusqlite::Connection;
use serde_json::{json, Value};

fn batch() -> Batch {
    Batch {
        version: VERSION.into(),
        batch_id: "b".repeat(32),
        application_version: Some("a".repeat(64)),
        callback_version: "callback-v1".into(),
        item_count: 1,
    }
}
fn invocation(n: u32, role: Role) -> Invocation {
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
    Invocation {
        invocation_id: format!("{n:032x}"),
        position: CallPosition { item: 0, call: 0 },
        ordinal: n,
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
fn complete() -> Completion {
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
fn command(conn: &mut Connection, value: Value) -> Value {
    serde_json::from_str(&handle_json(conn, &value.to_string()).unwrap()).unwrap()
}
fn start(conn: &mut Connection, call: &Invocation) {
    command(
        conn,
        json!({"operation":"begin_call","batch_id":batch().batch_id,"invocation":call}),
    );
}
fn finish(conn: &mut Connection, call: &Invocation, completion: &Completion) {
    command(
        conn,
        json!({"operation":"finish_call","batch_id":batch().batch_id,"invocation_id":call.invocation_id,"completion":completion}),
    );
}
fn seal(conn: &mut Connection) -> Value {
    command(conn,json!({"operation":"seal_batch","batch_id":batch().batch_id,"logical_calls":[{"item":0,"call":0}]}))["summary"].clone()
}
fn report(conn: &mut Connection) -> Value {
    command(
        conn,
        json!({"operation":"report","batch_id":batch().batch_id}),
    )["summary"]
        .clone()
}
fn store() -> Connection {
    let mut conn = Connection::open_in_memory().unwrap();
    command(
        &mut conn,
        json!({"operation":"begin_batch","batch":batch()}),
    );
    conn
}

fn usage() -> Value {
    json!({"version":"provider-billing-usage-v1","usage":{
        "provider_protocol":"openai.chat.completions.v1",
        "input_tokens":2,"output_tokens":1,"total_tokens":3,
        "input_details":{"cached_tokens":1,"cache_write_tokens":1,"audio_tokens":0,"image_tokens":0,"text_tokens":2},
        "output_details":{"accepted_prediction_tokens":0,"rejected_prediction_tokens":1,"audio_tokens":0,"reasoning_tokens":0,"text_tokens":1},
        "service_tier":"priority","unrecognized_fields":false
    }})
}

#[test]
fn billing_usage_is_immutable_original_evidence_and_does_not_resolve_estimated_cost() {
    let file = tempfile::NamedTempFile::new().unwrap();
    let mut conn = Connection::open(file.path()).unwrap();
    command(
        &mut conn,
        json!({"operation":"begin_batch","batch":batch()}),
    );
    let call = invocation(0, Role::Primary);
    start(&mut conn, &call);
    let mut done = complete();
    done.cost_source = InferenceCostSource::Estimated;
    done.billing_usage = Some(serde_json::from_value(usage()).unwrap());
    finish(&mut conn, &call, &done);
    let raw: String = conn
        .query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |row| row.get(0),
        )
        .unwrap();
    finish(&mut conn, &call, &done);
    let summary = seal(&mut conn);
    assert_eq!(summary["known_provider_cost_usd"], 0.0);
    assert_eq!(summary["estimated_cost_usd"], 0.01);
    assert_eq!(summary["cost_complete"], false);
    drop(conn);
    let mut conn = Connection::open(file.path()).unwrap();
    finish(&mut conn, &call, &done);
    assert_eq!(report(&mut conn)["cost_complete"], false);
    let after: String = conn
        .query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(after, raw);
    let mut changed = usage();
    changed["usage"]["service_tier"] = json!("default");
    done.billing_usage = Some(serde_json::from_value(changed).unwrap());
    assert!(agentc_optimizer::provider_receipts::finish_call(
        &conn,
        &batch().batch_id,
        &call.invocation_id,
        &done
    )
    .is_err());
    let after: String = conn
        .query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(after, raw);
}

#[test]
fn inconsistent_billing_usage_is_rejected_before_terminal_receipt_persistence() {
    let mut conn = store();
    let call = invocation(0, Role::Primary);
    start(&mut conn, &call);
    let mut variants = Vec::new();
    for (pointer, value) in [
        ("/version", json!("future-version")),
        ("/usage/input_tokens", json!(3)),
        ("/usage/output_tokens", json!(2)),
        ("/usage/total_tokens", json!(4)),
        ("/usage/input_details/cached_tokens", json!(3)),
        ("/usage/output_details/rejected_prediction_tokens", json!(2)),
        ("/usage/service_tier", json!("arbitrary-provider-content")),
    ] {
        let mut value_json = usage();
        *value_json.pointer_mut(pointer).unwrap() = value;
        variants.push(value_json);
    }
    variants.push(json!({"version":"provider-billing-usage-v1","usage":{
        "provider_protocol":"anthropic.messages.v1","input_tokens":2,"output_tokens":1,
        "cache_creation_input_tokens":0,"cache_read_input_tokens":0,
        "cache_creation":null,"server_tool_use":null,"service_tier":"standard",
        "inference_geo":"global","unrecognized_fields":false
    }}));
    for value in variants {
        let mut done = complete();
        done.billing_usage = Some(serde_json::from_value(value).unwrap());
        assert!(agentc_optimizer::provider_receipts::finish_call(
            &conn,
            &batch().batch_id,
            &call.invocation_id,
            &done
        )
        .is_err());
        let pending: Option<String> = conn
            .query_row(
                "SELECT completion_json FROM provider_receipt_invocation",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(pending.is_none());
    }
    let mut done = complete();
    done.status = Status::Failed;
    done.billing_usage = Some(serde_json::from_value(usage()).unwrap());
    assert!(agentc_optimizer::provider_receipts::finish_call(
        &conn,
        &batch().batch_id,
        &call.invocation_id,
        &done
    )
    .is_err());
    let mut extra = usage();
    extra["usage"]["input_details"]["secret"] = json!("private marker");
    assert!(
        serde_json::from_value::<agentc_optimizer::provider_usage::BillingUsage>(extra).is_err()
    );
    let mut negative = usage();
    negative["usage"]["input_details"]["cached_tokens"] = json!(-1);
    assert!(
        serde_json::from_value::<agentc_optimizer::provider_usage::BillingUsage>(negative).is_err()
    );
}

#[test]
fn anthropic_cache_categories_do_not_use_uncached_input_as_their_total() {
    let mut conn = store();
    let mut call = invocation(0, Role::Primary);
    call.provider_protocol = "anthropic.messages.v1".into();
    start(&mut conn, &call);
    let value = json!({"version":"provider-billing-usage-v1","usage":{
        "provider_protocol":"anthropic.messages.v1","input_tokens":2,"output_tokens":1,
        "cache_creation_input_tokens":100,"cache_read_input_tokens":900,
        "cache_creation":{"ephemeral_5m_input_tokens":40,"ephemeral_1h_input_tokens":60},
        "server_tool_use":{"web_search_requests":2,"web_fetch_requests":null},
        "service_tier":null,"inference_geo":null,"unrecognized_fields":true
    }});
    for one_hour in [59, 61, u32::MAX] {
        let mut malformed = value.clone();
        malformed["usage"]["cache_creation"]["ephemeral_1h_input_tokens"] = json!(one_hour);
        let mut done = complete();
        done.billing_usage = Some(serde_json::from_value(malformed).unwrap());
        assert!(agentc_optimizer::provider_receipts::finish_call(
            &conn,
            &batch().batch_id,
            &call.invocation_id,
            &done
        )
        .is_err());
    }
    let mut done = complete();
    done.cost_source = InferenceCostSource::Estimated;
    done.billing_usage = Some(serde_json::from_value(value).unwrap());
    finish(&mut conn, &call, &done);
    let raw: String = conn
        .query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let parsed: Completion = serde_json::from_str(&raw).unwrap();
    assert_eq!(
        serde_json::to_value(parsed.billing_usage).unwrap()["usage"]["cache_read_input_tokens"],
        900
    );
    assert_eq!(seal(&mut conn)["cost_complete"], false);
}

#[test]
fn legacy_receipt_retry_preserves_original_bytes_and_cannot_add_missing_usage() {
    let mut conn = store();
    let call = invocation(0, Role::Primary);
    start(&mut conn, &call);
    let done = complete();
    let legacy = agentc_core::storage::canonical_json(&serde_json::to_value(&done).unwrap());
    assert!(!legacy.contains("billing_usage"));
    conn.execute(
        "UPDATE provider_receipt_invocation SET completion_json=?1",
        [&legacy],
    )
    .unwrap();
    let restored: Completion = serde_json::from_str(&legacy).unwrap();
    assert!(restored.billing_usage.is_none());
    finish(&mut conn, &call, &restored);
    let mut edited = restored;
    edited.billing_usage = Some(serde_json::from_value(usage()).unwrap());
    assert!(agentc_optimizer::provider_receipts::finish_call(
        &conn,
        &batch().batch_id,
        &call.invocation_id,
        &edited
    )
    .is_err());
    let raw: String = conn
        .query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(raw, legacy);
}

#[test]
fn started_calls_remain_unknown_and_exact_terminal_retry_never_double_charges() {
    let mut conn = store();
    let call = invocation(0, Role::Primary);
    start(&mut conn, &call);
    start(&mut conn, &call);
    let pending = seal(&mut conn);
    assert_eq!(pending["pending_invocations"], 1);
    assert_eq!(pending["known_provider_cost_usd"], 0.0);
    assert_eq!(pending["cost_complete"], false);
    finish(&mut conn, &call, &complete());
    finish(&mut conn, &call, &complete());
    let done = report(&mut conn);
    assert_eq!(done["sdk_provider_invocations"], 1);
    assert_eq!(done["known_provider_cost_usd"], 0.01);
    assert_eq!(done["cost_complete"], true);
    let mut changed = complete();
    changed.cost_usd = Some(0.02);
    assert!(handle_json(&mut conn,&json!({"operation":"finish_call","batch_id":batch().batch_id,"invocation_id":call.invocation_id,"completion":changed}).to_string()).is_err());
    assert_eq!(report(&mut conn)["known_provider_cost_usd"], 0.01);
}

#[test]
fn a_successful_fallback_does_not_resolve_failed_candidate_uncertainty() {
    let mut conn = store();
    let call = invocation(0, Role::Primary);
    start(&mut conn, &call);
    let failure = Completion {
        status: Status::Failed,
        model_version: None,
        input_tokens: None,
        output_tokens: None,
        cost_usd: None,
        cost_source: InferenceCostSource::Unknown,
        latency_ms: 5.0,
        http_attempts: Some(1),
        exception_type: Some("APIStatusError".into()),
        billing_scope_sha256: None,
        provider_generation_sha256: None,
        provider_request_id_sha256: None,
        billing_usage: None,
    };
    finish(&mut conn, &call, &failure);
    let fallback = invocation(1, Role::Fallback);
    start(&mut conn, &fallback);
    finish(&mut conn, &fallback, &complete());
    let summary = seal(&mut conn);
    assert_eq!(summary["known_provider_cost_usd"], 0.01);
    assert_eq!(summary["failed_invocations"], 1);
    assert_eq!(summary["unknown_cost_invocations"], 1);
    assert_eq!(summary["known_http_attempts"], 2);
    assert_eq!(summary["cost_complete"], false);
}

#[test]
fn late_validation_and_exploration_are_retained_after_foreground_seal() {
    let mut conn = store();
    let main = invocation(0, Role::Primary);
    start(&mut conn, &main);
    finish(&mut conn, &main, &complete());
    assert_eq!(seal(&mut conn)["cost_complete"], true);
    let probe = invocation(1, Role::Exploration);
    start(&mut conn, &probe);
    assert_eq!(report(&mut conn)["pending_invocations"], 1);
    assert_eq!(report(&mut conn)["cost_complete"], false);
    let validation = invocation(2, Role::Validation);
    start(&mut conn, &validation);
    finish(&mut conn, &validation, &complete());
    finish(&mut conn, &probe, &complete());
    let summary = report(&mut conn);
    assert_eq!(summary["sdk_provider_invocations"], 3);
    assert!((summary["known_provider_cost_usd"].as_f64().unwrap() - 0.03).abs() < 1e-12);
    assert_eq!(summary["cost_complete"], true);
}

#[test]
fn missing_ordinals_foreground_roots_and_unverified_retries_never_look_complete() {
    let mut conn = store();
    let mut call = invocation(1, Role::Primary);
    start(&mut conn, &call);
    finish(&mut conn, &call, &complete());
    assert_eq!(seal(&mut conn)["cost_complete"], false);
    let mut conn = store();
    call = invocation(0, Role::Validation);
    start(&mut conn, &call);
    finish(&mut conn, &call, &complete());
    assert_eq!(seal(&mut conn)["cost_complete"], false);
    let mut conn = store();
    call = invocation(0, Role::Primary);
    call.transport_verified = false;
    start(&mut conn, &call);
    assert!(handle_json(&mut conn,&json!({"operation":"finish_call","batch_id":batch().batch_id,"invocation_id":call.invocation_id,"completion":complete()}).to_string()).is_err());
    let mut unverified = complete();
    unverified.http_attempts = None;
    finish(&mut conn, &call, &unverified);
    let summary = seal(&mut conn);
    assert_eq!(summary["unverified_transport_invocations"], 1);
    assert_eq!(summary["unknown_cost_invocations"], 1);
    assert_eq!(summary["unknown_http_attempt_invocations"], 1);
    assert_eq!(summary["cost_complete"], false);
}

#[test]
fn identity_conflicts_invalid_usage_and_foreign_completions_leave_the_journal_unchanged() {
    let mut conn = store();
    let call = invocation(0, Role::Primary);
    start(&mut conn, &call);
    let mut edited = call.clone();
    edited.request_sha256 = Some("a".repeat(64));
    assert!(handle_json(
        &mut conn,
        &json!({"operation":"begin_call","batch_id":batch().batch_id,"invocation":edited})
            .to_string()
    )
    .is_err());
    let mut invalid = complete();
    invalid.cost_usd = Some(-1.0);
    assert!(handle_json(&mut conn,&json!({"operation":"finish_call","batch_id":batch().batch_id,"invocation_id":call.invocation_id,"completion":invalid}).to_string()).is_err());
    assert!(handle_json(&mut conn,&json!({"operation":"finish_call","batch_id":"a".repeat(32),"invocation_id":call.invocation_id,"completion":complete()}).to_string()).is_err());
    assert_eq!(report(&mut conn)["pending_invocations"], 1);
}

#[test]
fn cancellation_and_estimates_are_distinct_from_complete_provider_billing() {
    let mut conn = store();
    let call = invocation(0, Role::Primary);
    start(&mut conn, &call);
    let mut cancelled = complete();
    cancelled.status = Status::Cancelled;
    cancelled.cost_usd = None;
    cancelled.input_tokens = None;
    cancelled.output_tokens = None;
    cancelled.cost_source = InferenceCostSource::Unknown;
    cancelled.http_attempts = None;
    cancelled.exception_type = Some("CancelledError".into());
    finish(&mut conn, &call, &cancelled);
    let summary = seal(&mut conn);
    assert_eq!(summary["cancelled_invocations"], 1);
    assert_eq!(summary["cost_complete"], false);
    let mut conn = store();
    start(&mut conn, &call);
    let mut estimated = complete();
    estimated.cost_source = InferenceCostSource::Estimated;
    finish(&mut conn, &call, &estimated);
    let summary = seal(&mut conn);
    assert_eq!(summary["known_provider_cost_usd"], 0.0);
    assert_eq!(summary["estimated_cost_usd"], 0.01);
    assert_eq!(summary["unknown_cost_invocations"], 1);
    assert_eq!(summary["cost_complete"], false);
}

#[test]
fn restart_preserves_pending_calls_and_one_to_one_profile_publication() {
    use agentc_optimizer::provider_receipts::{finish_publication, reserve_publication};
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("receipts.db");
    let mut conn = Connection::open(&path).unwrap();
    command(
        &mut conn,
        json!({"operation":"begin_batch","batch":batch()}),
    );
    let call = invocation(0, Role::Primary);
    start(&mut conn, &call);
    seal(&mut conn);
    drop(conn);
    let mut conn = Connection::open(&path).unwrap();
    assert_eq!(report(&mut conn)["pending_invocations"], 1);
    finish(&mut conn, &call, &complete());
    assert!(reserve_publication(&conn, &batch().batch_id, "receipt")
        .unwrap()
        .is_none());
    drop(conn);
    let mut conn = Connection::open(&path).unwrap();
    assert!(reserve_publication(&conn, &batch().batch_id, "receipt").is_err());
    finish_publication(&conn, &batch().batch_id, "published").unwrap();
    assert_eq!(
        reserve_publication(&conn, &batch().batch_id, "receipt").unwrap(),
        Some("published".into())
    );
    assert!(reserve_publication(&conn, &batch().batch_id, "edited receipt").is_err());
    assert_eq!(report(&mut conn)["known_provider_cost_usd"], 0.01);
}

#[test]
fn multiple_http_attempts_retain_known_spend_and_remaining_uncertainty() {
    let mut conn = store();
    let call = invocation(0, Role::Primary);
    start(&mut conn, &call);
    let mut retried = complete();
    retried.http_attempts = Some(2);
    finish(&mut conn, &call, &retried);
    let summary = seal(&mut conn);
    assert_eq!(summary["known_http_attempts"], 2);
    assert_eq!(summary["known_provider_cost_usd"], 0.01);
    assert_eq!(summary["unknown_cost_invocations"], 1);
    assert_eq!(summary["cost_complete"], false);
}
