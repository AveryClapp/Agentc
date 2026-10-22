//! Lifetime acquisition economics owned before SDK dispatch, independent of
//! profile publication. Each physical receipt contributes once; old comparison
//! summaries are coverage checks, never a second source of the same charges.

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::multi_call::{InferenceCostSource, MultiCallPlanSpec};
use crate::provider_receipts::{Batch, Completion, Invocation, Role, Status};

pub const VERSION: &str = "physical-acquisition-economics-v1";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Acquisition,
    Deployment,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct Economics {
    pub owned_batches: u64,
    pub known_cost_usd: f64,
    pub estimated_cost_usd: f64,
    pub charged_invocations: u64,
    pub unknown_cost_invocations: u64,
    pub incomplete_batches: u64,
    pub acquisition_latency_ms: f64,
    pub cost_complete: bool,
    pub uncovered_profile_executions: u64,
    pub pending_billing_verifications: u64,
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    crate::provider_receipts::ensure_schema(conn)?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_economic_cohort (
           owner_call_site_version TEXT PRIMARY KEY,owned_batches INTEGER NOT NULL DEFAULT 0,
           known_cost_usd REAL NOT NULL DEFAULT 0,estimated_cost_usd REAL NOT NULL DEFAULT 0,
           charged_invocations INTEGER NOT NULL DEFAULT 0,unknown_cost_invocations INTEGER NOT NULL DEFAULT 0,
           incomplete_batches INTEGER NOT NULL DEFAULT 0,acquisition_latency_ms REAL NOT NULL DEFAULT 0,
           covered_executions INTEGER NOT NULL DEFAULT 0,covered_cost_usd REAL NOT NULL DEFAULT 0,covered_latency_ms REAL NOT NULL DEFAULT 0
         );
         CREATE TABLE IF NOT EXISTS provider_economic_batch (
           batch_id TEXT PRIMARY KEY, owner_call_site_version TEXT NOT NULL,
           reference_plan_id TEXT NOT NULL, phase TEXT NOT NULL,
           wall_latency_ms REAL, persistence_failed INTEGER NOT NULL DEFAULT 0,
           coverage_complete INTEGER NOT NULL DEFAULT 0,
           FOREIGN KEY(batch_id) REFERENCES provider_receipt_batch(batch_id)
         );
         CREATE INDEX IF NOT EXISTS provider_economic_by_owner ON provider_economic_batch(owner_call_site_version);
         CREATE TABLE IF NOT EXISTS provider_economic_branch (
           batch_id TEXT NOT NULL,item INTEGER NOT NULL,logical_call INTEGER NOT NULL,key_json TEXT NOT NULL,
           PRIMARY KEY(batch_id,item,logical_call)
         );
         CREATE TABLE IF NOT EXISTS provider_economic_invocation (
           invocation_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,owner_call_site_version TEXT NOT NULL,
           charged INTEGER NOT NULL,known_cost_usd REAL NOT NULL,estimated_cost_usd REAL NOT NULL,
           unknown_cost INTEGER NOT NULL,latency_ms REAL NOT NULL,late INTEGER NOT NULL, terminal INTEGER NOT NULL,
           FOREIGN KEY(invocation_id) REFERENCES provider_receipt_invocation(invocation_id)
         );
         CREATE INDEX IF NOT EXISTS provider_economic_charged ON provider_economic_invocation(owner_call_site_version) WHERE charged=1;
         CREATE TABLE IF NOT EXISTS provider_economic_publication (
           batch_id TEXT PRIMARY KEY, call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,observation_sequence INTEGER NOT NULL,
           UNIQUE(call_site_version,execution_plan_id,observation_sequence)
         );
         CREATE INDEX IF NOT EXISTS provider_economic_published_cohort ON provider_economic_publication(call_site_version);",
    )?;
    let has_column:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM pragma_table_info('provider_economic_cohort') WHERE name='pending_billing_verifications')",[],|row|row.get(0))?;
    if !has_column {
        conn.execute_batch("ALTER TABLE provider_economic_cohort ADD COLUMN pending_billing_verifications INTEGER NOT NULL DEFAULT 0;")?;
    }
    crate::provider_reconciliation::ensure_schema(conn)?;
    Ok(())
}

