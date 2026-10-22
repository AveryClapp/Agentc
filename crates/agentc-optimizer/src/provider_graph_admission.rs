//! Conservative graph evidence selection before trusted dispatch preparation.
//! This evaluator issues no dispatch lease and clears no quality disable. The
//! preparation caller must revalidate its snapshot and own deployment accounting.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::execution_plan::{PlanSemantics, SelectionObjective, SelectionPolicy};
use crate::plan_profile::PlanProfileKey;
use crate::provider_graph_comparison::Pair;
use crate::provider_graph_economics::{self as finance, Acquisition, Kind, UsdInterval};
use crate::provider_graph_quality::{self as quality, Domain};
use crate::provider_tariff::{self as tariff, Bound, Interval};

pub const VERSION: &str = "provider-graph-admission-evidence-v1";

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(tag = "quantity", rename_all = "snake_case", deny_unknown_fields)]
pub enum Money {
    #[default]
    BilledUsd,
    PublishedTokenTariffUsd {
        snapshot_sha256: String,
        max_expected_nanodollars: Option<u64>,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub selection: SelectionPolicy,
    pub quality: quality::Policy,
    #[serde(default)]
    pub money: Money,
    pub max_workers: u32,
    pub max_physical_invocations: u64,
}
impl Policy {
    fn validate(&self, size: usize, remaining: u64) -> Result<()> {
        self.selection.validate()?;
        self.quality.validate()?;
        if size < self.quality.min_pairs
            || remaining == 0
            || remaining > 1_u64 << 53
            || self.max_workers == 0
            || self.max_workers > crate::prepared_batch::MAX_PREPARED_BATCH_WORKERS
            || self.max_physical_invocations == 0
            || self.selection.divergence_exposure_budget != self.quality.exposure_budget
        {
            bail!("graph admission policy, horizon or resources are invalid");
        }
        if let Money::PublishedTokenTariffUsd {
            snapshot_sha256, ..
        } = &self.money
        {
            if snapshot_sha256 != &tariff::snapshot_sha256()
                || self.selection.max_expected_cost_usd.is_some()
            {
                bail!("graph admission monetary quantities or snapshots are incompatible");
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub kind: Kind,
    pub source_pair_ids: Vec<String>,
    pub window_size: usize,
    pub remaining_batches: u64,
    pub policy: Policy,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "quantity", rename_all = "snake_case")]
pub enum Certificate {
    BilledUsd {
        reference_lower_usd: f64,
        candidate_upper_usd: f64,
        acquisition_upper_usd: f64,
        expected_upper_usd: f64,
        net_savings_lower_usd: f64,
    },
    PublishedTokenTariffUsd {
        reference: Bound,
        candidate: Bound,
        acquisition: Bound,
        expected_upper_nanodollars: u64,
        #[serde(with = "signed_nanodollars")]
        net_savings_lower_nanodollars: i128,
    },
}

mod signed_nanodollars {
    use serde::{Deserialize, Deserializer, Serializer};
    pub fn serialize<S: Serializer>(value: &i128, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(value)
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<i128, D::Error> {
        let text = String::deserialize(deserializer)?;
        let value = text.parse::<i128>().map_err(serde::de::Error::custom)?;
        if value.to_string() != text {
            return Err(serde::de::Error::custom(
                "noncanonical signed nanodollar integer",
            ));
        }
        Ok(value)
    }
}
impl Certificate {
    fn positive(&self) -> bool {
        match self {
            Self::BilledUsd {
                net_savings_lower_usd,
                ..
            } => *net_savings_lower_usd > 0.,
            Self::PublishedTokenTariffUsd {
                net_savings_lower_nanodollars,
                ..
            } => *net_savings_lower_nanodollars > 0,
        }
    }
    fn cost_order(&self, other: &Self) -> std::cmp::Ordering {
        match (self, other) {
            (
                Self::BilledUsd {
                    expected_upper_usd: a,
                    ..
                },
                Self::BilledUsd {
                    expected_upper_usd: b,
                    ..
                },
            ) => a.total_cmp(b),
            (
                Self::PublishedTokenTariffUsd {
                    expected_upper_nanodollars: a,
                    ..
                },
                Self::PublishedTokenTariffUsd {
                    expected_upper_nanodollars: b,
                    ..
                },
            ) => a.cmp(b),
            _ => std::cmp::Ordering::Equal,
        }
    }
}

fn rank(a: &Alternative, b: &Alternative, objective: SelectionObjective) -> std::cmp::Ordering {
    let e = a.estimate.as_ref().unwrap();
    let old = b.estimate.as_ref().unwrap();
    let primary = match objective {
        SelectionObjective::Cost => e.money.cost_order(&old.money),
        SelectionObjective::Latency => e
            .expected_latency_upper_ms
            .total_cmp(&old.expected_latency_upper_ms),
    };
    let count = |e: &Estimate| {
        if e.readmission_required {
            e.quality.prepared_pairs.len()
        } else {
            e.quality.pairs.len()
        }
    };
    let divergence = |e: &Estimate| {
        if e.readmission_required {
            e.quality.prepared_divergence_upper_p95
        } else {
            e.quality.divergence_upper_p95
        }
    };
    primary
        .then_with(|| count(old).cmp(&count(e)))
        .then_with(|| {
            divergence(e)
                .unwrap_or(f64::INFINITY)
                .total_cmp(&divergence(old).unwrap_or(f64::INFINITY))
        })
        .then_with(|| e.mutation_count.cmp(&old.mutation_count))
        .then_with(|| a.key.execution_plan_id.cmp(&b.key.execution_plan_id))
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Estimate {
    pub money: Certificate,
    pub expected_latency_upper_ms: f64,
    pub net_latency_savings_lower_ms: f64,
    pub declared_workers: u32,
    pub declared_physical_invocations: u64,
    pub mutation_count: usize,
    pub quality: quality::Window,
    pub reference_costs: finance::Window,
    pub candidate_costs: finance::Window,
    pub readmission_required: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Alternative {
    pub source_pair_id: String,
    pub key: PlanProfileKey,
    pub rejection: Option<String>,
    pub estimate: Option<Estimate>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Decision {
    pub version: String,
    pub request: Request,
    pub reference_key: PlanProfileKey,
    pub selected_key: PlanProfileKey,
    pub selected_source_pair_id: Option<String>,
    pub selected_reference: bool,
    pub acquisition: Acquisition,
    pub alternatives: Vec<Alternative>,
    pub evidence_sha256: String,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub future_samples: u64,
}

fn is_zero(n: &u64) -> bool {
    *n == 0
}

impl Decision {
    /// Exact bytes hashed by the native selector, without depending on another
    /// language's floating-point JSON spelling.
    pub fn evidence_json(&self) -> Result<String> {
        let mut value = serde_json::to_value(self)?;
        value["evidence_sha256"] = serde_json::Value::String(String::new());
        Ok(canonical_json(&value))
    }
}

fn domain(kind: Kind) -> Domain {
    match kind {
        Kind::Primary => Domain::Primary,
        Kind::Roles => Domain::Roles,
    }
}
fn pair(conn: &Connection, kind: Kind, id: &str) -> Result<Pair> {
    match kind {
        Kind::Primary => crate::provider_graph_comparison::load(conn, id),
        Kind::Roles => crate::provider_graph_role_comparison::load(conn, id),
    }
}
fn canonical(value: &impl Serialize) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}

fn driver(conn: &Connection, observation: &finance::Observation) -> Result<String> {
    let id: Option<String> = conn
        .query_row(
            "SELECT driver_id FROM provider_graph_acquisition WHERE batch_id=?1",
            [&observation.batch_id],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(id) = id {
        return Ok(id);
    }
    let metadata: String = conn.query_row(
        "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
        [&observation.batch_id],
        |r| r.get(0),
    )?;
    crate::provider_graph_acquisition::Driver::from_batch(&serde_json::from_str(&metadata)?)?
        .identity()
}

fn resources(conn: &Connection, kind: Kind, source: &Pair) -> Result<(u32, u64, usize)> {
    match kind {
        Kind::Primary => {
            let e = crate::provider_graph::load(conn, &source.candidate_execution_id)?;
            Ok((
                e.spec.calls.max_workers,
                e.spec.calls.branches.len() as u64,
                e.spec.mutation_count(),
            ))
        }
        Kind::Roles => {
            let mut archive = crate::provider_graph_archive::Archive::new(conn)?;
            let e = archive.execution(&source.candidate_execution_id)?;
            let (_, p) = archive.preparation(&e.lease_id)?;
            Ok((
                p.program.graph.calls.max_workers,
                p.program.dispatches.len() as u64,
                p.program.graph.mutation_count()
                    + p.program
                        .dispatches
                        .iter()
                        .filter(|d| d.role != crate::provider_receipts::Role::Primary)
                        .count(),
            ))
        }
    }
}

fn contract(
    conn: &Connection,
    kind: Kind,
    id: &str,
    archive: &mut crate::provider_graph_archive::Archive<'_>,
) -> Result<String> {
    let (spec, mut controls) = match kind {
        Kind::Primary => {
            let e = crate::provider_graph::load(conn, id)?;
            (e.spec, e.source_contracts)
        }
        Kind::Roles => {
            let e = archive.execution(id)?;
            let (_, p) = archive.preparation(&e.lease_id)?;
            (p.program.graph, p.source_contracts)
        }
    };
    if controls.is_empty() {
        bail!("graph admission original controls are missing");
    }
    controls.sort_by_key(|v| v.position);
    let mut edges = spec.dependencies;
    edges.sort();
    let mut nodes = spec
        .calls
        .branches
        .iter()
        .map(|b| {
            (
                &b.position,
                &b.plan.provider_protocol,
                &b.plan.requested_model_id,
            )
        })
        .collect::<Vec<_>>();
    nodes.sort();
    canonical(
        &serde_json::json!({"application":spec.calls.application_version,"callback":spec.calls.callback_version,"items":spec.calls.item_count,"results":spec.results,"edges":edges,"nodes":nodes,"controls":controls}),
    )
}

fn bound(interval: Interval) -> Bound {
    Bound {
        quantity: tariff::QUANTITY.into(),
        snapshot_sha256: tariff::snapshot_sha256(),
        interval,
    }
}

fn certificate(
    r: &finance::Window,
    c: &finance::Window,
    a: &Acquisition,
    request: &Request,
    future_samples: u64,
) -> Result<Option<Certificate>> {
    match &request.policy.money {
        Money::BilledUsd => {
            let Some(acquisition) = a.costs.billed_usd else {
                return Ok(None);
            };
            if r.billed_usd_mean.is_none() || c.billed_usd_mean.is_none() {
                return Ok(None);
            }
            let reference = r
                .observations
                .iter()
                .map(|o| o.costs.billed_usd.unwrap().lower_usd)
                .reduce(f64::min)
                .unwrap();
            let candidate = c
                .observations
                .iter()
                .map(|o| o.costs.billed_usd.unwrap().upper_usd)
                .reduce(f64::max)
                .unwrap();
            let amortized = UsdInterval::exact(acquisition.upper_usd)?
                .mean(request.remaining_batches)?
                .upper_usd;
            let expected = UsdInterval::exact(candidate)?
                .add(UsdInterval::exact(amortized)?)?
                .add(
                    UsdInterval::exact(
                        r.observations
                            .iter()
                            .map(|o| o.costs.billed_usd.unwrap().upper_usd)
                            .reduce(f64::max)
                            .unwrap(),
                    )?
                    .scale(future_samples)?
                    .mean(request.remaining_batches)?,
                )?
                .upper_usd;
            let delta = reference - expected;
            let lower = if delta > 0. {
                finance::down(delta)
            } else {
                delta
            };
            if !delta.is_finite() {
                bail!("graph billed benefit overflow");
            }
            Ok(Some(Certificate::BilledUsd {
                reference_lower_usd: reference,
                candidate_upper_usd: candidate,
                acquisition_upper_usd: acquisition.upper_usd,
                expected_upper_usd: expected,
                net_savings_lower_usd: lower,
            }))
        }
        Money::PublishedTokenTariffUsd {
            snapshot_sha256, ..
        } => {
            let Some(acquisition) = &a.costs.published_token_tariff else {
                return Ok(None);
            };
            if r.published_token_tariff_mean.is_none() || c.published_token_tariff_mean.is_none() {
                return Ok(None);
            }
            let values = r
                .observations
                .iter()
                .chain(&c.observations)
                .filter_map(|o| o.costs.published_token_tariff.as_ref())
                .chain(std::iter::once(acquisition));
            if values
                .into_iter()
                .any(|b| b.quantity != tariff::QUANTITY || &b.snapshot_sha256 != snapshot_sha256)
            {
                bail!("graph tariff benefit snapshots are incompatible");
            }
            let lower = r
                .observations
                .iter()
                .map(|o| {
                    o.costs
                        .published_token_tariff
                        .as_ref()
                        .unwrap()
                        .interval
                        .lower_nanodollars
                })
                .min()
                .unwrap();
            let upper = c
                .observations
                .iter()
                .map(|o| {
                    o.costs
                        .published_token_tariff
                        .as_ref()
                        .unwrap()
                        .interval
                        .upper_nanodollars
                })
                .max()
                .unwrap();
            let reference = bound(Interval {
                lower_nanodollars: lower,
                upper_nanodollars: lower,
            });
            let candidate = bound(Interval {
                lower_nanodollars: upper,
                upper_nanodollars: upper,
            });
            let future = r
                .observations
                .iter()
                .map(|o| {
                    o.costs
                        .published_token_tariff
                        .as_ref()
                        .unwrap()
                        .interval
                        .upper_nanodollars
                })
                .max()
                .unwrap()
                .checked_mul(future_samples)
                .context("future graph tariff sampling overflow")?;
            let mut forecast = acquisition.clone();
            forecast.interval.upper_nanodollars = forecast
                .interval
                .upper_nanodollars
                .checked_add(future)
                .context("future graph tariff acquisition overflow")?;
            let net = tariff::savings_lower_bound(
                &reference,
                &candidate,
                &forecast,
                request.remaining_batches,
            )?;
            let expected = upper
                .checked_add(
                    forecast
                        .interval
                        .upper_nanodollars
                        .div_ceil(request.remaining_batches),
                )
                .context("graph tariff expected cost overflow")?;
            Ok(Some(Certificate::PublishedTokenTariffUsd {
                reference,
                candidate,
                acquisition: acquisition.clone(),
                expected_upper_nanodollars: expected,
                net_savings_lower_nanodollars: net,
            }))
        }
    }
}

fn fresh(
    window: &finance::Window,
    quality: &quality::Window,
    reference: bool,
    request: &Request,
) -> bool {
    let floor = if reference {
        quality.reference_floor
    } else {
        quality.candidate_floor
    };
    window.observations.iter().all(|o| {
        o.source_sequence >= floor
            && o.started_at_us
                .is_some_and(|start| start >= quality.evidence_boundary_us)
            && o.observed_at_us <= request.policy.selection.now_us
            && request
                .policy
                .selection
                .now_us
                .saturating_sub(o.observed_at_us)
                <= request.policy.selection.max_profile_age_us
    })
}

fn evaluate(
    conn: &Connection,
    p: &Pair,
    request: &Request,
    acquisition: &Acquisition,
    archive: &mut crate::provider_graph_archive::Archive<'_>,
    future_samples: u64,
) -> Result<Alternative> {
    let mut alternative = Alternative {
        source_pair_id: p.pair_id.clone(),
        key: p.key.clone(),
        rejection: Some("current_graph_evidence_missing".into()),
        estimate: None,
    };
    let (Some(r), Some(c), Some(q)) = (
        finance::window(
            conn,
            request.kind,
            &p.reference_key,
            &p.reference_runtime,
            request.window_size,
        )?,
        finance::window(
            conn,
            request.kind,
            &p.key,
            &p.runtime_version,
            request.window_size,
        )?,
        quality::window_for(
            domain(request.kind),
            conn,
            quality::Request::from_pair(
                p,
                request.window_size,
                request.policy.selection.now_us,
                request.policy.quality,
            ),
        )?,
    ) else {
        return Ok(alternative);
    };
    let min = request
        .policy
        .quality
        .min_pairs
        .max(request.policy.selection.min_paired_observations as usize);
    let pairs = if q.disabled.is_some() {
        &q.prepared_pairs
    } else {
        &q.pairs
    };
    let divergence = if q.disabled.is_some() {
        q.prepared_divergence_upper_p95
    } else {
        q.divergence_upper_p95
    };
    let hold = if r.observations.len() < min || c.observations.len() < min || pairs.len() < min {
        Some("insufficient_complete_graph_evidence")
    } else if q
        .disabled
        .as_ref()
        .is_some_and(|d| request.policy.selection.now_us < d.reenable_at_us)
    {
        Some("readmission_cooldown")
    } else if divergence.is_none_or(|d| d > request.policy.quality.divergence_threshold) {
        Some("whole_result_divergence")
    } else if q.divergence_exposure > 0.
        && q.divergence_exposure >= request.policy.quality.exposure_budget
    {
        Some("whole_result_exposure")
    } else if !fresh(&r, &q, true, request) || !fresh(&c, &q, false, request) {
        Some("stale_or_preboundary_cost_evidence")
    } else {
        None
    };
    if let Some(hold) = hold {
        alternative.rejection = Some(hold.into());
        return Ok(alternative);
    }
    let expected_contract = contract(conn, request.kind, &p.reference_execution_id, archive)?;
    for id in r
        .observations
        .iter()
        .chain(&c.observations)
        .map(|o| &o.execution_id)
        .chain(
            pairs
                .iter()
                .flat_map(|p| [&p.reference_execution_id, &p.candidate_execution_id]),
        )
    {
        if contract(conn, request.kind, id, archive)? != expected_contract {
            alternative.rejection = Some("incompatible_original_source_controls".into());
            return Ok(alternative);
        }
    }
    for o in r.observations.iter().chain(&c.observations) {
        if driver(conn, o)? != acquisition.economics.driver_id {
            bail!("graph financial evidence belongs to another acquisition driver");
        }
    }
    let (workers, physical, mutations) = resources(conn, request.kind, p)?;
    // A never-realized conditional cost path is not a free future fallback.
    if !c
        .observations
        .iter()
        .any(|o| o.costs.physical_invocations == physical)
    {
        alternative.rejection = Some("unobserved_physical_cost_branch".into());
        return Ok(alternative);
    }
    let Some(money) = certificate(&r, &c, acquisition, request, future_samples)? else {
        alternative.rejection = Some("incomplete_compatible_quantity_costs".into());
        return Ok(alternative);
    };
    if r.observations
        .iter()
        .chain(&c.observations)
        .any(|o| o.resource_latency_ms.is_none())
    {
        alternative.rejection = Some("incomplete_resource_accounting".into());
        return Ok(alternative);
    }
    let reference_latency = r
        .observations
        .iter()
        .map(|o| o.resource_latency_ms.unwrap().lower_ms)
        .reduce(f64::min)
        .unwrap();
    let candidate_latency = c
        .observations
        .iter()
        .map(|o| o.resource_latency_ms.unwrap().upper_ms)
        .reduce(f64::max)
        .unwrap();
    let acquisition_latency =
        finance::UsdInterval::exact(acquisition.acquisition_latency_upper_ms)?;
    let expected = finance::UsdInterval::exact(candidate_latency)?
        .add(acquisition_latency.mean(request.remaining_batches)?)?
        .add(
            finance::UsdInterval::exact(
                r.observations
                    .iter()
                    .map(|o| o.resource_latency_ms.unwrap().upper_ms)
                    .reduce(f64::max)
                    .unwrap(),
            )?
            .scale(future_samples)?
            .mean(request.remaining_batches)?,
        )?
        .upper_usd;
    let net_latency = reference_latency - expected;
    let estimate = Estimate {
        money,
        expected_latency_upper_ms: expected,
        net_latency_savings_lower_ms: if net_latency > 0. {
            finance::down(net_latency)
        } else {
            net_latency
        },
        declared_workers: workers,
        declared_physical_invocations: physical,
        mutation_count: mutations,
        readmission_required: q.disabled.is_some(),
        quality: q,
        reference_costs: r,
        candidate_costs: c,
    };
    let rejection = if workers > request.policy.max_workers
        || physical > request.policy.max_physical_invocations
    {
        Some("physical_resource_limit")
    } else if matches!(request.policy.selection.objective, SelectionObjective::Cost)
        && !estimate.money.positive()
    {
        Some("nonpositive_net_cost_benefit")
    } else if match (&estimate.money, &request.policy.money) {
        (
            Certificate::BilledUsd {
                expected_upper_usd, ..
            },
            Money::BilledUsd,
        ) => request
            .policy
            .selection
            .max_expected_cost_usd
            .is_some_and(|m| *expected_upper_usd > m),
        (
            Certificate::PublishedTokenTariffUsd {
                expected_upper_nanodollars,
                ..
            },
            Money::PublishedTokenTariffUsd {
                max_expected_nanodollars,
                ..
            },
        ) => max_expected_nanodollars.is_some_and(|m| *expected_upper_nanodollars > m),
        _ => true,
    } {
        Some("expected_cost_limit")
    } else if request
        .policy
        .selection
        .max_expected_latency_ms
        .is_some_and(|m| expected > m)
    {
        Some("expected_latency_limit")
    } else if matches!(
        request.policy.selection.objective,
        SelectionObjective::Latency
    ) && estimate.net_latency_savings_lower_ms <= 0.
    {
        Some("nonpositive_net_latency_benefit")
    } else {
        None
    };
    alternative.rejection = rejection.map(str::to_owned);
    alternative.estimate = Some(estimate);
    Ok(alternative)
}

/// Selects admissible evidence; the selected graph is still an acquisition
/// proposal until a trusted caller atomically binds deployment preparation.
pub fn select(conn: &Connection, request: Request) -> Result<Decision> {
    select_inner(conn, request, None, 0)
}

/// Each remaining workflow already includes candidate cost. A future sampled
/// replay adds its original reference cost and serial foreground latency.
pub(crate) fn select_with_future_samples(
    conn: &Connection,
    request: Request,
    future_samples: u64,
) -> Result<Decision> {
    select_inner(conn, request, None, future_samples)
}

pub(crate) fn select_for_dispatch(
    conn: &Connection,
    request: Request,
    lease_id: &str,
    future_samples: u64,
) -> Result<Decision> {
    select_inner(conn, request, Some(lease_id), future_samples)
}

fn select_inner(
    conn: &Connection,
    request: Request,
    lease_id: Option<&str>,
    future_samples: u64,
) -> Result<Decision> {
    let future_samples = future_samples.min(request.remaining_batches.saturating_sub(1));
    request
        .policy
        .validate(request.window_size, request.remaining_batches)?;
    if request.source_pair_ids.is_empty() {
        bail!("graph admission requires explicit original source pairs");
    }
    quality::ensure_schema_for(domain(request.kind), conn)?;
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let decision = select_inner(&tx, request, lease_id, future_samples)?;
        tx.commit()?;
        return Ok(decision);
    }
    let mut pairs = vec![];
    let mut archive = crate::provider_graph_archive::Archive::new(conn)?;
    let mut keys = std::collections::HashSet::new();
    for id in &request.source_pair_ids {
        let p = pair(conn, request.kind, id)?;
        if p.observed_at_us > request.policy.selection.now_us || !keys.insert(p.key.clone()) {
            bail!("graph admission source pair is future or duplicated");
        }
        if let Some(first) = pairs.first() {
            let first: &Pair = first;
            if p.reference_key != first.reference_key
                || p.reference_runtime != first.reference_runtime
                || contract(conn, request.kind, &p.reference_execution_id, &mut archive)?
                    != contract(
                        conn,
                        request.kind,
                        &first.reference_execution_id,
                        &mut archive,
                    )?
            {
                bail!("graph admission alternatives have different reference contracts");
            }
        }
        pairs.push(p);
    }
    let first = &pairs[0];
    let original = finance::execution(conn, request.kind, &first.reference_execution_id)?;
    let acquisition =
        finance::summarize_acquisition_for_dispatch(conn, &driver(conn, &original)?, lease_id)?;
    let mut alternatives = vec![];
    let mut selected: Option<usize> = None;
    for p in &pairs {
        let alternative = evaluate(
            conn,
            p,
            &request,
            &acquisition,
            &mut archive,
            future_samples.min(request.remaining_batches.saturating_sub(1)),
        )?;
        if alternative.rejection.is_none() {
            let better = selected.is_none_or(|i| {
                rank(
                    &alternative,
                    &alternatives[i],
                    request.policy.selection.objective,
                )
                .is_lt()
            });
            if better {
                selected = Some(alternatives.len());
            }
        }
        alternatives.push(alternative);
    }
    let mut decision = Decision {
        version: VERSION.into(),
        reference_key: first.reference_key.clone(),
        selected_key: selected.map_or_else(
            || first.reference_key.clone(),
            |i| alternatives[i].key.clone(),
        ),
        selected_source_pair_id: selected.map(|i| alternatives[i].source_pair_id.clone()),
        selected_reference: selected.is_none(),
        request,
        acquisition,
        alternatives,
        evidence_sha256: String::new(),
        future_samples,
    };
    decision.evidence_sha256 = content_hash(&decision.evidence_json()?);
    Ok(decision)
}

#[cfg(test)]
mod forecast_tests {
    use super::*;
    fn window(cost: u64) -> finance::Window {
        let value = serde_json::json!({
        "kind":"primary","key":{"call_site_version":"a".repeat(64),"execution_plan_id":"b".repeat(64)},
        "runtime":{"provider_protocol":"fixture","target_model_id":"fixture","target_model_version":"v1","price_table_version":"p1"},
        "observation_floor":1,"source_sequence":1,"lifetime_executions":1,
        "billed_usd_mean":{"lower_usd":cost as f64,"upper_usd":cost as f64},
        "published_token_tariff_mean":bound(Interval{lower_nanodollars:cost,upper_nanodollars:cost}),
        "foreground_latency_mean_ms":null,"observations":[{
            "version":"fixture","kind":"primary","execution_id":"e","batch_id":"b","source_sequence":1,
            "key":{"call_site_version":"a".repeat(64),"execution_plan_id":"b".repeat(64)},
            "runtime":{"provider_protocol":"fixture","target_model_id":"fixture","target_model_version":"v1","price_table_version":"p1"},
            "journal_sha256":"c".repeat(64),"started_at_us":0,"observed_at_us":1,"foreground_latency_ms":null,
            "costs":{"physical_invocations":1,"unpriced_usd_invocations":0,"unpriced_tariff_invocations":0,
                "billed_usd":{"lower_usd":cost as f64,"upper_usd":cost as f64},
                "known_billed_usd":{"lower_usd":cost as f64,"upper_usd":cost as f64},
                "published_token_tariff":bound(Interval{lower_nanodollars:cost,upper_nanodollars:cost}),
                "observed_tariff_interval":{"lower_nanodollars":cost,"upper_nanodollars":cost},"receipts":[]}
        }]});
        serde_json::from_value(value).unwrap()
    }
    #[test]
    fn future_replay_changes_billed_and_tariff_benefit_and_unknown_prices_stay_held() {
        let r = window(100);
        let c = window(10);
        let mut acquisition = Acquisition {
            version: "fixture".into(),
            economics: Default::default(),
            costs: window(1000).observations.remove(0).costs,
            deployment: None,
            acquisition_latency_upper_ms: 0.,
        };
        let mut request = Request {
            kind: Kind::Primary,
            source_pair_ids: vec![],
            window_size: 20,
            remaining_batches: 100,
            policy: Policy {
                selection: SelectionPolicy::default(),
                quality: quality::Policy::default(),
                money: Money::BilledUsd,
                max_workers: 1,
                max_physical_invocations: 1,
            },
        };
        for tariff in [false, true] {
            request.policy.money = if tariff {
                Money::PublishedTokenTariffUsd {
                    snapshot_sha256: crate::provider_tariff::snapshot_sha256(),
                    max_expected_nanodollars: None,
                }
            } else {
                Money::BilledUsd
            };
            assert!(certificate(&r, &c, &acquisition, &request, 0)
                .unwrap()
                .unwrap()
                .positive());
            let forecast = certificate(&r, &c, &acquisition, &request, 99)
                .unwrap()
                .unwrap();
            assert!(!forecast.positive());
            match forecast {
                Certificate::BilledUsd {
                    expected_upper_usd, ..
                } => assert!((119.0..119.00000000001).contains(&expected_upper_usd)),
                Certificate::PublishedTokenTariffUsd {
                    expected_upper_nanodollars,
                    net_savings_lower_nanodollars,
                    ..
                } => {
                    assert_eq!(expected_upper_nanodollars, 119);
                    assert_eq!(net_savings_lower_nanodollars, -19);
                }
            }
        }
        assert!(certificate(&window(u64::MAX), &c, &acquisition, &request, 99).is_err());
        acquisition.costs.published_token_tariff = None;
        assert!(certificate(&r, &c, &acquisition, &request, 99)
            .unwrap()
            .is_none());
        request.policy.money = Money::BilledUsd;
        acquisition.costs.billed_usd = None;
        assert!(certificate(&r, &c, &acquisition, &request, 99)
            .unwrap()
            .is_none());
    }
}
