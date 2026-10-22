use agentc_optimizer::provider_graph_acquisition as acquisition;
use agentc_optimizer::provider_graph_preparation::{self as preparation, Begin};
use agentc_optimizer::provider_graph_quality::Policy;
use agentc_optimizer::provider_receipts;
use rusqlite::Connection;
use serde_json::{json, Value};

struct Store {
    conn: Connection,
    batch: String,
    driver: String,
}
impl Store {
    fn new() -> Self {
        Self::with_conn(Connection::open_in_memory().unwrap())
    }
    fn with_conn(conn: Connection) -> Self {
        let mut s = Self {
            conn,
            batch: "b".repeat(32),
            driver: String::new(),
        };
        agentc_optimizer::schema::ensure_cost_model_schema(&s.conn).unwrap();
        s.command(json!({"operation":"begin_batch","batch":{"version":provider_receipts::VERSION,"batch_id":s.batch,"application_version":"a".repeat(64),"callback_version":"acquisition-test-v1","item_count":1}})).unwrap();
        preparation::begin(
            &mut s.conn,
            Begin {
                role_program: None,
                batch_id: s.batch.clone(),
                max_workers: 1,
                input_sha256: None,
                template_execution_id: None,
                comparison_pair_id: None,
            },
            Policy::default(),
            20,
            1,
        )
        .unwrap();
        s.driver = s.summary().driver_id;
        s
    }
    fn command(&mut self, v: Value) -> anyhow::Result<Value> {
        Ok(serde_json::from_str(&provider_receipts::handle_json(
            &mut self.conn,
            &v.to_string(),
        )?)?)
    }
    fn begin(&mut self, n: u32, role: &str) {
        self.command(json!({"operation":"begin_call","batch_id":self.batch,"invocation":{
            "invocation_id":format!("{n:032x}"),"position":{"item":0,"call":0},"ordinal":n,"role":role,
            "provider_protocol":"openai.chat.completions.v1","requested_model_id":"model","request_sha256":"e".repeat(64),
            "logical_request_sha256":"e".repeat(64),"transport_sha256":"f".repeat(64),"logical_plan_key":null,"logical_runtime":null,
            "dispatch_plan_key":null,"dispatch_runtime":null,"parent_plan_key":null,"transport_verified":true
        }})).unwrap();
    }
    fn done(&mut self, n: u32, status: &str, source: &str, cost: f64) -> Value {
        let value = json!({"operation":"finish_call","batch_id":self.batch,"invocation_id":format!("{n:032x}"),"completion":{
            "status":status,"model_version":"model","input_tokens":2,"output_tokens":1,"cost_usd":cost,"cost_source":source,
            "latency_ms":10.,"http_attempts":1,"exception_type":if status=="completed" {None} else {Some("ProviderError")}
        }});
        self.command(value.clone()).unwrap();
        value
    }
    fn seal(&mut self) {
        self.command(json!({"operation":"seal_batch","batch_id":self.batch,"logical_calls":[{"item":0,"call":0}]})).unwrap();
        self.command(json!({"operation":"finish_batch_economics","batch_id":self.batch,"wall_latency_ms":20.})).unwrap();
    }
    fn summary(&self) -> acquisition::Economics {
        acquisition::report_batch(&self.conn, &self.batch)
            .unwrap()
            .unwrap()
    }
}

#[test]
fn all_roles_and_late_work_are_charged_once_even_without_graph_publication() {
    let mut s = Store::new();
    for (n, role) in ["primary", "validation", "shadow", "fallback"]
        .into_iter()
        .enumerate()
    {
        s.begin(n as u32, role);
        s.done(n as u32, "completed", "provider_reported", 0.01);
    }
    s.seal();
    let before = s.summary();
    assert!(before.cost_complete);
    assert_eq!(before.physical_invocations, 4);
    assert!((before.known_cost_usd - 0.04).abs() < 1e-12);
    s.begin(4, "exploration");
    let pending = s.summary();
    assert!(!pending.cost_complete);
    assert_eq!(pending.pending_invocations, 1);
    let receipt = s.done(4, "completed", "provider_reported", 0.02);
    s.command(receipt).unwrap();
    let after = s.summary();
    assert!(after.cost_complete);
    assert_eq!(after.physical_invocations, 5);
    assert_eq!(after.roles.len(), 5);
    assert_eq!(after.late_invocations, 1);
    assert_eq!(after.late_dispatch_latency_ms, 10.);
    assert!((after.known_cost_usd - 0.06).abs() < 1e-12);
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM provider_graph_execution", [], |r| r
                .get::<_, i64>(
                0
            ))
            .unwrap(),
        0
    );
    assert_eq!(
        s.command(json!({"operation":"graph_acquisition","driver_id":s.driver}))
            .unwrap()["acquisition"]["physical_invocations"],
        5
    );
}

