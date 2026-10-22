//! Prospective, bounded whole-workflow learning with optional guarded deployment.
//! Sampled workflows return the reference; admitted unsampled workflows can
//! return the candidate under the same immutable resource commitments.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};

use crate::provider_graph_comparison as comparison;
use crate::provider_graph_deployment as deployment;
use crate::provider_graph_preparation::{self as preparation, Authorization, Lease};
use crate::provider_graph_quality::{self as quality, Policy as QualityPolicy};
use crate::provider_receipts::{self as receipts, Batch, Invocation};

pub const VERSION: &str = "provider-graph-sampling-v1";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub sampling_rate: f64,
    pub max_workflows: u64,
    pub max_samples: u64,
    pub max_physical_invocations: u64,
    /// Shared lifetime allowance. Each open workflow reserves its full per-run
    /// allowance; finishing releases only slots that never dispatched.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_lifetime_physical_invocations: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub commitments: Option<Commitments>,
    /// Rediscover on the next eligible sampled workflow after this many runs.
    /// Retirement refresh is automatic for learned templates in the same driver.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refresh_interval_workflows: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub campaign: Option<crate::provider_graph_campaign::Policy>,
    pub replay_contract_sha256: String,
}

impl Policy {
    pub(crate) fn validate(&self) -> Result<()> {
        crate::execution_plan::ExecutionPlanId::parse(&self.replay_contract_sha256)?;
        if !self.sampling_rate.is_finite()
            || !(0.0..=1.0).contains(&self.sampling_rate)
            || self.max_workflows == 0
            || self.max_workflows > 1 << 53
            || self.max_samples > self.max_workflows
            || self.max_physical_invocations == 0
            || self
                .refresh_interval_workflows
                .is_some_and(|n| n == 0 || n > self.max_workflows)
            || self
                .max_lifetime_physical_invocations
                .is_some_and(|n| n < self.max_physical_invocations || n > 1 << 53)
        {
            bail!("graph sampling rate, lifetime limits or replay contract are invalid");
        }
        if let Some(c) = &self.commitments {
            c.validate()?;
        }
        if let Some(c) = &self.campaign {
            c.validate()?;
            if self.max_physical_invocations > c.max_physical_invocations {
                bail!("sampling call allowance exceeds its shared campaign commitment");
            }
        }
        Ok(())
    }
}

/// Declared workflow commitments are held before callbacks. They are not
/// provider-enforced price ceilings or latency guarantees.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Commitments {
    pub money: CommitmentMoney,
    pub max_open_workflows: u64,
    pub max_pending_invocations: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "quantity", rename_all = "snake_case", deny_unknown_fields)]
pub enum CommitmentMoney {
    BilledUsd {
        workflow_usd: f64,
        lifetime_usd: f64,
    },
    PublishedTokenTariffUsd {
        snapshot_sha256: String,
        workflow_nanodollars: u64,
        lifetime_nanodollars: u64,
    },
}
impl Commitments {
    pub(crate) fn validate(&self) -> Result<()> {
        if self.max_open_workflows == 0
            || self.max_open_workflows > 1 << 53
            || self.max_pending_invocations == 0
            || self.max_pending_invocations > 1 << 53
        {
            bail!("sampling commitment resource limits are invalid");
        }
        match &self.money {
            CommitmentMoney::BilledUsd {
                workflow_usd,
                lifetime_usd,
            } => {
                if !workflow_usd.is_finite()
                    || !lifetime_usd.is_finite()
                    || *workflow_usd <= 0.
                    || lifetime_usd < workflow_usd
                {
                    bail!("sampling billed commitments are invalid");
                }
            }
            CommitmentMoney::PublishedTokenTariffUsd {
                snapshot_sha256,
                workflow_nanodollars,
                lifetime_nanodollars,
            } => {
                if snapshot_sha256 != &crate::provider_tariff::snapshot_sha256()
                    || *workflow_nanodollars == 0
                    || lifetime_nanodollars < workflow_nanodollars
                    || *lifetime_nanodollars > 1 << 53
                {
                    bail!("sampling tariff commitments or snapshot are invalid");
                }
            }
        }
        Ok(())
    }
}

