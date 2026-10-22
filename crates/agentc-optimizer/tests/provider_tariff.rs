use agentc_optimizer::multi_call::{CallPosition, InferenceCostSource};
use agentc_optimizer::provider_receipts::{self, Batch, Completion, Invocation, Role, Status};
use agentc_optimizer::provider_tariff::{self, Bound, Declaration, Interval, QUANTITY, VERSION};
use rusqlite::Connection;
use serde_json::{json, Value};

fn invocation(protocol: &str, model: &str, n: u32) -> Invocation {
    let url = if protocol.starts_with("openai") {
        "https://api.openai.com/v1"
    } else {
        "https://api.anthropic.com"
    };
    Invocation {
        invocation_id: format!("{n:032x}"),
        position: CallPosition { item: 0, call: 0 },
        ordinal: n,
        role: Role::Primary,
        provider_protocol: protocol.into(),
        requested_model_id: model.into(),
        request_sha256: Some("a".repeat(64)),
        logical_request_sha256: Some("a".repeat(64)),
        transport_sha256: Some("b".repeat(64)),
        logical_plan_key: None,
        logical_runtime: None,
        dispatch_plan_key: None,
        dispatch_runtime: None,
        parent_plan_key: None,
        transport_verified: true,
        tariff_declaration: Some(Declaration {
            version: VERSION.into(),
            snapshot_sha256: provider_tariff::snapshot_sha256(),
            endpoint_sha256: Some(agentc_core::storage::content_hash(&format!(
                "billing-scope-v1\0{url}"
            ))),
            request_scope_supported: true,
        }),
    }
}

fn completion(call: &Invocation, usage: Value) -> Completion {
    Completion {
        status: Status::Completed,
        model_version: Some(call.requested_model_id.clone()),
        input_tokens: Some(usage["input_tokens"].as_u64().unwrap() as u32),
        output_tokens: Some(usage["output_tokens"].as_u64().unwrap() as u32),
        cost_usd: Some(0.01),
        cost_source: InferenceCostSource::Estimated,
        latency_ms: 10.0,
        http_attempts: Some(1),
        exception_type: None,
        billing_scope_sha256: call
            .tariff_declaration
            .as_ref()
            .unwrap()
            .endpoint_sha256
            .clone(),
        provider_generation_sha256: None,
        provider_request_id_sha256: None,
        billing_usage: Some(
            serde_json::from_value(json!({"version":"provider-billing-usage-v1","usage":usage}))
                .unwrap(),
        ),
    }
}

fn openai(cache: Option<u32>, tier: &str) -> Value {
    json!({"provider_protocol":"openai.chat.completions.v1","input_tokens":10000,"output_tokens":1000,
        "total_tokens":11000,"input_details":{"cached_tokens":cache,"cache_write_tokens":0,"audio_tokens":0,"image_tokens":0,"text_tokens":10000},
        "output_details":null,"service_tier":tier,"unrecognized_fields":false})
}

fn anthropic(split: Value) -> Value {
    json!({"provider_protocol":"anthropic.messages.v1","input_tokens":10000,"output_tokens":1000,
        "cache_creation_input_tokens":5000,"cache_read_input_tokens":20000,"cache_creation":split,
        "server_tool_use":null,"service_tier":"standard","inference_geo":"global","unrecognized_fields":false})
}

fn command(conn: &mut Connection, value: Value) -> Value {
    serde_json::from_str(&provider_receipts::handle_json(conn, &value.to_string()).unwrap())
        .unwrap()
}
fn store() -> Connection {
    let mut conn = Connection::open_in_memory().unwrap();
    command(
        &mut conn,
        json!({"operation":"begin_batch","batch":Batch {version:provider_receipts::VERSION.into(),batch_id:"c".repeat(32),application_version:None,callback_version:"tariff-fixture".into(),item_count:1}}),
    );
    command(&mut conn, json!({"operation":"tariff_policy"}));
    conn
}
fn begin(conn: &mut Connection, call: &Invocation) {
    command(
        conn,
        json!({"operation":"begin_call","batch_id":"c".repeat(32),"invocation":call}),
    );
}
fn finish(conn: &mut Connection, call: &Invocation, done: &Completion) {
    command(
        conn,
        json!({"operation":"finish_call","batch_id":"c".repeat(32),"invocation_id":call.invocation_id,"completion":done}),
    );
}
fn report(conn: &mut Connection) -> Value {
    command(
        conn,
        json!({"operation":"tariff_report","batch_id":"c".repeat(32)}),
    )["tariff"]
        .clone()
}
fn seal(conn: &mut Connection, failed: bool) {
    command(
        conn,
        json!({"operation":"seal_batch","batch_id":"c".repeat(32),"logical_calls":[{"item":0,"call":0}],"persistence_failed":failed}),
    );
}

