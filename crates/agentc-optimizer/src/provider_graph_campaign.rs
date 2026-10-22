//! Shared immutable commitments across graph learning and independent deployment.
//! Declared monetary reservations are accounting limits, not provider price caps.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::provider_graph_economics::{self as finance, UsdInterval};
use crate::provider_graph_preparation::Lease;
use crate::provider_graph_sampling::{CommitmentMoney, Commitments, Finished, Ticket};
use crate::provider_receipts::{Batch, Invocation};

pub const VERSION: &str = "provider-graph-campaign-v1";

#[derive(Debug)]
struct BindingRequired;
impl std::fmt::Display for BindingRequired {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("graph driver cannot omit or replace its original campaign commitment")
    }
}
impl std::error::Error for BindingRequired {}

/// Preserve a binding rejection across the SDK boundary as a dispatch stop.
pub fn binding_abstention(error: &anyhow::Error) -> Option<String> {
    error.downcast_ref::<BindingRequired>().map(|_| {
        serde_json::json!({"status":"abstained","reason":error.to_string(),"hard_stop":"graph_campaign_binding"}).to_string()
    })
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub campaign_id: String,
    pub commitments: Commitments,
    pub max_workflows: u64,
    pub max_physical_invocations: u64,
    pub max_lifetime_physical_invocations: u64,
}
impl Policy {
    pub(crate) fn validate(&self) -> Result<()> {
        crate::execution_plan::ExecutionPlanId::parse(&self.campaign_id)?;
        self.commitments.validate()?;
        if self.max_workflows == 0
            || self.max_workflows > 1 << 53
            || self.max_physical_invocations == 0
            || self.max_physical_invocations > self.max_lifetime_physical_invocations
            || self.max_lifetime_physical_invocations > 1 << 53
        {
            bail!("graph campaign lifetime limits are invalid");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Owner {
    Sampling { ticket_id: String },
    Deployment { lease_id: String },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Enrollment {
    version: String,
    policy: Policy,
    ordinal: u64,
    owner: Owner,
    owner_sha256: String,
    batches: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct Report {
    pub campaign_id: String,
    pub policy: Policy,
    pub workflows: u64,
    pub open_workflows: u64,
    pub held_workflows: u64,
    pub unknown_cost_workflows: u64,
    pub physical_invocations: u64,
    pub committed_physical_invocations: u64,
    pub pending_invocations: u64,
    pub billed_committed_upper_usd: Option<f64>,
    pub tariff_committed_upper_nanodollars: Option<u64>,
    pub budget_exceeded: bool,
}

fn canonical(value: &impl Serialize) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}
fn exists(conn: &Connection, table: &str) -> Result<bool> {
    Ok(conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
        [table],
        |r| r.get(0),
    )?)
}

/// A driver that has joined a campaign cannot drop or replace that commitment.
/// Derive the binding from original native requests, including separate sampling
/// states for explicit candidate pairs, rather than a resettable projection.
pub(crate) fn verify_driver(
    conn: &Connection,
    batch: &Batch,
    requested: Option<&Policy>,
) -> Result<()> {
    let driver = crate::provider_graph_acquisition::Driver::from_batch(batch)?;
    let matches = |original: &Policy| -> Result<()> {
        original.validate()?;
        if requested != Some(original) {
            return Err(BindingRequired.into());
        }
        Ok(())
    };
    if exists(conn, "provider_graph_sampling")? {
        let mut q = conn.prepare("SELECT ticket_sha256,ticket_json FROM provider_graph_sampling WHERE json_extract(ticket_json,'$.request.policy.campaign') IS NOT NULL AND json_extract(ticket_json,'$.request.reference_batch.application_version')=?1 AND json_extract(ticket_json,'$.request.reference_batch.callback_version')=?2 AND json_extract(ticket_json,'$.request.reference_batch.item_count')=?3")?;
        for row in q.query_map(
            params![
                driver.application_version,
                driver.callback_version,
                driver.item_count
            ],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
        )? {
            let (hash, raw) = row?;
            let ticket: Ticket = serde_json::from_str(&raw)?;
            if content_hash(&raw) != hash || canonical(&ticket)? != raw {
                bail!("campaign driver sampling request custody changed");
            }
            if crate::provider_graph_acquisition::Driver::from_batch(
                &ticket.request.reference_batch,
            )? != driver
                || crate::provider_graph_acquisition::Driver::from_batch(
                    &ticket.request.candidate_batch,
                )? != driver
            {
                bail!("campaign driver sampling batch custody changed");
            }
            matches(
                ticket
                    .request
                    .policy
                    .campaign
                    .as_ref()
                    .context("original sampling campaign is missing")?,
            )?;
        }
    }
    if exists(conn, "provider_graph_deployment")? {
        let mut q = conn.prepare("SELECT d.approval_sha256,d.approval_json,p.lease_id,b.metadata_json FROM provider_graph_deployment d JOIN provider_graph_preparation p USING(lease_id) JOIN provider_receipt_batch b ON b.batch_id=d.batch_id WHERE json_extract(d.approval_json,'$.request.campaign') IS NOT NULL AND json_extract(b.metadata_json,'$.application_version')=?1 AND json_extract(b.metadata_json,'$.callback_version')=?2 AND json_extract(b.metadata_json,'$.item_count')=?3")?;
        for row in q.query_map(
            params![
                driver.application_version,
                driver.callback_version,
                driver.item_count
            ],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                ))
            },
        )? {
            let (hash, raw, lease_id, metadata) = row?;
            let value: serde_json::Value = serde_json::from_str(&raw)?;
            let lease = crate::provider_graph_preparation::load(conn, &lease_id)?;
            let original: Batch = serde_json::from_str(&metadata)?;
            if content_hash(&raw) != hash
                || canonical_json(&value) != raw
                || canonical(&original)? != metadata
                || content_hash(&metadata) != lease.metadata_sha256
                || original.batch_id != lease.batch_id
                || value["request"]["preparation"]["batch_id"] != lease.batch_id
                || crate::provider_graph_acquisition::Driver::from_batch(&original)? != driver
            {
                bail!("campaign driver deployment request custody changed");
            }
            let policy: Policy = serde_json::from_value(value["request"]["campaign"].clone())?;
            matches(&policy)?;
        }
    }
    Ok(())
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch("CREATE TABLE IF NOT EXISTS provider_graph_campaign (
        campaign_id TEXT PRIMARY KEY,policy_sha256 TEXT NOT NULL,policy_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS provider_graph_campaign_workflow (
        enrollment_id TEXT PRIMARY KEY,campaign_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
        enrollment_sha256 TEXT NOT NULL,enrollment_json TEXT NOT NULL,
        UNIQUE(campaign_id,ordinal),FOREIGN KEY(campaign_id) REFERENCES provider_graph_campaign(campaign_id));
        CREATE TABLE IF NOT EXISTS provider_graph_campaign_batch (
        batch_id TEXT PRIMARY KEY,enrollment_id TEXT NOT NULL,
        FOREIGN KEY(batch_id) REFERENCES provider_receipt_batch(batch_id),
        FOREIGN KEY(enrollment_id) REFERENCES provider_graph_campaign_workflow(enrollment_id));
        CREATE TABLE IF NOT EXISTS provider_graph_campaign_invocation (
        invocation_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,enrollment_id TEXT NOT NULL,invocation_sha256 TEXT NOT NULL,
        FOREIGN KEY(invocation_id) REFERENCES provider_receipt_invocation(invocation_id),
        FOREIGN KEY(batch_id) REFERENCES provider_graph_campaign_batch(batch_id),
        FOREIGN KEY(enrollment_id) REFERENCES provider_graph_campaign_workflow(enrollment_id));")?;
    Ok(())
}
fn policy(conn: &Connection, id: &str) -> Result<Policy> {
    let (hash, raw): (String, String) = conn
        .query_row(
            "SELECT policy_sha256,policy_json FROM provider_graph_campaign WHERE campaign_id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .context("original graph campaign policy is missing")?;
    let p: Policy = serde_json::from_str(&raw)?;
    p.validate()?;
    if p.campaign_id != id || canonical(&p)? != raw || content_hash(&raw) != hash {
        bail!("graph campaign policy differs from its original commitment");
    }
    Ok(p)
}

fn sampling_batches(t: &Ticket) -> Vec<String> {
    if t.deployed {
        vec![t.request.candidate_batch.batch_id.clone()]
    } else if t.sampled {
        vec![
            t.request.reference_batch.batch_id.clone(),
            t.request.candidate_batch.batch_id.clone(),
        ]
    } else {
        vec![t.request.reference_batch.batch_id.clone()]
    }
}

struct Custody {
    policy: Policy,
    hash: String,
    batches: Vec<String>,
    finished: bool,
    complete: bool,
}
fn owner(conn: &Connection, value: &Owner) -> Result<Custody> {
    match value {
        Owner::Sampling { ticket_id } => {
            let (hash,raw,result_hash,result):(String,String,Option<String>,Option<String>)=conn.query_row("SELECT ticket_sha256,ticket_json,result_sha256,result_json FROM provider_graph_sampling WHERE ticket_id=?1",[ticket_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
            let t: Ticket = serde_json::from_str(&raw)?;
            t.request.policy.validate()?;
            if content_hash(&raw) != hash || canonical(&t)? != raw || t.ticket_id != *ticket_id {
                bail!("campaign sampling owner custody changed");
            }
            let finished = result.is_some();
            let mut complete = false;
            if let Some(raw) = result {
                let done: Finished = serde_json::from_str(&raw)?;
                if result_hash.as_deref() != Some(content_hash(&raw).as_str())
                    || canonical(&done)? != raw
                    || done.ticket_id != *ticket_id
                    || done.sampled != t.sampled
                {
                    bail!("campaign sampling completion custody changed");
                }
                if ![
                    "paired",
                    "reference_only",
                    "deployed",
                    "no_alternative",
                    "incomplete",
                ]
                .contains(&done.status.as_str())
                {
                    bail!("campaign sampling completion status is invalid");
                }
                complete = done.status != "incomplete";
                if let Some(pair) = done.pair_id {
                    crate::provider_graph_comparison::load(conn, &pair)?;
                }
            } else if result_hash.is_some() {
                bail!("campaign sampling completion custody is incomplete");
            }
            let batches = sampling_batches(&t);
            for batch in &batches {
                let (sealed,execution):(bool,Option<String>)=conn.query_row("SELECT b.sealed,p.execution_id FROM provider_receipt_batch b JOIN provider_graph_preparation p USING(batch_id) WHERE b.batch_id=?1",[batch],|r|Ok((r.get(0)?,r.get(1)?)))?;
                if finished && !sealed {
                    bail!("campaign sampling completion has an open batch");
                }
                if let Some(id) = execution {
                    crate::provider_graph::load(conn, &id)?;
                } else {
                    complete = false;
                }
            }
            Ok(Custody {
                policy: t
                    .request
                    .policy
                    .campaign
                    .context("sampling owner has no campaign commitment")?,
                hash,
                batches,
                finished,
                complete,
            })
        }
        Owner::Deployment { lease_id } => {
            let (batch,hash,raw):(String,String,String)=conn.query_row("SELECT batch_id,approval_sha256,approval_json FROM provider_graph_deployment WHERE lease_id=?1",[lease_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
            let value: serde_json::Value = serde_json::from_str(&raw)?;
            if content_hash(&raw) != hash || canonical_json(&value) != raw {
                bail!("campaign deployment owner custody changed");
            }
            let p: Policy = serde_json::from_value(value["request"]["campaign"].clone())?;
            p.validate()?;
            let kind: finance::Kind =
                serde_json::from_value(value["request"]["admission"]["kind"].clone())?;
            let selected: crate::plan_profile::PlanProfileKey =
                serde_json::from_value(value["decision"]["selected_key"].clone())?;
            let (sealed,persistence_failed):(bool,bool)=conn.query_row("SELECT b.sealed,a.persistence_failed FROM provider_receipt_batch b JOIN provider_graph_acquisition a USING(batch_id) WHERE b.batch_id=?1",[&batch],|r|Ok((r.get(0)?,r.get(1)?)))?;
            let execution: Option<String> = match kind {
                finance::Kind::Primary => conn.query_row(
                    "SELECT execution_id FROM provider_graph_preparation WHERE lease_id=?1",
                    [lease_id],
                    |r| r.get(0),
                )?,
                finance::Kind::Roles => conn
                    .query_row(
                        "SELECT execution_id FROM provider_graph_role_execution WHERE lease_id=?1",
                        [lease_id],
                        |r| r.get(0),
                    )
                    .optional()?,
            };
            let complete = if sealed && !persistence_failed {
                if let Some(id) = execution {
                    let projection = finance::execution(conn, kind, &id)?;
                    projection.key == selected && projection.resource_latency_ms.is_some()
                } else {
                    false
                }
            } else {
                false
            };
            Ok(Custody {
                policy: p,
                hash,
                batches: vec![batch],
                finished: sealed,
                complete,
            })
        }
    }
}

fn load_enrollment(conn: &Connection, id: &str) -> Result<(Enrollment, Custody)> {
    let (campaign,ordinal,hash,raw):(String,u64,String,String)=conn.query_row("SELECT campaign_id,ordinal,enrollment_sha256,enrollment_json FROM provider_graph_campaign_workflow WHERE enrollment_id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).context("original campaign workflow enrollment is missing")?;
    let e: Enrollment = serde_json::from_str(&raw)?;
    if e.version != VERSION
        || e.policy.campaign_id != campaign
        || e.ordinal != ordinal
        || ordinal == 0
        || content_hash(&raw) != hash
        || hash != id
        || canonical(&e)? != raw
        || policy(conn, &campaign)? != e.policy
    {
        bail!("campaign enrollment differs from its original commitment");
    }
    let original = owner(conn, &e.owner)?;
    if original.policy != e.policy
        || original.hash != e.owner_sha256
        || original.batches != e.batches
    {
        bail!("campaign workflow owner or batch custody changed");
    }
    let rows = {
        let mut q=conn.prepare("SELECT batch_id FROM provider_graph_campaign_batch WHERE enrollment_id=?1 ORDER BY batch_id")?;
        let rows = q
            .query_map([id], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let mut expected = e.batches.clone();
    expected.sort();
    if rows != expected {
        bail!("campaign workflow batch coverage is missing or changed");
    }
    Ok((e, original))
}

fn expected(conn: &Connection, batch: &str) -> Result<Option<Owner>> {
    if exists(conn, "provider_graph_sampling")? {
        let t:Option<String>=conn.query_row("SELECT ticket_json FROM provider_graph_sampling WHERE reference_batch_id=?1 OR candidate_batch_id=?1",[batch],|r|r.get(0)).optional()?;
        if let Some(raw) = t {
            let t: Ticket = serde_json::from_str(&raw)?;
            if t.request.policy.campaign.is_some() {
                return Ok(Some(Owner::Sampling {
                    ticket_id: t.ticket_id,
                }));
            }
        }
    }
    if exists(conn, "provider_graph_deployment")? {
        let id:Option<String>=conn.query_row("SELECT lease_id FROM provider_graph_deployment WHERE batch_id=?1 AND json_extract(approval_json,'$.request.campaign') IS NOT NULL",[batch],|r|r.get(0)).optional()?;
        if let Some(lease_id) = id {
            return Ok(Some(Owner::Deployment { lease_id }));
        }
    }
    Ok(None)
}
fn enrollment_for_batch(conn: &Connection, batch: &str) -> Result<Option<(String, Enrollment)>> {
    let original = expected(conn, batch)?;
    if !exists(conn, "provider_graph_campaign_batch")? {
        if original.is_some() {
            bail!("campaign ledger custody is missing before dispatch");
        }
        return Ok(None);
    }
    let id: Option<String> = conn
        .query_row(
            "SELECT enrollment_id FROM provider_graph_campaign_batch WHERE batch_id=?1",
            [batch],
            |r| r.get(0),
        )
        .optional()?;
    match (original, id) {
        (None, None) => Ok(None),
        (Some(original), Some(id)) => {
            let (e, _) = load_enrollment(conn, &id)?;
            if e.owner != original {
                bail!("campaign enrollment belongs to another workflow");
            }
            Ok(Some((id, e)))
        }
        _ => bail!("campaign workflow coverage is missing or changed"),
    }
}

fn report_inner(conn: &Connection, id: &str) -> Result<Report> {
    report_issuing(conn, id, None)
}

fn report_issuing(conn: &Connection, id: &str, issuing_ticket: Option<&str>) -> Result<Report> {
    let p = policy(conn, id)?;
    let mut report = Report {
        campaign_id: id.into(),
        policy: p.clone(),
        workflows: 0,
        open_workflows: 0,
        held_workflows: 0,
        unknown_cost_workflows: 0,
        physical_invocations: 0,
        committed_physical_invocations: 0,
        pending_invocations: 0,
        billed_committed_upper_usd: None,
        tariff_committed_upper_nanodollars: None,
        budget_exceeded: false,
    };
    let missing_sampling = if exists(conn, "provider_graph_sampling")? {
        conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_graph_sampling s WHERE json_extract(s.ticket_json,'$.request.policy.campaign.campaign_id')=?1 AND (?2 IS NULL OR s.ticket_id!=?2) AND NOT EXISTS(SELECT 1 FROM provider_graph_campaign_workflow w WHERE json_extract(w.enrollment_json,'$.owner.ticket_id')=s.ticket_id))",params![id,issuing_ticket],|r|r.get::<_,bool>(0))?
    } else {
        false
    };
    let missing_deployment = if exists(conn, "provider_graph_deployment")? {
        conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_graph_deployment d JOIN provider_graph_preparation p USING(lease_id) WHERE json_extract(d.approval_json,'$.request.campaign.campaign_id')=?1 AND json_extract(p.lease_json,'$.sampling_id') IS NULL AND NOT EXISTS(SELECT 1 FROM provider_graph_campaign_workflow w WHERE json_extract(w.enrollment_json,'$.owner.lease_id')=d.lease_id))",[id],|r|r.get::<_,bool>(0))?
    } else {
        false
    };
    if missing_sampling || missing_deployment {
        bail!("shared campaign lifetime enrollment coverage is incomplete");
    }
    let orphan:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_graph_campaign_invocation i LEFT JOIN provider_graph_campaign_workflow w USING(enrollment_id) LEFT JOIN provider_graph_campaign_batch b USING(batch_id) LEFT JOIN provider_receipt_invocation r USING(invocation_id) WHERE w.enrollment_id IS NULL OR (w.campaign_id=?1 AND (b.enrollment_id IS NULL OR b.enrollment_id!=i.enrollment_id OR r.invocation_id IS NULL OR r.batch_id!=i.batch_id)))",[id],|r|r.get(0))?;
    if orphan {
        bail!("campaign physical slot ownership is missing or changed");
    }
    let mut billed = UsdInterval::exact(0.)?;
    let mut tariff = 0_u64;
    let mut q=conn.prepare("SELECT enrollment_id FROM provider_graph_campaign_workflow WHERE campaign_id=?1 ORDER BY ordinal")?;
    for row in q.query_map([id], |r| r.get::<_, String>(0))? {
        let enrollment_id = row?;
        let (e, original) = load_enrollment(conn, &enrollment_id)?;
        report.workflows += 1;
        if e.ordinal != report.workflows {
            bail!("campaign lifetime workflow counter custody is incomplete");
        }
        report.open_workflows += u64::from(!original.finished);
        let mut count = 0_u64;
        let mut unknown = false;
        let mut known = UsdInterval::exact(0.)?;
        let mut priced = 0_u64;
        for batch in &e.batches {
            let missing:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_receipt_invocation r WHERE batch_id=?1 AND NOT EXISTS(SELECT 1 FROM provider_graph_campaign_invocation i WHERE i.invocation_id=r.invocation_id AND i.batch_id=r.batch_id AND i.enrollment_id=?2))",params![batch,enrollment_id],|r|r.get(0))?;
            if missing {
                bail!("campaign physical invocation coverage is incomplete");
            }
            let mut calls=conn.prepare("SELECT i.invocation_id,i.invocation_sha256,r.invocation_json,r.completion_json IS NULL FROM provider_graph_campaign_invocation i LEFT JOIN provider_receipt_invocation r USING(invocation_id) WHERE i.enrollment_id=?1 AND i.batch_id=?2")?;
            for call in calls.query_map(params![enrollment_id, batch], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, Option<String>>(2)?,
                    r.get::<_, bool>(3)?,
                ))
            })? {
                let (call, hash, raw, pending) = call?;
                count += 1;
                report.pending_invocations += u64::from(pending);
                let raw = raw.context("campaign original physical invocation is missing")?;
                if content_hash(&raw) != hash {
                    bail!("campaign physical invocation custody changed");
                }
                let receipt = finance::receipt(conn, batch, &call)?;
                match &p.commitments.money {
                    CommitmentMoney::BilledUsd { .. } => {
                        known = known.add(UsdInterval::exact(receipt.known_billed_usd)?)?;
                        unknown |= receipt.billed_usd.is_none();
                    }
                    CommitmentMoney::PublishedTokenTariffUsd {
                        snapshot_sha256, ..
                    } => {
                        if let Some(bound) = receipt.tariff {
                            if bound.snapshot_sha256 != *snapshot_sha256 {
                                bail!("campaign tariff snapshot changed");
                            }
                            priced = priced
                                .checked_add(bound.interval.upper_nanodollars)
                                .context("campaign tariff sum overflow")?;
                        } else {
                            unknown = true;
                        }
                    }
                }
            }
        }
        report.physical_invocations = report
            .physical_invocations
            .checked_add(count)
            .context("campaign physical count overflow")?;
        report.committed_physical_invocations = report
            .committed_physical_invocations
            .checked_add(if original.finished {
                count
            } else {
                p.max_physical_invocations
            })
            .context("campaign physical reservation overflow")?;
        report.budget_exceeded |= count > p.max_physical_invocations;
        let held = !original.complete || unknown;
        report.held_workflows += u64::from(held);
        report.unknown_cost_workflows += u64::from(unknown);
        match p.commitments.money {
            CommitmentMoney::BilledUsd { workflow_usd, .. } => {
                billed = billed.add(UsdInterval::exact(if held {
                    known.upper_usd.max(workflow_usd)
                } else {
                    known.upper_usd
                })?)?;
                report.budget_exceeded |= known.upper_usd > workflow_usd;
            }
            CommitmentMoney::PublishedTokenTariffUsd {
                workflow_nanodollars,
                ..
            } => {
                tariff = tariff
                    .checked_add(if held {
                        priced.max(workflow_nanodollars)
                    } else {
                        priced
                    })
                    .context("campaign monetary reservation overflow")?;
                report.budget_exceeded |= priced > workflow_nanodollars;
            }
        }
    }
    report.budget_exceeded |= report.workflows > p.max_workflows
        || report.committed_physical_invocations > p.max_lifetime_physical_invocations;
    match p.commitments.money {
        CommitmentMoney::BilledUsd { lifetime_usd, .. } => {
            report.billed_committed_upper_usd = Some(billed.upper_usd);
            report.budget_exceeded |= billed.upper_usd > lifetime_usd;
        }
        CommitmentMoney::PublishedTokenTariffUsd {
            lifetime_nanodollars,
            ..
        } => {
            report.tariff_committed_upper_nanodollars = Some(tariff);
            report.budget_exceeded |= tariff > lifetime_nanodollars;
        }
    }
    Ok(report)
}

pub fn report(conn: &Connection, campaign_id: &str) -> Result<Report> {
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let value = report_inner(&tx, campaign_id)?;
        tx.commit()?;
        Ok(value)
    } else {
        report_inner(conn, campaign_id)
    }
}

fn enroll(
    conn: &Connection,
    policy: &Policy,
    owner: Owner,
    owner_hash: String,
    batches: Vec<String>,
) -> Result<()> {
    policy.validate()?;
    ensure_schema(conn)?;
    let raw = canonical(policy)?;
    let existed: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_graph_campaign WHERE campaign_id=?1)",
        [&policy.campaign_id],
        |r| r.get(0),
    )?;
    if existed && self::policy(conn, &policy.campaign_id)? != *policy {
        bail!("shared campaign policy cannot replenish its original allowance");
    }
    if !existed {
        let prior: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM provider_graph_campaign_workflow WHERE campaign_id=?1)",
            [&policy.campaign_id],
            |r| r.get(0),
        )?;
        if prior {
            bail!("original shared campaign policy custody is missing");
        }
        conn.execute(
            "INSERT INTO provider_graph_campaign VALUES(?1,?2,?3)",
            params![policy.campaign_id, content_hash(&raw), raw],
        )?;
    }
    let prior = {
        let mut q=conn.prepare("SELECT enrollment_id FROM provider_graph_campaign_workflow WHERE campaign_id=?1 ORDER BY ordinal")?;
        let rows = q
            .query_map([&policy.campaign_id], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let e = Enrollment {
        version: VERSION.into(),
        policy: policy.clone(),
        ordinal: prior.len() as u64 + 1,
        owner,
        owner_sha256: owner_hash,
        batches,
    };
    let encoded = canonical(&e)?;
    let id = content_hash(&encoded);
    conn.execute(
        "INSERT INTO provider_graph_campaign_workflow VALUES(?1,?2,?3,?1,?4)",
        params![id, policy.campaign_id, e.ordinal, encoded],
    )?;
    for batch in &e.batches {
        conn.execute(
            "INSERT INTO provider_graph_campaign_batch VALUES(?1,?2)",
            params![batch, id],
        )?;
    }
    let r = report_inner(conn, &policy.campaign_id)?;
    if r.budget_exceeded
        || r.open_workflows > policy.commitments.max_open_workflows
        || r.workflows > policy.max_workflows
    {
        bail!("shared campaign commitment or lifetime workflow/physical allowance is exhausted");
    }
    Ok(())
}

pub(crate) fn enroll_sampling(conn: &Connection, p: &Policy, t: &Ticket) -> Result<()> {
    let hash: String = conn.query_row(
        "SELECT ticket_sha256 FROM provider_graph_sampling WHERE ticket_id=?1",
        [&t.ticket_id],
        |r| r.get(0),
    )?;
    enroll(
        conn,
        p,
        Owner::Sampling {
            ticket_id: t.ticket_id.clone(),
        },
        hash,
        sampling_batches(t),
    )
}
pub(crate) fn enroll_deployment(
    conn: &Connection,
    p: &Policy,
    lease: &Lease,
    hash: &str,
) -> Result<()> {
    enroll(
        conn,
        p,
        Owner::Deployment {
            lease_id: lease.lease_id.clone(),
        },
        hash.into(),
        vec![lease.batch_id.clone()],
    )
}

pub(crate) fn verify_dispatch(conn: &Connection, lease: &Lease, call: &Invocation) -> Result<()> {
    let Some((_, e)) = enrollment_for_batch(conn, &lease.batch_id)? else {
        let metadata: String = conn.query_row(
            "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
            [&lease.batch_id],
            |r| r.get(0),
        )?;
        let batch: Batch = serde_json::from_str(&metadata)?;
        verify_driver(conn, &batch, None)?;
        return Ok(());
    };
    let r = report_inner(conn, &e.policy.campaign_id)?;
    if let CommitmentMoney::PublishedTokenTariffUsd {
        snapshot_sha256, ..
    } = &e.policy.commitments.money
    {
        if call
            .tariff_declaration
            .as_ref()
            .is_none_or(|d| d.snapshot_sha256 != *snapshot_sha256 || !d.request_scope_supported)
        {
            bail!("campaign dispatch requires its pinned tariff declaration");
        }
    }
    let (retry,count):(bool,u64)=conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE invocation_id=?1),(SELECT COUNT(*) FROM provider_graph_campaign_invocation WHERE enrollment_id=?2)",params![call.invocation_id,content_hash(&canonical(&e)?)],|r|Ok((r.get(0)?,r.get(1)?)))?;
    if !retry
        && (r.budget_exceeded
            || r.pending_invocations >= e.policy.commitments.max_pending_invocations
            || count >= e.policy.max_physical_invocations)
    {
        bail!("shared campaign monetary commitment or pending/physical call limit is exhausted");
    }
    let original = owner(conn, &e.owner)?;
    if original.finished {
        bail!("shared campaign workflow is already finished");
    }
    Ok(())
}

