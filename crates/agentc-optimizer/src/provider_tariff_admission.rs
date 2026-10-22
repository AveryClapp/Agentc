//! Explicit published-token-tariff admission from original economic and quality
//! evidence. Default billed-USD selection remains a separate contract.

use anyhow::{bail, Result};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};

use crate::execution_plan::{SelectionObjective, SelectionPolicy};
use crate::multi_call::{
    MultiCallPlanSpec, EXACT_MAP_COMPARISON_VERSION, PREPARED_PROVIDER_BATCH_VERSION,
};
use crate::plan_guard::{PlanGuard, PlanGuardDecision};
use crate::plan_profile::{PlanProfileKey, PlanRuntimeVersion};
use crate::provider_interval_profiles;
use crate::provider_quality;
use crate::provider_tariff::{self, Acquisition, Bound, Interval, QUANTITY};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub quantity: String,
    pub snapshot_sha256: String,
    pub max_expected_nanodollars: Option<u64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Estimate {
    pub reference: Bound,
    pub candidate: Bound,
    pub acquisition: Bound,
    pub expected_upper_nanodollars: u64,
    pub net_savings_lower_nanodollars: i128,
    pub expected_latency_ms: f64,
    pub net_latency_savings_ms: f64,
    pub quality: provider_quality::Window,
}

#[derive(Debug, Clone, Serialize)]
pub struct Alternative {
    pub key: PlanProfileKey,
    pub max_workers: u32,
    pub rejection: Option<String>,
    pub estimate: Option<Estimate>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Schedule {
    pub max_workers: u32,
    pub selected_reference: bool,
    pub reason: String,
    pub selected_key: PlanProfileKey,
    pub economic_quantity: String,
    pub snapshot_sha256: String,
    pub remaining_batches: u64,
    pub acquisition_accounting: Acquisition,
    pub alternatives: Vec<Alternative>,
}

pub struct Request<'a> {
    pub reference: &'a MultiCallPlanSpec,
    pub runtime: &'a PlanRuntimeVersion,
    pub max_workers: u32,
    pub remaining_batches: u64,
    pub window_size: usize,
    pub policy: &'a SelectionPolicy,
    pub tariff: &'a Policy,
}

fn mean_latency(conn: &Connection, window: &provider_interval_profiles::Window) -> Result<f64> {
    if window.observations.is_empty() {
        bail!("original tariff window has no executions");
    }
    let mut total = 0.0;
    for observation in &window.observations {
        if observation.original_execution_id.is_none() {
            bail!("tariff observation lacks original execution custody");
        }
        let original = crate::provider_publication::load_input(conn, &observation.batch_id)?;
        let latency = original.update.latency_ms;
        if !latency.is_finite() || latency < 0.0 {
            bail!("original tariff latency is invalid");
        }
        total += latency;
    }
    let mean = total / window.observations.len() as f64;
    if !mean.is_finite() {
        bail!("original tariff latency overflow");
    }
    Ok(mean)
}

fn bound(interval: Interval) -> Bound {
    Bound {
        quantity: QUANTITY.into(),
        snapshot_sha256: provider_tariff::snapshot_sha256(),
        interval,
    }
}