#[test]
fn openai_cache_interval_contains_every_possible_actual_cache_count() {
    for (model, tier, input, cached, output) in [
        ("gpt-4o-mini-2024-07-18", "default", 150, 75, 600),
        ("gpt-4o-mini-2024-07-18", "priority", 250, 125, 1000),
        ("gpt-4o-2024-11-20", "default", 2500, 1250, 10000),
        ("gpt-4o-2024-08-06", "fast", 4250, 2125, 17000),
    ] {
        let call = invocation("openai.chat.completions.v1", model, 0);
        let bound = provider_tariff::quote(&call, &completion(&call, openai(None, tier))).unwrap();
        for cache in 0..=10000u64 {
            let actual = (10000 - cache) * input + cache * cached + 1000 * output;
            assert!(
                bound.interval.lower_nanodollars <= actual
                    && actual <= bound.interval.upper_nanodollars
            );
        }
        let exact =
            provider_tariff::quote(&call, &completion(&call, openai(Some(4096), tier))).unwrap();
        assert_eq!(
            exact.interval.lower_nanodollars,
            (10000 - 4096) * input + 4096 * cached + 1000 * output
        );
        assert_eq!(
            exact.interval.lower_nanodollars,
            exact.interval.upper_nanodollars
        );
    }
}

#[test]
fn anthropic_ttl_interval_covers_full_and_partial_creation_breakdowns() {
    for (model, multiplier) in [
        ("claude-haiku-4-5-20251001", 1),
        ("claude-sonnet-4-5-20250929", 3),
    ] {
        let call = invocation("anthropic.messages.v1", model, 0);
        let interval = provider_tariff::quote(&call, &completion(&call, anthropic(Value::Null)))
            .unwrap()
            .interval;
        assert_eq!(
            interval,
            Interval {
                lower_nanodollars: 23_250_000 * multiplier,
                upper_nanodollars: 27_000_000 * multiplier
            }
        );
        for hour in [0u32, 1, 2000, 4999, 5000] {
            for split in [
                json!({"ephemeral_5m_input_tokens":5000-hour,"ephemeral_1h_input_tokens":hour}),
                json!({"ephemeral_5m_input_tokens":null,"ephemeral_1h_input_tokens":hour}),
                json!({"ephemeral_5m_input_tokens":5000-hour,"ephemeral_1h_input_tokens":null}),
            ] {
                let exact = provider_tariff::quote(&call, &completion(&call, anthropic(split)))
                    .unwrap()
                    .interval;
                let actual =
                    (17_000_000 + (5000 - u64::from(hour)) * 1250 + u64::from(hour) * 2000)
                        * multiplier;
                assert_eq!(
                    exact,
                    Interval {
                        lower_nanodollars: actual,
                        upper_nanodollars: actual
                    }
                );
                assert!(
                    interval.lower_nanodollars <= actual && actual <= interval.upper_nanodollars
                );
            }
        }
    }
}

#[test]
fn unsupported_pricing_dimensions_never_gain_a_cost_upper_bound() {
    let base = invocation("openai.chat.completions.v1", "gpt-4o-mini-2024-07-18", 0);
    for tier in ["auto", "scale", "flex"] {
        assert!(provider_tariff::quote(&base, &completion(&base, openai(None, tier))).is_err());
    }
    let mut variants = Vec::new();
    for pointer in ["/service_tier", "/input_details/cached_tokens"] {
        let mut usage = openai(None, "default");
        *usage.pointer_mut(pointer).unwrap() = if pointer.ends_with("tier") {
            Value::Null
        } else {
            json!(10001)
        };
        variants.push(usage);
    }
    let mut usage = openai(None, "default");
    usage["unrecognized_fields"] = json!(true);
    variants.push(usage);
    let mut usage = openai(None, "default");
    usage["input_details"]["audio_tokens"] = json!(1);
    variants.push(usage);
    for usage in variants {
        assert!(provider_tariff::quote(&base, &completion(&base, usage)).is_err());
    }
    for model in ["gpt-4o-2024-05-13", "gpt-4o-mini-next", "ft:gpt-4o-mini"] {
        let call = invocation("openai.chat.completions.v1", model, 0);
        assert!(
            provider_tariff::quote(&call, &completion(&call, openai(None, "default"))).is_err()
        );
    }
    let mut wrong = base.clone();
    wrong
        .tariff_declaration
        .as_mut()
        .unwrap()
        .request_scope_supported = false;
    assert!(provider_tariff::quote(&wrong, &completion(&wrong, openai(None, "default"))).is_err());
    let mut done = completion(&base, openai(None, "default"));
    done.http_attempts = Some(2);
    assert!(provider_tariff::quote(&base, &done).is_err());
    done.http_attempts = Some(1);
    done.billing_scope_sha256 = Some("f".repeat(64));
    assert!(provider_tariff::quote(&base, &done).is_err());
    let call = invocation("anthropic.messages.v1", "claude-haiku-4-5-20251001", 0);
    for (key, value) in [
        ("service_tier", json!("priority")),
        ("inference_geo", json!("us")),
        ("cache_read_input_tokens", Value::Null),
        (
            "server_tool_use",
            json!({"web_search_requests":1,"web_fetch_requests":0}),
        ),
    ] {
        let mut usage = anthropic(Value::Null);
        usage[key] = value;
        assert!(provider_tariff::quote(&call, &completion(&call, usage)).is_err());
    }
}

