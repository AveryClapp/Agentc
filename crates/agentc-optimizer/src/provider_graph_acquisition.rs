//! Lifetime acquisition liabilities owned before graph SDK dispatch.
//! Every physical role contributes, including incomplete and late work. This
//! ledger supplies no graph profile, quality, scheduling or billing permission.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

use crate::multi_call::InferenceCostSource;
use crate::provider_graph_preparation::{self as preparation, Lease};
use crate::provider_receipts::{Batch, Completion, Invocation, Status};

pub const VERSION: &str = "provider-graph-acquisition-v1";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Driver {
    pub version: String,
    pub application_version: String,
    pub callback_version: String,
    pub item_count: u32,
}

impl Driver {
    pub(crate) fn from_batch(batch: &Batch) -> Result<Self> {
        Ok(Self {
            version: VERSION.into(),
            application_version: batch
                .application_version
                .clone()
                .context("graph acquisition needs an application revision")?,
            callback_version: batch.callback_version.clone(),
            item_count: batch.item_count,
        })
    }
    pub fn identity(&self) -> Result<String> {
        Ok(content_hash(&canonical(self)?))
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Charge {
    invocation_id: String,
    batch_id: String,
    receipt_sha256: String,
    settled_cost_usd: Option<f64>,
    settlement_sha256: Option<String>,
    role: String,
    pending: bool,
    known_cost_usd: f64,
    estimated_cost_usd: f64,
    cost_complete: bool,
    latency_ms: f64,
    late: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Economics {
    pub driver_id: String,
    pub owned_batches: u64,
    pub physical_invocations: u64,
    pub roles: BTreeMap<String, u64>,
    pub pending_invocations: u64,
    pub unknown_cost_invocations: u64,
    pub known_cost_usd: f64,
    pub estimated_cost_usd: f64,
    pub foreground_latency_ms: f64,
    pub late_invocations: u64,
    pub late_dispatch_latency_ms: f64,
    pub billing_verification_latency_ms: f64,
    pub pending_billing_verifications: u64,
    pub unfinished_batches: u64,
    pub persistence_failed_batches: u64,
    pub uncovered_preparations: u64,
    pub cost_complete: bool,
}

fn canonical<T: Serialize>(value: &T) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    crate::provider_receipts::ensure_schema(conn)?;
    crate::provider_reconciliation::ensure_schema(conn)?;
    conn.execute_batch("CREATE TABLE IF NOT EXISTS provider_graph_acquisition (
        batch_id TEXT PRIMARY KEY,lease_id TEXT NOT NULL UNIQUE,driver_id TEXT NOT NULL,
        driver_sha256 TEXT NOT NULL,driver_json TEXT NOT NULL,wall_latency_ms REAL,
        persistence_failed INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(batch_id) REFERENCES provider_receipt_batch(batch_id),
        FOREIGN KEY(lease_id) REFERENCES provider_graph_preparation(lease_id));
        CREATE INDEX IF NOT EXISTS provider_graph_acquisition_driver ON provider_graph_acquisition(driver_id);
        CREATE TABLE IF NOT EXISTS provider_graph_acquisition_invocation (
        invocation_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,charge_sha256 TEXT NOT NULL,charge_json TEXT NOT NULL,
        FOREIGN KEY(invocation_id) REFERENCES provider_receipt_invocation(invocation_id),
        FOREIGN KEY(batch_id) REFERENCES provider_graph_acquisition(batch_id));
        CREATE TABLE IF NOT EXISTS provider_graph_foreground_input (
        batch_id TEXT PRIMARY KEY,input_sha256 TEXT NOT NULL,input_json TEXT NOT NULL,
        FOREIGN KEY(batch_id) REFERENCES provider_graph_acquisition(batch_id));")?;
    Ok(())
}

/// Inside the preparation transaction, before any physical invocation exists.
pub(crate) fn enroll(conn: &Connection, lease: &Lease) -> Result<()> {
    ensure_schema(conn)?;
    let metadata: String = conn.query_row(
        "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
        [&lease.batch_id],
        |r| r.get(0),
    )?;
    let driver = Driver::from_batch(&serde_json::from_str(&metadata)?)?;
    let raw = canonical(&driver)?;
    conn.execute("INSERT INTO provider_graph_acquisition VALUES(?1,?2,?3,?4,?5,NULL,0) ON CONFLICT DO NOTHING",
        params![lease.batch_id,lease.lease_id,driver.identity()?,content_hash(&raw),raw])?;
    let saved: (String, String) = conn.query_row(
        "SELECT lease_id,driver_json FROM provider_graph_acquisition WHERE batch_id=?1",
        [&lease.batch_id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if saved != (lease.lease_id.clone(), raw) {
        bail!("graph acquisition owner conflicts with its original preparation");
    }
    Ok(())
}

pub(crate) fn owned(conn: &Connection, batch_id: &str) -> Result<bool> {
    let exists:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_graph_acquisition')",[],|r|r.get(0))?;
    if !exists {
        return Ok(false);
    }
    Ok(conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_graph_acquisition WHERE batch_id=?1)",
        [batch_id],
        |r| r.get(0),
    )?)
}

fn charge(conn: &Connection, batch: &str, id: &str, late: bool) -> Result<Charge> {
    let (raw,done): (String,Option<String>) = conn.query_row("SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2",params![batch,id],|r|Ok((r.get(0)?,r.get(1)?)))?;
    let invocation: Invocation = serde_json::from_str(&raw)?;
    let completion: Option<Completion> = done.as_deref().map(serde_json::from_str).transpose()?;
    let settled = crate::provider_reconciliation::settled_cost(conn, id)?;
    let settlement: Option<String> = conn
        .query_row(
            "SELECT invoice_json FROM provider_billing_settlement WHERE invocation_id=?1",
            [id],
            |r| r.get(0),
        )
        .optional()?;
    let (known, estimated, latency, complete) =
        completion.as_ref().map_or((0., 0., 0., false), |c| {
            let reported = c.cost_source == InferenceCostSource::ProviderReported;
            (
                settled.unwrap_or(if reported {
                    c.cost_usd.unwrap_or(0.)
                } else {
                    0.
                }),
                if c.cost_source == InferenceCostSource::Estimated {
                    c.cost_usd.unwrap_or(0.)
                } else {
                    0.
                },
                c.latency_ms,
                (reported && c.status == Status::Completed && c.cost_usd.is_some()
                    || settled.is_some())
                    && c.http_attempts == Some(1),
            )
        });
    Ok(Charge {
        invocation_id: id.into(),
        batch_id: batch.into(),
        receipt_sha256: content_hash(&canonical(&(raw, done))?),
        settled_cost_usd: settled,
        settlement_sha256: settlement.map(|raw| content_hash(&raw)),
        role: serde_json::to_value(invocation.role)?
            .as_str()
            .unwrap()
            .into(),
        pending: completion.is_none(),
        known_cost_usd: known,
        estimated_cost_usd: estimated,
        cost_complete: complete
            && invocation.transport_verified
            && invocation.transport_sha256.is_some()
            && invocation.request_sha256.is_some(),
        latency_ms: latency,
        late,
    })
}

/// Runs within the SDK reservation/completion or billing transaction. A failed
/// projection rolls back the receipt change instead of losing its liability.
pub(crate) fn refresh_invocation(conn: &Connection, batch: &str, id: &str) -> Result<()> {
    if !owned(conn, batch)? {
        return Ok(());
    }
    let prior:Option<(String,String)>=conn.query_row("SELECT charge_sha256,charge_json FROM provider_graph_acquisition_invocation WHERE invocation_id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    let old = prior
        .map(|(hash, raw)| -> Result<Charge> {
            if content_hash(&raw) != hash {
                bail!("graph acquisition charge fingerprint is invalid");
            }
            Ok(serde_json::from_str(&raw)?)
        })
        .transpose()?;
    let wall: Option<f64> = conn.query_row(
        "SELECT wall_latency_ms FROM provider_graph_acquisition WHERE batch_id=?1",
        [batch],
        |r| r.get(0),
    )?;
    let mut next = charge(conn, batch, id, old.as_ref().is_some_and(|c| c.late))?;
    next.late |= wall.is_some()
        && (old.is_none() || old.as_ref().is_some_and(|c| c.pending) && !next.pending);
    let raw = canonical(&next)?;
    conn.execute("INSERT INTO provider_graph_acquisition_invocation VALUES(?1,?2,?3,?4) ON CONFLICT(invocation_id) DO UPDATE SET charge_sha256=excluded.charge_sha256,charge_json=excluded.charge_json",
        params![id,batch,content_hash(&raw),raw])?;
    Ok(())
}

pub(crate) fn seal(conn: &Connection, batch: &str, failed: bool) -> Result<()> {
    conn.execute("UPDATE provider_graph_acquisition SET persistence_failed=MAX(persistence_failed,?2) WHERE batch_id=?1",params![batch,failed])?;
    Ok(())
}

pub(crate) fn finish_foreground(conn: &Connection, batch: &str, wall: f64) -> Result<()> {
    if !wall.is_finite() || wall < 0. {
        bail!("graph acquisition foreground time is invalid");
    }
    if !owned(conn, batch)? {
        return Ok(());
    }
    let old: Option<f64> = conn.query_row(
        "SELECT wall_latency_ms FROM provider_graph_acquisition WHERE batch_id=?1",
        [batch],
        |r| r.get(0),
    )?;
    if old.is_some_and(|old| old != wall) {
        bail!("graph acquisition foreground retry changed its original time");
    }
    let raw = canonical(
        &serde_json::json!({"version":"provider-graph-foreground-input-v1","batch_id":batch,"wall_latency_ms":wall}),
    )?;
    conn.execute(
        "INSERT INTO provider_graph_foreground_input VALUES(?1,?2,?3) ON CONFLICT DO NOTHING",
        params![batch, content_hash(&raw), raw],
    )?;
    let saved: (String, String) = conn.query_row(
        "SELECT input_sha256,input_json FROM provider_graph_foreground_input WHERE batch_id=?1",
        [batch],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if saved != (content_hash(&raw), raw) {
        bail!("graph foreground input changed its original resource time");
    }
    conn.execute("UPDATE provider_graph_acquisition SET wall_latency_ms=?2 WHERE batch_id=?1 AND wall_latency_ms IS NULL",params![batch,wall])?;
    Ok(())
}

fn verify_foreground(conn: &Connection, batch: &str, wall: Option<f64>) -> Result<()> {
    let Some(wall) = wall else {
        return Ok(());
    };
    let input: Option<(String, String)> = conn
        .query_row(
            "SELECT input_sha256,input_json FROM provider_graph_foreground_input WHERE batch_id=?1",
            [batch],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    if let Some((hash, raw)) = input {
        let expected = canonical(
            &serde_json::json!({"version":"provider-graph-foreground-input-v1","batch_id":batch,"wall_latency_ms":wall}),
        )?;
        if hash != content_hash(&raw) || raw != expected {
            bail!("graph resource projection differs from original foreground input");
        }
        return Ok(());
    }
    // Historical completed graphs attest their original wall time. A mutable
    // metric alone cannot establish resource completeness.
    for (table, field) in [
        (
            "provider_graph_role_execution",
            "$.acquisition.foreground_latency_ms",
        ),
        ("provider_graph_execution", "$.totals.wall_latency_ms"),
    ] {
        let exists: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
            [table],
            |r| r.get(0),
        )?;
        if !exists {
            continue;
        }
        let row: Option<(String,String,f64)> = conn.query_row(&format!("SELECT execution_sha256,execution_json,json_extract(execution_json,'{field}') FROM {table} WHERE json_extract(execution_json,'$.batch_id')=?1"),[batch],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
        if let Some((hash, raw, original)) = row {
            if content_hash(&raw) != hash || original != wall {
                bail!("historical foreground differs from its original graph execution");
            }
            return Ok(());
        }
    }
    bail!("graph foreground time has no original input or execution custody")
}

fn billing_times(conn: &Connection, batch: &str) -> Result<(u64, Vec<f64>)> {
    let mut q=conn.prepare("SELECT invocation_id,receipt_sha256,result_input_json,result_json FROM provider_billing_attempt WHERE batch_id=?1 ORDER BY attempt_id")?;
    let rows = q
        .query_map([batch], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, Option<String>>(3)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut pending = 0;
    let mut times = vec![];
    for (id, hash, input, result) in rows {
        let (call,done):(String,Option<String>)=conn.query_row("SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2",params![batch,id],|r|Ok((r.get(0)?,r.get(1)?)))?;
        let done = done.context("graph billing attempt lacks a terminal original receipt")?;
        let binding = canonical(
            &serde_json::json!({"version":"provider-billing-binding-v1","batch_id":batch,"invocation_id":id,"invocation":call,"completion":done}),
        )?;
        if content_hash(&binding) != hash {
            bail!("graph billing resource attempt differs from its original receipt");
        }
        let Some(result) = result else {
            pending += 1;
            continue;
        };
        if input.as_ref() != Some(&result) {
            bail!("graph billing resource result differs from its reserved input");
        }
        let value: serde_json::Value = serde_json::from_str(&result)?;
        let time = value["latency_ms"]
            .as_f64()
            .context("graph billing resource time is missing")?;
        if !time.is_finite() || time < 0. || canonical(&value)? != result {
            bail!("graph billing resource time or custody is invalid");
        }
        times.push(time);
    }
    Ok((pending, times))
}

pub(crate) fn resource_latency_bound(
    conn: &Connection,
    batches: &[String],
) -> Result<crate::provider_graph_economics::UsdInterval> {
    use crate::provider_graph_economics::UsdInterval;
    let mut bound = UsdInterval::exact(0.)?;
    for batch in batches {
        let wall: Option<f64> = conn.query_row(
            "SELECT wall_latency_ms FROM provider_graph_acquisition WHERE batch_id=?1",
            [batch],
            |r| r.get(0),
        )?;
        verify_foreground(conn, batch, wall)?;
        bound = bound.add(UsdInterval::exact(wall.unwrap_or(0.))?)?;
        let mut q=conn.prepare("SELECT charge_json FROM provider_graph_acquisition_invocation WHERE batch_id=?1 ORDER BY invocation_id")?;
        let rows = q
            .query_map([batch], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        for raw in rows {
            let charge: Charge = serde_json::from_str(&raw)?;
            if charge.late {
                bound = bound.add(UsdInterval::exact(charge.latency_ms)?)?;
            }
        }
        for time in billing_times(conn, batch)?.1 {
            bound = bound.add(UsdInterval::exact(time)?)?;
        }
    }
    Ok(bound)
}

fn validate_owner(
    conn: &Connection,
    batch: &str,
    id: &str,
    hash: &str,
    raw: &str,
) -> Result<String> {
    let driver: Driver = serde_json::from_str(raw)?;
    let lease = preparation::load(conn, id)?;
    let metadata: String = conn.query_row(
        "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
        [batch],
        |r| r.get(0),
    )?;
    if content_hash(raw) != hash
        || lease.batch_id != batch
        || Driver::from_batch(&serde_json::from_str(&metadata)?)? != driver
    {
        bail!("graph acquisition driver custody is invalid");
    }
    driver.identity()
}

/// This broad driver scope conservatively retains acquisition spend across
/// plans, source-control variants, price/runtime changes and quality resets.
/// It attests accounting completeness only, never graph evidence or admission.
pub fn summarize_driver(conn: &Connection, driver_id: &str) -> Result<Economics> {
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let result = summarize(&tx, driver_id, None, None)?;
        tx.commit()?;
        return Ok(result);
    }
    summarize(conn, driver_id, None, None)
}

pub(crate) fn summarize_batches(
    conn: &Connection,
    driver_id: &str,
    batches: &[String],
) -> Result<Economics> {
    summarize(conn, driver_id, None, Some(batches))
}

fn summarize(
    conn: &Connection,
    driver_id: &str,
    only_batch: Option<&str>,
    included: Option<&[String]>,
) -> Result<Economics> {
    let mut result = Economics {
        driver_id: driver_id.into(),
        cost_complete: true,
        ..Default::default()
    };
    let mut q=conn.prepare("SELECT batch_id,lease_id,driver_sha256,driver_json,wall_latency_ms,persistence_failed FROM provider_graph_acquisition WHERE driver_id=?1 AND (?2 IS NULL OR batch_id=?2)")?;
    let batches = q
        .query_map(params![driver_id, only_batch], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, Option<f64>>(4)?,
                r.get::<_, bool>(5)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for (batch, lease, hash, raw, wall, failed) in batches {
        if validate_owner(conn, &batch, &lease, &hash, &raw)? != driver_id {
            bail!("graph acquisition owner projection conflicts");
        }
        if included.is_some_and(|ids| !ids.contains(&batch)) {
            continue;
        }
        verify_foreground(conn, &batch, wall)?;
        let sealed: bool = conn.query_row(
            "SELECT sealed FROM provider_receipt_batch WHERE batch_id=?1",
            [&batch],
            |r| r.get(0),
        )?;
        result.owned_batches += 1;
        result.foreground_latency_ms += wall.unwrap_or(0.);
        result.unfinished_batches += u64::from(!sealed || wall.is_none());
        result.persistence_failed_batches += u64::from(failed);
        let mut calls=conn.prepare("SELECT r.invocation_id,g.charge_sha256,g.charge_json FROM provider_receipt_invocation r LEFT JOIN provider_graph_acquisition_invocation g USING(invocation_id) WHERE r.batch_id=?1")?;
        let rows = calls
            .query_map([&batch], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, Option<String>>(2)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let projected: u64 = conn.query_row(
            "SELECT COUNT(*) FROM provider_graph_acquisition_invocation WHERE batch_id=?1",
            [&batch],
            |r| r.get(0),
        )?;
        if projected != rows.len() as u64 {
            bail!("graph acquisition invocation coverage conflicts with its journal");
        }
        for (id, hash, raw) in rows {
            let raw = raw.context("graph acquisition is missing an owned invocation")?;
            let saved: Charge = serde_json::from_str(&raw)?;
            if Some(content_hash(&raw)) != hash || saved != charge(conn, &batch, &id, saved.late)? {
                bail!("graph acquisition charge differs from its original SDK receipt");
            }
            result.physical_invocations += 1;
            *result.roles.entry(saved.role).or_default() += 1;
            result.pending_invocations += u64::from(saved.pending);
            result.unknown_cost_invocations += u64::from(!saved.cost_complete);
            result.known_cost_usd += saved.known_cost_usd;
            result.estimated_cost_usd += saved.estimated_cost_usd;
            result.late_invocations += u64::from(saved.late);
            if saved.late {
                result.late_dispatch_latency_ms += saved.latency_ms;
            }
        }
        let (pending, times) = billing_times(conn, &batch)?;
        result.pending_billing_verifications += pending;
        result.billing_verification_latency_ms += times.into_iter().sum::<f64>();
    }
    let raw: Option<String> = conn
        .query_row(
            "SELECT driver_json FROM provider_graph_acquisition WHERE driver_id=?1 LIMIT 1",
            [driver_id],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(raw) = raw.filter(|_| only_batch.is_none()) {
        let d: Driver = serde_json::from_str(&raw)?;
        result.uncovered_preparations=conn.query_row("SELECT COUNT(*) FROM provider_graph_preparation p JOIN provider_receipt_batch b USING(batch_id) LEFT JOIN provider_graph_acquisition a USING(batch_id)
            WHERE (a.batch_id IS NULL OR a.driver_id!=?4) AND json_extract(b.metadata_json,'$.application_version')=?1 AND json_extract(b.metadata_json,'$.callback_version')=?2 AND json_extract(b.metadata_json,'$.item_count')=?3",
            params![d.application_version,d.callback_version,d.item_count,driver_id],|r|r.get(0))?;
    }
    for value in [
        result.known_cost_usd,
        result.estimated_cost_usd,
        result.foreground_latency_ms,
        result.late_dispatch_latency_ms,
        result.billing_verification_latency_ms,
    ] {
        if !value.is_finite() || value < 0. {
            bail!("graph acquisition totals overflow or are invalid");
        }
    }
    result.cost_complete = result.owned_batches > 0
        && result.unknown_cost_invocations == 0
        && result.pending_invocations == 0
        && result.unfinished_batches == 0
        && result.persistence_failed_batches == 0
        && result.pending_billing_verifications == 0
        && result.uncovered_preparations == 0;
    Ok(result)
}

pub fn report_batch(conn: &Connection, batch: &str) -> Result<Option<Economics>> {
    ensure_schema(conn)?;
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let result = report_batch(&tx, batch)?;
        tx.commit()?;
        return Ok(result);
    }
    let driver: Option<String> = conn
        .query_row(
            "SELECT driver_id FROM provider_graph_acquisition WHERE batch_id=?1",
            [batch],
            |r| r.get(0),
        )
        .optional()?;
    driver
        .map(|id| summarize(conn, &id, Some(batch), None))
        .transpose()
}
