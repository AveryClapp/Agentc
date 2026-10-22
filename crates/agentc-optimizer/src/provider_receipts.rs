//! Durable, content-free receipts for SDK provider dispatches in a batch.
//!
//! A started call remains unknown until an exact terminal receipt arrives.
//! Sealing the foreground does not erase late counterfactuals or their spend.
//! This journal is accounting evidence, never a quality or admission token.

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

use crate::multi_call::{CallPosition, InferenceCostSource};
use crate::plan_profile::{PlanProfileKey, PlanRuntimeVersion};

pub const VERSION: &str = "provider-dispatch-receipts-v1";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Batch {
    pub version: String,
    pub batch_id: String,
    pub application_version: Option<String>,
    pub callback_version: String,
    pub item_count: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Primary,
    Fallback,
    Validation,
    Exploration,
    Shadow,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Invocation {
    pub invocation_id: String,
    pub position: CallPosition,
    pub ordinal: u32,
    pub role: Role,
    pub provider_protocol: String,
    pub requested_model_id: String,
    pub request_sha256: Option<String>,
    pub logical_request_sha256: Option<String>,
    pub transport_sha256: Option<String>,
    pub logical_plan_key: Option<PlanProfileKey>,
    pub logical_runtime: Option<PlanRuntimeVersion>,
    pub dispatch_plan_key: Option<PlanProfileKey>,
    pub dispatch_runtime: Option<PlanRuntimeVersion>,
    pub parent_plan_key: Option<PlanProfileKey>,
    pub transport_verified: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tariff_declaration: Option<crate::provider_tariff::Declaration>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Completed,
    Failed,
    Cancelled,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Completion {
    pub status: Status,
    pub model_version: Option<String>,
    pub input_tokens: Option<u32>,
    pub output_tokens: Option<u32>,
    pub cost_usd: Option<f64>,
    pub cost_source: InferenceCostSource,
    pub latency_ms: f64,
    pub http_attempts: Option<u32>,
    pub exception_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub billing_scope_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_generation_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_request_id_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub billing_usage: Option<crate::provider_usage::BillingUsage>,
}

#[derive(Debug, Serialize)]
pub struct Summary {
    pub version: &'static str,
    pub batch_id: String,
    pub sealed: bool,
    pub sdk_provider_invocations: u64,
    pub pending_invocations: u64,
    pub failed_invocations: u64,
    pub cancelled_invocations: u64,
    pub known_provider_cost_usd: f64,
    pub estimated_cost_usd: f64,
    pub unknown_cost_invocations: u64,
    pub unverified_transport_invocations: u64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub known_http_attempts: u64,
    pub unknown_http_attempt_invocations: u64,
    pub summed_dispatch_latency_ms: f64,
    pub cost_complete: bool,
}

#[derive(Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
enum Command {
    TariffPolicy,
    TariffReport {
        batch_id: String,
    },
    TariffAcquisition {
        call_site_version: crate::plan_profile::CallSiteVersion,
    },
    GraphAcquisition {
        driver_id: String,
    },
    GraphExecutionCosts {
        kind: crate::provider_graph_economics::Kind,
        execution_id: String,
    },
    GraphCostWindow {
        kind: crate::provider_graph_economics::Kind,
        key: PlanProfileKey,
        runtime: PlanRuntimeVersion,
        window_size: usize,
    },
    GraphAcquisitionCosts {
        driver_id: String,
    },
    GraphAdmission {
        request: crate::provider_graph_admission::Request,
    },
    GraphCampaign {
        campaign_id: String,
    },
    VerifyUnbudgetedGraph {
        batch_id: String,
    },
    BeginGraphDeployment {
        request: Box<crate::provider_graph_deployment::Begin>,
    },
    FinishRoleGraph {
        lease_id: String,
        comparison: crate::provider_graph::Comparison,
    },
    RecordGraphValidation {
        lease_id: String,
        invocation_id: String,
        decision: crate::provider_graph_roles::VerdictDecision,
    },
    RoleGraphExecution {
        execution_id: String,
    },
    BillingBindings {
        batch_id: String,
    },
    BeginBillingLookup {
        batch_id: String,
        invocation_id: String,
        attempt_id: String,
        receipt_sha256: String,
    },
    FinishBillingLookup {
        attempt_id: String,
        latency_ms: f64,
        invoice: Option<crate::provider_reconciliation::Invoice>,
    },
    BeginBatch {
        batch: Batch,
    },
    BeginCall {
        batch_id: String,
        invocation: Box<Invocation>,
    },
    FinishCall {
        batch_id: String,
        invocation_id: String,
        completion: Completion,
    },
    SealBatch {
        batch_id: String,
        logical_calls: Vec<CallPosition>,
        #[serde(default)]
        persistence_failed: bool,
    },
    FinishBatchEconomics {
        batch_id: String,
        wall_latency_ms: f64,
    },
    Report {
        batch_id: String,
    },
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_receipt_batch (
           batch_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL, sealed INTEGER NOT NULL DEFAULT 0,
           expected_calls_json TEXT
         );
         CREATE TABLE IF NOT EXISTS provider_receipt_invocation (
           invocation_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
           item INTEGER NOT NULL, logical_call INTEGER NOT NULL, ordinal INTEGER NOT NULL,
           invocation_json TEXT NOT NULL, completion_json TEXT,
           UNIQUE(batch_id,item,logical_call,ordinal),
           FOREIGN KEY(batch_id) REFERENCES provider_receipt_batch(batch_id)
         );
         CREATE INDEX IF NOT EXISTS provider_receipt_by_batch ON provider_receipt_invocation(batch_id);",
    )?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_receipt_profile_publication (
        batch_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL, result_json TEXT
    );",
    )?;
    Ok(())
}