/// The trusted native prepare path enrolls the complete original graph before
/// returning dispatch plans. Runtime drift never deletes these lifetime rows.
pub fn enroll_batch(
    conn: &mut Connection,
    id: &str,
    spec: &MultiCallPlanSpec,
    phase: Phase,
) -> Result<bool> {
    ensure_schema(conn)?;
    crate::provider_tariff::ensure_schema(conn)?;
    let mut reference = spec.clone();
    reference.max_workers = 1;
    let key = reference.profile_key().map_err(anyhow::Error::msg)?;
    let phase = match phase {
        Phase::Acquisition => "acquisition",
        Phase::Deployment => "deployment",
    };
    let transaction = conn.transaction()?;
    crate::provider_receipts::begin_batch(
        &transaction,
        &Batch {
            version: crate::provider_receipts::VERSION.into(),
            batch_id: id.into(),
            application_version: Some(spec.application_version.clone()),
            callback_version: spec.callback_version.clone(),
            item_count: spec.item_count,
        },
    )?;
    let inserted=transaction.execute("INSERT INTO provider_economic_batch(batch_id,owner_call_site_version,reference_plan_id,phase) VALUES(?1,?2,?3,?4) ON CONFLICT DO NOTHING",params![id,key.call_site_version.as_str(),key.execution_plan_id.as_str(),phase])?;
    let old:(String,String,String)=transaction.query_row("SELECT owner_call_site_version,reference_plan_id,phase FROM provider_economic_batch WHERE batch_id=?1",[id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?)))?;
    if old
        != (
            key.call_site_version.to_string(),
            key.execution_plan_id.to_string(),
            phase.into(),
        )
    {
        bail!("provider economic owner or phase conflicts");
    }
    for branch in &spec.branches {
        let branch_key = crate::plan_profile::PlanProfileKey {
            call_site_version: branch.call_site_version.clone(),
            execution_plan_id: branch.plan.execution_plan_id()?,
        };
        let json = serde_json::to_string(&branch_key)?;
        transaction.execute(
            "INSERT INTO provider_economic_branch VALUES(?1,?2,?3,?4) ON CONFLICT DO NOTHING",
            params![id, branch.position.item, branch.position.call, json],
        )?;
        let old:String=transaction.query_row("SELECT key_json FROM provider_economic_branch WHERE batch_id=?1 AND item=?2 AND logical_call=?3",params![id,branch.position.item,branch.position.call],|row|row.get(0))?;
        if old != json {
            bail!("provider economic branch conflicts");
        }
    }
    if inserted == 1 {
        transaction.execute("INSERT INTO provider_economic_cohort(owner_call_site_version,owned_batches,incomplete_batches) VALUES(?1,1,1) ON CONFLICT(owner_call_site_version) DO UPDATE SET owned_batches=owned_batches+1,incomplete_batches=incomplete_batches+1",[key.call_site_version.as_str()])?;
    }
    crate::provider_tariff::refresh_batch_coverage(&transaction, id)?;
    transaction.commit()?;
    Ok(inserted == 1)
}

