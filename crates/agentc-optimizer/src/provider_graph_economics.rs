//! Read-only financial projections of original graph executions and liabilities.
//! Bills and pinned published tariffs remain separate quantities. Reconciliation
//! never changes execution order, quality custody or retirement boundaries.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::multi_call::InferenceCostSource;
use crate::plan_profile::{PlanProfileKey, PlanRuntimeVersion};
use crate::provider_graph_acquisition::{self as acquisition, Economics};
use crate::provider_receipts::{Completion, Invocation, Status};
use crate::provider_tariff::{self as tariff, Bound, Interval};

pub const VERSION: &str = "provider-graph-economic-projection-v1";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Primary,
    Roles,
}
impl Kind {
    fn prefix(self) -> &'static str {
        match self {
            Self::Primary => "provider_graph",
            Self::Roles => "provider_graph_role",
        }
    }
    fn ensure(self, conn: &Connection) -> Result<()> {
        match self {
            Self::Primary => crate::provider_graph::ensure_schema(conn),
            Self::Roles => crate::provider_graph_roles::ensure_schema(conn),
        }
    }
}

/// Outward-rounded arithmetic keeps an observed bill's binary value contained
/// when summing and averaging. Tariff nanodollars use integer arithmetic.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsdInterval {
    pub lower_usd: f64,
    pub upper_usd: f64,
}
impl UsdInterval {
    pub(crate) fn exact(value: f64) -> Result<Self> {
        if !value.is_finite() || value < 0. {
            bail!("graph billed USD is invalid");
        }
        Ok(Self {
            lower_usd: value,
            upper_usd: value,
        })
    }
    pub(crate) fn add(self, other: Self) -> Result<Self> {
        if self.upper_usd == 0. {
            return Ok(other);
        }
        if other.upper_usd == 0. {
            return Ok(self);
        }
        let upper = self.upper_usd + other.upper_usd;
        let lower = self.lower_usd + other.lower_usd;
        if !upper.is_finite() {
            bail!("graph billed USD overflow");
        }
        let result = Self {
            lower_usd: down(lower),
            upper_usd: up(upper),
        };
        if !result.upper_usd.is_finite() {
            bail!("graph billed USD overflow");
        }
        Ok(result)
    }
    pub(crate) fn mean(self, count: u64) -> Result<Self> {
        if count == 0 {
            bail!("graph economic window is empty");
        }
        if count == 1 || self.upper_usd == 0. {
            return Ok(self);
        }
        // Window sizes exceeding exact integer-to-float representation cannot
        // prove a conservative denominator.
        if count > 1_u64 << 53 {
            bail!("graph economic window is too large");
        }
        Ok(Self {
            lower_usd: down(self.lower_usd / count as f64),
            upper_usd: up(self.upper_usd / count as f64),
        })
    }
    pub(crate) fn scale(self, count: u64) -> Result<Self> {
        if count == 0 || self.upper_usd == 0. {
            return Self::exact(0.);
        }
        if count == 1 {
            return Ok(self);
        }
        if count > 1_u64 << 53 {
            bail!("graph economic multiplier is too large");
        }
        let upper = up(self.upper_usd * count as f64);
        if !upper.is_finite() {
            bail!("graph economic forecast overflow");
        }
        Ok(Self {
            lower_usd: down(self.lower_usd * count as f64),
            upper_usd: upper,
        })
    }
}
pub(crate) fn up(value: f64) -> f64 {
    f64::from_bits(value.to_bits() + 1)
}
pub(crate) fn down(value: f64) -> f64 {
    if value <= 0. {
        0.
    } else {
        f64::from_bits(value.to_bits() - 1)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Receipt {
    pub invocation_id: String,
    pub receipt_sha256: String,
    pub settlement_sha256: Option<String>,
    pub billed_usd: Option<f64>,
    pub known_billed_usd: f64,
    pub tariff: Option<Bound>,
    pub billed_hold: Option<String>,
    pub tariff_hold: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Costs {
    pub physical_invocations: u64,
    pub unpriced_usd_invocations: u64,
    pub unpriced_tariff_invocations: u64,
    pub billed_usd: Option<UsdInterval>,
    pub published_token_tariff: Option<Bound>,
    pub known_billed_usd: UsdInterval,
    pub observed_tariff_interval: Interval,
    pub receipts: Vec<Receipt>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Observation {
    pub version: String,
    pub kind: Kind,
    pub execution_id: String,
    pub batch_id: String,
    pub source_sequence: i64,
    pub key: PlanProfileKey,
    pub runtime: PlanRuntimeVersion,
    pub journal_sha256: String,
    pub started_at_us: Option<i64>,
    pub observed_at_us: i64,
    pub foreground_latency_ms: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resource_latency_ms: Option<LatencyInterval>,
    pub costs: Costs,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct LatencyInterval {
    pub lower_ms: f64,
    pub upper_ms: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Window {
    pub kind: Kind,
    pub key: PlanProfileKey,
    pub runtime: PlanRuntimeVersion,
    pub observation_floor: i64,
    pub source_sequence: i64,
    pub lifetime_executions: u64,
    pub observations: Vec<Observation>,
    pub billed_usd_mean: Option<UsdInterval>,
    pub published_token_tariff_mean: Option<Bound>,
    pub foreground_latency_mean_ms: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Acquisition {
    pub version: String,
    pub economics: Economics,
    pub costs: Costs,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deployment: Option<DeploymentAccounting>,
    pub acquisition_latency_upper_ms: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DeploymentAccounting {
    pub batches: Vec<String>,
    pub costs: Costs,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub economics: Option<Economics>,
}

fn bound(interval: Interval) -> Bound {
    Bound {
        quantity: tariff::QUANTITY.into(),
        snapshot_sha256: tariff::snapshot_sha256(),
        interval,
    }
}
fn canonical(value: &impl Serialize) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}

pub(crate) fn receipt(conn: &Connection, batch: &str, id: &str) -> Result<Receipt> {
    let (raw, done): (String, Option<String>) = conn.query_row(
        "SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2",
        params![batch, id], |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let invocation: Invocation = serde_json::from_str(&raw)?;
    let completion: Option<Completion> = done.as_deref().map(serde_json::from_str).transpose()?;
    let binding = content_hash(&canonical(
        &serde_json::json!({"version":"provider-billing-binding-v1","batch_id":batch,"invocation_id":id,"invocation":raw,"completion":done}),
    )?);
    let settlement = crate::provider_reconciliation::settled_cost(conn, id)?;
    let settlement_json: Option<String> = conn
        .query_row(
            "SELECT invoice_json FROM provider_billing_settlement WHERE invocation_id=?1",
            [id],
            |r| r.get(0),
        )
        .optional()?;
    let mut result = Receipt {
        invocation_id: id.into(),
        receipt_sha256: binding,
        settlement_sha256: settlement_json.map(|v| content_hash(&v)),
        billed_usd: None,
        tariff: None,
        known_billed_usd: 0.,
        billed_hold: Some("pending_completion".into()),
        tariff_hold: Some("pending_completion".into()),
    };
    if let Some(c) = completion {
        result.known_billed_usd = settlement.unwrap_or_else(|| {
            if c.cost_source == InferenceCostSource::ProviderReported {
                c.cost_usd.unwrap_or(0.)
            } else {
                0.
            }
        });
        UsdInterval::exact(result.known_billed_usd)?;
        if !invocation.transport_verified
            || invocation.transport_sha256.is_none()
            || invocation.request_sha256.is_none()
            || c.http_attempts != Some(1)
        {
            result.billed_hold = Some("unverified_dispatch".into());
        } else if let Some(value) = settlement.or_else(|| {
            (c.status == Status::Completed
                && c.cost_source == InferenceCostSource::ProviderReported)
                .then_some(c.cost_usd)
                .flatten()
        }) {
            UsdInterval::exact(value)?;
            result.billed_usd = Some(value);
            result.billed_hold = None;
        } else {
            result.billed_hold = Some("unresolved_bill".into());
        }
        match tariff::quote(&invocation, &c) {
            Ok(value) => {
                result.tariff = Some(value);
                result.tariff_hold = None;
            }
            Err(reason) => result.tariff_hold = Some(reason.into()),
        }
    }
    Ok(result)
}

fn costs(conn: &Connection, batches: &[String]) -> Result<Costs> {
    let mut result = Costs {
        physical_invocations: 0,
        unpriced_usd_invocations: 0,
        unpriced_tariff_invocations: 0,
        billed_usd: None,
        published_token_tariff: None,
        known_billed_usd: UsdInterval::exact(0.)?,
        observed_tariff_interval: Interval {
            lower_nanodollars: 0,
            upper_nanodollars: 0,
        },
        receipts: vec![],
    };
    for batch in batches {
        let ids = {
            let mut query = conn.prepare("SELECT invocation_id FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal")?;
            let rows = query
                .query_map([batch], |r| r.get::<_, String>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            rows
        };
        for id in ids {
            let r = receipt(conn, batch, &id)?;
            result.physical_invocations += 1;
            result.known_billed_usd = result
                .known_billed_usd
                .add(UsdInterval::exact(r.known_billed_usd)?)?;
            if r.billed_usd.is_none() {
                result.unpriced_usd_invocations += 1;
            }
            if let Some(value) = &r.tariff {
                result.observed_tariff_interval = result
                    .observed_tariff_interval
                    .checked_add(value.interval)?;
            } else {
                result.unpriced_tariff_invocations += 1;
            }
            result.receipts.push(r);
        }
    }
    if result.physical_invocations > 0 {
        if result.unpriced_usd_invocations == 0 {
            result.billed_usd = Some(result.known_billed_usd);
        }
        if result.unpriced_tariff_invocations == 0 {
            result.published_token_tariff = Some(bound(result.observed_tariff_interval));
        }
    }
    Ok(result)
}

fn observation(
    conn: &Connection,
    kind: Kind,
    id: &str,
    archive: &mut crate::provider_graph_archive::Archive<'_>,
) -> Result<Observation> {
    let (batch, key, runtime, journal, start, observed, original_latency) = match kind {
        Kind::Primary => {
            let e = crate::provider_graph::load(conn, id)?;
            let start = crate::provider_graph_preparation::original_start(conn, &e)?;
            (
                e.batch_id,
                e.key,
                e.runtime,
                e.journal_sha256,
                start,
                e.observed_at_us,
                e.totals.wall_latency_ms,
            )
        }
        Kind::Roles => {
            let e = archive.execution(id)?;
            (
                e.batch_id,
                e.key,
                e.runtime,
                e.journal_sha256,
                Some(e.started_at_us),
                e.observed_at_us,
                e.acquisition.foreground_latency_ms,
            )
        }
    };
    let sequence = conn.query_row(
        &format!(
            "SELECT rowid FROM {}_execution WHERE execution_id=?1",
            kind.prefix()
        ),
        [id],
        |r| r.get(0),
    )?;
    let economics = acquisition::report_batch(conn, &batch)?;
    let latency = economics
        .as_ref()
        .filter(|e| e.unfinished_batches == 0 && e.persistence_failed_batches == 0)
        .map(|e| e.foreground_latency_ms);
    if latency.is_some_and(|latency| latency != original_latency) {
        bail!("graph resource time differs from its original execution");
    }
    let resource_latency_ms = if let (Some(foreground), Some(economics)) = (latency, &economics) {
        if economics.pending_invocations == 0 && economics.pending_billing_verifications == 0 {
            Some(LatencyInterval {
                lower_ms: foreground,
                upper_ms: acquisition::resource_latency_bound(conn, std::slice::from_ref(&batch))?
                    .upper_usd,
            })
        } else {
            None
        }
    } else {
        None
    };
    Ok(Observation {
        version: VERSION.into(),
        kind,
        execution_id: id.into(),
        batch_id: batch.clone(),
        source_sequence: sequence,
        key,
        runtime,
        journal_sha256: journal,
        started_at_us: start,
        observed_at_us: observed,
        foreground_latency_ms: latency,
        resource_latency_ms,
        costs: costs(conn, &[batch])?,
    })
}

pub fn execution(conn: &Connection, kind: Kind, id: &str) -> Result<Observation> {
    kind.ensure(conn)?;
    acquisition::ensure_schema(conn)?;
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let result = execution(&tx, kind, id)?;
        tx.commit()?;
        return Ok(result);
    }
    observation(
        conn,
        kind,
        id,
        &mut crate::provider_graph_archive::Archive::new(conn)?,
    )
}

/// Select exactly the current original publication window before pricing it.
/// Unsupported/unknown bills or usage occupy their original slots. A later
/// invoice resolves an existing slot without adding or reordering evidence.
pub fn window(
    conn: &Connection,
    kind: Kind,
    key: &PlanProfileKey,
    runtime: &PlanRuntimeVersion,
    size: usize,
) -> Result<Option<Window>> {
    if size == 0 {
        bail!("graph economic window is empty");
    }
    kind.ensure(conn)?;
    acquisition::ensure_schema(conn)?;
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let result = window(&tx, kind, key, runtime, size)?;
        tx.commit()?;
        return Ok(result);
    }
    let head: Option<(String, i64, i64, usize, u64)> = conn.query_row(
        &format!("SELECT runtime_json,source_sequence,observation_floor,window_size,lifetime_executions FROM {}_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2",kind.prefix()),
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)),
    ).optional()?;
    let Some((raw_runtime, sequence, floor, window_size, lifetime)) = head else {
        return Ok(None);
    };
    if floor <= 0 || sequence < floor || lifetime == 0 || window_size == 0 {
        bail!("graph economic execution boundary is invalid");
    }
    if raw_runtime != canonical(runtime)? || window_size != size {
        return Ok(None);
    }
    let (count, latest): (u64, Option<i64>) = conn.query_row(
        &format!("SELECT COUNT(*),MAX(rowid) FROM {}_execution WHERE json_extract(execution_json,'$.key.call_site_version')=?1 AND json_extract(execution_json,'$.key.execution_plan_id')=?2", kind.prefix()),
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()], |r| Ok((r.get(0)?,r.get(1)?)),
    )?;
    if count != lifetime || latest != Some(sequence) {
        bail!("graph economic head has incomplete original execution coverage");
    }
    let ids = {
        let mut query = conn.prepare(&format!("SELECT execution_id FROM {}_execution WHERE json_extract(execution_json,'$.key.call_site_version')=?1 AND json_extract(execution_json,'$.key.execution_plan_id')=?2 AND rowid>=?3 AND rowid<=?4 ORDER BY rowid DESC LIMIT ?5",kind.prefix()))?;
        let rows = query
            .query_map(
                params![
                    key.call_site_version.as_str(),
                    key.execution_plan_id.as_str(),
                    floor,
                    sequence,
                    size
                ],
                |r| r.get::<_, String>(0),
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let mut archive = crate::provider_graph_archive::Archive::new(conn)?;
    let mut observations = vec![];
    let mut usd = Some(UsdInterval::exact(0.)?);
    let mut tokens = Some(Interval {
        lower_nanodollars: 0,
        upper_nanodollars: 0,
    });
    let mut latency = Some(0.);
    for id in ids {
        let o = observation(conn, kind, &id, &mut archive)?;
        if o.key != *key || o.runtime != *runtime {
            bail!("graph economic projection differs from its original runtime");
        }
        usd = usd
            .zip(o.costs.billed_usd)
            .map(|(a, b)| a.add(b))
            .transpose()?;
        tokens = tokens
            .zip(o.costs.published_token_tariff.as_ref())
            .map(|(a, b)| a.checked_add(b.interval))
            .transpose()?;
        latency = latency.zip(o.foreground_latency_ms).map(|(a, b)| a + b);
        observations.push(o);
    }
    let count = observations.len() as u64;
    if count == 0 || count > lifetime {
        bail!("graph economic execution coverage is invalid");
    }
    if latency.is_some_and(|v| !v.is_finite() || v < 0.) {
        bail!("graph economic latency overflow");
    }
    Ok(Some(Window {
        kind,
        key: key.clone(),
        runtime: runtime.clone(),
        observation_floor: floor,
        source_sequence: sequence,
        lifetime_executions: lifetime,
        observations,
        billed_usd_mean: usd.map(|v| v.mean(count)).transpose()?,
        published_token_tariff_mean: tokens.map(|v| {
            bound(Interval {
                lower_nanodollars: v.lower_nanodollars / count,
                upper_nanodollars: v.upper_nanodollars.div_ceil(count),
            })
        }),
        foreground_latency_mean_ms: latency.map(|v| v / count as f64),
    }))
}

/// Full driver lifetime, including all variants, unfinished/late/failed work and
/// verified billing lookup time. Retiring a runtime cannot erase acquisition.
pub fn summarize_acquisition(conn: &Connection, driver: &str) -> Result<Acquisition> {
    summarize_acquisition_for_dispatch(conn, driver, None)
}

pub(crate) fn summarize_acquisition_for_dispatch(
    conn: &Connection,
    driver: &str,
    current_lease: Option<&str>,
) -> Result<Acquisition> {
    acquisition::ensure_schema(conn)?;
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let result = summarize_acquisition_for_dispatch(&tx, driver, current_lease)?;
        tx.commit()?;
        return Ok(result);
    }
    let all_batches = {
        let mut query = conn.prepare(
            "SELECT batch_id FROM provider_graph_acquisition WHERE driver_id=?1 ORDER BY batch_id",
        )?;
        let rows = query
            .query_map([driver], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let (batches, deployed) =
        crate::provider_graph_deployment::partition(conn, all_batches, current_lease)?;
    let economics = acquisition::summarize_batches(conn, driver, &batches)?;
    let mut costs = costs(conn, &batches)?;
    if !economics.cost_complete {
        costs.billed_usd = None;
    }
    if economics.owned_batches == 0
        || economics.pending_invocations > 0
        || economics.unfinished_batches > 0
        || economics.persistence_failed_batches > 0
        || economics.pending_billing_verifications > 0
        || economics.uncovered_preparations > 0
    {
        costs.published_token_tariff = None;
    }
    if costs.physical_invocations != economics.physical_invocations {
        bail!("graph acquisition financial coverage differs from its owned ledger");
    }
    Ok(Acquisition {
        version: VERSION.into(),
        economics,
        costs,
        acquisition_latency_upper_ms: acquisition::resource_latency_bound(conn, &batches)?
            .upper_usd,
        deployment: if deployed.is_empty() {
            None
        } else {
            Some(DeploymentAccounting {
                costs: self::costs(conn, &deployed)?,
                economics: Some(acquisition::summarize_batches(conn, driver, &deployed)?),
                batches: deployed,
            })
        },
    })
}
