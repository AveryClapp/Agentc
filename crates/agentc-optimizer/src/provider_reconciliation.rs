//! Immutable verified billing overlays. Original SDK receipts and quality
//! observations remain unchanged; an invoice can settle only one invocation.

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::provider_receipts::{Completion, Invocation};

pub const ISSUER: &str = "openrouter-generation-metadata-v1";

#[derive(Debug, Clone, Serialize)]
pub struct Binding {
    pub batch_id: String,
    pub invocation_id: String,
    pub receipt_sha256: String,
    pub provider_protocol: String,
    pub requested_model_id: String,
    pub model_version: Option<String>,
    pub billing_scope_sha256: Option<String>,
    pub generation_sha256: Option<String>,
    pub request_id_sha256: Option<String>,
    pub input_tokens: Option<u32>,
    pub output_tokens: Option<u32>,
    pub settled_cost_usd: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Invoice {
    pub issuer: String,
    pub receipt_sha256: String,
    pub billing_scope_sha256: String,
    pub generation_sha256: String,
    pub request_id_sha256: Option<String>,
    pub model_id: String,
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub cost_usd: f64,
    pub cancelled: bool,
    pub finish_reason: Option<String>,
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_billing_attempt (
           attempt_id TEXT PRIMARY KEY, invocation_id TEXT NOT NULL,
           batch_id TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, result_json TEXT, result_input_json TEXT,
           FOREIGN KEY(invocation_id) REFERENCES provider_receipt_invocation(invocation_id)
         );
         CREATE UNIQUE INDEX IF NOT EXISTS provider_billing_pending
           ON provider_billing_attempt(invocation_id) WHERE result_json IS NULL;
         CREATE INDEX IF NOT EXISTS provider_billing_lookup_custody
           ON provider_billing_attempt(invocation_id,batch_id,receipt_sha256);
         CREATE TABLE IF NOT EXISTS provider_billing_settlement (
           invocation_id TEXT PRIMARY KEY, issuer TEXT NOT NULL,
           generation_sha256 TEXT NOT NULL, invoice_json TEXT NOT NULL,
           UNIQUE(issuer,generation_sha256),
           FOREIGN KEY(invocation_id) REFERENCES provider_receipt_invocation(invocation_id)
         );",
    )?;
    let has_input: bool = conn.query_row("SELECT EXISTS(SELECT 1 FROM pragma_table_info('provider_billing_attempt') WHERE name='result_input_json')",[],|row|row.get(0))?;
    if !has_input {
        conn.execute_batch(
            "ALTER TABLE provider_billing_attempt ADD COLUMN result_input_json TEXT;",
        )?;
    }
    Ok(())
}

fn load(conn: &Connection, batch: &str, id: &str) -> Result<(Invocation, Completion, String)> {
    let (invocation,completion):(String,Option<String>)=conn.query_row(
        "SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2",
        params![batch,id],|row|Ok((row.get(0)?,row.get(1)?))
    )?;
    let completion =
        completion.context("billing reconciliation requires a terminal SDK receipt")?;
    let hash = agentc_core::storage::content_hash(&agentc_core::storage::canonical_json(
        &serde_json::json!({
            "version":"provider-billing-binding-v1","batch_id":batch,"invocation_id":id,
            "invocation":invocation,"completion":completion,
        }),
    ));
    Ok((
        serde_json::from_str(&invocation)?,
        serde_json::from_str(&completion)?,
        hash,
    ))
}