#[test]
fn failures_estimates_and_failed_persistence_hold_without_discarding_known_charges() {
    let mut s = Store::new();
    s.begin(0, "primary");
    s.done(0, "failed", "provider_reported", 0.02);
    s.begin(1, "fallback");
    s.done(1, "completed", "estimated", 0.003);
    s.seal();
    let q = s.summary();
    assert!(!q.cost_complete);
    assert_eq!(q.unknown_cost_invocations, 2);
    assert_eq!(q.known_cost_usd, 0.02);
    assert_eq!(q.estimated_cost_usd, 0.003);
    s.command(json!({"operation":"seal_batch","batch_id":s.batch,"logical_calls":[{"item":0,"call":0}],"persistence_failed":true})).unwrap();
    assert_eq!(s.summary().persistence_failed_batches, 1);
}

#[test]
fn acquisition_and_receipt_updates_roll_back_together_and_tampering_cannot_hide_a_charge() {
    let mut s = Store::new();
    s.begin(0, "primary");
    s.conn.execute_batch("CREATE TRIGGER reject_charge BEFORE UPDATE ON provider_graph_acquisition_invocation BEGIN SELECT RAISE(ABORT,'fixture'); END").unwrap();
    let done = json!({"operation":"finish_call","batch_id":s.batch,"invocation_id":format!("{:032x}",0),"completion":{"status":"completed","model_version":"model","input_tokens":2,"output_tokens":1,"cost_usd":0.01,"cost_source":"provider_reported","latency_ms":10.,"http_attempts":1,"exception_type":null}});
    assert!(s.command(done.clone()).is_err());
    assert_eq!(s.summary().pending_invocations, 1);
    s.conn.execute_batch("DROP TRIGGER reject_charge").unwrap();
    s.command(done).unwrap();
    s.seal();
    s.conn.execute("UPDATE provider_graph_acquisition_invocation SET charge_json=json_set(charge_json,'$.known_cost_usd',0)",[]).unwrap();
    assert!(acquisition::report_batch(&s.conn, &s.batch).is_err());
}

