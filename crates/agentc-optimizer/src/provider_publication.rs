//! Recoverable publication of content-free provider accounting into profiles.
//!
//! The input reservation is durable first. Profile, spend, comparison binding
//! and final result then commit together. Recovery never dispatches requests.

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::execution_plan::ExecutionPlanId;
use crate::multi_call::MultiCallTotals;
use crate::multi_call::{
    CallPosition, InferenceCostSource, MultiCallPlanSpec, MultiCallRuntimeBranch,
};
use crate::multi_call_comparison::{self, ComparisonBinding};
use crate::plan_profile::{
    PlanObservationToken, PlanProfileUpdate, PlanProfiles, PublicationBoundary,
};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScalarProvenance {
    pub position: CallPosition,
    pub cost_source: InferenceCostSource,
    pub observation: Option<PlanObservationToken>,
}

/// Frozen original graph and runtime, with no prompts or response content.
/// Existing scalar observations retain their original estimated provenance;
/// resolving this batch never re-observes or reprices those executions.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeferredEvidence {
    pub spec: MultiCallPlanSpec,
    pub runtimes: Vec<MultiCallRuntimeBranch>,
    pub request_fingerprints: Vec<(CallPosition, String)>,
    pub scalar_provenance: Vec<ScalarProvenance>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ComparisonInput {
    pub reference_plan_id: ExecutionPlanId,
    pub max_workers: u32,
    pub input_fingerprint: String,
    pub output_fingerprint: String,
}

/// Native-derived identities and metrics only; no source calls or responses.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicationInput {
    pub version: String,
    pub journal_sha256: String,
    pub predecessor: PublicationBoundary,
    pub retained_window_size: usize,
    pub update: PlanProfileUpdate,
    pub totals: MultiCallTotals,
    pub charge_acquisition: bool,
    pub comparison: Option<ComparisonInput>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deferred_evidence: Option<DeferredEvidence>,
    /// Original graph custody also applies to immediately reported costs.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub original_evidence: Option<DeferredEvidence>,
}

impl PublicationInput {
    pub(crate) fn original_graph(&self) -> Option<&DeferredEvidence> {
        self.original_evidence
            .as_ref()
            .or(self.deferred_evidence.as_ref())
    }
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    crate::provider_receipts::ensure_schema(conn)?;
    crate::provider_reconciliation::ensure_schema(conn)?;
    multi_call_comparison::ensure_schema(conn)?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_receipt_publication_input (
           batch_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, input_json TEXT NOT NULL,
           FOREIGN KEY(batch_id) REFERENCES provider_receipt_profile_publication(batch_id)
         );
         CREATE INDEX IF NOT EXISTS provider_receipt_pending_publication
           ON provider_receipt_profile_publication(result_json) WHERE result_json IS NULL;",
    )?;
    Ok(())
}