pub fn settled_cost(conn: &Connection, id: &str) -> Result<Option<f64>> {
    let row: Option<(String, String, String, String)> = conn
        .query_row(
            "SELECT s.issuer,s.generation_sha256,s.invoice_json,r.batch_id FROM provider_billing_settlement s JOIN provider_receipt_invocation r USING(invocation_id) WHERE s.invocation_id=?1",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;
    let Some((issuer, generation, json, batch)) = row else {
        return Ok(None);
    };
    let invoice: Invoice = serde_json::from_str(&json)?;
    if issuer != invoice.issuer
        || generation != invoice.generation_sha256
        || validated_invoice(conn, &batch, id, &invoice.receipt_sha256, &invoice)? != json
    {
        bail!("billing settlement differs from its original verified invoice");
    }
    // The reserved authenticated input owns the monetary evidence. It is also
    // available while finish_lookup atomically refreshes its derived ledgers.
    let inputs = {
        let mut query = conn.prepare("SELECT result_input_json FROM provider_billing_attempt WHERE invocation_id=?1 AND batch_id=?2 AND receipt_sha256=?3 AND result_input_json IS NOT NULL AND (result_json IS NULL OR result_json=result_input_json)")?;
        let rows = query
            .query_map(params![id, batch, invoice.receipt_sha256], |r| {
                r.get::<_, String>(0)
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let mut matched = false;
    for raw in inputs {
        let input: ResultInput = serde_json::from_str(&raw)?;
        if input.latency_ms.is_finite()
            && input.latency_ms >= 0.
            && input
                .invoice
                .as_ref()
                .map(serde_json::to_value)
                .transpose()?
                == Some(serde_json::to_value(&invoice)?)
        {
            matched = true;
        }
    }
    if !matched {
        bail!("billing settlement has no matching verified lookup input");
    }
    Ok(Some(invoice.cost_usd))
}

pub fn bindings(conn: &Connection, batch: &str) -> Result<Vec<Binding>> {
    ensure_schema(conn)?;
    let ids = {
        let mut query=conn.prepare("SELECT invocation_id FROM provider_receipt_invocation WHERE batch_id=?1 AND completion_json IS NOT NULL ORDER BY item,logical_call,ordinal")?;
        let rows = query
            .query_map([batch], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    ids.into_iter()
        .map(|id| {
            let (invocation, completion, hash) = load(conn, batch, &id)?;
            Ok(Binding {
                batch_id: batch.into(),
                invocation_id: id.clone(),
                receipt_sha256: hash,
                provider_protocol: invocation.provider_protocol,
                requested_model_id: invocation.requested_model_id,
                model_version: completion.model_version,
                billing_scope_sha256: completion.billing_scope_sha256,
                generation_sha256: completion.provider_generation_sha256,
                request_id_sha256: completion.provider_request_id_sha256,
                input_tokens: completion.input_tokens,
                output_tokens: completion.output_tokens,
                settled_cost_usd: settled_cost(conn, &id)?,
            })
        })
        .collect()
}

pub fn openrouter_scope() -> String {
    agentc_core::storage::content_hash("billing-scope-v1\0https://openrouter.ai/api/v1")
}

fn digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub(crate) fn begin_lookup(
    conn: &Connection,
    batch: &str,
    id: &str,
    attempt: &str,
    hash: &str,
) -> Result<()> {
    if attempt.len() != 32
        || !attempt
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        bail!("billing attempt identity is invalid");
    }
    let (invocation, completion, binding) = load(conn, batch, id)?;
    let owned: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_economic_batch WHERE batch_id=?1)",
        [batch],
        |row| row.get(0),
    )?;
    if binding != hash
        || !(owned || crate::provider_graph_acquisition::report_batch(conn, batch)?.is_some())
        || invocation.provider_protocol != "openai.chat.completions.v1"
        || !invocation.transport_verified
        || completion.http_attempts != Some(1)
        || completion.billing_scope_sha256.as_deref() != Some(openrouter_scope().as_str())
        || completion.provider_generation_sha256.is_none()
    {
        bail!("billing lookup does not match a verified single-send receipt");
    }
    let inserted=conn.execute("INSERT INTO provider_billing_attempt(attempt_id,invocation_id,batch_id,receipt_sha256) VALUES(?1,?2,?3,?4) ON CONFLICT(attempt_id) DO NOTHING",params![attempt,id,batch,hash])?;
    let old:(String,String,String,Option<String>,Option<String>)=conn.query_row("SELECT invocation_id,batch_id,receipt_sha256,result_json,result_input_json FROM provider_billing_attempt WHERE attempt_id=?1",[attempt],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?)))?;
    if old.0 != id || old.1 != batch || old.2 != hash {
        bail!("billing lookup identity conflicts");
    }
    if old.3.is_some() || old.4.is_some() {
        bail!("billing lookup was already completed");
    }
    if inserted == 1 {
        conn.execute("UPDATE provider_economic_cohort SET pending_billing_verifications=pending_billing_verifications+1 WHERE owner_call_site_version=(SELECT owner_call_site_version FROM provider_economic_batch WHERE batch_id=?1)",[batch])?;
    }
    Ok(())
}

pub(crate) fn finish_lookup(
    conn: &Connection,
    attempt: &str,
    latency: f64,
    invoice: Option<&Invoice>,
) -> Result<()> {
    if !latency.is_finite() || latency < 0.0 {
        bail!("billing lookup time is invalid");
    }
    let (id,batch,hash,prior):(String,String,String,Option<String>)=conn.query_row("SELECT invocation_id,batch_id,receipt_sha256,result_json FROM provider_billing_attempt WHERE attempt_id=?1",[attempt],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?)))?;
    let result = agentc_core::storage::canonical_json(
        &serde_json::json!({"latency_ms":latency,"invoice":invoice}),
    );
    let input: Option<String> = conn.query_row(
        "SELECT result_input_json FROM provider_billing_attempt WHERE attempt_id=?1",
        [attempt],
        |row| row.get(0),
    )?;
    if input.as_ref() != Some(&result) {
        bail!("billing lookup verified input is missing or changed");
    }
    if let Some(prior) = prior {
        if prior != result {
            bail!("billing lookup completion conflicts");
        }
        return Ok(());
    }
    if let Some(invoice) = invoice {
        let json = validated_invoice(conn, &batch, &id, &hash, invoice)?;
        conn.execute("INSERT INTO provider_billing_settlement VALUES(?1,?2,?3,?4) ON CONFLICT(invocation_id) DO NOTHING",params![id,invoice.issuer,invoice.generation_sha256,json])?;
        let old: String = conn.query_row(
            "SELECT invoice_json FROM provider_billing_settlement WHERE invocation_id=?1",
            [&id],
            |row| row.get(0),
        )?;
        if old != json {
            bail!("billing settlement conflicts with its immutable invoice");
        }
        crate::provider_economics::refresh_invocation(conn, &batch, &id)?;
        crate::provider_graph_acquisition::refresh_invocation(conn, &batch, &id)?;
    }
    conn.execute("UPDATE provider_billing_attempt SET result_json=?1 WHERE attempt_id=?2 AND result_json IS NULL",params![result,attempt])?;
    conn.execute("UPDATE provider_economic_cohort SET pending_billing_verifications=pending_billing_verifications-1,acquisition_latency_ms=acquisition_latency_ms+?1 WHERE owner_call_site_version=(SELECT owner_call_site_version FROM provider_economic_batch WHERE batch_id=?2)",params![latency,batch])?;
    Ok(())
}