#[derive(Debug, Serialize)]
pub struct CommitmentReport {
    pub driver_id: String,
    pub policy: Commitments,
    pub open_workflows: u64,
    pub pending_invocations: u64,
    pub unknown_cost_workflows: u64,
    pub held_workflows: u64,
    pub billed_committed_upper_usd: Option<f64>,
    pub tariff_committed_upper_nanodollars: Option<u64>,
    pub budget_exceeded: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Begin {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_pair_id: Option<String>,
    pub reference_batch: Batch,
    pub candidate_batch: Batch,
    pub input_sha256: String,
    pub policy: Policy,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deployment: Option<Deployment>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Deployment {
    pub remaining_batches: u64,
    pub policy: crate::provider_graph_admission::Policy,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Begin { request: Box<Begin> },
    Finish { ticket_id: String },
    Commitments { ticket_id: String },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    version: String,
    driver_id: String,
    policy: Policy,
    seed: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    deployment: Option<Deployment>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Ticket {
    pub version: String,
    pub ticket_id: String,
    pub driver_id: String,
    pub ordinal: u64,
    pub sampled: bool,
    pub issued_at_us: i64,
    pub quality: QualityPolicy,
    pub window_size: usize,
    pub request: Begin,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub discovered_pair_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refresh: Option<Refresh>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub deployed: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Refresh {
    pub source_ticket_id: String,
    pub source_pair_id: String,
    pub source_pair_sha256: String,
    pub reason: RefreshReason,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RefreshReason {
    Retired,
    Interval,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct TemplateCheckpoint {
    driver_id: String,
    generation: u64,
    ticket_id: String,
    pair_id: String,
    prior_pair_id: Option<String>,
}

impl Ticket {
    fn source_pair(&self) -> Option<&str> {
        self.request
            .source_pair_id
            .as_deref()
            .or(self.discovered_pair_id.as_deref())
    }
    fn cold(&self) -> bool {
        self.source_pair().is_none()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Finished {
    pub ticket_id: String,
    pub sampled: bool,
    pub status: String,
    pub pair_id: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct Outcome {
    pub ticket: Ticket,
    pub reference: Option<Lease>,
    pub candidate: Option<Lease>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub deployment: Option<Lease>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub decision: Option<crate::provider_graph_admission::Decision>,
}

fn canonical(value: &impl Serialize) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    preparation::ensure_schema(conn)?;
    comparison::ensure_schema(conn)?;
    quality::ensure_schema(conn)?;
    conn.execute_batch("CREATE TABLE IF NOT EXISTS provider_graph_sampling_state (
        driver_id TEXT PRIMARY KEY,state_sha256 TEXT NOT NULL,state_json TEXT NOT NULL,
        workflows INTEGER NOT NULL,samples INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS provider_graph_sampling (
        ticket_id TEXT PRIMARY KEY,driver_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
        reference_batch_id TEXT NOT NULL UNIQUE,candidate_batch_id TEXT UNIQUE,
        ticket_sha256 TEXT NOT NULL,ticket_json TEXT NOT NULL,result_sha256 TEXT,result_json TEXT,
        UNIQUE(driver_id,ordinal),FOREIGN KEY(driver_id) REFERENCES provider_graph_sampling_state(driver_id),
        FOREIGN KEY(reference_batch_id) REFERENCES provider_receipt_batch(batch_id),
        FOREIGN KEY(candidate_batch_id) REFERENCES provider_receipt_batch(batch_id));
        CREATE TABLE IF NOT EXISTS provider_graph_sampling_discovered (
        driver_id TEXT PRIMARY KEY,ticket_id TEXT NOT NULL,pair_id TEXT NOT NULL,
        FOREIGN KEY(driver_id) REFERENCES provider_graph_sampling_state(driver_id),
        FOREIGN KEY(ticket_id) REFERENCES provider_graph_sampling(ticket_id),
        FOREIGN KEY(pair_id) REFERENCES provider_graph_pair(pair_id));
        CREATE TABLE IF NOT EXISTS provider_graph_sampling_invocation (
        invocation_id TEXT PRIMARY KEY,ticket_id TEXT NOT NULL,batch_id TEXT NOT NULL,
        invocation_sha256 TEXT NOT NULL,
        FOREIGN KEY(invocation_id) REFERENCES provider_receipt_invocation(invocation_id),
        FOREIGN KEY(ticket_id) REFERENCES provider_graph_sampling(ticket_id),
        FOREIGN KEY(batch_id) REFERENCES provider_receipt_batch(batch_id));
        CREATE INDEX IF NOT EXISTS provider_graph_sampling_invocation_ticket
        ON provider_graph_sampling_invocation(ticket_id);
        CREATE TABLE IF NOT EXISTS provider_graph_sampling_template (
        driver_id TEXT NOT NULL,generation INTEGER NOT NULL CHECK(generation>0),
        checkpoint_sha256 TEXT NOT NULL,checkpoint_json TEXT NOT NULL,
        PRIMARY KEY(driver_id,generation),FOREIGN KEY(driver_id) REFERENCES provider_graph_sampling_state(driver_id));")?;
    Ok(())
}

/// Derive committed liabilities from original tickets and immutable dispatch
/// slots. No resettable counter can replenish the shared lifetime allowance.
fn lifetime_physical(conn: &Connection, s: &State) -> Result<u64> {
    if s.policy.max_lifetime_physical_invocations.is_none() && s.policy.commitments.is_none() {
        return Ok(0);
    }
    let changed: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_graph_sampling_invocation i
         LEFT JOIN provider_receipt_invocation r USING(invocation_id)
         LEFT JOIN provider_graph_sampling t USING(ticket_id)
         WHERE t.ticket_id IS NULL OR (t.driver_id=?1 AND
         (r.invocation_id IS NULL OR r.batch_id!=i.batch_id OR
          i.batch_id NOT IN (t.reference_batch_id,COALESCE(t.candidate_batch_id,'')))))
         OR EXISTS(SELECT 1 FROM provider_graph_sampling t
         JOIN provider_receipt_invocation r ON r.batch_id=t.reference_batch_id OR r.batch_id=t.candidate_batch_id
         LEFT JOIN provider_graph_sampling_invocation i USING(invocation_id)
         WHERE t.driver_id=?1 AND (i.invocation_id IS NULL OR i.ticket_id!=t.ticket_id))",
        [&s.driver_id], |r| r.get(0),
    )?;
    if changed {
        bail!("graph sampling physical custody is missing or changed");
    }
    let mut q = conn.prepare(
        "SELECT t.ticket_id,t.result_json,t.result_sha256,COUNT(i.invocation_id)
         FROM provider_graph_sampling t LEFT JOIN provider_graph_sampling_invocation i USING(ticket_id)
         WHERE t.driver_id=?1 GROUP BY t.ticket_id",
    )?;
    let mut used = 0_u64;
    for row in q.query_map([&s.driver_id], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, Option<String>>(1)?,
            r.get::<_, Option<String>>(2)?,
            r.get::<_, u64>(3)?,
        ))
    })? {
        let (ticket, result, result_hash, count) = row?;
        let finished = result.is_some();
        if let Some(raw) = result {
            let done: Finished = serde_json::from_str(&raw)?;
            let sealed: bool = conn.query_row(
                "SELECT NOT EXISTS(SELECT 1 FROM provider_receipt_batch b JOIN provider_graph_sampling t
                 ON b.batch_id=t.reference_batch_id OR b.batch_id=t.candidate_batch_id
                 WHERE t.ticket_id=?1 AND NOT b.sealed
                 AND (COALESCE(json_extract(t.ticket_json,'$.deployed'),0)=0 OR b.batch_id=t.candidate_batch_id))",
                [&ticket], |r| r.get(0),
            )?;
            if result_hash.as_deref() != Some(content_hash(&raw).as_str())
                || canonical(&done)? != raw
                || done.ticket_id != ticket
                || !sealed
            {
                bail!("sampling completion cannot release its physical reservation");
            }
        } else if result_hash.is_some() {
            bail!("sampling completion custody is incomplete");
        }
        if count > s.policy.max_physical_invocations {
            bail!("sampling workflow exceeded its original physical reservation");
        }
        let mut calls = conn.prepare(
            "SELECT i.invocation_sha256,r.invocation_json FROM provider_graph_sampling_invocation i
             JOIN provider_receipt_invocation r USING(invocation_id) WHERE i.ticket_id=?1",
        )?;
        for call in calls.query_map([&ticket], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })? {
            let (hash, raw) = call?;
            if content_hash(&raw) != hash {
                bail!("sampling physical invocation differs from its original reservation");
            }
        }
        used = used
            .checked_add(if finished {
                count
            } else {
                s.policy.max_physical_invocations
            })
            .context("sampling lifetime physical allowance overflowed")?;
    }
    if s.policy
        .max_lifetime_physical_invocations
        .is_some_and(|limit| used > limit)
    {
        bail!("sampling lifetime physical allowance is exhausted");
    }
    Ok(used)
}

fn committed(conn: &Connection, s: &State) -> Result<Option<CommitmentReport>> {
    use crate::provider_graph_economics as finance;
    let Some(policy) = &s.policy.commitments else {
        return Ok(None);
    };
    let mut report = CommitmentReport {
        driver_id: s.driver_id.clone(),
        policy: policy.clone(),
        open_workflows: 0,
        pending_invocations: 0,
        unknown_cost_workflows: 0,
        held_workflows: 0,
        billed_committed_upper_usd: None,
        tariff_committed_upper_nanodollars: None,
        budget_exceeded: false,
    };
    let mut billed = finance::UsdInterval::exact(0.)?;
    let mut tariff = 0_u64;
    let mut q = conn
        .prepare("SELECT ticket_id,result_json FROM provider_graph_sampling WHERE driver_id=?1")?;
    for row in q.query_map([&s.driver_id], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
    })? {
        let (ticket, result) = row?;
        let finished = result.is_some();
        let incomplete = result
            .as_deref()
            .map(|r| serde_json::from_str::<Finished>(r).map(|r| r.status == "incomplete"))
            .transpose()?
            .unwrap_or(true);
        report.open_workflows += u64::from(!finished);
        let mut unknown = false;
        let mut known_billed = finance::UsdInterval::exact(0.)?;
        let mut known_tariff = 0_u64;
        let mut calls=conn.prepare("SELECT i.invocation_id,i.batch_id,r.completion_json IS NULL FROM provider_graph_sampling_invocation i JOIN provider_receipt_invocation r USING(invocation_id) WHERE i.ticket_id=?1")?;
        for call in calls.query_map([&ticket], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, bool>(2)?,
            ))
        })? {
            let (id, batch, pending) = call?;
            report.pending_invocations += u64::from(pending);
            let receipt = finance::receipt(conn, &batch, &id)?;
            match &policy.money {
                CommitmentMoney::BilledUsd { .. } => {
                    known_billed =
                        known_billed.add(finance::UsdInterval::exact(receipt.known_billed_usd)?)?;
                    unknown |= receipt.billed_usd.is_none();
                }
                CommitmentMoney::PublishedTokenTariffUsd {
                    snapshot_sha256, ..
                } => {
                    if let Some(bound) = receipt.tariff {
                        if bound.snapshot_sha256 != *snapshot_sha256 {
                            bail!("sampling commitment tariff snapshot changed");
                        }
                        known_tariff = known_tariff
                            .checked_add(bound.interval.upper_nanodollars)
                            .context("sampling committed tariff overflow")?;
                    } else {
                        unknown = true;
                    }
                }
            }
        }
        report.unknown_cost_workflows += u64::from(unknown);
        let held = !finished || unknown || incomplete;
        report.held_workflows += u64::from(held);
        match &policy.money {
            CommitmentMoney::BilledUsd { workflow_usd, .. } => {
                let amount = if held {
                    known_billed.upper_usd.max(*workflow_usd)
                } else {
                    known_billed.upper_usd
                };
                billed = billed.add(finance::UsdInterval::exact(amount)?)?;
                report.budget_exceeded |= known_billed.upper_usd > *workflow_usd;
            }
            CommitmentMoney::PublishedTokenTariffUsd {
                workflow_nanodollars,
                ..
            } => {
                let amount = if held {
                    known_tariff.max(*workflow_nanodollars)
                } else {
                    known_tariff
                };
                tariff = tariff
                    .checked_add(amount)
                    .context("sampling lifetime commitment overflow")?;
                report.budget_exceeded |= known_tariff > *workflow_nanodollars;
            }
        }
    }
    match &policy.money {
        CommitmentMoney::BilledUsd { lifetime_usd, .. } => {
            report.billed_committed_upper_usd = Some(billed.upper_usd);
            report.budget_exceeded |= billed.upper_usd > *lifetime_usd;
        }
        CommitmentMoney::PublishedTokenTariffUsd {
            lifetime_nanodollars,
            ..
        } => {
            report.tariff_committed_upper_nanodollars = Some(tariff);
            report.budget_exceeded |= tariff > *lifetime_nanodollars;
        }
    }
    Ok(Some(report))
}

