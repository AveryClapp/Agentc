//! Economic projections of original prepared batches, independent of USD and quality.
//!
//! Original execution checkpoints establish identity and order. Unsupported
//! usage advances the retained window as unknown. No projection observes an
//! execution, issues a comparison token, settles a bill or authorizes a plan.

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::plan_profile::{PlanProfileKey, PlanRuntimeVersion};
use crate::provider_tariff::{Bound, Interval, QUANTITY};

pub const VERSION: &str = "provider-tariff-plan-observation-v1";

pub use crate::provider_execution::PhysicalReceipt;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Observation {
    pub version: String,
    pub batch_id: String,
    pub source_sequence: u64,
    pub key: PlanProfileKey,
    pub runtime_version: PlanRuntimeVersion,
    pub observed_at_us: i64,
    pub input_sha256: String,
    pub journal_sha256: String,
    pub bound: Option<Bound>,
    pub receipts: Vec<PhysicalReceipt>,
    pub original_comparison: Option<crate::provider_publication::ComparisonInput>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub original_execution_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Window {
    pub quantity: String,
    pub snapshot_sha256: String,
    pub key: PlanProfileKey,
    pub runtime_version: PlanRuntimeVersion,
    pub lifetime_observations: u64,
    pub observations: Vec<Observation>,
    pub mean_interval: Option<Interval>,
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_tariff_plan_observation (
           batch_id TEXT PRIMARY KEY,call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,
           source_sequence INTEGER NOT NULL CHECK(source_sequence>0),
           observation_sha256 TEXT NOT NULL,observation_json TEXT NOT NULL,
           FOREIGN KEY(batch_id) REFERENCES provider_receipt_publication_input(batch_id));
         CREATE INDEX IF NOT EXISTS provider_tariff_plan_window
           ON provider_tariff_plan_observation(call_site_version,execution_plan_id,source_sequence DESC);
         CREATE TABLE IF NOT EXISTS provider_tariff_plan_state (
           call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,
           source_sequence INTEGER NOT NULL,observation_floor INTEGER NOT NULL,
           runtime_json TEXT NOT NULL,window_size INTEGER NOT NULL CHECK(window_size>0),
           lifetime_observations INTEGER NOT NULL CHECK(typeof(lifetime_observations)='integer' AND lifetime_observations>0),
           PRIMARY KEY(call_site_version,execution_plan_id));",
    )?;
    Ok(())
}

fn canonical<T: Serialize>(value: &T) -> Result<String> {
    Ok(agentc_core::storage::canonical_json(&serde_json::to_value(
        value,
    )?))
}

