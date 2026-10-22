//! Admission for fully materialized, independent provider-request batches.
//!
//! Arbitrary map callbacks never enter this selector. The SDK driver freezes
//! every request before dispatch and owns only stateless completion invocations.
//! Quality, freshness, exposure and benefit use the same selector as single-call
//! plans. All completed acquisition spend in the cohort is amortized explicitly.

use anyhow::{bail, Result};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};

use crate::execution_plan::{select_candidate, CandidatePlan, PlanEstimate, SelectionPolicy};
use crate::multi_call::{
    MultiCallPlanSpec, EXACT_MAP_COMPARISON_VERSION, PREPARED_PROVIDER_BATCH_VERSION,
};
use crate::plan_guard::{PlanGuard, PlanGuardDecision};
use crate::plan_profile::{PlanProfiles, PlanRuntimeVersion};

pub const MAX_PREPARED_BATCH_WORKERS: u32 = 32;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreparedBatchOptions {
    pub provider_receipt_batch_id: Option<String>,
    pub max_workers: u32,
    pub remaining_batches: u64,
    pub objective: crate::execution_plan::SelectionObjective,
    pub mode: String,
    pub scope_eligible: bool,
    pub max_expected_cost_usd: Option<f64>,
    pub max_expected_latency_ms: Option<f64>,
    #[serde(default)]
    pub tariff_policy: Option<crate::provider_tariff_admission::Policy>,
}

#[derive(Debug, Serialize)]
pub struct PreparedSchedule {
    pub max_workers: u32,
    pub selected_reference: bool,
    pub reason: crate::execution_plan::SelectionReason,
    pub rejections: Vec<crate::execution_plan::CandidateRejection>,
    pub selected_key: crate::plan_profile::PlanProfileKey,
    pub completed_acquisition_cost_usd: f64,
    pub completed_acquisition_latency_ms: f64,
    pub remaining_batches: u64,
    pub acquisition_accounting: crate::provider_economics::Economics,
}

pub struct PreparedScheduleRequest<'a> {
    pub reference: &'a MultiCallPlanSpec,
    pub runtime: &'a PlanRuntimeVersion,
    pub max_workers: u32,
    pub remaining_batches: u64,
    pub policy: &'a SelectionPolicy,
}