#[test]
fn declared_intervals_are_exactly_once_and_do_not_settle_bills_or_publish_quality() {
    let mut conn = store();
    let call = invocation("openai.chat.completions.v1", "gpt-4o-mini-2024-07-18", 0);
    begin(&mut conn, &call);
    assert!(report(&mut conn)["upper_nanodollars"].is_null());
    let done = completion(&call, openai(Some(4096), "default"));
    finish(&mut conn, &call, &done);
    seal(&mut conn, false);
    let first = report(&mut conn);
    assert_eq!(first["lower_nanodollars"], 1_792_800);
    assert_eq!(first["upper_nanodollars"], 1_792_800);
    finish(&mut conn, &call, &done);
    assert_eq!(report(&mut conn), first);
    assert_eq!(first["acquisition"]["owned_invocations"], 1);
    assert_eq!(
        first["acquisition"]["observed_charged_lower_nanodollars"],
        1_792_800
    );
    assert_eq!(first["acquisition"]["lower_nanodollars"], 0);
    assert!(first["acquisition"]["upper_nanodollars"].is_null());
    let raw: String = conn
        .query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&raw).unwrap()["cost_source"],
        "estimated"
    );
    assert_eq!(
        command(
            &mut conn,
            json!({"operation":"report","batch_id":"c".repeat(32)})
        )["summary"]["known_provider_cost_usd"],
        0.0
    );
    assert_eq!(
        conn.query_row(
            "SELECT COUNT(*) FROM provider_billing_settlement",
            [],
            |r| r.get::<_, u64>(0)
        )
        .unwrap(),
        0
    );
    let mut changed = call.clone();
    changed
        .tariff_declaration
        .as_mut()
        .unwrap()
        .request_scope_supported = false;
    assert!(provider_receipts::handle_json(
        &mut conn,
        &json!({"operation":"begin_call","batch_id":"c".repeat(32),"invocation":changed})
            .to_string()
    )
    .is_err());
    assert_eq!(report(&mut conn), first);
}

#[test]
fn journal_loss_and_unknown_late_work_retain_observed_charges_without_a_total_upper() {
    let mut conn = store();
    let call = invocation("openai.chat.completions.v1", "gpt-4o-mini-2024-07-18", 0);
    begin(&mut conn, &call);
    finish(
        &mut conn,
        &call,
        &completion(&call, openai(None, "default")),
    );
    seal(&mut conn, true);
    let first = report(&mut conn);
    assert!(first["upper_nanodollars"].is_null());
    assert_eq!(first["lower_nanodollars"], 1_350_000);
    seal(&mut conn, false);
    assert!(report(&mut conn)["upper_nanodollars"].is_null());
    let mut late = invocation("openai.chat.completions.v1", "gpt-4o-mini-2024-07-18", 1);
    late.role = Role::Shadow;
    begin(&mut conn, &late);
    assert_eq!(report(&mut conn)["acquisition"]["unknown_invocations"], 1);
    let mut done = completion(&late, openai(None, "default"));
    done.status = Status::Failed;
    done.billing_usage = None;
    finish(&mut conn, &late, &done);
    assert_eq!(report(&mut conn)["acquisition"]["unknown_invocations"], 1);
    assert_eq!(
        report(&mut conn)["acquisition"]["observed_charged_lower_nanodollars"],
        1_350_000
    );
}