/// Refresh only the changed invocation inside the receipt's transaction.
/// This avoids rescanning the whole batch after every SDK send.
pub(crate) fn refresh_invocation(conn: &Connection, batch_id: &str, id: &str) -> Result<()> {
    let owner:Option<(String,String,Option<f64>)>=conn.query_row("SELECT owner_call_site_version,phase,wall_latency_ms FROM provider_economic_batch WHERE batch_id=?1",[batch_id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?))).optional()?;
    let Some((owner, phase, wall)) = owner else {
        return Ok(());
    };
    let (invocation,completion):(String,Option<String>)=conn.query_row("SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2",params![batch_id,id],|row|Ok((row.get(0)?,row.get(1)?)))?;
    let invocation: Invocation = serde_json::from_str(&invocation)?;
    let expected:Option<String>=conn.query_row("SELECT key_json FROM provider_economic_branch WHERE batch_id=?1 AND item=?2 AND logical_call=?3",params![batch_id,invocation.position.item,invocation.position.call],|row|row.get(0)).optional()?;
    let binding_matches = expected.as_deref()
        == invocation
            .logical_plan_key
            .as_ref()
            .map(serde_json::to_string)
            .transpose()?
            .as_deref()
        && expected.is_some();
    let completion: Option<Completion> = completion
        .map(|value| serde_json::from_str(&value))
        .transpose()?;
    let settled = crate::provider_reconciliation::settled_cost(conn, id)?;
    let complete = completion.as_ref().is_some_and(|value| {
        ((value.status == Status::Completed
            && value.cost_source == InferenceCostSource::ProviderReported)
            || settled.is_some())
            && value.http_attempts == Some(1)
    }) && invocation.transport_verified
        && invocation.transport_sha256.is_some()
        && invocation.request_sha256.is_some()
        && binding_matches;
    let charged = phase == "acquisition"
        || invocation.role != Role::Primary
        || invocation.ordinal != 0
        || !completion
            .as_ref()
            .is_some_and(|value| value.status == Status::Completed)
        || !complete;
    let (known, estimated, latency) = completion.as_ref().map_or((0.0, 0.0, 0.0), |value| {
        (
            settled.unwrap_or_else(|| {
                if value.cost_source == InferenceCostSource::ProviderReported {
                    value.cost_usd.unwrap_or(0.0)
                } else {
                    0.0
                }
            }),
            if value.cost_source == InferenceCostSource::Estimated {
                value.cost_usd.unwrap_or(0.0)
            } else {
                0.0
            },
            value.latency_ms,
        )
    });
    let prior:Option<(bool,f64,f64,bool,f64,bool)>=conn.query_row("SELECT charged,known_cost_usd,estimated_cost_usd,unknown_cost,latency_ms,late FROM provider_economic_invocation WHERE invocation_id=?1",[id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?))).optional()?;
    let old: Option<(bool, bool)> = conn
        .query_row(
            "SELECT late,terminal FROM provider_economic_invocation WHERE invocation_id=?1",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    let terminal = completion.is_some();
    let late = old.is_some_and(|(late, _)| late)
        || (wall.is_some() && terminal && !old.is_some_and(|(_, terminal)| terminal));
    conn.execute("INSERT INTO provider_economic_invocation VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10) ON CONFLICT(invocation_id) DO UPDATE SET charged=excluded.charged,known_cost_usd=excluded.known_cost_usd,estimated_cost_usd=excluded.estimated_cost_usd,unknown_cost=excluded.unknown_cost,latency_ms=excluded.latency_ms,late=excluded.late,terminal=excluded.terminal",params![id,batch_id,owner,charged,known,estimated,!complete,latency,late,terminal])?;
    let (old_charged, old_known, old_estimated, old_unknown, old_latency, old_late) =
        prior.unwrap_or((false, 0.0, 0.0, false, 0.0, false));
    let money_delta =
        (if charged { known } else { 0.0 }) - (if old_charged { old_known } else { 0.0 });
    let estimated_delta =
        (if charged { estimated } else { 0.0 }) - (if old_charged { old_estimated } else { 0.0 });
    let extra = if charged && (phase == "deployment" || late) {
        latency
    } else {
        0.0
    };
    let old_extra = if old_charged && (phase == "deployment" || old_late) {
        old_latency
    } else {
        0.0
    };
    conn.execute("UPDATE provider_economic_cohort SET known_cost_usd=known_cost_usd+?1,estimated_cost_usd=estimated_cost_usd+?2,charged_invocations=charged_invocations+?3,unknown_cost_invocations=unknown_cost_invocations+?4,acquisition_latency_ms=acquisition_latency_ms+?5 WHERE owner_call_site_version=?6",params![money_delta,estimated_delta,i64::from(charged)-i64::from(old_charged),i64::from(charged && !complete)-i64::from(old_charged && old_unknown),extra-old_extra,owner])?;
    Ok(())
}