pub fn commitment_report(conn: &Connection, ticket_id: &str) -> Result<Option<CommitmentReport>> {
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let report = commitment_report(&tx, ticket_id)?;
        tx.commit()?;
        return Ok(report);
    }
    let t = load(conn, ticket_id)?;
    let (s, _, _) = state(conn, &t.driver_id)?;
    committed(conn, &s)
}

fn reserve_commitment(conn: &Connection, s: &State) -> Result<()> {
    use crate::provider_graph_economics::UsdInterval;
    let Some(r) = committed(conn, s)? else {
        return Ok(());
    };
    if r.budget_exceeded || r.open_workflows >= r.policy.max_open_workflows {
        bail!("sampling shared commitment budget or open workflow limit is exhausted");
    }
    let exhausted = match r.policy.money {
        CommitmentMoney::BilledUsd {
            workflow_usd,
            lifetime_usd,
        } => {
            UsdInterval::exact(r.billed_committed_upper_usd.unwrap())?
                .add(UsdInterval::exact(workflow_usd)?)?
                .upper_usd
                > lifetime_usd
        }
        CommitmentMoney::PublishedTokenTariffUsd {
            workflow_nanodollars,
            lifetime_nanodollars,
            ..
        } => r
            .tariff_committed_upper_nanodollars
            .unwrap()
            .checked_add(workflow_nanodollars)
            .is_none_or(|n| n > lifetime_nanodollars),
    };
    if exhausted {
        bail!("sampling lifetime monetary commitment cannot reserve another workflow");
    }
    Ok(())
}

/// Called after receipt insertion in the same writer transaction. A failed
/// receipt or conflicting retry never consumes a separate lifetime slot.
pub(crate) fn record_dispatch(conn: &Connection, batch: &str, call: &Invocation) -> Result<()> {
    let exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_graph_sampling')",
        [], |r| r.get(0),
    )?;
    if !exists {
        return Ok(());
    }
    let row: Option<(String, String)> = conn.query_row(
        "SELECT ticket_id,ticket_json FROM provider_graph_sampling WHERE reference_batch_id=?1 OR candidate_batch_id=?1",
        [batch], |r| Ok((r.get(0)?,r.get(1)?)),
    ).optional()?;
    let Some((id, raw)) = row else {
        return Ok(());
    };
    let t: Ticket = serde_json::from_str(&raw)?;
    if t.request.policy.max_lifetime_physical_invocations.is_none()
        && t.request.policy.commitments.is_none()
    {
        return Ok(());
    }
    let hash = content_hash(&canonical(call)?);
    conn.execute(
        "INSERT INTO provider_graph_sampling_invocation VALUES(?1,?2,?3,?4) ON CONFLICT DO NOTHING",
        params![call.invocation_id, id, batch, hash],
    )?;
    let original: (String, String, String) = conn.query_row(
        "SELECT ticket_id,batch_id,invocation_sha256 FROM provider_graph_sampling_invocation WHERE invocation_id=?1",
        [&call.invocation_id], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?)),
    )?;
    if original != (id, batch.into(), hash) {
        bail!("sampling physical reservation retry changed its original invocation");
    }
    Ok(())
}

fn state(conn: &Connection, driver: &str) -> Result<(State, u64, u64)> {
    let (hash, raw, workflows, samples): (String, String, u64, u64) = conn.query_row(
        "SELECT state_sha256,state_json,workflows,samples FROM provider_graph_sampling_state WHERE driver_id=?1",
        [driver], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)),
    )?;
    let s: State = serde_json::from_str(&raw)?;
    let (count,last,selected):(u64,u64,u64)=conn.query_row(
        "SELECT COUNT(*),COALESCE(MAX(ordinal),0),COALESCE(SUM(json_extract(ticket_json,'$.sampled')),0) FROM provider_graph_sampling WHERE driver_id=?1",
        [driver], |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)),
    )?;
    s.policy.validate()?;
    crate::execution_plan::ExecutionPlanId::parse(&s.seed)?;
    let orphan:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_graph_preparation p WHERE json_extract(p.lease_json,'$.sampling_id') IS NOT NULL AND NOT EXISTS(SELECT 1 FROM provider_graph_sampling s WHERE s.ticket_id=json_extract(p.lease_json,'$.sampling_id')))",[],|r|r.get(0))?;
    if content_hash(&raw) != hash
        || canonical(&s)? != raw
        || s.version != VERSION
        || s.driver_id != driver
        || (count, last, selected) != (workflows, workflows, samples)
        || workflows > s.policy.max_workflows
        || samples > s.policy.max_samples
        || orphan
    {
        bail!("graph sampling lifetime custody is missing or changed");
    }
    lifetime_physical(conn, &s)?;
    Ok((s, workflows, samples))
}

fn draw(seed: &str, ordinal: u64) -> f64 {
    let hash = content_hash(&format!("{VERSION}\0{seed}\0{ordinal}"));
    let bits = u64::from_str_radix(&hash[..13], 16).expect("SHA256 contains hex");
    bits as f64 / (1_u64 << 52) as f64
}