pub(crate) fn record_dispatch(conn: &Connection, batch: &str, call: &Invocation) -> Result<()> {
    let Some((id, _)) = enrollment_for_batch(conn, batch)? else {
        return Ok(());
    };
    let hash = content_hash(&canonical(call)?);
    conn.execute(
        "INSERT INTO provider_graph_campaign_invocation VALUES(?1,?2,?3,?4) ON CONFLICT DO NOTHING",
        params![call.invocation_id, batch, id, hash],
    )?;
    let original:(String,String,String)=conn.query_row("SELECT batch_id,enrollment_id,invocation_sha256 FROM provider_graph_campaign_invocation WHERE invocation_id=?1",[&call.invocation_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
    if original != (batch.into(), id, hash) {
        bail!("campaign dispatch retry changed its original physical reservation");
    }
    Ok(())
}

/// Bound the benefit forecast by remaining immutable monetary and physical
/// reservations. The current workflow is included during a dispatch recheck.
pub(crate) fn remaining_horizon(
    conn: &Connection,
    p: &Policy,
    issuing_ticket: Option<&str>,
    current: bool,
) -> Result<u64> {
    p.validate()?;
    let original = if exists(conn, "provider_graph_campaign")? {
        let saved: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM provider_graph_campaign WHERE campaign_id=?1)",
            [&p.campaign_id],
            |r| r.get(0),
        )?;
        if saved {
            if policy(conn, &p.campaign_id)? != *p {
                bail!("campaign forecast changed its original policy");
            }
            Some(report_issuing(conn, &p.campaign_id, issuing_ticket)?)
        } else {
            None
        }
    } else {
        None
    };
    if original.as_ref().is_some_and(|r| r.budget_exceeded) {
        return Ok(0);
    }
    let workflows = original.as_ref().map_or(0, |r| r.workflows);
    let physical = original
        .as_ref()
        .map_or(0, |r| r.committed_physical_invocations);
    let mut count = p.max_workflows.saturating_sub(workflows).min(
        p.max_lifetime_physical_invocations.saturating_sub(physical) / p.max_physical_invocations,
    );
    match &p.commitments.money {
        CommitmentMoney::BilledUsd {
            workflow_usd,
            lifetime_usd,
        } => {
            let used = original
                .as_ref()
                .and_then(|r| r.billed_committed_upper_usd)
                .unwrap_or(0.);
            let available = (*lifetime_usd - used).max(0.);
            let tentative = (available / workflow_usd).floor().min(count as f64) as u64;
            count = count.min(tentative);
            // Outward-rounded verification also covers an integer boundary that
            // floating division rounded up. One workflow keeps exact arithmetic.
            while count > 0
                && UsdInterval::exact(used)?
                    .add(UsdInterval::exact(*workflow_usd)?.scale(count)?)?
                    .upper_usd
                    > *lifetime_usd
            {
                count -= 1;
            }
        }
        CommitmentMoney::PublishedTokenTariffUsd {
            workflow_nanodollars,
            lifetime_nanodollars,
            ..
        } => {
            let used = original
                .as_ref()
                .and_then(|r| r.tariff_committed_upper_nanodollars)
                .unwrap_or(0);
            count = count.min(lifetime_nanodollars.saturating_sub(used) / workflow_nanodollars);
        }
    }
    Ok(count.saturating_add(u64::from(current)))
}