/// Reserve one economic observation per physical execution before touching
/// derived profiles. A crash during publication leaves an explicit pending
/// record rather than allowing a retry to manufacture another execution.
pub fn reserve_publication(conn: &Connection, id: &str, receipt: &str) -> Result<Option<String>> {
    let hash = agentc_core::storage::content_hash(receipt);
    let prior: Option<(String,Option<String>)>=conn.query_row("SELECT receipt_sha256,result_json FROM provider_receipt_profile_publication WHERE batch_id=?1",[id],|row| Ok((row.get(0)?,row.get(1)?))).optional()?;
    if let Some((old, result)) = prior {
        if old != hash {
            bail!("provider journal was already bound to a different batch receipt");
        }
        return result.map(Some).context(
            "provider batch profile publication is incomplete; its physical journal is retained",
        );
    }
    conn.execute(
        "INSERT INTO provider_receipt_profile_publication(batch_id,receipt_sha256) VALUES(?1,?2)",
        params![id, hash],
    )?;
    Ok(None)
}

pub fn finish_publication(conn: &Connection, id: &str, result: &str) -> Result<()> {
    let updated=conn.execute("UPDATE provider_receipt_profile_publication SET result_json=?1 WHERE batch_id=?2 AND result_json IS NULL",params![result,id])?;
    if updated != 1 {
        bail!("provider batch publication reservation is missing");
    }
    Ok(())
}