fn load(conn: &Connection, id: &str) -> Result<Ticket> {
    let (hash,raw,driver,ordinal,reference,candidate):(String,String,String,u64,String,Option<String>)=conn.query_row(
        "SELECT ticket_sha256,ticket_json,driver_id,ordinal,reference_batch_id,candidate_batch_id FROM provider_graph_sampling WHERE ticket_id=?1",
        [id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)),
    ).context("original graph sampling ticket is missing")?;
    let t: Ticket = serde_json::from_str(&raw)?;
    let (s, workflows, _) = state(conn, &driver)?;
    let mut unsigned = t.clone();
    unsigned.ticket_id.clear();
    if content_hash(&raw) != hash
        || canonical(&t)? != raw
        || content_hash(&canonical(&unsigned)?) != id
        || t.version != VERSION
        || t.ticket_id != id
        || t.driver_id != driver
        || t.ordinal != ordinal
        || ordinal == 0
        || ordinal > workflows
        || t.request.policy != s.policy
        || t.issued_at_us < 0
        || t.window_size == 0
        || t.request.reference_batch.batch_id != reference
        || candidate.as_ref()
            != (t.sampled || t.deployed).then_some(&t.request.candidate_batch.batch_id)
        || t.deployed && (t.sampled || t.request.deployment.is_none() || t.source_pair().is_none())
        || t.request.deployment != s.deployment
        || t.sampled && draw(&s.seed, ordinal) >= s.policy.sampling_rate
    {
        bail!("graph sampling ticket differs from its original decision");
    }
    if let Some(refresh) = &t.refresh {
        let (source_hash, source_raw): (String, String) = conn.query_row(
            "SELECT pair_sha256,pair_json FROM provider_graph_pair WHERE pair_id=?1",
            [&refresh.source_pair_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        let (owner_hash,owner_raw,owner_driver,owner_ordinal):(String,String,String,u64)=conn.query_row("SELECT ticket_sha256,ticket_json,driver_id,ordinal FROM provider_graph_sampling WHERE ticket_id=?1",[&refresh.source_ticket_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
        if !t.cold()
            || t.deployed
            || source_hash != refresh.source_pair_sha256
            || content_hash(&source_raw) != source_hash
            || content_hash(&owner_raw) != owner_hash
            || owner_driver != t.driver_id
            || owner_ordinal >= t.ordinal
        {
            bail!("sampling refresh predecessor custody is missing or changed");
        }
        let history = checkpoint_headers(conn, &t.driver_id)?;
        let pointer: (String, String) = conn.query_row(
            "SELECT ticket_id,pair_id FROM provider_graph_sampling_discovered WHERE driver_id=?1",
            [&t.driver_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if !history
            .iter()
            .any(|h| h.pair_id == refresh.source_pair_id && h.ticket_id == refresh.source_ticket_id)
            || history.last().is_none_or(|h| {
                (h.ticket_id.as_str(), h.pair_id.as_str())
                    != (pointer.0.as_str(), pointer.1.as_str())
            })
        {
            bail!(
                "sampling refresh checkpoint or discovered pointer custody is missing or changed"
            );
        }
    }
    Ok(t)
}

fn lease(conn: &Connection, batch: &str) -> Result<Lease> {
    let id: String = conn.query_row(
        "SELECT lease_id FROM provider_graph_preparation WHERE batch_id=?1",
        [batch],
        |r| r.get(0),
    )?;
    preparation::load(conn, &id)
}

fn owned_pair(
    conn: &Connection,
    driver: &str,
    ticket: &str,
    pair: &str,
) -> Result<comparison::Pair> {
    let t = load(conn, ticket)?;
    let p = comparison::load(conn, pair)?;
    let (hash, raw): (String, String) = conn.query_row(
        "SELECT result_sha256,result_json FROM provider_graph_sampling WHERE ticket_id=?1",
        [ticket],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let done: Finished = serde_json::from_str(&raw)?;
    if content_hash(&raw) != hash
        || canonical(&done)? != raw
        || done.ticket_id != ticket
        || done.status != "paired"
        || t.driver_id != driver
        || !t.cold()
        || done.pair_id.as_deref() != Some(pair)
        || t.request.reference_batch.batch_id
            != crate::provider_graph::load(conn, &p.reference_execution_id)?.batch_id
        || t.request.candidate_batch.batch_id
            != crate::provider_graph::load(conn, &p.candidate_execution_id)?.batch_id
    {
        bail!("discovered template belongs to another original acquisition");
    }
    Ok(p)
}

// Hash-chain checks are separate from archival loading: original execution
// verification loads its sampling owner, so recursively loading archives here
// would revisit the entire graph from inside each dispatch check.
fn checkpoint_headers(conn: &Connection, driver: &str) -> Result<Vec<TemplateCheckpoint>> {
    let mut q=conn.prepare("SELECT generation,checkpoint_sha256,checkpoint_json FROM provider_graph_sampling_template WHERE driver_id=?1 ORDER BY generation")?;
    let mut result: Vec<TemplateCheckpoint> = Vec::new();
    for row in q.query_map([driver], |r| {
        Ok((
            r.get::<_, u64>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
        ))
    })? {
        let (generation, hash, raw) = row?;
        let value: TemplateCheckpoint = serde_json::from_str(&raw)?;
        if value.driver_id != driver
            || value.generation != generation
            || generation != result.len() as u64 + 1
            || canonical(&value)? != raw
            || content_hash(&raw) != hash
            || value.prior_pair_id.as_deref() != result.last().map(|p| p.pair_id.as_str())
        {
            bail!("sampling template checkpoint history is missing or changed");
        }
        result.push(value);
    }
    Ok(result)
}

fn checkpoints(conn: &Connection, driver: &str) -> Result<Vec<TemplateCheckpoint>> {
    let history = checkpoint_headers(conn, driver)?;
    for (index, value) in history.iter().enumerate() {
        owned_pair(conn, driver, &value.ticket_id, &value.pair_id)?;
        if index > 0 {
            let prior = &history[index - 1];
            let t = load(conn, &value.ticket_id)?;
            if t.refresh.as_ref().is_none_or(|r| {
                r.source_pair_id != prior.pair_id || r.source_ticket_id != prior.ticket_id
            }) {
                bail!("sampling template checkpoint changed its original predecessor");
            }
        }
    }
    Ok(history)
}

fn save_checkpoint(conn: &Connection, value: &TemplateCheckpoint) -> Result<()> {
    let raw = canonical(value)?;
    conn.execute(
        "INSERT INTO provider_graph_sampling_template VALUES(?1,?2,?3,?4)",
        params![value.driver_id, value.generation, content_hash(&raw), raw],
    )?;
    Ok(())
}

fn discovered_pair(conn: &Connection, driver: &str) -> Result<Option<(comparison::Pair, Ticket)>> {
    let row: Option<(String, String)> = conn
        .query_row(
            "SELECT ticket_id,pair_id FROM provider_graph_sampling_discovered WHERE driver_id=?1",
            [driver],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    let Some((ticket, pair)) = row else {
        let lost:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_graph_sampling WHERE driver_id=?1 AND json_extract(ticket_json,'$.request.source_pair_id') IS NULL AND json_extract(ticket_json,'$.discovered_pair_id') IS NULL AND json_extract(result_json,'$.pair_id') IS NOT NULL)",[driver],|r|r.get(0))?;
        if lost {
            bail!("discovered graph template custody is missing");
        }
        return Ok(None);
    };
    let t = load(conn, &ticket)?;
    let p = owned_pair(conn, driver, &ticket, &pair)?;
    let history = checkpoints(conn, driver)?;
    if history.is_empty() {
        if t.refresh.is_some() {
            bail!("sampling refreshed template checkpoint is missing");
        }
        save_checkpoint(
            conn,
            &TemplateCheckpoint {
                driver_id: driver.into(),
                generation: 1,
                ticket_id: ticket.clone(),
                pair_id: pair.clone(),
                prior_pair_id: None,
            },
        )?;
    } else if history
        .last()
        .is_none_or(|h| h.ticket_id != ticket || h.pair_id != pair)
    {
        bail!("sampling discovered pointer differs from its original checkpoint head");
    }
    Ok(Some((p, t)))
}

/// A retired projection can authorize fresh discovery only when its latest
/// execution and epoch floor still have complete original archival custody.
fn checked_head(
    conn: &Connection,
    key: &crate::plan_profile::PlanProfileKey,
) -> Result<(crate::plan_profile::PlanRuntimeVersion, usize)> {
    let (raw,sequence,floor,size,lifetime):(String,i64,i64,usize,u64)=conn.query_row("SELECT runtime_json,source_sequence,observation_floor,window_size,lifetime_executions FROM provider_graph_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2",params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
    let (last,count):(i64,u64)=conn.query_row("SELECT COALESCE(MAX(rowid),0),COUNT(*) FROM provider_graph_execution WHERE json_extract(execution_json,'$.key.call_site_version')=?1 AND json_extract(execution_json,'$.key.execution_plan_id')=?2",params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?)))?;
    if floor <= 0 || sequence != last || floor > sequence || size == 0 || lifetime != count {
        bail!("sampling refresh execution head custody is missing or changed");
    }
    for ordinal in [sequence, floor] {
        let id: String = conn.query_row(
            "SELECT execution_id FROM provider_graph_execution WHERE rowid=?1",
            [ordinal],
            |r| r.get(0),
        )?;
        let e = crate::provider_graph::load(conn, &id)?;
        if e.key != *key || canonical(&e.runtime)? != raw {
            bail!("sampling refresh head differs from its original execution");
        }
        if let Some(lease) = e.preparation_id {
            if preparation::load(conn, &lease)?.window_size != size {
                bail!("sampling refresh window has no original preparation");
            }
        } else {
            bail!("sampling refresh head has no trusted workflow preparation");
        }
    }
    let interrupted:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_graph_execution WHERE rowid>=?3 AND rowid<=?4 AND json_extract(execution_json,'$.key.call_site_version')=?1 AND json_extract(execution_json,'$.key.execution_plan_id')=?2 AND json_extract(execution_json,'$.runtime')<>?5)",params![key.call_site_version.as_str(),key.execution_plan_id.as_str(),floor,sequence,raw],|r|r.get(0))?;
    if interrupted {
        bail!("sampling refresh epoch crosses a retired runtime");
    }
    Ok((serde_json::from_str(&raw)?, size))
}

fn discovery_reference(conn: &Connection, t: &Ticket) -> Result<crate::provider_graph::Execution> {
    let id: String = conn
        .query_row(
            "SELECT execution_id FROM provider_graph_preparation WHERE batch_id=?1",
            [&t.request.reference_batch.batch_id],
            |r| r.get(0),
        )
        .context("discovery candidate needs its original completed reference")?;
    crate::provider_graph::load(conn, &id)
}

fn discovery_node(
    conn: &Connection,
    lease: &Lease,
    position: crate::multi_call::CallPosition,
) -> Result<preparation::Node> {
    let (hash,raw):(String,String)=conn.query_row("SELECT node_sha256,node_json FROM provider_graph_prepared_node WHERE lease_id=?1 AND item=?2 AND logical_call=?3",params![lease.lease_id,position.item,position.call],|r|Ok((r.get(0)?,r.get(1)?))).context("discovery dispatch has no issued node")?;
    if content_hash(&raw) != hash {
        bail!("discovery node custody changed");
    }
    let node: preparation::Node = serde_json::from_str(&raw)?;
    if node.position != position || node.plan.execution_plan_id()? != node.key.execution_plan_id {
        bail!("discovery node differs from its native instruction");
    }
    Ok(node)
}

fn discovery_controls(conn: &Connection, t: &Ticket, node: &preparation::Node) -> Result<()> {
    let r = discovery_reference(conn, t)?;
    let branch = r
        .spec
        .calls
        .branches
        .iter()
        .find(|b| b.position == node.position)
        .context("discovery candidate changed its reference topology")?;
    if branch.call_site_version != node.key.call_site_version
        || branch.plan.requested_model_id != node.plan.requested_model_id
        || branch.plan.provider_protocol != node.plan.provider_protocol
        || r.source_contracts
            .iter()
            .find(|s| s.position == node.position)
            .is_none_or(|s| s.sha256 != node.source_contract_sha256)
    {
        bail!("discovery candidate changed its original source contract");
    }
    Ok(())
}

/// The SDK cannot choose an undispatched cold instruction. The native compiler
/// issues and binds it under its original ticket before returning a Plan.
pub fn plan_discovery(
    conn: &mut Connection,
    opt: &crate::Optimizer,
    catalog: &crate::model_catalog::ModelCatalog,
    lease_id: &str,
    position: crate::multi_call::CallPosition,
    source: &str,
    observation_only: bool,
) -> Result<String> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let lease = preparation::load(&tx, lease_id)?;
    let t = load(
        &tx,
        lease
            .sampling_id
            .as_deref()
            .context("discovery has no sampling owner")?,
    )?;
    if !t.cold() || position.item >= t.request.reference_batch.item_count || lease.window_size == 0
    {
        bail!("workflow is not a supported cold discovery");
    }
    let reference = lease.batch_id == t.request.reference_batch.batch_id;
    if !reference && (observation_only || !opt.config().enabled) {
        bail!("excluded discovery cannot run a candidate");
    }
    let config = opt.config();
    let active = QualityPolicy {
        divergence_threshold: config.selection.global_divergence_threshold.unwrap_or(0.0),
        exposure_budget: config.selection.divergence_exposure_budget,
        min_pairs: config.selection.min_plan_evidence.max(20) as usize,
    };
    if active != t.quality {
        bail!("discovery quality policy changed");
    }
    let raw = crate::ffi::discover_graph_node(opt, catalog, source, reference)
        .map_err(anyhow::Error::msg)?;
    let node = crate::ffi::prepared_graph_node(catalog, position, source, &raw)
        .map_err(anyhow::Error::msg)?;
    if !reference {
        discovery_controls(&tx, &t, &node)?;
    }
    let (closed,began):(bool,bool)=tx.query_row("SELECT b.sealed OR s.result_json IS NOT NULL,EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE batch_id=b.batch_id AND item=?3 AND logical_call=?4) FROM provider_receipt_batch b JOIN provider_graph_sampling s ON s.ticket_id=?2 WHERE b.batch_id=?1",params![lease.batch_id,t.ticket_id,position.item,position.call],|r|Ok((r.get(0)?,r.get(1)?)))?;
    if closed || began {
        bail!("discovery position is finished or has dispatched");
    }
    let encoded = canonical(&node)?;
    tx.execute(
        "INSERT INTO provider_graph_prepared_node VALUES(?1,?2,?3,?4,?5) ON CONFLICT DO NOTHING",
        params![
            lease_id,
            position.item,
            position.call,
            content_hash(&encoded),
            encoded
        ],
    )?;
    if discovery_node(&tx, &lease, position)? != node {
        bail!("discovery node retry changed its original instruction");
    }
    tx.commit()?;
    Ok(raw)
}

pub(crate) fn verify_discovery_execution(
    conn: &Connection,
    lease: &Lease,
    e: &crate::provider_graph::Execution,
) -> Result<()> {
    let Some(id) = &lease.sampling_id else {
        return Ok(());
    };
    let t = load(conn, id)?;
    if !t.cold() {
        return Ok(());
    }
    let reference = lease.batch_id == t.request.reference_batch.batch_id;
    let count: u64 = conn.query_row(
        "SELECT COUNT(*) FROM provider_graph_prepared_node WHERE lease_id=?1",
        [&lease.lease_id],
        |r| r.get(0),
    )?;
    if count != e.spec.calls.branches.len() as u64 {
        bail!("discovery execution has incomplete issued node coverage");
    }
    for b in &e.spec.calls.branches {
        let n = discovery_node(conn, lease, b.position)?;
        if n.plan != b.plan
            || n.key.call_site_version != b.call_site_version
            || e.runtime_branches
                .iter()
                .find(|v| v.position == b.position)
                .is_none_or(|v| v.runtime != n.runtime)
            || e.source_contracts
                .iter()
                .find(|s| s.position == b.position)
                .is_none_or(|s| s.sha256 != n.source_contract_sha256)
            || reference
                && (n.plan.requested_model_id != n.plan.target_model_id
                    || !n.plan.rewrites.is_empty())
        {
            bail!("discovery publication differs from its original issued program");
        }
    }
    if !reference {
        let r = discovery_reference(conn, &t)?;
        if r.spec.dependencies != e.spec.dependencies
            || r.spec.results != e.spec.results
            || r.source_contracts != e.source_contracts
        {
            bail!("discovery candidate changed its original dependency/result contract");
        }
    }
    Ok(())
}

pub(crate) fn verify_custody(conn: &Connection, lease: &Lease) -> Result<()> {
    let Some(id) = &lease.sampling_id else {
        return Ok(());
    };
    let t = load(conn, id)?;
    if t.deployed {
        let pair = comparison::load(
            conn,
            t.source_pair()
                .context("deployment has no learned template")?,
        )?;
        if lease.batch_id != t.request.candidate_batch.batch_id
            || lease.deployment_id.is_none()
            || lease.comparison_context.is_some()
            || lease
                .template
                .as_ref()
                .is_none_or(|e| e.execution_id != pair.candidate_execution_id)
            || lease.started_at_us != t.issued_at_us
            || lease.policy != t.quality
            || lease.window_size != t.window_size
            || lease.input_sha256.as_ref() != Some(&t.request.input_sha256)
        {
            bail!("sampled deployment differs from its original decision");
        }
        return Ok(());
    }
    let expected = if lease.batch_id == t.request.reference_batch.batch_id {
        preparation::ComparisonRole::Reference
    } else if t.sampled && lease.batch_id == t.request.candidate_batch.batch_id {
        preparation::ComparisonRole::Candidate
    } else {
        bail!("sampling lease belongs to another workflow");
    };
    if lease.deployment_id.is_some()
        || lease.role_program_id.is_some()
        || lease.started_at_us != t.issued_at_us
        || lease.policy != t.quality
        || lease.window_size != t.window_size
        || lease.input_sha256.as_ref() != Some(&t.request.input_sha256)
        || match t.source_pair() {
            Some(pair) => lease
                .comparison_context
                .as_ref()
                .is_none_or(|c| c.source_pair_id != pair || c.role != expected),
            None => lease.comparison_context.is_some() || lease.template.is_some(),
        }
    {
        bail!("sampling preparation differs from its original ticket");
    }
    if t.cold() {
        let saved:Option<String>=conn.query_row("SELECT e.execution_json FROM provider_graph_preparation p JOIN provider_graph_execution e ON e.execution_id=p.execution_id WHERE p.lease_id=?1",[&lease.lease_id],|r|r.get(0)).optional()?;
        if let Some(raw) = saved {
            verify_discovery_execution(conn, lease, &serde_json::from_str(&raw)?)?;
        }
    }
    Ok(())
}

pub(crate) fn verify_dispatch(conn: &Connection, lease: &Lease, call: &Invocation) -> Result<()> {
    let Some(id) = &lease.sampling_id else {
        return Ok(());
    };
    let t = load(conn, id)?;
    let (s, _, _) = state(conn, &t.driver_id)?;
    if let Some(report) = committed(conn, &s)? {
        if let CommitmentMoney::PublishedTokenTariffUsd {
            snapshot_sha256, ..
        } = &report.policy.money
        {
            if call
                .tariff_declaration
                .as_ref()
                .is_none_or(|d| d.snapshot_sha256 != *snapshot_sha256 || !d.request_scope_supported)
            {
                bail!("sampling monetary commitment requires its pinned tariff declaration before dispatch");
            }
        }
        let retry: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE invocation_id=?1)",
            [&call.invocation_id],
            |r| r.get(0),
        )?;
        if !retry
            && (report.budget_exceeded
                || report.pending_invocations >= report.policy.max_pending_invocations)
        {
            bail!("sampling monetary commitment or shared pending invocation limit is exhausted");
        }
    }
    let (finished,retry,count):(bool,bool,u64)=conn.query_row(
        "SELECT result_json IS NOT NULL,EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE invocation_id=?2),
        (SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id IN (reference_batch_id,candidate_batch_id)) FROM provider_graph_sampling WHERE ticket_id=?1",
        params![id,call.invocation_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)),
    )?;
    if finished || !retry && count >= t.request.policy.max_physical_invocations {
        bail!("sampling workflow is finished or its physical allowance is exhausted");
    }
    if !t.deployed && lease.batch_id == t.request.candidate_batch.batch_id {
        let complete: bool = conn.query_row(
            "SELECT execution_id IS NOT NULL FROM provider_graph_preparation WHERE batch_id=?1",
            [&t.request.reference_batch.batch_id],
            |r| r.get(0),
        )?;
        if !complete {
            bail!("sampled graph must wait for the original reference result");
        }
    }
    if t.cold() {
        let node = discovery_node(conn, lease, call.position)?;
        if lease.batch_id == t.request.candidate_batch.batch_id {
            discovery_controls(conn, &t, &node)?;
        }
        if call.position.call > 0 {
            let ready:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE batch_id=?1 AND item=?2 AND logical_call=?3 AND ordinal=0 AND json_extract(completion_json,'$.status')='completed')",params![lease.batch_id,call.position.item,call.position.call-1],|r|r.get(0))?;
            if !ready {
                bail!("discovery node must wait for its preceding logical call");
            }
        }
        preparation::verify_prepared_physical(conn, lease, call)?;
    }
    Ok(())
}