fn load(conn: &Connection, batch: &str) -> Result<Option<Observation>> {
    let stored: Option<(String, String)> = conn.query_row(
        "SELECT observation_sha256,observation_json FROM provider_tariff_plan_observation WHERE batch_id=?1",
        [batch], |r| Ok((r.get(0)?,r.get(1)?)),
    ).optional()?;
    let Some((hash, json)) = stored else {
        return Ok(None);
    };
    if agentc_core::storage::content_hash(&json) != hash {
        bail!("tariff plan observation fingerprint is invalid");
    }
    let observation: Observation = serde_json::from_str(&json)?;
    let input: (String, u64) = conn.query_row(
        "SELECT input_sha256,rowid FROM provider_receipt_publication_input WHERE batch_id=?1",
        [batch],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if observation.version != VERSION
        || observation.batch_id != batch
        || observation.input_sha256 != input.0
        || observation.source_sequence != input.1
    {
        bail!("tariff plan observation no longer matches its original checkpoint");
    }
    let original = crate::provider_publication::load_input(conn, batch)?;
    if observation.key != original.update.key
        || observation.runtime_version != original.update.runtime_version
        || Some(observation.observed_at_us) != original.update.observed_at_us
        || observation.journal_sha256 != original.journal_sha256
        || observation.original_comparison != original.comparison
    {
        bail!("tariff plan identity or original comparison no longer matches its checkpoint");
    }
    if let Some(id) = &observation.original_execution_id {
        let execution = crate::provider_execution::register(conn, batch)?
            .context("original execution identity is unavailable")?;
        if execution.execution_id != *id {
            bail!("tariff projection differs from its original execution identity");
        }
    }
    for receipt in &observation.receipts {
        let (raw, done): (String, String) = conn.query_row(
            "SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2",
            params![batch,receipt.invocation_id], |r| Ok((r.get(0)?,r.get(1)?)),
        )?;
        let binding = serde_json::json!({"version":"provider-billing-binding-v1",
            "batch_id":batch,"invocation_id":receipt.invocation_id,"invocation":raw,"completion":done});
        if receipt.receipt_sha256 != agentc_core::storage::content_hash(&canonical(&binding)?) {
            bail!("tariff plan evidence no longer matches its original SDK receipt");
        }
    }
    Ok(Some(observation))
}

/// Project one original execution checkpoint. Returns None when the journal
/// lacks an original complete graph or a tariff declaration before dispatch.
/// Native USD publication remains independently pending or completed.
pub fn publish(conn: &mut Connection, batch: &str) -> Result<Option<Observation>> {
    ensure_schema(conn)?;
    let transaction = conn.transaction()?;
    let original_execution = crate::provider_execution::register(&transaction, batch)?;
    if let Some(observation) = load(&transaction, batch)? {
        transaction.commit()?;
        return Ok(Some(observation));
    }
    let eligible: bool = transaction.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_receipt_publication_input WHERE batch_id=?1)
          AND EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE batch_id=?1
          AND json_extract(invocation_json,'$.tariff_declaration') IS NOT NULL)",
        [batch],
        |r| r.get(0),
    )?;
    if !eligible {
        return Ok(None);
    }
    let input = crate::provider_publication::load_input(&transaction, batch)?;
    let Some(evidence) = input.original_graph() else {
        return Ok(None);
    };
    if crate::provider_publication::journal_fingerprint(&transaction, batch)?
        != input.journal_sha256
    {
        bail!("tariff plan journal differs from the original execution checkpoint");
    }
    let requests = evidence.request_fingerprints.iter().cloned().collect();
    crate::provider_receipts::verify_original_evidence(
        &transaction,
        batch,
        &evidence.spec,
        &input.totals,
        &requests,
        false,
    )?;
    let source_sequence: u64 = transaction.query_row(
        "SELECT rowid FROM provider_receipt_publication_input WHERE batch_id=?1",
        [batch],
        |r| r.get(0),
    )?;
    let input_sha256: String = transaction.query_row(
        "SELECT input_sha256 FROM provider_receipt_publication_input WHERE batch_id=?1",
        [batch],
        |r| r.get(0),
    )?;
    let mut total = Interval {
        lower_nanodollars: 0,
        upper_nanodollars: 0,
    };
    let mut qualified = true;
    let mut receipts = Vec::new();
    {
        let mut query = transaction.prepare(
            "SELECT invocation_id,invocation_json,completion_json FROM provider_receipt_invocation
             WHERE batch_id=?1 ORDER BY item,logical_call,ordinal",
        )?;
        let rows = query.query_map([batch], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
            ))
        })?;
        for row in rows {
            let (id, raw, done) = row?;
            let invocation = serde_json::from_str::<crate::provider_receipts::Invocation>(&raw)?;
            let completion = serde_json::from_str::<crate::provider_receipts::Completion>(&done)?;
            match crate::provider_tariff::quote(&invocation, &completion) {
                Ok(bound) => total = total.checked_add(bound.interval)?,
                Err(_) => qualified = false,
            }
            let binding = serde_json::json!({"version":"provider-billing-binding-v1",
                "batch_id":batch,"invocation_id":id,"invocation":raw,"completion":done});
            receipts.push(PhysicalReceipt {
                invocation_id: id,
                receipt_sha256: agentc_core::storage::content_hash(&canonical(&binding)?),
            });
        }
    }
    let observation = Observation {
        version: VERSION.into(),
        batch_id: batch.into(),
        source_sequence,
        key: input.update.key.clone(),
        runtime_version: input.update.runtime_version.clone(),
        observed_at_us: input
            .update
            .observed_at_us
            .context("original execution timestamp is missing")?,
        input_sha256,
        journal_sha256: input.journal_sha256,
        bound: qualified.then(|| Bound {
            quantity: QUANTITY.into(),
            snapshot_sha256: crate::provider_tariff::snapshot_sha256(),
            interval: total,
        }),
        receipts,
        original_comparison: input.comparison,
        original_execution_id: original_execution.map(|execution| execution.execution_id),
    };
    let json = canonical(&observation)?;
    transaction.execute(
        "INSERT INTO provider_tariff_plan_observation VALUES(?1,?2,?3,?4,?5,?6)",
        params![
            batch,
            observation.key.call_site_version.as_str(),
            observation.key.execution_plan_id.as_str(),
            source_sequence,
            agentc_core::storage::content_hash(&json),
            json
        ],
    )?;
    let runtime = canonical(&observation.runtime_version)?;
    transaction.execute(
        "INSERT INTO provider_tariff_plan_state VALUES(?1,?2,?3,?3,?4,?5,1)
         ON CONFLICT(call_site_version,execution_plan_id) DO UPDATE SET
         lifetime_observations=lifetime_observations+1,
         observation_floor=CASE WHEN excluded.source_sequence>source_sequence AND (excluded.runtime_json<>runtime_json OR excluded.window_size<>window_size) THEN excluded.observation_floor ELSE observation_floor END,
         runtime_json=CASE WHEN excluded.source_sequence>source_sequence THEN excluded.runtime_json ELSE runtime_json END,
         window_size=CASE WHEN excluded.source_sequence>source_sequence THEN excluded.window_size ELSE window_size END,
         source_sequence=MAX(source_sequence,excluded.source_sequence)",
        params![observation.key.call_site_version.as_str(),observation.key.execution_plan_id.as_str(),source_sequence,runtime,input.retained_window_size],
    )?;
    transaction.commit()?;
    Ok(Some(observation))
}