fn identifier(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn encode(value: &impl Serialize) -> Result<String> {
    Ok(agentc_core::storage::canonical_json(&serde_json::to_value(
        value,
    )?))
}

fn load_batch(conn: &Connection, id: &str) -> Result<(Batch, bool)> {
    let (json, sealed): (String, bool) = conn
        .query_row(
            "SELECT metadata_json,sealed FROM provider_receipt_batch WHERE batch_id=?1",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .context("provider receipt batch does not exist")?;
    Ok((serde_json::from_str(&json)?, sealed))
}

pub fn begin_batch(conn: &Connection, batch: &Batch) -> Result<()> {
    if batch.version != VERSION
        || !identifier(&batch.batch_id, 32)
        || batch.item_count == 0
        || batch.callback_version.is_empty()
        || batch.callback_version.len() > 256
        || batch
            .application_version
            .as_ref()
            .is_some_and(|value| !identifier(value, 64))
    {
        bail!("provider receipt batch metadata is invalid");
    }
    let json = encode(batch)?;
    conn.execute("INSERT INTO provider_receipt_batch(batch_id,metadata_json) VALUES(?1,?2) ON CONFLICT DO NOTHING",params![batch.batch_id,json])?;
    let (stored, _) = load_batch(conn, &batch.batch_id)?;
    if encode(&stored)? != json {
        bail!("provider receipt batch identity conflicts");
    }
    Ok(())
}

pub fn begin_call(conn: &Connection, batch_id: &str, call: &Invocation) -> Result<()> {
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        begin_call(&tx, batch_id, call)?;
        tx.commit()?;
        return Ok(());
    }
    let (batch, _) = load_batch(conn, batch_id)?;
    crate::provider_graph_preparation::verify_dispatch(conn, batch_id, call)?;
    if let Some(declaration) = &call.tariff_declaration {
        crate::provider_tariff::validate_declaration(conn, declaration)?;
    }
    if !identifier(&call.invocation_id, 32)
        || call.position.item >= batch.item_count
        || !matches!(
            call.provider_protocol.as_str(),
            "openai.chat.completions.v1" | "anthropic.messages.v1"
        )
        || call.requested_model_id.trim().is_empty()
        || call
            .request_sha256
            .as_ref()
            .is_some_and(|value| !identifier(value, 64))
        || call
            .transport_sha256
            .as_ref()
            .is_some_and(|value| !identifier(value, 64))
        || call
            .logical_request_sha256
            .as_ref()
            .is_some_and(|value| !identifier(value, 64))
        || call.logical_plan_key.is_some() != call.logical_runtime.is_some()
        || call.dispatch_plan_key.is_some() != call.dispatch_runtime.is_some()
    {
        bail!("provider invocation metadata is invalid");
    }
    for runtime in [&call.logical_runtime, &call.dispatch_runtime]
        .into_iter()
        .flatten()
    {
        if [
            &runtime.provider_protocol,
            &runtime.target_model_id,
            &runtime.target_model_version,
            &runtime.price_table_version,
        ]
        .iter()
        .any(|v| v.trim().is_empty())
        {
            bail!("provider logical runtime is incomplete");
        }
    }
    for key in [
        &call.logical_plan_key,
        &call.dispatch_plan_key,
        &call.parent_plan_key,
    ]
    .into_iter()
    .flatten()
    {
        if !identifier(key.call_site_version.as_str(), 64)
            || !identifier(key.execution_plan_id.as_str(), 64)
        {
            bail!("provider logical plan identity is invalid");
        }
    }
    let json = encode(call)?;
    conn.execute("INSERT INTO provider_receipt_invocation(invocation_id,batch_id,item,logical_call,ordinal,invocation_json) VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT DO NOTHING",
        params![call.invocation_id,batch_id,call.position.item,call.position.call,call.ordinal,json])?;
    let stored: Option<(String,String)>=conn.query_row("SELECT batch_id,invocation_json FROM provider_receipt_invocation WHERE invocation_id=?1",[&call.invocation_id],|row| Ok((row.get(0)?,row.get(1)?))).optional()?;
    if stored != Some((batch_id.to_string(), json)) {
        bail!("provider invocation identity conflicts");
    }
    crate::provider_graph_sampling::record_dispatch(conn, batch_id, call)?;
    crate::provider_graph_campaign::record_dispatch(conn, batch_id, call)?;
    crate::provider_graph_acquisition::refresh_invocation(conn, batch_id, &call.invocation_id)?;
    Ok(())
}

pub fn finish_call(
    conn: &Connection,
    batch_id: &str,
    id: &str,
    completion: &Completion,
) -> Result<()> {
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        finish_call(&tx, batch_id, id, completion)?;
        tx.commit()?;
        return Ok(());
    }
    if [
        &completion.billing_scope_sha256,
        &completion.provider_generation_sha256,
        &completion.provider_request_id_sha256,
    ]
    .into_iter()
    .flatten()
    .any(|value| !identifier(value, 64))
    {
        bail!("provider billing identity is invalid");
    }
    if !completion.latency_ms.is_finite()
        || completion.latency_ms < 0.0
        || completion
            .cost_usd
            .is_some_and(|v| !v.is_finite() || v < 0.0)
        || completion
            .model_version
            .as_ref()
            .is_some_and(|v| v.trim().is_empty())
        || completion.exception_type.as_ref().is_some_and(|v| {
            v.is_empty()
                || v.len() > 256
                || !v
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.".contains(&b))
        })
        || matches!(completion.cost_source, InferenceCostSource::NoInference)
        || matches!(
            completion.cost_source,
            InferenceCostSource::ProviderReported
        ) && (completion.cost_usd.is_none()
            || completion.input_tokens.is_none()
            || completion.output_tokens.is_none())
        || completion.status == Status::Completed && completion.exception_type.is_some()
    {
        bail!("provider completion metadata is invalid");
    }
    let (json,old): (String,Option<String>)=conn.query_row("SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2",params![batch_id,id],|row| Ok((row.get(0)?,row.get(1)?))).context("provider invocation does not exist")?;
    let invocation: Invocation = serde_json::from_str(&json)?;
    if let Some(usage) = &completion.billing_usage {
        if completion.status != Status::Completed {
            bail!("provider billing usage requires an original completed SDK response");
        }
        usage.validate(
            &invocation.provider_protocol,
            completion.input_tokens,
            completion.output_tokens,
        )?;
    }
    if !invocation.transport_verified && completion.http_attempts.is_some() {
        bail!("HTTP attempt count is not attested by the dispatch transport");
    }
    let terminal = encode(completion)?;
    if old.as_ref().is_some_and(|old| old != &terminal) {
        bail!("provider completion conflicts with its durable receipt");
    }
    conn.execute("UPDATE provider_receipt_invocation SET completion_json=?1 WHERE invocation_id=?2 AND batch_id=?3 AND completion_json IS NULL",params![terminal,id,batch_id])?;
    crate::provider_graph_acquisition::refresh_invocation(conn, batch_id, id)?;
    Ok(())
}