#[test]
fn atomic_write_failures_and_recovery_never_reprice_or_double_charge_original_sdk_usage() {
    let file = tempfile::NamedTempFile::new().unwrap();
    let mut conn = Connection::open(file.path()).unwrap();
    provider_receipts::ensure_schema(&conn).unwrap();
    agentc_optimizer::provider_economics::ensure_schema(&conn).unwrap();
    command(
        &mut conn,
        json!({"operation":"begin_batch","batch":{"version":provider_receipts::VERSION,"batch_id":"c".repeat(32),"application_version":null,"callback_version":"tariff-fixture","item_count":1}}),
    );
    command(&mut conn, json!({"operation":"tariff_policy"}));
    let call = invocation("openai.chat.completions.v1", "gpt-4o-mini-2024-07-18", 0);
    begin(&mut conn, &call);
    let done = completion(&call, openai(None, "default"));
    conn.execute_batch("CREATE TRIGGER tariff_fault BEFORE UPDATE ON provider_tariff_cohort BEGIN SELECT RAISE(ABORT,'fixture'); END;").unwrap();
    assert!(provider_receipts::handle_json(&mut conn,&json!({"operation":"finish_call","batch_id":"c".repeat(32),"invocation_id":call.invocation_id,"completion":done}).to_string()).is_err());
    assert!(conn
        .query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |r| r.get::<_, Option<String>>(0)
        )
        .unwrap()
        .is_none());
    conn.execute_batch("DROP TRIGGER tariff_fault;").unwrap();
    finish(&mut conn, &call, &done);
    seal(&mut conn, false);
    let before = report(&mut conn);
    let raw: String = conn
        .query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |r| r.get(0),
        )
        .unwrap();
    drop(conn);
    let mut conn = Connection::open(file.path()).unwrap();
    assert_eq!(report(&mut conn), before);
    assert_eq!(
        conn.query_row(
            "SELECT completion_json FROM provider_receipt_invocation",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        raw
    );
    conn.execute(
        "UPDATE provider_receipt_invocation SET completion_json=?1",
        [raw.replace("\"input_tokens\":10000", "\"input_tokens\":9999")],
    )
    .unwrap();
    assert!(provider_tariff::report(&mut conn, &"c".repeat(32)).is_err());
}

#[test]
fn undeclared_history_cannot_gain_a_tariff_after_registration() {
    let mut conn = store();
    let mut call = invocation("openai.chat.completions.v1", "gpt-4o-mini-2024-07-18", 0);
    call.tariff_declaration = None;
    begin(&mut conn, &call);
    let template = invocation("openai.chat.completions.v1", "gpt-4o-mini-2024-07-18", 0);
    finish(
        &mut conn,
        &call,
        &completion(&template, openai(None, "default")),
    );
    seal(&mut conn, false);
    assert!(report(&mut conn)["upper_nanodollars"].is_null());
    assert_eq!(
        report(&mut conn)["invocations"][0]["result"]["reason"],
        "undeclared_tariff"
    );
    call.tariff_declaration = template.tariff_declaration;
    assert!(provider_receipts::handle_json(
        &mut conn,
        &json!({"operation":"begin_call","batch_id":"c".repeat(32),"invocation":call}).to_string()
    )
    .is_err());
    let mut duplicate = invocation("openai.chat.completions.v1", "gpt-4o-mini-2024-07-18", 1);
    duplicate
        .tariff_declaration
        .as_mut()
        .unwrap()
        .snapshot_sha256 = "a".repeat(64);
    assert!(provider_receipts::handle_json(
        &mut conn,
        &json!({"operation":"begin_call","batch_id":"c".repeat(32),"invocation":duplicate})
            .to_string()
    )
    .is_err());
    assert_eq!(
        conn.query_row(
            "SELECT COUNT(*) FROM provider_receipt_invocation",
            [],
            |r| r.get::<_, u64>(0)
        )
        .unwrap(),
        1
    );
}

#[test]
fn conservative_savings_use_reference_lower_candidate_upper_and_all_acquisition_upper() {
    let bound = |lo, hi| Bound {
        quantity: QUANTITY.into(),
        snapshot_sha256: provider_tariff::snapshot_sha256(),
        interval: Interval {
            lower_nanodollars: lo,
            upper_nanodollars: hi,
        },
    };
    assert_eq!(
        provider_tariff::savings_lower_bound(&bound(100, 120), &bound(50, 80), &bound(0, 101), 10)
            .unwrap(),
        9
    );
    assert_eq!(
        provider_tariff::savings_lower_bound(&bound(0, 120), &bound(50, 80), &bound(0, 101), 10)
            .unwrap(),
        -91
    );
    let mut wrong = bound(100, 120);
    wrong.quantity = "provider_billed_usd".into();
    assert!(provider_tariff::savings_lower_bound(&wrong, &bound(0, 10), &bound(0, 0), 10).is_err());
    wrong = bound(100, 120);
    wrong.snapshot_sha256 = "a".repeat(64);
    assert!(
        provider_tariff::savings_lower_bound(&bound(100, 120), &wrong, &bound(0, 0), 10).is_err()
    );
    assert!(
        provider_tariff::savings_lower_bound(&bound(100, 120), &bound(0, 10), &bound(0, 0), 0)
            .is_err()
    );
    assert!(Interval {
        lower_nanodollars: u64::MAX,
        upper_nanodollars: u64::MAX
    }
    .checked_add(Interval {
        lower_nanodollars: 1,
        upper_nanodollars: 1
    })
    .is_err());
}