/// Read the exact newest economic window for the current runtime and window
/// contract. Unknown intervals remain in the window and prevent a mean bound.
/// Historical costs remain in the original lifetime physical ledger.
pub fn window(
    conn: &Connection,
    key: &PlanProfileKey,
    runtime: &PlanRuntimeVersion,
    size: usize,
) -> Result<Option<Window>> {
    if size == 0 {
        bail!("tariff plan window is empty")
    }
    ensure_schema(conn)?;
    if conn.is_autocommit() {
        let transaction = conn.unchecked_transaction()?;
        let result = window_in_transaction(&transaction, key, runtime, size)?;
        transaction.commit()?;
        return Ok(result);
    }
    window_in_transaction(conn, key, runtime, size)
}

fn window_in_transaction(
    conn: &Connection,
    key: &PlanProfileKey,
    runtime: &PlanRuntimeVersion,
    size: usize,
) -> Result<Option<Window>> {
    let state: Option<(u64,String,usize,u64)> = conn.query_row(
        "SELECT observation_floor,runtime_json,window_size,lifetime_observations FROM provider_tariff_plan_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],
        |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)),
    ).optional()?;
    let Some((floor, expected_runtime, expected_size, lifetime)) = state else {
        return Ok(None);
    };
    if expected_runtime != canonical(runtime)? || expected_size != size {
        return Ok(None);
    }
    let mut query = conn.prepare(
        "SELECT batch_id FROM provider_tariff_plan_observation WHERE call_site_version=?1 AND execution_plan_id=?2 AND source_sequence>=?3 ORDER BY source_sequence DESC LIMIT ?4",
    )?;
    let ids = query
        .query_map(
            params![
                key.call_site_version.as_str(),
                key.execution_plan_id.as_str(),
                floor,
                size
            ],
            |r| r.get::<_, String>(0),
        )?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    drop(query);
    let mut total = Interval {
        lower_nanodollars: 0,
        upper_nanodollars: 0,
    };
    let mut qualified = !ids.is_empty();
    let mut observations = Vec::new();
    for id in ids {
        let observation = load(conn, &id)?.context("tariff plan observation disappeared")?;
        if observation.key != *key || observation.runtime_version != *runtime {
            bail!("tariff plan window contains a different identity or runtime");
        }
        if let Some(bound) = &observation.bound {
            if bound.quantity != QUANTITY
                || bound.snapshot_sha256 != crate::provider_tariff::snapshot_sha256()
            {
                qualified = false;
            } else {
                total = total.checked_add(bound.interval)?;
            }
        } else {
            qualified = false;
        }
        observations.push(observation);
    }
    let mean_interval = if qualified {
        let count = observations.len() as u64;
        Some(Interval {
            lower_nanodollars: total.lower_nanodollars / count,
            upper_nanodollars: total.upper_nanodollars.div_ceil(count),
        })
    } else {
        None
    };
    Ok(Some(Window {
        quantity: QUANTITY.into(),
        snapshot_sha256: crate::provider_tariff::snapshot_sha256(),
        key: key.clone(),
        runtime_version: runtime.clone(),
        lifetime_observations: lifetime,
        observations,
        mean_interval,
    }))
}