pub fn select(conn: &mut Connection, guard: &PlanGuard, request: Request<'_>) -> Result<Schedule> {
    let Request {
        reference,
        runtime,
        max_workers,
        remaining_batches,
        window_size,
        policy,
        tariff,
    } = request;
    policy.validate()?;
    if tariff.quantity != QUANTITY
        || tariff.snapshot_sha256 != provider_tariff::snapshot_sha256()
        || policy.max_expected_cost_usd.is_some()
        || remaining_batches == 0
        || window_size == 0
        || max_workers == 0
        || max_workers > crate::prepared_batch::MAX_PREPARED_BATCH_WORKERS
        || reference.max_workers != 1
        || reference.execution_contract.as_deref() != Some(PREPARED_PROVIDER_BATCH_VERSION)
        || reference.comparison_version.as_deref() != Some(EXACT_MAP_COMPARISON_VERSION)
        || reference.branches.iter().any(|b| {
            b.position.call != 0
                || b.plan.requested_model_id != b.plan.target_model_id
                || !b.plan.rewrites.is_empty()
                || b.plan.cache_policy.stable_name != "provider-call"
        })
    {
        bail!("tariff admission quantity, graph, snapshot or policy is incompatible");
    }
    crate::provider_interval_profiles::ensure_schema(conn)?;
    crate::provider_execution::ensure_schema(conn)?;
    crate::provider_quality::ensure_schema(conn)?;
    let tx = conn.transaction()?;
    let key = reference.profile_key().map_err(anyhow::Error::msg)?;
    let acquisition = provider_tariff::summarize_acquisition(&tx, &key.call_site_version)?;
    let mut schedule = Schedule {
        max_workers: 1,
        selected_reference: true,
        reason: "acquisition_costs_incomplete".into(),
        selected_key: key.clone(),
        economic_quantity: QUANTITY.into(),
        snapshot_sha256: tariff.snapshot_sha256.clone(),
        remaining_batches,
        acquisition_accounting: acquisition.clone(),
        alternatives: Vec::new(),
    };
    let Some(acquisition_upper) = acquisition.upper_nanodollars else {
        tx.commit()?;
        return Ok(schedule);
    };
    let costs = provider_interval_profiles::window(&tx, &key, runtime, window_size)?;
    let Some(reference_window) = costs else {
        schedule.reason = "reference_evidence_missing".into();
        tx.commit()?;
        return Ok(schedule);
    };
    let Some(reference_interval) = reference_window.mean_interval else {
        schedule.reason = "reference_costs_incomplete".into();
        tx.commit()?;
        return Ok(schedule);
    };
    if reference_window
        .observations
        .iter()
        .any(|o| o.original_execution_id.is_none())
    {
        schedule.reason = "reference_execution_custody_missing".into();
        tx.commit()?;
        return Ok(schedule);
    }
    let reference_latency = mean_latency(&tx, &reference_window)?;
    let economic = crate::provider_economics::summarize_cohort(&tx, &key.call_site_version)?;
    if !economic.acquisition_latency_ms.is_finite() || economic.acquisition_latency_ms < 0.0 {
        bail!("tariff acquisition latency is invalid");
    }
    let acquisition_bound = bound(Interval {
        lower_nanodollars: acquisition.lower_nanodollars,
        upper_nanodollars: acquisition_upper,
    });
    let mut selected: Option<(u32, PlanProfileKey, f64, i128)> = None;
    let mut ordinary_readmissions = Vec::new();
    for workers in 2..=max_workers.min(reference.item_count) {
        let mut spec = reference.clone();
        spec.max_workers = workers;
        let candidate_key = spec.profile_key().map_err(anyhow::Error::msg)?;
        guard.sync_original_runtime(&tx, &candidate_key, runtime, policy.now_us)?;
        let ordinary_boundary = match guard.decision(&candidate_key, policy.now_us) {
            PlanGuardDecision::Allow => 0,
            PlanGuardDecision::Disabled(disabled)
            | PlanGuardDecision::ColdReadmissionRequired(disabled) => disabled.reenable_at_us,
        };
        let quality = provider_quality::window_after(
            &tx,
            &candidate_key,
            runtime,
            window_size,
            policy.now_us,
            ordinary_boundary,
        )?;
        let costs = provider_interval_profiles::window(&tx, &candidate_key, runtime, window_size)?;
        let mut alternative = Alternative {
            key: candidate_key.clone(),
            max_workers: workers,
            rejection: Some("original_evidence_missing".into()),
            estimate: None,
        };
        if let (Some(quality), Some(costs)) = (quality, costs) {
            if costs
                .observations
                .iter()
                .any(|o| o.original_execution_id.is_none())
            {
                alternative.rejection = Some("candidate_execution_custody_missing".into());
                schedule.alternatives.push(alternative);
                continue;
            }
            if let Some(interval) = costs.mean_interval {
                let latency = mean_latency(&tx, &costs)?
                    + economic.acquisition_latency_ms / remaining_batches as f64;
                let savings = provider_tariff::savings_lower_bound(
                    &bound(reference_interval),
                    &bound(interval),
                    &acquisition_bound,
                    remaining_batches,
                )?;
                let expected_upper = interval
                    .upper_nanodollars
                    .checked_add(acquisition_upper.div_ceil(remaining_batches))
                    .ok_or_else(|| anyhow::anyhow!("tariff expected cost overflow"))?;
                let estimate = Estimate {
                    reference: bound(reference_interval),
                    candidate: bound(interval),
                    acquisition: acquisition_bound.clone(),
                    expected_upper_nanodollars: expected_upper,
                    net_savings_lower_nanodollars: savings,
                    expected_latency_ms: latency,
                    net_latency_savings_ms: reference_latency - latency,
                    quality,
                };
                let rejection = reject(guard, &candidate_key, &estimate, policy, tariff);
                alternative.rejection = rejection;
                if alternative.rejection.is_none() {
                    if matches!(
                        guard.decision(&candidate_key, policy.now_us),
                        PlanGuardDecision::ColdReadmissionRequired(_)
                    ) {
                        ordinary_readmissions.push(estimate.quality.clone());
                    }
                    if estimate.quality.disabled.is_some() {
                        provider_quality::mark_readmitted(&tx, &estimate.quality, policy.now_us)?;
                    }
                    let better = selected.as_ref().is_none_or(
                        |(old_workers, _, old_latency, old_savings)| match policy.objective {
                            SelectionObjective::Cost => {
                                savings > *old_savings
                                    || (savings == *old_savings
                                        && (latency < *old_latency
                                            || (latency == *old_latency && workers < *old_workers)))
                            }
                            SelectionObjective::Latency => {
                                latency < *old_latency
                                    || (latency == *old_latency
                                        && (savings > *old_savings
                                            || (savings == *old_savings && workers < *old_workers)))
                            }
                        },
                    );
                    if better {
                        selected = Some((workers, candidate_key, latency, savings));
                    }
                }
                alternative.estimate = Some(estimate);
            } else {
                alternative.rejection = Some("candidate_costs_incomplete".into());
            }
        }
        schedule.alternatives.push(alternative);
    }
    if let Some((workers, key, _, _)) = selected {
        schedule.max_workers = workers;
        schedule.selected_reference = false;
        schedule.selected_key = key;
        schedule.reason = "best_admissible_alternative".into();
    } else {
        schedule.reason = "no_admissible_alternative".into();
    }
    tx.commit()?;
    for quality in ordinary_readmissions {
        guard.mark_readmitted_with_original_quality(conn, &quality, policy)?;
    }
    guard.flush_dirty(conn)?;
    Ok(schedule)
}