pub fn summarize(conn: &Connection, id: &str) -> Result<Summary> {
    let (_, sealed) = load_batch(conn, id)?;
    let mut summary = Summary {
        version: VERSION,
        batch_id: id.into(),
        sealed,
        sdk_provider_invocations: 0,
        pending_invocations: 0,
        failed_invocations: 0,
        cancelled_invocations: 0,
        known_provider_cost_usd: 0.0,
        estimated_cost_usd: 0.0,
        unknown_cost_invocations: 0,
        unverified_transport_invocations: 0,
        input_tokens: 0,
        output_tokens: 0,
        known_http_attempts: 0,
        unknown_http_attempt_invocations: 0,
        summed_dispatch_latency_ms: 0.0,
        cost_complete: sealed,
    };
    let mut statement=conn.prepare("SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal")?;
    let rows = statement.query_map([id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
    })?;
    let mut roots = BTreeSet::new();
    let mut ordinal_floor = std::collections::BTreeMap::new();
    for row in rows {
        let (call, completion) = row?;
        let call: Invocation = serde_json::from_str(&call)?;
        let expected_ordinal = ordinal_floor.entry(call.position).or_insert(0_u32);
        summary.cost_complete &= call.ordinal == *expected_ordinal;
        *expected_ordinal = expected_ordinal.saturating_add(1);
        if matches!(call.role, Role::Primary | Role::Fallback) {
            roots.insert(call.position);
        }
        summary.sdk_provider_invocations += 1;
        summary.unverified_transport_invocations += u64::from(!call.transport_verified);
        summary.cost_complete &= call.transport_verified
            && call.transport_sha256.is_some()
            && call.logical_plan_key.is_some()
            && call.request_sha256.is_some();
        let Some(completion) = completion else {
            summary.pending_invocations += 1;
            summary.unknown_cost_invocations += 1;
            summary.unknown_http_attempt_invocations += 1;
            summary.cost_complete = false;
            continue;
        };
        let completion: Completion = serde_json::from_str(&completion)?;
        summary.failed_invocations += u64::from(completion.status == Status::Failed);
        summary.cancelled_invocations += u64::from(completion.status == Status::Cancelled);
        summary.summed_dispatch_latency_ms += completion.latency_ms;
        summary.input_tokens += u64::from(completion.input_tokens.unwrap_or(0));
        summary.output_tokens += u64::from(completion.output_tokens.unwrap_or(0));
        if let Some(count) = completion.http_attempts {
            summary.known_http_attempts += u64::from(count);
        } else {
            summary.unknown_http_attempt_invocations += 1;
        }
        match completion.cost_source {
            InferenceCostSource::ProviderReported => {
                summary.known_provider_cost_usd +=
                    completion.cost_usd.context("reported cost is missing")?
            }
            InferenceCostSource::Estimated => {
                summary.estimated_cost_usd += completion.cost_usd.unwrap_or(0.0);
            }
            _ => {}
        }
        summary.unknown_cost_invocations += u64::from(
            !call.transport_verified
                || completion.http_attempts != Some(1)
                || completion.status != Status::Completed
                || !matches!(
                    completion.cost_source,
                    InferenceCostSource::ProviderReported
                ),
        );
        summary.cost_complete &= completion.status == Status::Completed
            && matches!(
                completion.cost_source,
                InferenceCostSource::ProviderReported
            )
            && completion.http_attempts == Some(1);
    }
    let expected: Option<String> = conn.query_row(
        "SELECT expected_calls_json FROM provider_receipt_batch WHERE batch_id=?1",
        [id],
        |row| row.get(0),
    )?;
    let expected = expected
        .map(|value| serde_json::from_str::<Vec<CallPosition>>(&value))
        .transpose()?;
    summary.cost_complete &=
        expected.is_some_and(|calls| !calls.is_empty() && roots == calls.into_iter().collect());
    if !summary.known_provider_cost_usd.is_finite()
        || !summary.estimated_cost_usd.is_finite()
        || !summary.summed_dispatch_latency_ms.is_finite()
    {
        bail!("provider receipt totals overflow");
    }
    Ok(summary)
}