pub(crate) fn seal_batch(conn: &Connection, id: &str, persistence_failed: bool) -> Result<()> {
    let owned: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_economic_batch WHERE batch_id=?1)",
        [id],
        |row| row.get(0),
    )?;
    if !owned {
        return Ok(());
    }
    // Coverage is distinct from billing: a missing or mismatched root remains
    // unknown even when every visible invocation reports some cost.
    let expected: Option<String> = conn.query_row(
        "SELECT expected_calls_json FROM provider_receipt_batch WHERE batch_id=?1",
        [id],
        |row| row.get(0),
    )?;
    let declared: Vec<crate::multi_call::CallPosition> = expected
        .map(|json| serde_json::from_str(&json))
        .transpose()?
        .unwrap_or_default();
    let positions = {
        let mut query=conn.prepare("SELECT item,logical_call FROM provider_economic_branch WHERE batch_id=?1 ORDER BY item,logical_call")?;
        let rows = query
            .query_map([id], |row| {
                Ok(crate::multi_call::CallPosition {
                    item: row.get(0)?,
                    call: row.get(1)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let roots = {
        let mut query=conn.prepare("SELECT item,logical_call FROM provider_receipt_invocation WHERE batch_id=?1 AND ordinal=0 AND json_extract(invocation_json,'$.role') IN ('primary','fallback') ORDER BY item,logical_call")?;
        let rows = query
            .query_map([id], |row| {
                Ok(crate::multi_call::CallPosition {
                    item: row.get(0)?,
                    call: row.get(1)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let coverage =
        roots == declared && declared.iter().all(|position| positions.contains(position));
    let (owner,wall,failed,old_coverage):(String,Option<f64>,bool,bool)=conn.query_row("SELECT owner_call_site_version,wall_latency_ms,persistence_failed,coverage_complete FROM provider_economic_batch WHERE batch_id=?1",[id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?)))?;
    let old_incomplete = wall.is_none() || failed || !old_coverage;
    let new_incomplete = wall.is_none() || failed || persistence_failed || !coverage;
    conn.execute("UPDATE provider_economic_cohort SET incomplete_batches=incomplete_batches+?1 WHERE owner_call_site_version=?2",params![i64::from(new_incomplete)-i64::from(old_incomplete),owner])?;
    conn.execute("UPDATE provider_economic_batch SET coverage_complete=?1,persistence_failed=MAX(persistence_failed,?2) WHERE batch_id=?3",params![coverage,persistence_failed,id])?;
    Ok(())
}

/// The driver records full foreground time after publishing its observation.
/// Post-foreground dispatch durations are additional conservative time charges.
pub(crate) fn finish_batch(conn: &Connection, id: &str, wall: f64) -> Result<()> {
    if !wall.is_finite() || wall < 0.0 {
        bail!("provider batch foreground time is invalid");
    }
    let sealed: bool = conn.query_row(
        "SELECT sealed FROM provider_receipt_batch WHERE batch_id=?1",
        [id],
        |row| row.get(0),
    )?;
    if !sealed {
        bail!("provider foreground has not been sealed");
    }
    let prior: Option<f64> = conn
        .query_row(
            "SELECT wall_latency_ms FROM provider_economic_batch WHERE batch_id=?1",
            [id],
            |row| row.get(0),
        )
        .context("provider economic owner is unavailable")?;
    if prior.is_some_and(|old| old != wall) {
        bail!("provider foreground metric conflicts");
    }
    let (owner,phase,failed,coverage):(String,String,bool,bool)=conn.query_row("SELECT owner_call_site_version,phase,persistence_failed,coverage_complete FROM provider_economic_batch WHERE batch_id=?1",[id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?)))?;
    if prior.is_none() {
        conn.execute("UPDATE provider_economic_cohort SET incomplete_batches=incomplete_batches-?1,acquisition_latency_ms=acquisition_latency_ms+?2 WHERE owner_call_site_version=?3",params![i64::from(!failed && coverage),if phase=="acquisition" {wall} else {0.0},owner])?;
    }
    conn.execute("UPDATE provider_economic_batch SET wall_latency_ms=?1 WHERE batch_id=?2 AND wall_latency_ms IS NULL",params![wall,id])?;
    Ok(())
}

pub fn cover_acquisition(
    conn: &Connection,
    id: &str,
    observation: &crate::plan_profile::PlanObservationToken,
) -> Result<()> {
    ensure_schema(conn)?;
    let owner: Option<(String, String)> = conn
        .query_row(
            "SELECT owner_call_site_version,phase FROM provider_economic_batch WHERE batch_id=?1",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    let Some((owner, phase)) = owner else {
        return Ok(());
    };
    if owner != observation.key().call_site_version.as_str() || phase != "acquisition" {
        bail!("profile acquisition does not match its physical economic phase");
    }
    let inserted = conn.execute(
        "INSERT INTO provider_economic_publication VALUES(?1,?2,?3,?4) ON CONFLICT DO NOTHING",
        params![
            id,
            owner,
            observation.key().execution_plan_id.as_str(),
            observation.sequence()
        ],
    )?;
    let old:(String,String,u64)=conn.query_row("SELECT call_site_version,execution_plan_id,observation_sequence FROM provider_economic_publication WHERE batch_id=?1",[id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?)))?;
    if old
        != (
            owner,
            observation.key().execution_plan_id.to_string(),
            observation.sequence(),
        )
    {
        bail!("provider acquisition publication conflicts");
    }
    if inserted == 1 {
        let (cost,latency):(f64,f64)=conn.query_row("SELECT cost_usd,latency_ms FROM execution_plan_observation WHERE call_site_version=?1 AND execution_plan_id=?2 AND sample_sequence=?3",params![observation.key().call_site_version.as_str(),observation.key().execution_plan_id.as_str(),observation.sequence()],|row|Ok((row.get(0)?,row.get(1)?)))?;
        conn.execute("UPDATE provider_economic_cohort SET covered_executions=covered_executions+1,covered_cost_usd=covered_cost_usd+?1,covered_latency_ms=covered_latency_ms+?2 WHERE owner_call_site_version=?3",params![cost,latency,observation.key().call_site_version.as_str()])?;
    }
    Ok(())
}

pub fn summarize_cohort(
    conn: &Connection,
    owner: &crate::plan_profile::CallSiteVersion,
) -> Result<Economics> {
    ensure_schema(conn)?;
    let row:Option<(Economics,u64,f64,f64)>=conn.query_row("SELECT owned_batches,known_cost_usd,estimated_cost_usd,charged_invocations,unknown_cost_invocations,incomplete_batches,acquisition_latency_ms,covered_executions,covered_cost_usd,covered_latency_ms,pending_billing_verifications FROM provider_economic_cohort WHERE owner_call_site_version=?1",[owner.as_str()],|row|Ok((Economics {
        owned_batches:row.get(0)?,known_cost_usd:row.get(1)?,estimated_cost_usd:row.get(2)?,charged_invocations:row.get(3)?,unknown_cost_invocations:row.get(4)?,incomplete_batches:row.get(5)?,acquisition_latency_ms:row.get(6)?,pending_billing_verifications:row.get(10)?,..Economics::default()
    },row.get(7)?,row.get(8)?,row.get(9)?))).optional()?;
    let (mut result, covered, covered_cost, covered_latency) = row.unwrap_or_default();
    crate::multi_call_comparison::ensure_schema(conn)?;
    let (legacy,cost,latency):(u64,f64,f64)=conn.query_row("SELECT COALESCE(SUM(completed_executions),0),COALESCE(SUM(total_cost_usd),0),COALESCE(SUM(total_latency_ms),0) FROM multi_call_comparison_spend WHERE call_site_version=?1",[owner.as_str()],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?)))?;
    result.uncovered_profile_executions = legacy.saturating_sub(covered);
    if result.uncovered_profile_executions > 0 {
        // Retain known historical charges, while refusing to treat them as
        // complete physical accounting or to charge covered work twice.
        result.known_cost_usd += (cost - covered_cost).max(0.0);
        result.acquisition_latency_ms += (latency - covered_latency).max(0.0);
    }
    if [
        result.known_cost_usd,
        result.estimated_cost_usd,
        result.acquisition_latency_ms,
        cost,
        latency,
        covered_cost,
        covered_latency,
    ]
    .into_iter()
    .any(|value| !value.is_finite() || value < 0.0)
    {
        bail!("provider acquisition economics overflow");
    }
    result.cost_complete = result.owned_batches > 0
        && result.incomplete_batches == 0
        && result.unknown_cost_invocations == 0
        && result.uncovered_profile_executions == 0
        && result.pending_billing_verifications == 0;
    Ok(result)
}