/// Hash the exact content-free physical journal, including its foreground seal.
pub fn journal_fingerprint(conn: &Connection, id: &str) -> Result<String> {
    let header: (String, bool, Option<String>) = conn.query_row(
        "SELECT metadata_json,sealed,expected_calls_json FROM provider_receipt_batch WHERE batch_id=?1",
        [id], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )?;
    let mut query = conn.prepare(
        "SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal",
    )?;
    let calls = query
        .query_map([id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(agentc_core::storage::content_hash(
        &agentc_core::storage::canonical_json(&serde_json::json!([header, calls])),
    ))
}

fn encode(input: &PublicationInput) -> Result<String> {
    Ok(agentc_core::storage::canonical_json(&serde_json::to_value(
        input,
    )?))
}

pub(crate) fn load_input(conn: &Connection, id: &str) -> Result<PublicationInput> {
    let row: Option<(String, String)> = conn.query_row(
        "SELECT input_sha256,input_json FROM provider_receipt_publication_input WHERE batch_id=?1",
        [id], |row| Ok((row.get(0)?, row.get(1)?)),
    ).optional()?;
    let (hash, json) = row.context("legacy pending publication has no recovery input")?;
    if agentc_core::storage::content_hash(&json) != hash {
        bail!("provider publication input fingerprint is invalid");
    }
    let input: PublicationInput = serde_json::from_str(&json)?;
    if input.version != "provider-profile-publication-v1"
        || input.update.observed_at_us.is_none()
        || input.update.divergence.is_some()
        || input.update.dispatch_fallback
        || input.totals.input_tokens != input.update.input_tokens
        || input.totals.output_tokens != input.update.output_tokens
        || input.totals.cost_usd != input.update.cost_usd
        || input.totals.wall_latency_ms != input.update.latency_ms
    {
        bail!("provider publication input is invalid");
    }
    if input.original_evidence.is_some() && input.deferred_evidence.is_some() {
        bail!("original graph has conflicting custody declarations");
    }
    if let Some(evidence) = input.original_graph() {
        let mut reference = evidence.spec.clone();
        reference.max_workers = 1;
        let owner = reference.profile_key().map_err(anyhow::Error::msg)?;
        let actual: (String, String) = conn.query_row(
            "SELECT owner_call_site_version,reference_plan_id FROM provider_economic_batch WHERE batch_id=?1",
            [id], |row| Ok((row.get(0)?, row.get(1)?)),
        )?;
        if evidence.spec.profile_key().map_err(anyhow::Error::msg)? != input.update.key
            || crate::multi_call::profile_runtime(&evidence.spec, &evidence.runtimes)
                .map_err(anyhow::Error::msg)?
                != input.update.runtime_version
            || evidence.spec.execution_contract.as_deref()
                != Some(crate::multi_call::PREPARED_PROVIDER_BATCH_VERSION)
            || evidence.scalar_provenance.len() != evidence.spec.branches.len()
            || actual
                != (
                    owner.call_site_version.to_string(),
                    owner.execution_plan_id.to_string(),
                )
        {
            bail!("deferred publication graph or runtime is invalid");
        }
    }
    Ok(input)
}

/// Read costs only from the unchanged SDK journal or its verified overlay.
/// An invoice supplies no execution, runtime, output, or quality evidence.
fn resolved_cost(conn: &Connection, id: &str) -> Result<Option<f64>> {
    let pending: u64 = conn.query_row(
        "SELECT COUNT(*) FROM provider_billing_attempt WHERE batch_id=?1 AND result_json IS NULL",
        [id],
        |row| row.get(0),
    )?;
    if pending != 0 {
        return Ok(None);
    }
    let mut query = conn.prepare(
        "SELECT invocation_id,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal",
    )?;
    let rows = query.query_map([id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut total = 0.0;
    for row in rows {
        let (invocation, json) = row?;
        let completion: crate::provider_receipts::Completion = serde_json::from_str(&json)?;
        let cost = if completion.cost_source == InferenceCostSource::ProviderReported {
            completion.cost_usd
        } else {
            crate::provider_reconciliation::settled_cost(conn, &invocation)?
        };
        let Some(cost) = cost else {
            return Ok(None);
        };
        total += cost;
        if !total.is_finite() || cost < 0.0 {
            bail!("resolved batch cost is invalid");
        }
    }
    Ok(Some(total))
}

/// Exact retries reuse the original timestamp and predecessor. Historical
/// pending reservations without inputs remain quarantined rather than guessed.
pub fn reserve(
    conn: &mut Connection,
    id: &str,
    receipt: &str,
    input: &PublicationInput,
) -> Result<Option<String>> {
    ensure_schema(conn)?;
    let hash = agentc_core::storage::content_hash(receipt);
    let transaction = conn.transaction()?;
    let prior: Option<(String, Option<String>)> = transaction.query_row(
        "SELECT receipt_sha256,result_json FROM provider_receipt_profile_publication WHERE batch_id=?1",
        [id], |row| Ok((row.get(0)?, row.get(1)?)),
    ).optional()?;
    if let Some((old, result)) = prior {
        if old != hash {
            bail!("provider journal was already bound to a different batch receipt");
        }
        if result.is_none() {
            load_input(&transaction, id)?;
        }
        return Ok(result);
    }
    let json = encode(input)?;
    transaction.execute(
        "INSERT INTO provider_receipt_profile_publication(batch_id,receipt_sha256) VALUES(?1,?2)",
        params![id, hash],
    )?;
    transaction.execute(
        "INSERT INTO provider_receipt_publication_input VALUES(?1,?2,?3)",
        params![id, agentc_core::storage::content_hash(&json), json],
    )?;
    load_input(&transaction, id)?;
    transaction.commit()?;
    Ok(None)
}

pub fn publish(conn: &mut Connection, profiles: &PlanProfiles, id: &str) -> Result<String> {
    let result: Option<String> = conn.query_row(
        "SELECT result_json FROM provider_receipt_profile_publication WHERE batch_id=?1",
        [id],
        |row| row.get(0),
    )?;
    if let Some(result) = result {
        return Ok(result);
    }
    let mut input = load_input(conn, id)?;
    if profiles.publication_boundary(&input.update.key) != input.predecessor
        || profiles.retained_window_size() != input.retained_window_size
        || journal_fingerprint(conn, id)? != input.journal_sha256
    {
        let result = serde_json::json!({"status":"abstained","retired":true,"reason":"provider publication predecessor or physical journal has retired"}).to_string();
        crate::provider_receipts::finish_publication(conn, id, &result)?;
        return Ok(result);
    }
    let original_execution = crate::provider_execution::register(conn, id)?;
    crate::provider_interval_profiles::publish(conn, id)?;
    let cost_source = if let Some(evidence) = &input.deferred_evidence {
        let requests = evidence.request_fingerprints.iter().cloned().collect();
        crate::provider_receipts::verify_original_evidence(
            conn,
            id,
            &evidence.spec,
            &input.totals,
            &requests,
            false,
        )?;
        let Some(cost) = resolved_cost(conn, id)? else {
            return Ok(serde_json::json!({"status":"pending_billing","reason":"original execution retained; verified provider costs required"}).to_string());
        };
        input.update.cost_usd = cost;
        input.totals.cost_usd = cost;
        input.update.cost_evidence.source =
            crate::cost_evidence::CostSource::ProviderBillingVerified;
        "provider_billing_verified"
    } else {
        // Legacy recovery inputs did not carry provenance. Only the original
        // complete raw SDK journal can establish reported cost for them.
        if crate::provider_receipts::summarize(conn, id)?.cost_complete {
            input.update.cost_evidence.source = crate::cost_evidence::CostSource::ProviderReported;
        }
        "provider_reported"
    };
    input.update.cost_evidence.physical_invocations = {
        let mut query = conn.prepare("SELECT invocation_id FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal")?;
        let values = query
            .query_map([id], |row| {
                Ok(crate::cost_evidence::PhysicalInvocation {
                    batch_id: id.into(),
                    invocation_id: row.get(0)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        values
    };
    profiles.publish_atomic(
        conn,
        &input.predecessor,
        input.update.clone(),
        |database, staged, observation| {
            if journal_fingerprint(database, id)? != input.journal_sha256 {
                bail!("provider journal changed during publication");
            }
            if input.deferred_evidence.is_some()
                && resolved_cost(database, id)? != Some(input.update.cost_usd)
            {
                bail!("verified provider costs changed during publication");
            }
            if input.charge_acquisition {
                multi_call_comparison::record_acquisition_in_transaction(
                    database,
                    staged,
                    observation,
                )?;
                crate::provider_economics::cover_acquisition(database, id, observation)?;
            }
            let comparison_token = if let Some(comparison) = &input.comparison {
                Some(multi_call_comparison::register_binding_in_transaction(
                    database,
                    staged,
                    &ComparisonBinding {
                        observation: observation.clone(),
                        reference_plan_id: comparison.reference_plan_id.clone(),
                        max_workers: comparison.max_workers,
                        input_fingerprint: comparison.input_fingerprint.clone(),
                        output_fingerprint: comparison.output_fingerprint.clone(),
                        cost_usd: input.update.cost_usd,
                        latency_ms: input.update.latency_ms,
                        observed_at_us: input
                            .update
                            .observed_at_us
                            .context("publication time missing")?,
                        comparison_version: crate::multi_call::EXACT_MAP_COMPARISON_VERSION.into(),
                    },
                )?)
            } else {
                None
            };
            if let (Some(execution), Some(token)) = (&original_execution, &comparison_token) {
                crate::provider_execution::bind_usd_comparison(
                    database,
                    &execution.execution_id,
                    token,
                )?;
            }
            let mut result = serde_json::json!({
                "status":"recorded", "admission":"observe_only",
                "key":observation.key(), "runtime_version":observation.runtime_version(),
                "totals":input.totals, "comparison_token":comparison_token,
                "cost_source":cost_source,
            });
            if let Some(execution) = &original_execution {
                result["original_execution_id"] = serde_json::json!(execution.execution_id);
            }
            let result = result.to_string();
            crate::provider_receipts::finish_publication(database, id, &result)?;
            Ok(result)
        },
    )
}

/// Startup runs this before comparison feedback recovery or new batch work.
/// Storage errors leave the reservation pending for the next retry.
pub fn recover_pending(conn: &mut Connection, profiles: &PlanProfiles) -> Result<usize> {
    ensure_schema(conn)?;
    let ids = {
        let mut query = conn.prepare(
            "SELECT p.batch_id FROM provider_receipt_profile_publication p JOIN provider_receipt_publication_input i USING(batch_id) WHERE p.result_json IS NULL ORDER BY p.rowid",
        )?;
        let ids = query
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        ids
    };
    let mut recovered = 0;
    for id in ids {
        let result: serde_json::Value = serde_json::from_str(&publish(conn, profiles, &id)?)?;
        if result["status"] != "pending_billing" {
            recovered += 1;
        }
    }
    Ok(recovered)
}