/// Match prepared original-request profiles to a complete durable SDK journal.
/// Extra probes, validation, fallback, missing bindings or different requests
/// cannot acquire evidence under the original-request execution contract.
pub fn verify_original_batch(
    conn: &Connection,
    id: &str,
    spec: &crate::multi_call::MultiCallPlanSpec,
    totals: &crate::multi_call::MultiCallTotals,
    requests: &std::collections::BTreeMap<CallPosition, String>,
) -> Result<()> {
    verify_original_evidence(conn, id, spec, totals, requests, true)
}

pub(crate) fn verify_original_evidence(
    conn: &Connection,
    id: &str,
    spec: &crate::multi_call::MultiCallPlanSpec,
    totals: &crate::multi_call::MultiCallTotals,
    requests: &std::collections::BTreeMap<CallPosition, String>,
    require_billing: bool,
) -> Result<()> {
    let summary = summarize(conn, id)?;
    if !summary.sealed
        || summary.pending_invocations != 0
        || summary.failed_invocations != 0
        || summary.cancelled_invocations != 0
        || summary.unverified_transport_invocations != 0
        || summary.unknown_http_attempt_invocations != 0
        || summary.known_http_attempts != spec.branches.len() as u64
        || require_billing && !summary.cost_complete
        || summary.sdk_provider_invocations != spec.branches.len() as u64
        || summary.input_tokens != u64::from(totals.input_tokens)
        || summary.output_tokens != u64::from(totals.output_tokens)
        || require_billing
            && (summary.known_provider_cost_usd - totals.cost_usd).abs()
                > 1e-12_f64.max(totals.cost_usd.abs() * 1e-12)
    {
        bail!("prepared batch provider journal is incomplete or its economics differ");
    }
    let expected: Option<String> = conn.query_row(
        "SELECT expected_calls_json FROM provider_receipt_batch WHERE batch_id=?1",
        [id],
        |row| row.get(0),
    )?;
    let expected: Vec<CallPosition> =
        serde_json::from_str(&expected.context("batch seal missing")?)?;
    if expected.into_iter().collect::<BTreeSet<_>>()
        != spec.branches.iter().map(|branch| branch.position).collect()
    {
        bail!("prepared batch seal differs from its original graph");
    }
    let (batch, _) = load_batch(conn, id)?;
    if batch.application_version.as_deref() != Some(spec.application_version.as_str())
        || batch.callback_version != spec.callback_version
        || batch.item_count != spec.item_count
    {
        bail!("prepared batch provider journal belongs to another application or callback");
    }
    let mut statement=conn.prepare("SELECT invocation_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal")?;
    let values = statement.query_map([id], |row| row.get::<_, String>(0))?;
    let mut branches = spec.branches.iter().collect::<Vec<_>>();
    branches.sort_by_key(|branch| branch.position);
    for (branch, value) in branches.into_iter().zip(values) {
        let call: Invocation = serde_json::from_str(&value?)?;
        let expected = crate::plan_profile::PlanProfileKey {
            call_site_version: branch.call_site_version.clone(),
            execution_plan_id: branch.plan.execution_plan_id()?,
        };
        let (protocol, model) = if branch.plan.provider_protocol == "litellm.completion.v1" {
            (
                "openai.chat.completions.v1",
                branch
                    .plan
                    .requested_model_id
                    .strip_prefix("openai/")
                    .context("prepared LiteLLM journal backend is unsupported")?,
            )
        } else {
            (
                branch.plan.provider_protocol.as_str(),
                branch.plan.requested_model_id.as_str(),
            )
        };
        if call.position != branch.position
            || call.ordinal != 0
            || call.role != Role::Primary
            || call.logical_plan_key.as_ref() != Some(&expected)
            || call.provider_protocol != protocol
            || call.requested_model_id != model
            || call.logical_request_sha256.as_ref() != requests.get(&branch.position)
            || call.request_sha256.is_none()
            || call.transport_sha256.is_none()
        {
            bail!("prepared batch provider journal does not match its original branch graph");
        }
    }
    Ok(())
}