fn reject(
    guard: &PlanGuard,
    key: &PlanProfileKey,
    estimate: &Estimate,
    policy: &SelectionPolicy,
    tariff: &Policy,
) -> Option<String> {
    let quality = &estimate.quality;
    let ordinary = match guard.admission(key, true, 0.0, policy.now_us) {
        Ok(admission) => admission,
        Err(_) => return Some("ordinary_guard_unavailable".into()),
    };
    if ordinary.disabled
        && !matches!(
            guard.decision(key, policy.now_us),
            PlanGuardDecision::ColdReadmissionRequired(_)
        )
    {
        return Some("ordinary_guard_disabled".into());
    }
    if guard.validation_rejections_active(key, policy.now_us) {
        return Some("validation_rejections_active".into());
    }
    if quality
        .disabled
        .as_ref()
        .is_some_and(|d| policy.now_us < d.reenable_at_us)
    {
        return Some("original_quality_disabled".into());
    }
    if quality
        .divergence_exposure
        .max(ordinary.divergence_exposure)
        >= policy.divergence_exposure_budget
    {
        return Some("exposure_budget_exceeded".into());
    }
    if quality.pairs.len() < policy.min_paired_observations as usize {
        return Some("insufficient_original_pairs".into());
    }
    if (quality.disabled.is_some() || ordinary.disabled)
        && quality.pairs.len() < SelectionPolicy::default().min_paired_observations as usize
    {
        return Some("insufficient_readmission_pairs".into());
    }
    if quality.divergence_upper_p95 != Some(0.0) {
        return Some("divergence_exceeded".into());
    }
    let Some(last) = quality
        .last_execution_at_us
        .zip(quality.last_paired_at_us)
        .map(|(execution, pair)| execution.min(pair))
    else {
        return Some("quality_missing".into());
    };
    if last > policy.now_us {
        return Some("observation_from_future".into());
    }
    if policy.now_us - last > policy.max_profile_age_us {
        return Some("stale_quality".into());
    }
    if !estimate.expected_latency_ms.is_finite() || estimate.expected_latency_ms < 0.0 {
        return Some("invalid_latency".into());
    }
    if tariff
        .max_expected_nanodollars
        .is_some_and(|limit| estimate.expected_upper_nanodollars > limit)
    {
        return Some("tariff_cost_limit_exceeded".into());
    }
    if policy
        .max_expected_latency_ms
        .is_some_and(|limit| estimate.expected_latency_ms > limit)
    {
        return Some("latency_limit_exceeded".into());
    }
    if match policy.objective {
        SelectionObjective::Cost => estimate.net_savings_lower_nanodollars <= 0,
        SelectionObjective::Latency => estimate.net_latency_savings_ms <= 0.0,
    } {
        return Some("non_positive_net_benefit".into());
    }
    None
}