/// Decide and prepare both legs in one immediate transaction before callbacks.
pub fn begin(
    conn: &mut Connection,
    mut request: Begin,
    quality: QualityPolicy,
    size: usize,
    now: i64,
) -> Result<Outcome> {
    ensure_schema(conn)?;
    request.policy.validate()?;
    quality.validate()?;
    if request.source_pair_id.is_some() && request.policy.refresh_interval_workflows.is_some() {
        bail!("periodic refresh requires a learned template, not an explicit source pair");
    }
    if let Some(d) = &mut request.deployment {
        d.policy.selection.now_us = 0;
        d.policy.selection.validate()?;
        if d.policy.quality != quality
            || d.remaining_batches == 0
            || d.remaining_batches > 1 << 53
            || d.policy.max_physical_invocations > request.policy.max_physical_invocations
        {
            bail!("sampling deployment policy or horizon is incompatible");
        }
        deployment::ensure_schema(conn)?;
    }
    crate::execution_plan::ExecutionPlanId::parse(&request.input_sha256)?;
    if now < 0
        || size < quality.min_pairs
        || request.reference_batch.batch_id == request.candidate_batch.batch_id
    {
        bail!("sampling clock, window or batch identities are invalid");
    }
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    crate::provider_graph_campaign::verify_driver(
        &tx,
        &request.reference_batch,
        request.policy.campaign.as_ref(),
    )?;
    let old: Option<String> = tx
        .query_row(
            "SELECT ticket_id FROM provider_graph_sampling WHERE reference_batch_id=?1",
            [&request.reference_batch.batch_id],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(id) = old {
        let t = load(&tx, &id)?;
        if canonical(&t.request)? != canonical(&request)?
            || t.quality != quality
            || t.window_size != size
        {
            bail!("sampling retry changed its original request");
        }
        let used:bool=tx.query_row("SELECT result_json IS NOT NULL OR EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE batch_id IN (reference_batch_id,candidate_batch_id)) OR EXISTS(SELECT 1 FROM provider_receipt_batch WHERE batch_id IN (reference_batch_id,candidate_batch_id) AND sealed) FROM provider_graph_sampling WHERE ticket_id=?1",[&id],|r|r.get(0))?;
        if used {
            bail!("sampling workflow cannot replay a callback after SDK dispatch");
        }
        let result = Outcome {
            reference: if !t.deployed {
                Some(lease(&tx, &request.reference_batch.batch_id)?)
            } else {
                None
            },
            candidate: if t.sampled {
                Some(lease(&tx, &request.candidate_batch.batch_id)?)
            } else {
                None
            },
            deployment: if t.deployed {
                Some(lease(&tx, &request.candidate_batch.batch_id)?)
            } else {
                None
            },
            decision: if t.deployed {
                Some(deployment::decision(
                    &tx,
                    &lease(&tx, &request.candidate_batch.batch_id)?,
                )?)
            } else {
                None
            },
            ticket: t,
        };
        for lease in result
            .reference
            .as_ref()
            .into_iter()
            .chain(result.candidate.as_ref())
            .chain(result.deployment.as_ref())
        {
            if result.ticket.deployed {
                deployment::verify_current(&tx, lease, now)?;
            }
            let Some(template) = lease.template.as_ref() else {
                continue;
            };
            let root = template
                .spec
                .calls
                .branches
                .iter()
                .find(|b| {
                    !template
                        .spec
                        .dependencies
                        .iter()
                        .any(|d| d.consumer == b.position)
                })
                .context("sampling template has no dependency root")?;
            preparation::template_for_dispatch(
                &tx,
                &lease.lease_id,
                root.position,
                quality,
                size,
                now,
            )?;
        }
        tx.commit()?;
        return Ok(result);
    }
    let explicit = request
        .source_pair_id
        .as_deref()
        .map(|id| comparison::load(&tx, id))
        .transpose()?;
    let driver = crate::provider_graph_acquisition::Driver::from_batch(&request.reference_batch)?;
    if crate::provider_graph_acquisition::Driver::from_batch(&request.candidate_batch)? != driver {
        bail!("sampling batches belong to different drivers");
    }
    let driver_id = content_hash(&canonical(
        &serde_json::json!({"version":VERSION,"driver":driver,"candidate":explicit.as_ref().map(|p|&p.key)}),
    )?);
    let cached = if explicit.is_none() {
        discovered_pair(&tx, &driver_id)?
    } else {
        None
    };
    let mut pair = explicit.or_else(|| cached.as_ref().map(|(p, _)| p.clone()));
    let mut graphs = pair
        .as_ref()
        .map(|p| -> Result<_> {
            let r = crate::provider_graph::load(&tx, &p.reference_execution_id)?;
            let c = crate::provider_graph::load(&tx, &p.candidate_execution_id)?;
            if r.spec.calls.max_workers != 1
                || r.spec.calls.application_version != driver.application_version
                || r.spec.calls.callback_version != driver.callback_version
                || r.spec.calls.item_count != driver.item_count
            {
                bail!("sampling driver does not match the original paired templates");
            }
            Ok((r, c))
        })
        .transpose()?;
    let exists: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_graph_sampling_state WHERE driver_id=?1)",
        [&driver_id],
        |r| r.get(0),
    )?;
    if !exists {
        let s = State {
            version: VERSION.into(),
            driver_id: driver_id.clone(),
            policy: request.policy.clone(),
            seed: content_hash(&format!("{VERSION}\0{driver_id}\0{now}")),
            deployment: request.deployment.clone(),
        };
        let raw = canonical(&s)?;
        tx.execute(
            "INSERT INTO provider_graph_sampling_state VALUES(?1,?2,?3,0,0)",
            params![driver_id, content_hash(&raw), raw],
        )?;
    }
    let (s, workflows, samples) = state(&tx, &driver_id)?;
    if s.policy != request.policy
        || s.deployment != request.deployment
        || workflows >= s.policy.max_workflows
    {
        bail!("sampling policy changed or its lifetime workflow limit is exhausted");
    }
    if let Some(limit) = s.policy.max_lifetime_physical_invocations {
        if lifetime_physical(&tx, &s)?
            .checked_add(s.policy.max_physical_invocations)
            .is_none_or(|used| used > limit)
        {
            bail!("sampling lifetime physical allowance cannot reserve another workflow");
        }
    }
    reserve_commitment(&tx, &s)?;
    let window = match pair.as_ref() {
        Some(p) => quality::window(&tx, quality::Request::from_pair(p, size, now, quality))?,
        None => None,
    };
    let retired = pair.is_some() && window.is_none();
    let cooldown = if retired {
        let (p, _) = cached
            .as_ref()
            .context("explicit sampling template cohort has retired")?;
        checked_head(&tx, &p.key)?;
        checked_head(&tx, &p.reference_key)?;
        quality::refresh_cooldown(&tx, &p.key, now, quality)?
    } else {
        window.as_ref().and_then(|w| w.disabled.clone())
    };
    let sampled = samples < s.policy.max_samples
        && draw(&s.seed, workflows + 1) < s.policy.sampling_rate
        && cooldown.as_ref().is_none_or(|d| now >= d.reenable_at_us);
    let interval_due = sampled
        && cached.as_ref().is_some_and(|(_, owner)| {
            s.policy
                .refresh_interval_workflows
                .is_some_and(|interval| (workflows + 1).saturating_sub(owner.ordinal) >= interval)
        });
    let refresh = if retired || interval_due {
        let (p, owner) = cached
            .as_ref()
            .context("refresh has no original learned template")?;
        let value = Refresh {
            source_ticket_id: owner.ticket_id.clone(),
            source_pair_id: p.pair_id.clone(),
            source_pair_sha256: content_hash(&canonical(p)?),
            reason: if retired {
                RefreshReason::Retired
            } else {
                RefreshReason::Interval
            },
        };
        pair = None;
        graphs = None;
        Some(value)
    } else {
        None
    };
    // Only an unsampled workflow may return an admitted candidate. Sampled
    // counterfactuals retain their original reference-visible behavior.
    let guarded = if !sampled {
        match (&request.deployment, &pair, &graphs) {
            (Some(config), Some(pair), Some((_, candidate))) => Some(deployment::Begin {
                preparation: preparation::Begin {
                    batch_id: request.candidate_batch.batch_id.clone(),
                    max_workers: candidate.spec.calls.max_workers,
                    input_sha256: Some(request.input_sha256.clone()),
                    template_execution_id: Some(candidate.execution_id.clone()),
                    comparison_pair_id: None,
                    role_program: None,
                },
                admission: crate::provider_graph_admission::Request {
                    kind: crate::provider_graph_economics::Kind::Primary,
                    source_pair_ids: vec![pair.pair_id.clone()],
                    window_size: size,
                    remaining_batches: config
                        .remaining_batches
                        .min(s.policy.max_workflows - workflows),
                    policy: config.policy.clone(),
                },
                primary_bindings: vec![],
                dynamic_primary_nodes: true,
                campaign: s.policy.campaign.clone(),
            }),
            _ => None,
        }
    } else {
        None
    };
    // The receipt metadata establishes the upcoming driver before admission;
    // it creates no execution, acquisition charge or quality evidence.
    if guarded.is_some() {
        receipts::begin_batch(&tx, &request.candidate_batch)?;
    }
    let future_samples = if s.policy.sampling_rate == 0. {
        0
    } else {
        s.policy
            .max_samples
            .saturating_sub(samples)
            .min(s.policy.max_workflows - workflows - 1)
    };
    let deployed = guarded
        .as_ref()
        .map(|r| {
            deployment::select_for_begin(&tx, r, now, future_samples)
                .map(|(d, _, _)| !d.selected_reference)
        })
        .transpose()?
        .unwrap_or(false);
    let required = graphs.as_ref().map_or(
        u64::from(driver.item_count) * (1 + u64::from(sampled)),
        |(r, c)| {
            (if deployed {
                c.spec.calls.branches.len() as u64
            } else {
                r.spec.calls.branches.len() as u64
            }) + if sampled {
                c.spec.calls.branches.len() as u64
            } else {
                0
            }
        },
    );
    if required > request.policy.max_physical_invocations {
        bail!("sampling physical allowance cannot cover its graphs");
    }
    let mut ticket = Ticket {
        version: VERSION.into(),
        ticket_id: String::new(),
        driver_id: driver_id.clone(),
        ordinal: workflows + 1,
        sampled,
        issued_at_us: now,
        quality,
        window_size: size,
        request: request.clone(),
        discovered_pair_id: if request.source_pair_id.is_none() {
            pair.as_ref().map(|p| p.pair_id.clone())
        } else {
            None
        },
        refresh,
        deployed,
    };
    ticket.ticket_id = content_hash(&canonical(&ticket)?);
    for batch in [&request.reference_batch, &request.candidate_batch]
        .into_iter()
        .take(if sampled || deployed { 2 } else { 1 })
    {
        receipts::begin_batch(&tx, batch)?;
    }
    let raw = canonical(&ticket)?;
    tx.execute(
        "INSERT INTO provider_graph_sampling VALUES(?1,?2,?3,?4,?5,?6,?7,NULL,NULL)",
        params![
            ticket.ticket_id,
            driver_id,
            ticket.ordinal,
            request.reference_batch.batch_id,
            (sampled || deployed).then_some(&request.candidate_batch.batch_id),
            content_hash(&raw),
            raw
        ],
    )?;
    tx.execute("UPDATE provider_graph_sampling_state SET workflows=workflows+1,samples=samples+?2 WHERE driver_id=?1",params![driver_id,u64::from(sampled)])?;
    if deployed {
        let outcome = deployment::begin_inner(
            &tx,
            guarded.context("original deployment request is missing")?,
            now,
            Some(&ticket.ticket_id),
            future_samples,
        )?;
        let lease = outcome
            .lease
            .context("admitted sampling candidate changed before preparation")?;
        if let Some(policy) = &request.policy.campaign {
            crate::provider_graph_campaign::enroll_sampling(&tx, policy, &ticket)?;
        }
        verify_custody(&tx, &lease)?;
        tx.commit()?;
        return Ok(Outcome {
            ticket,
            reference: None,
            candidate: None,
            deployment: Some(lease),
            decision: Some(outcome.decision),
        });
    }
    let mut prepared = Vec::new();
    for (index, batch) in [&request.reference_batch, &request.candidate_batch]
        .into_iter()
        .take(if sampled { 2 } else { 1 })
        .enumerate()
    {
        let graph = graphs.as_ref().map(|(r, c)| if index == 0 { r } else { c });
        prepared.push(preparation::begin_inner(
            &tx,
            preparation::Begin {
                batch_id: batch.batch_id.clone(),
                max_workers: graph.map_or(1, |e| e.spec.calls.max_workers),
                input_sha256: Some(request.input_sha256.clone()),
                template_execution_id: graph.map(|e| e.execution_id.clone()),
                comparison_pair_id: pair.as_ref().map(|p| p.pair_id.clone()),
                role_program: None,
            },
            quality,
            size,
            now,
            Authorization::Sampling(ticket.ticket_id.clone()),
        )?);
    }
    let result = Outcome {
        ticket,
        reference: Some(prepared.remove(0)),
        candidate: prepared.pop(),
        deployment: None,
        decision: None,
    };
    if let Some(policy) = &request.policy.campaign {
        crate::provider_graph_campaign::enroll_sampling(&tx, policy, &result.ticket)?;
    }
    verify_custody(&tx, result.reference.as_ref().unwrap())?;
    if let Some(c) = &result.candidate {
        verify_custody(&tx, c)?;
    }
    tx.commit()?;
    Ok(result)
}