pub fn select_prepared_schedule(
    conn: &mut Connection,
    profiles: &PlanProfiles,
    guard: &PlanGuard,
    request: PreparedScheduleRequest<'_>,
) -> Result<PreparedSchedule> {
    let PreparedScheduleRequest {
        reference,
        runtime,
        max_workers,
        remaining_batches,
        policy,
    } = request;
    if reference.max_workers != 1
        || reference.execution_contract.as_deref() != Some(PREPARED_PROVIDER_BATCH_VERSION)
        || reference.comparison_version.as_deref() != Some(EXACT_MAP_COMPARISON_VERSION)
        || reference.branches.iter().any(|branch| {
            branch.position.call != 0
                || branch.plan.target_model_id != branch.plan.requested_model_id
                || !branch.plan.rewrites.is_empty()
                || branch.plan.cache_policy.stable_name != "provider-call"
        })
    {
        bail!("scheduling requires a verified independent original-request batch");
    }
    if remaining_batches == 0 || max_workers == 0 || max_workers > MAX_PREPARED_BATCH_WORKERS {
        bail!("prepared batch worker limit and remaining deployment volume must be valid");
    }
    crate::multi_call_comparison::replay_pairs(conn, profiles, guard)?;
    let key = reference.profile_key().map_err(anyhow::Error::msg)?;
    let accounting = crate::provider_economics::summarize_cohort(conn, &key.call_site_version)?;
    let cost = accounting.known_cost_usd;
    let latency = accounting.acquisition_latency_ms;
    if !accounting.cost_complete {
        return Ok(PreparedSchedule {
            max_workers: 1,
            selected_reference: true,
            reason: crate::execution_plan::SelectionReason::AcquisitionCostsIncomplete,
            rejections: Vec::new(),
            selected_key: key,
            completed_acquisition_cost_usd: cost,
            completed_acquisition_latency_ms: latency,
            remaining_batches,
            acquisition_accounting: accounting,
        });
    }
    if !cost.is_finite() || cost < 0.0 || !latency.is_finite() || latency < 0.0 {
        bail!("prepared batch acquisition economics are invalid");
    }
    let reference_profile = profiles.get(&key, runtime);
    guard.sync_original_runtime(conn, &key, runtime, policy.now_us)?;
    let reference_candidate = CandidatePlan::new(
        reference.clone(),
        None,
        guard.admission(&key, true, 0.0, policy.now_us)?,
    )?;
    let mut alternatives = Vec::new();
    for workers in 2..=max_workers.min(reference.item_count) {
        let mut spec = reference.clone();
        spec.max_workers = workers;
        let alternative_key = spec.profile_key().map_err(anyhow::Error::msg)?;
        guard.sync_original_runtime(conn, &alternative_key, runtime, policy.now_us)?;
        let original_boundary = match guard.decision(&alternative_key, policy.now_us) {
            PlanGuardDecision::Allow => 0,
            PlanGuardDecision::Disabled(disabled)
            | PlanGuardDecision::ColdReadmissionRequired(disabled) => disabled.reenable_at_us,
        };
        let original_quality = crate::provider_quality::window_after(
            conn,
            &alternative_key,
            runtime,
            profiles.retained_window_size(),
            policy.now_us,
            original_boundary,
        )?;
        let original_registered: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM provider_original_execution_state
             WHERE call_site_version=?1 AND execution_plan_id=?2)",
            rusqlite::params![
                alternative_key.call_site_version.as_str(),
                alternative_key.execution_plan_id.as_str()
            ],
            |row| row.get(0),
        )?;
        // Original quality has its own persisted cold-admission boundary.
        // Resetting scalar profiles here would discard already collected
        // post-cooldown cost samples merely because selection ran later.
        if original_quality.is_none() && !original_registered {
            if let PlanGuardDecision::ColdReadmissionRequired(disabled) =
                guard.decision(&alternative_key, policy.now_us)
            {
                profiles.begin_readmission(&alternative_key, runtime, disabled.reenable_at_us);
            }
        }
        let profile = profiles.get(&alternative_key, runtime);
        let estimate =
            profile
                .as_ref()
                .zip(reference_profile.as_ref())
                .and_then(|(candidate, reference)| {
                    if !candidate.measured_cost_complete() || !reference.measured_cost_complete() {
                        return None;
                    }
                    let (divergence_upper_p95, last_observed_at_us, paired_observations) =
                        if let Some(quality) = &original_quality {
                            (
                                quality.divergence_upper_p95?,
                                quality
                                    .last_paired_at_us?
                                    .min(quality.last_execution_at_us?),
                                quality.pairs.len() as u32,
                            )
                        } else if !original_registered {
                            (
                                candidate.divergence_upper_p95?,
                                candidate.last_paired_at_us?,
                                candidate.paired_observations,
                            )
                        } else {
                            // Original custody exists but its runtime/window
                            // has retired. Legacy quality cannot rewarm it.
                            return None;
                        };
                    Some(PlanEstimate {
                        paired_observations,
                        expected_cost_usd: candidate.cost_usd.mean
                            + cost / remaining_batches as f64,
                        expected_latency_ms: candidate.latency_ms.mean
                            + latency / remaining_batches as f64,
                        divergence_upper_p95,
                        expected_net_cost_savings_usd: reference.cost_usd.mean
                            - candidate.cost_usd.mean
                            - cost / remaining_batches as f64,
                        expected_net_latency_savings_ms: reference.latency_ms.mean
                            - candidate.latency_ms.mean
                            - latency / remaining_batches as f64,
                        last_observed_at_us,
                    })
                });
        let mut admission = guard.admission(&alternative_key, true, 0.0, policy.now_us)?;
        if let Some(quality) = &original_quality {
            admission.divergence_exposure = admission
                .divergence_exposure
                .max(quality.divergence_exposure);
            if quality
                .disabled
                .as_ref()
                .is_some_and(|d| policy.now_us < d.reenable_at_us)
            {
                admission.disabled = true;
            }
        }
        let original_cold = original_quality
            .as_ref()
            .is_some_and(|q| q.disabled.is_some());
        let mut original_readmitted = false;
        if original_cold
            && original_quality.as_ref().is_some_and(|q| {
                q.pairs.len()
                    >= crate::execution_plan::SelectionPolicy::default().min_paired_observations
                        as usize
            })
        {
            let cold = CandidatePlan::new(
                spec.clone(),
                estimate.clone(),
                crate::execution_plan::PlanAdmission {
                    disabled: false,
                    ..admission.clone()
                },
            )?;
            if policy.validate().is_ok()
                && crate::execution_plan::rejection_reason(&cold, policy).is_none()
            {
                crate::provider_quality::mark_readmitted(
                    conn,
                    original_quality.as_ref().unwrap(),
                    policy.now_us,
                )?;
                original_readmitted = true;
            }
        }
        if original_cold && !original_readmitted {
            admission.disabled = true;
        }
        if matches!(
            guard.decision(&alternative_key, policy.now_us),
            PlanGuardDecision::ColdReadmissionRequired(_)
        ) {
            // Fresh comparison evidence must pass every ordinary constraint
            // before the persistent guard can clear its expired cooldown.
            let cold = CandidatePlan::new(
                spec.clone(),
                estimate.clone(),
                crate::execution_plan::PlanAdmission {
                    disabled: false,
                    ..admission.clone()
                },
            )?;
            if policy.validate().is_ok()
                && crate::execution_plan::rejection_reason(&cold, policy).is_none()
            {
                if let Some(quality) = &original_quality {
                    guard.mark_readmitted_with_original_quality(conn, quality, policy)?;
                } else {
                    profiles.flush_dirty(conn)?;
                    guard.mark_readmitted_with_fresh_profile(&alternative_key, policy.now_us)?;
                }
                admission = guard.admission(&alternative_key, true, 0.0, policy.now_us)?;
                if original_cold && !original_readmitted {
                    admission.disabled = true;
                }
            }
        }
        alternatives.push(CandidatePlan::new(spec, estimate, admission)?);
    }
    profiles.flush_dirty(conn)?;
    guard.flush_dirty(conn)?;
    let selection = select_candidate(&reference_candidate, &alternatives, policy);
    Ok(PreparedSchedule {
        max_workers: selection.selected.spec.max_workers,
        selected_reference: selection.selected_reference,
        reason: selection.reason,
        rejections: selection.rejections,
        selected_key: selection
            .selected
            .spec
            .profile_key()
            .map_err(anyhow::Error::msg)?,
        completed_acquisition_cost_usd: cost,
        completed_acquisition_latency_ms: latency,
        remaining_batches,
        acquisition_accounting: accounting,
    })
}