fn validated_invoice(
    conn: &Connection,
    batch: &str,
    id: &str,
    hash: &str,
    invoice: &Invoice,
) -> Result<String> {
    let (invocation, completion, binding) = load(conn, batch, id)?;
    let reported =
        if completion.cost_source == crate::multi_call::InferenceCostSource::ProviderReported {
            completion.cost_usd.unwrap_or(0.0)
        } else {
            0.0
        };
    let model_matches = invoice.model_id == invocation.requested_model_id
        || completion.model_version.as_ref() == Some(&invoice.model_id)
        || invoice.model_id.strip_prefix("openai/") == Some(invocation.requested_model_id.as_str());
    if invoice.issuer != ISSUER
        || (!invoice.cancelled
            && invoice
                .finish_reason
                .as_ref()
                .is_none_or(|reason| reason.is_empty()))
        || invoice.finish_reason.as_ref().is_some_and(|reason| {
            reason.len() > 64
                || !reason
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        })
        || invoice.receipt_sha256 != hash
        || binding != hash
        || !digest(&invoice.generation_sha256)
        || !digest(&invoice.billing_scope_sha256)
        || invoice
            .request_id_sha256
            .as_ref()
            .is_some_and(|value| !digest(value))
        || invoice.model_id.len() > 256
        || !invoice
            .model_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"/._:-".contains(&b))
        || completion.provider_generation_sha256.as_ref() != Some(&invoice.generation_sha256)
        || completion.billing_scope_sha256.as_ref() != Some(&invoice.billing_scope_sha256)
        || completion
            .provider_request_id_sha256
            .as_ref()
            .is_some_and(|id| Some(id) != invoice.request_id_sha256.as_ref())
        || !model_matches
        || !invoice.cost_usd.is_finite()
        || invoice.cost_usd < reported
        || completion
            .input_tokens
            .is_some_and(|n| n != invoice.input_tokens)
        || completion
            .output_tokens
            .is_some_and(|n| n != invoice.output_tokens)
    {
        bail!("billing invoice does not match its SDK receipt or known charges");
    }
    // Already complete response costs cannot be silently repriced: that
    // requires separate pricing-profile invalidation, not this hold resolver.
    if completion.status == crate::provider_receipts::Status::Completed
        && completion.cost_source == crate::multi_call::InferenceCostSource::ProviderReported
        && invoice.cost_usd != reported
    {
        bail!("billing correction requires pricing-profile invalidation");
    }
    let json = agentc_core::storage::canonical_json(&serde_json::to_value(invoice)?);

    let claimed:Option<String>=conn.query_row("SELECT invocation_id FROM provider_billing_settlement WHERE issuer=?1 AND generation_sha256=?2",params![invoice.issuer,invoice.generation_sha256],|row|row.get(0)).optional()?;
    if claimed.is_some_and(|owner| owner != id) {
        bail!("generation bill was already assigned to another invocation");
    }
    Ok(json)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResultInput {
    latency_ms: f64,
    invoice: Option<Invoice>,
}