/// Pair only the two original owned executions, once both foregrounds end.
pub fn finish(
    conn: &mut Connection,
    id: &str,
    now: i64,
    quality: QualityPolicy,
    size: usize,
) -> Result<Finished> {
    let t = load(conn, id)?;
    let (old_hash, old): (Option<String>, Option<String>) = conn.query_row(
        "SELECT result_sha256,result_json FROM provider_graph_sampling WHERE ticket_id=?1",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if let Some(raw) = old {
        let done: Finished = serde_json::from_str(&raw)?;
        if old_hash.as_deref() != Some(content_hash(&raw).as_str())
            || canonical(&done)? != raw
            || done.ticket_id != id
            || done.sampled != t.sampled
        {
            bail!("sampling completion custody changed");
        }
        if let Some(pair) = &done.pair_id {
            let p = comparison::load(conn, pair)?;
            let owned:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_graph_preparation WHERE batch_id=?1 AND execution_id=?2) AND EXISTS(SELECT 1 FROM provider_graph_preparation WHERE batch_id=?3 AND execution_id=?4)",params![t.request.reference_batch.batch_id,p.reference_execution_id,t.request.candidate_batch.batch_id,p.candidate_execution_id],|r|r.get(0))?;
            if !owned || !t.sampled || done.status != "paired" {
                bail!("sampling completion pair belongs to another workflow");
            }
        } else if done.status == "paired"
            || !["reference_only", "incomplete", "no_alternative", "deployed"]
                .contains(&done.status.as_str())
        {
            bail!("sampling completion status has no original pair");
        }
        if t.deployed {
            let candidate = lease(conn, &t.request.candidate_batch.batch_id)?;
            let complete: bool = conn.query_row(
                "SELECT execution_id IS NOT NULL FROM provider_graph_preparation WHERE lease_id=?1",
                [&candidate.lease_id],
                |r| r.get(0),
            )?;
            if !["deployed", "incomplete"].contains(&done.status.as_str())
                || done.status == "deployed" && !complete
                || done.pair_id.is_some()
            {
                bail!("sampling deployment completion differs from its original visible workflow");
            }
            return Ok(done);
        } else if done.status == "deployed" {
            bail!("learning workflow has no deployment authorization");
        }
        let reference_complete: bool = conn.query_row(
            "SELECT execution_id IS NOT NULL FROM provider_graph_preparation WHERE batch_id=?1",
            [&t.request.reference_batch.batch_id],
            |r| r.get(0),
        )?;
        if done.status == "reference_only" && (t.sampled || !reference_complete) {
            bail!("sampling reference-only completion has no original visible execution");
        }
        if done.status == "no_alternative" {
            if !t.cold() || !t.sampled || !reference_complete {
                bail!("discovery has no original no-alternative completion");
            }
            let candidate = lease(conn, &t.request.candidate_batch.batch_id)?;
            let execution: String = conn.query_row(
                "SELECT execution_id FROM provider_graph_preparation WHERE lease_id=?1",
                [&candidate.lease_id],
                |r| r.get(0),
            )?;
            let e = crate::provider_graph::load(conn, &execution)?;
            if e.spec.calls.branches.iter().any(|b| {
                b.plan.target_model_id != b.plan.requested_model_id || !b.plan.rewrites.is_empty()
            }) {
                bail!("discovery no-alternative completion contains an alternative");
            }
        }
        return Ok(done);
    }
    if t.quality != quality || t.window_size != size || now < t.issued_at_us {
        bail!("sampling completion policy, window or clock differs from its original issue");
    }
    let mut ids = Vec::new();
    for batch in [
        if t.deployed {
            &t.request.candidate_batch
        } else {
            &t.request.reference_batch
        },
        &t.request.candidate_batch,
    ]
    .into_iter()
    .take(if t.sampled { 2 } else { 1 })
    {
        let (sealed,execution):(bool,Option<String>)=conn.query_row("SELECT b.sealed,p.execution_id FROM provider_receipt_batch b JOIN provider_graph_preparation p USING(batch_id) WHERE b.batch_id=?1",[&batch.batch_id],|r|Ok((r.get(0)?,r.get(1)?)))?;
        if !sealed {
            bail!("sampling foreground has not finished");
        }
        ids.push(execution);
    }
    let no_alternative = if t.cold() && t.sampled {
        ids[1]
            .as_deref()
            .map(|id| -> Result<bool> {
                let e = crate::provider_graph::load(conn, id)?;
                Ok(e.spec.calls.branches.iter().all(|b| {
                    b.plan.requested_model_id == b.plan.target_model_id
                        && b.plan.rewrites.is_empty()
                }))
            })
            .transpose()?
            .unwrap_or(false)
    } else {
        false
    };
    let pair = if t.sampled && !no_alternative {
        match (&ids[0], &ids[1]) {
            (Some(r), Some(c)) => Some(
                comparison::compare_with_policy(conn, r, c, now, t.window_size, t.quality)?
                    .pair
                    .pair_id,
            ),
            _ => None,
        }
    } else {
        None
    };
    let done = Finished {
        ticket_id: id.into(),
        sampled: t.sampled,
        status: if t.deployed && ids[0].is_some() {
            "deployed"
        } else if no_alternative {
            "no_alternative"
        } else if pair.is_some() {
            "paired"
        } else if !t.deployed && !t.sampled && ids[0].is_some() {
            "reference_only"
        } else {
            "incomplete"
        }
        .into(),
        pair_id: pair,
    };
    let raw = canonical(&done)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute("UPDATE provider_graph_sampling SET result_json=?2,result_sha256=?3 WHERE ticket_id=?1 AND result_json IS NULL",params![id,raw,content_hash(&raw)])?;
    if t.cold() {
        if let Some(pair) = &done.pair_id {
            if let Some(refresh) = &t.refresh {
                let current = discovered_pair(&tx, &t.driver_id)?
                    .context("refresh lost its original discovered template")?;
                if current.0.pair_id == refresh.source_pair_id
                    && current.1.ticket_id == refresh.source_ticket_id
                {
                    let history = checkpoints(&tx, &t.driver_id)?;
                    save_checkpoint(
                        &tx,
                        &TemplateCheckpoint {
                            driver_id: t.driver_id.clone(),
                            generation: history.len() as u64 + 1,
                            ticket_id: id.into(),
                            pair_id: pair.clone(),
                            prior_pair_id: Some(refresh.source_pair_id.clone()),
                        },
                    )?;
                    tx.execute("UPDATE provider_graph_sampling_discovered SET ticket_id=?2,pair_id=?3 WHERE driver_id=?1",params![t.driver_id,id,pair])?;
                }
            } else {
                let inserted = tx.execute("INSERT INTO provider_graph_sampling_discovered VALUES(?1,?2,?3) ON CONFLICT DO NOTHING",params![t.driver_id,id,pair])?;
                if inserted == 1 {
                    save_checkpoint(
                        &tx,
                        &TemplateCheckpoint {
                            driver_id: t.driver_id.clone(),
                            generation: 1,
                            ticket_id: id.into(),
                            pair_id: pair.clone(),
                            prior_pair_id: None,
                        },
                    )?;
                }
            }
        }
    }
    tx.commit()?;
    Ok(done)
}

/// The current visible workflow is included; concurrent completions cannot
/// restore an older, larger amortization horizon.
pub(crate) fn remaining_horizon(conn: &Connection, lease: &Lease) -> Result<Option<u64>> {
    let Some(id) = &lease.sampling_id else {
        return Ok(None);
    };
    let t = load(conn, id)?;
    if !t.deployed {
        return Ok(None);
    }
    let (s, workflows, _) = state(conn, &t.driver_id)?;
    Ok(Some(
        s.policy
            .max_workflows
            .saturating_sub(workflows)
            .saturating_add(1),
    ))
}