pub fn request_fingerprint(call: &crate::Call) -> Option<String> {
    let context = call
        .parameters
        .extra
        .get(crate::model_catalog::ROUTE_CONTEXT_KEY)?;
    let protocol = context.get("provider_protocol")?;
    let controls = context.get(crate::model_catalog::PROVIDER_CONFIGURATION_KEY)?;
    let payload = context.get("sdk_response_cache")?;
    if controls.get("supported")?.as_bool() != Some(true)
        || payload.get("supported")?.as_bool() != Some(true)
    {
        return None;
    }
    Some(agentc_core::storage::content_hash(
        &agentc_core::storage::canonical_json(
            &serde_json::json!({"version":VERSION,"protocol":protocol,"model":call.model,"controls":controls,"payload":payload}),
        ),
    ))
}

/// Bind transient adapter metrics to each original completed SDK receipt,
/// including estimated usage, before retaining any deferred quality evidence.
pub(crate) fn verify_observed_outcomes(
    conn: &Connection,
    id: &str,
    outcomes: &[crate::multi_call::MultiCallOutcome],
) -> Result<()> {
    let mut query = conn.prepare("SELECT completion_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal")?;
    let rows = query
        .query_map([id], |row| row.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut outcomes = outcomes.iter().collect::<Vec<_>>();
    outcomes.sort_by_key(|outcome| outcome.position);
    if rows.len() != outcomes.len() {
        bail!("SDK outcomes do not cover the original graph");
    }
    for (json, measured) in rows.iter().zip(outcomes) {
        let completion: Completion = serde_json::from_str(json)?;
        if completion.status != Status::Completed
            || completion.http_attempts != Some(1)
            || completion.input_tokens != Some(measured.outcome.input_tokens)
            || completion.output_tokens != Some(measured.outcome.output_tokens)
            || completion.model_version != measured.outcome.executed_model_id
            || completion.cost_source != measured.cost_source
            || completion
                .cost_usd
                .is_some_and(|cost| cost != measured.outcome.cost_usd)
        {
            bail!("adapter outcome differs from its completed SDK receipt");
        }
    }
    Ok(())
}

pub fn handle_json(conn: &mut Connection, json: &str) -> Result<String> {
    let command: Command = serde_json::from_str(json)?;
    ensure_schema(conn)?;
    crate::provider_economics::ensure_schema(conn)?;
    crate::provider_reconciliation::ensure_schema(conn)?;
    crate::provider_tariff::ensure_schema(conn)?;
    crate::provider_graph_acquisition::ensure_schema(conn)?;
    if let Command::VerifyUnbudgetedGraph { batch_id } = &command {
        let tx = conn.unchecked_transaction()?;
        let (batch, _) = load_batch(&tx, batch_id)?;
        if batch.application_version.is_some() {
            crate::provider_graph_campaign::verify_driver(&tx, &batch, None)?;
        }
        tx.commit()?;
        return Ok(
            serde_json::json!({"status":"recorded","admission":"unbudgeted_driver_only"})
                .to_string(),
        );
    }
    if matches!(command, Command::TariffPolicy) {
        let sha = crate::provider_tariff::register(conn)?;
        return Ok(serde_json::json!({"status":"recorded","snapshot_sha256":sha,"snapshot":crate::provider_tariff::snapshot()}).to_string());
    }
    if let Command::TariffReport { batch_id } = &command {
        return Ok(serde_json::json!({"status":"recorded","tariff":crate::provider_tariff::report(conn,batch_id)?}).to_string());
    }
    if let Command::TariffAcquisition { call_site_version } = &command {
        return Ok(serde_json::json!({
            "status":"recorded",
            "acquisition":crate::provider_tariff::summarize_acquisition(conn, call_site_version)?
        })
        .to_string());
    }
    if let Command::BillingBindings { batch_id } = &command {
        return Ok(serde_json::json!({"status":"recorded","bindings":crate::provider_reconciliation::bindings(conn,batch_id)?}).to_string());
    }
    if let Command::GraphAcquisition { driver_id } = &command {
        return Ok(serde_json::json!({"status":"recorded","acquisition":crate::provider_graph_acquisition::summarize_driver(conn,driver_id)?}).to_string());
    }
    if let Command::GraphExecutionCosts { kind, execution_id } = &command {
        let projection = crate::provider_graph_economics::execution(conn, *kind, execution_id)?;
        return Ok(serde_json::json!({"status":"recorded","projection":projection,"admission":"financial_projection_only"}).to_string());
    }
    if let Command::GraphCostWindow {
        kind,
        key,
        runtime,
        window_size,
    } = &command
    {
        let window =
            crate::provider_graph_economics::window(conn, *kind, key, runtime, *window_size)?;
        return Ok(serde_json::json!({"status":"recorded","window":window,"admission":"financial_projection_only"}).to_string());
    }
    if let Command::GraphAcquisitionCosts { driver_id } = &command {
        let acquisition = crate::provider_graph_economics::summarize_acquisition(conn, driver_id)?;
        return Ok(serde_json::json!({"status":"recorded","acquisition":acquisition,"admission":"financial_projection_only"}).to_string());
    }
    if let Command::GraphAdmission { request } = &command {
        let decision = crate::provider_graph_admission::select(conn, request.clone())?;
        let canonical_evidence_json = decision.evidence_json()?;
        return Ok(serde_json::json!({"status":"recorded","decision":decision,"canonical_evidence_json":canonical_evidence_json,"admission":"evidence_selection_only"}).to_string());
    }
    if let Command::GraphCampaign { campaign_id } = &command {
        let campaign = crate::provider_graph_campaign::report(conn, campaign_id)?;
        return Ok(serde_json::json!({"status":"recorded","campaign":campaign,"admission":"commitment_accounting_only"}).to_string());
    }
    if let Command::BeginGraphDeployment { request } = &command {
        let outcome = crate::provider_graph_deployment::begin(
            conn,
            request.as_ref().clone(),
            crate::provider_graph_deployment::now_us()?,
        )?;
        let admission = match request.admission.kind {
            crate::provider_graph_economics::Kind::Primary => "guarded_primary_deployment",
            crate::provider_graph_economics::Kind::Roles => "guarded_role_deployment",
        };
        return Ok(
            serde_json::json!({"status":"recorded","outcome":outcome,"admission":admission})
                .to_string(),
        );
    }
    if let Command::FinishRoleGraph {
        lease_id,
        comparison,
    } = &command
    {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_micros()
            .min(i64::MAX as u128) as i64;
        let execution =
            crate::provider_graph_roles::finish(conn, lease_id, comparison.clone(), now)?;
        let admission = if crate::provider_graph_preparation::load(conn, lease_id)?
            .deployment_id
            .is_some()
        {
            "guarded_role_deployment"
        } else {
            "acquisition_only"
        };
        return Ok(
            serde_json::json!({"status":"recorded","execution":execution,"admission":admission})
                .to_string(),
        );
    }
    if let Command::RecordGraphValidation {
        lease_id,
        invocation_id,
        decision,
    } = &command
    {
        let verdict = crate::provider_graph_roles::record_validation(
            conn,
            lease_id,
            invocation_id,
            *decision,
        )?;
        return Ok(serde_json::json!({"status":"recorded","verdict":verdict,"admission":"observation_only"}).to_string());
    }
    if let Command::RoleGraphExecution { execution_id } = &command {
        let execution = crate::provider_graph_roles::load(conn, execution_id)?;
        let admission = if crate::provider_graph_preparation::load(conn, &execution.lease_id)?
            .deployment_id
            .is_some()
        {
            "guarded_role_deployment"
        } else {
            "acquisition_only"
        };
        return Ok(
            serde_json::json!({"status":"recorded","execution":execution,"admission":admission})
                .to_string(),
        );
    }
    if let Command::FinishBillingLookup {
        attempt_id,
        latency_ms,
        invoice,
    } = &command
    {
        crate::provider_reconciliation::reserve_result(
            conn,
            attempt_id,
            *latency_ms,
            invoice.as_ref(),
        )?;
    }
    let transaction = conn.transaction()?;
    let mut report = false;
    let id = match command {
        Command::BillingBindings { .. }
        | Command::GraphAcquisition { .. }
        | Command::FinishRoleGraph { .. }
        | Command::RoleGraphExecution { .. }
        | Command::RecordGraphValidation { .. }
        | Command::TariffPolicy
        | Command::TariffReport { .. }
        | Command::TariffAcquisition { .. }
        | Command::GraphExecutionCosts { .. }
        | Command::GraphCostWindow { .. }
        | Command::GraphAcquisitionCosts { .. }
        | Command::GraphAdmission { .. }
        | Command::VerifyUnbudgetedGraph { .. }
        | Command::BeginGraphDeployment { .. } => unreachable!(),
        Command::BeginBillingLookup {
            batch_id,
            invocation_id,
            attempt_id,
            receipt_sha256,
        } => {
            crate::provider_reconciliation::begin_lookup(
                &transaction,
                &batch_id,
                &invocation_id,
                &attempt_id,
                &receipt_sha256,
            )?;
            batch_id
        }
        Command::FinishBillingLookup {
            attempt_id,
            latency_ms,
            invoice,
        } => {
            crate::provider_reconciliation::finish_lookup(
                &transaction,
                &attempt_id,
                latency_ms,
                invoice.as_ref(),
            )?;
            transaction.commit()?;
            return Ok(serde_json::json!({"status":"recorded"}).to_string());
        }
        Command::BeginBatch { batch } => {
            begin_batch(&transaction, &batch)?;
            batch.batch_id
        }
        Command::BeginCall {
            batch_id,
            invocation,
        } => {
            begin_call(&transaction, &batch_id, &invocation)?;
            crate::provider_economics::refresh_invocation(
                &transaction,
                &batch_id,
                &invocation.invocation_id,
            )?;
            if invocation.tariff_declaration.is_some() {
                crate::provider_tariff::refresh(
                    &transaction,
                    &batch_id,
                    &invocation.invocation_id,
                )?;
            }
            crate::provider_tariff::refresh_batch_coverage(&transaction, &batch_id)?;
            batch_id
        }
        Command::FinishCall {
            batch_id,
            invocation_id,
            completion,
        } => {
            finish_call(&transaction, &batch_id, &invocation_id, &completion)?;
            crate::provider_economics::refresh_invocation(&transaction, &batch_id, &invocation_id)?;
            let active: bool = transaction.query_row(
                "SELECT EXISTS(SELECT 1 FROM provider_tariff_invocation WHERE invocation_id=?1)",
                [&invocation_id],
                |row| row.get(0),
            )?;
            if active {
                crate::provider_tariff::refresh(&transaction, &batch_id, &invocation_id)?;
            }
            batch_id
        }
        Command::SealBatch {
            batch_id,
            mut logical_calls,
            persistence_failed,
        } => {
            let (batch, _) = load_batch(&transaction, &batch_id)?;
            logical_calls.sort();
            if logical_calls.iter().any(|p| p.item >= batch.item_count)
                || logical_calls.windows(2).any(|pair| pair[0] == pair[1])
            {
                bail!("provider batch logical coverage is invalid");
            }
            let expected = encode(&logical_calls)?;
            let old: Option<String> = transaction.query_row(
                "SELECT expected_calls_json FROM provider_receipt_batch WHERE batch_id=?1",
                [&batch_id],
                |row| row.get(0),
            )?;
            if old.is_some_and(|old| old != expected) {
                bail!("provider batch foreground coverage conflicts");
            }
            transaction.execute(
                "UPDATE provider_receipt_batch SET sealed=1,expected_calls_json=?1 WHERE batch_id=?2",
                params![expected,batch_id],
            )?;
            crate::provider_economics::seal_batch(&transaction, &batch_id, persistence_failed)?;
            crate::provider_graph_acquisition::seal(&transaction, &batch_id, persistence_failed)?;
            crate::provider_tariff::seal(&transaction, &batch_id, persistence_failed)?;
            crate::provider_tariff::refresh_batch_coverage(&transaction, &batch_id)?;
            report = true;
            batch_id
        }
        Command::FinishBatchEconomics {
            batch_id,
            wall_latency_ms,
        } => {
            let original_owner: bool = transaction.query_row(
                "SELECT EXISTS(SELECT 1 FROM provider_economic_batch WHERE batch_id=?1)",
                [&batch_id],
                |r| r.get(0),
            )?;
            if original_owner || !crate::provider_graph_acquisition::owned(&transaction, &batch_id)?
            {
                crate::provider_economics::finish_batch(&transaction, &batch_id, wall_latency_ms)?;
            }
            crate::provider_graph_acquisition::finish_foreground(
                &transaction,
                &batch_id,
                wall_latency_ms,
            )?;
            batch_id
        }
        Command::GraphCampaign { .. } => {
            unreachable!("campaign reports return before receipt mutations")
        }
        Command::Report { batch_id } => {
            report = true;
            batch_id
        }
    };
    let summary = if report {
        Some(summarize(&transaction, &id)?)
    } else {
        None
    };
    let acquisition = if report {
        crate::provider_graph_acquisition::report_batch(&transaction, &id)?
    } else {
        None
    };
    transaction.commit()?;
    if let Some(summary) = summary {
        Ok(serde_json::json!({"status":"recorded","summary":summary,"graph_acquisition":acquisition}).to_string())
    } else {
        Ok(serde_json::json!({"status":"recorded"}).to_string())
    }
}