/// Persist authenticated, validated inputs before the atomic accounting write.
/// A restart can finish this input without another provider request.
pub(crate) fn reserve_result(
    conn: &mut Connection,
    attempt: &str,
    latency: f64,
    invoice: Option<&Invoice>,
) -> Result<()> {
    if !latency.is_finite() || latency < 0.0 {
        bail!("billing lookup time is invalid");
    }
    let transaction = conn.transaction()?;
    let (id,batch,hash,prior,input):(String,String,String,Option<String>,Option<String>)=transaction.query_row("SELECT invocation_id,batch_id,receipt_sha256,result_json,result_input_json FROM provider_billing_attempt WHERE attempt_id=?1",[attempt],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?)))?;
    let result = agentc_core::storage::canonical_json(
        &serde_json::json!({"latency_ms":latency,"invoice":invoice}),
    );
    if prior.as_ref().is_some_and(|old| old != &result)
        || input.as_ref().is_some_and(|old| old != &result)
    {
        bail!("billing lookup verified input conflicts");
    }
    if let Some(invoice) = invoice {
        validated_invoice(&transaction, &batch, &id, &hash, invoice)?;
    }
    transaction.execute("UPDATE provider_billing_attempt SET result_input_json=?1 WHERE attempt_id=?2 AND result_input_json IS NULL",params![result,attempt])?;
    transaction.commit()?;
    Ok(())
}

/// Recover only completed verification inputs. An interrupted lookup without
/// verified inputs retains its pending hold; restart never invents a bill.
pub fn recover_pending(conn: &mut Connection) -> Result<usize> {
    crate::provider_economics::ensure_schema(conn)?;
    let pending = {
        let mut query=conn.prepare("SELECT attempt_id,result_input_json FROM provider_billing_attempt WHERE result_json IS NULL AND result_input_json IS NOT NULL")?;
        let rows = query
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let count = pending.len();
    for (attempt, input) in pending {
        let input: ResultInput = serde_json::from_str(&input)?;
        let transaction = conn.transaction()?;
        finish_lookup(
            &transaction,
            &attempt,
            input.latency_ms,
            input.invoice.as_ref(),
        )?;
        transaction.commit()?;
    }
    Ok(count)
}