#[test]
fn original_driver_and_costs_survive_restart_and_deleted_enrollment_is_rejected() {
    let file = tempfile::NamedTempFile::new().unwrap();
    let mut s = Store::with_conn(Connection::open(file.path()).unwrap());
    s.begin(0, "primary");
    s.done(0, "completed", "provider_reported", 0.01);
    s.seal();
    let expected = s.summary();
    let batch = s.batch.clone();
    drop(s);
    let conn = Connection::open(file.path()).unwrap();
    assert_eq!(
        acquisition::report_batch(&conn, &batch).unwrap().unwrap(),
        expected
    );
    conn.execute("DELETE FROM provider_graph_acquisition_invocation", [])
        .unwrap();
    assert!(conn
        .execute("DELETE FROM provider_graph_acquisition", [])
        .is_err());
    conn.execute("DELETE FROM provider_graph_foreground_input", [])
        .unwrap();
    conn.execute("DELETE FROM provider_graph_acquisition", [])
        .unwrap();
    let lease: String = conn
        .query_row("SELECT lease_id FROM provider_graph_preparation", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert!(preparation::load(&conn, &lease).is_err());
}

#[test]
fn original_verified_invoice_resolves_graph_liability_without_rewriting_sdk_costs() {
    use agentc_optimizer::provider_reconciliation as billing;
    let mut s = Store::new();
    s.begin(0, "primary");
    let done = json!({"operation":"finish_call","batch_id":s.batch,"invocation_id":format!("{:032x}",0),"completion":{
        "status":"completed","model_version":"model","input_tokens":2,"output_tokens":1,"cost_usd":0.003,"cost_source":"estimated","latency_ms":10.,"http_attempts":1,"exception_type":null,
        "billing_scope_sha256":billing::openrouter_scope(),"provider_generation_sha256":"1".repeat(64),"provider_request_id_sha256":"2".repeat(64)
    }});
    s.command(done).unwrap();
    s.seal();
    let before = s.summary();
    assert!(!before.cost_complete);
    let binding = billing::bindings(&s.conn, &s.batch).unwrap().remove(0);
    let invoice = json!({"issuer":billing::ISSUER,"receipt_sha256":binding.receipt_sha256,"billing_scope_sha256":binding.billing_scope_sha256,
        "generation_sha256":binding.generation_sha256,"request_id_sha256":binding.request_id_sha256,"model_id":"model","input_tokens":2,"output_tokens":1,"cost_usd":0.02,"cancelled":false,"finish_reason":"stop"});
    let start = json!({"operation":"begin_billing_lookup","batch_id":s.batch,"invocation_id":binding.invocation_id,"attempt_id":"3".repeat(32),"receipt_sha256":invoice["receipt_sha256"]});
    s.command(start).unwrap();
    assert_eq!(s.summary().pending_billing_verifications, 1);
    let finish = json!({"operation":"finish_billing_lookup","attempt_id":"3".repeat(32),"latency_ms":7.,"invoice":invoice});
    s.command(finish.clone()).unwrap();
    s.command(finish).unwrap();
    let after = s.summary();
    assert!(after.cost_complete);
    assert_eq!(after.known_cost_usd, 0.02);
    assert_eq!(after.estimated_cost_usd, 0.003);
    assert_eq!(after.billing_verification_latency_ms, 7.);
    assert_eq!(after.physical_invocations, 1);
    assert_eq!(after.late_invocations, 0);
    let raw: String = s
        .conn
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
    s.conn.execute("UPDATE provider_billing_settlement SET invoice_json=json_set(invoice_json,'$.issuer','changed')",[]).unwrap();
    assert!(acquisition::report_batch(&s.conn, &s.batch).is_err());
}

#[test]
fn legacy_unenrolled_preparations_and_reassigned_driver_rows_cannot_hide_prior_work() {
    use agentc_core::storage::{canonical_json, content_hash};
    let mut s = Store::new();
    s.begin(0, "primary");
    s.done(0, "completed", "provider_reported", 0.01);
    s.seal();
    let id = "c".repeat(32);
    s.command(json!({"operation":"begin_batch","batch":{"version":provider_receipts::VERSION,"batch_id":id,"application_version":"a".repeat(64),"callback_version":"acquisition-test-v1","item_count":1}})).unwrap();
    let lease = preparation::begin(
        &mut s.conn,
        Begin {
            role_program: None,
            batch_id: id.clone(),
            max_workers: 1,
            input_sha256: None,
            template_execution_id: None,
            comparison_pair_id: None,
        },
        Policy::default(),
        20,
        2,
    )
    .unwrap();
    s.conn
        .execute(
            "UPDATE provider_graph_acquisition SET driver_id=?2 WHERE batch_id=?1",
            rusqlite::params![id, "d".repeat(64)],
        )
        .unwrap();
    assert_eq!(
        acquisition::summarize_driver(&s.conn, &s.driver)
            .unwrap()
            .uncovered_preparations,
        1
    );
    s.conn
        .execute(
            "DELETE FROM provider_graph_acquisition WHERE batch_id=?1",
            [&id],
        )
        .unwrap();
    let mut legacy = serde_json::to_value(&lease).unwrap();
    legacy
        .as_object_mut()
        .unwrap()
        .remove("acquisition_version");
    let mut identity = legacy.clone();
    identity.as_object_mut().unwrap().remove("lease_id");
    let old_id = content_hash(&canonical_json(&identity));
    legacy["lease_id"] = json!(old_id);
    let raw = canonical_json(&legacy);
    s.conn.execute("UPDATE provider_graph_preparation SET lease_id=?2,lease_json=?3,lease_sha256=?4 WHERE batch_id=?1",rusqlite::params![id,old_id,raw,content_hash(&raw)]).unwrap();
    assert!(preparation::load(&s.conn, &old_id)
        .unwrap()
        .acquisition_version
        .is_none());
    let q = acquisition::summarize_driver(&s.conn, &s.driver).unwrap();
    assert_eq!(q.uncovered_preparations, 1);
    assert_eq!(q.known_cost_usd, 0.01);
    assert!(!q.cost_complete);
}
