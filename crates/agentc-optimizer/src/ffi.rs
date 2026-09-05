//! Vendor-free FFI surface.
//!
//! Pure-Rust planning/observation adapters that the PyO3
//! binding in `agentc-profiler::_native` re-exports. The adapters accept
//! JSON strings and — crucially — never panic on malformed input or
//! internal errors: every failure falls through to `{"kind":"pass_through"}`
//! so the caller always receives a valid [`crate::Plan`].
//!
//! Panic trapping lives HERE, inside [`optimize_plan`] and
//! [`optimize_profiled_plan`], so a planning failure is testable under
//! `cargo test` rather than only through the Python interpreter. The legacy
//! projected-savings baselines remain in [`optimize_plan`]; production joint
//! selection enters through [`optimize_profiled_plan`]. The PyO3 binding keeps
//! an outer `catch_unwind` as defence in depth at the actual boundary.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Instant;

use agentc_core::storage::{canonical_json, content_hash};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};

use crate::cost_model::{CostModel, CostModelUpdate};
use crate::dag::{Call, DepSource, Outcome};
use crate::diagnostics::{
    attach_planner_diagnostics, extract_planner_diagnostics, PlannerDecisionDiagnostics,
};
use crate::execution_plan::{
    rejection_reason, select_candidate, CachePolicy, CandidatePlan, CandidateRejectionReason,
    ExecutionPlanSpec, PlanAdmission, PlanEstimate,
    RewriteApplication, RewriteOrdering, ValidationPolicy,
    EXECUTION_PLAN_SCHEMA_VERSION,
};
use crate::exploration::{
    CounterfactualFeedback, CounterfactualLabel, ExplorationCandidate, ExplorationCompletion,
    ExplorationController, ExplorationLease,
};
use crate::model_catalog::{ModelCatalog, RequestRequirements, ROUTE_CONTEXT_KEY};
use crate::plan_guard::PlanGuard;
use crate::plan_profile::{
    CallSiteVersion, CallSiteVersionSpec, PlanObservationToken, PlanProfile, PlanProfileKey,
    PlanProfileUpdate, PlanProfiles, PlanRuntimeVersion,
};
use crate::planner::{Optimizer, Plan};

/// Canonical PassThrough JSON, returned whenever anything goes sideways.
pub const PASS_THROUGH_JSON: &str = "{\"kind\":\"pass_through\"}";

const OBSERVATION_CONTEXT_KEY: &str = "agentc_observation_context";
const EXPLORATION_CONTEXT_KEY: &str = "agentc_exploration_context";
const OBSERVATION_TOKEN_SCHEMA_VERSION: u16 = 2;
const EXPLORATION_TOKEN_SCHEMA_VERSION: u16 = 1;
const PROMPT_SHAPE_SCHEMA_VERSION: u16 = 1;
const PLAN_IMPLEMENTATION_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Identity and runtime metadata embedded in a returned Plan as an internal
/// JSON field. Python carries this field opaquely in ``Plan.raw_json``; it does
/// not need to understand or reconstruct any profile key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct EmbeddedObservationContext {
    call_site_id: String,
    key: PlanProfileKey,
    runtime_version: PlanRuntimeVersion,
    divergence_threshold: f64,
}

#[derive(Debug, Clone)]
struct ResolvedPlanDescriptor {
    context: EmbeddedObservationContext,
    spec: ExecutionPlanSpec,
}

/// Python-visible instructions for one reference-visible counterfactual.
/// The lease token is opaque and content-free; the candidate plan remains a
/// regular executable Plan so the existing provider adapter is the only
/// component that translates it back to provider-native arguments.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct EmbeddedExplorationContext {
    schema_version: u16,
    lease_token: String,
    candidate_plan: serde_json::Value,
}

/// Rust-issued binding between a durable lease and the exact candidate
/// profile it is allowed to update. It deliberately carries no prompt text.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct OpaqueExplorationToken {
    schema_version: u16,
    lease: ExplorationLease,
    context: EmbeddedObservationContext,
    rules: Vec<String>,
}

/// Opaque handle returned by ``optimize_observe`` and consumed by
/// ``optimize_record_divergence``. It binds delayed feedback to one exact
/// execution while retaining the legacy solo-rule attribution needed until the
/// plan-level guard replaces the compatibility guard.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct OpaqueObservationToken {
    schema_version: u16,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    plan_observation: Option<PlanObservationToken>,
    call_site_id: String,
    guard_eligible: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    divergence_threshold: Option<f64>,
    #[serde(default)]
    rules: Vec<String>,
}

/// Validated attribution returned to the PyO3 adapter after a divergence is
/// attached to its complete-plan profile.
#[derive(Debug, Clone, PartialEq)]
pub struct DivergenceAttribution {
    pub call_site_id: String,
    pub solo_rule: Option<String>,
    pub rules: Vec<String>,
    pub plan_observation: Option<PlanObservationToken>,
    pub guard_eligible: bool,
    pub divergence_threshold: Option<f64>,
    /// False when the same token/value pair was replayed idempotently.
    pub newly_recorded: bool,
}

/// Plan a call. Any deserialization failure, internal error, **or panic**
/// (e.g. a rule that panics inside `propose`) yields `PASS_THROUGH_JSON`.
///
/// The `catch_unwind` IS the fail-open guarantee — keep it. Deleting it makes
/// `tests/fail_open.rs::rule_panic_is_converted_to_pass_through` panic.
pub fn optimize_plan(opt: &Optimizer, call_json: &str) -> String {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let call: Call = match serde_json::from_str(call_json) {
            Ok(c) => c,
            Err(_) => return PASS_THROUGH_JSON.to_string(),
        };
        let plan = opt.plan(&call);
        serde_json::to_string(&plan).unwrap_or_else(|_| PASS_THROUGH_JSON.to_string())
    }))
    .unwrap_or_else(|_| PASS_THROUGH_JSON.to_string())
}

/// Enumerate and select complete model-plus-rewrite plans from exact empirical
/// profiles. This is the production joint-planning seam; [`optimize_plan`]
/// remains available to the evaluation harness for projected-savings baselines.
///
/// Every error, invalid policy, missing reference profile, or elapsed overhead
/// budget returns the immutable request with its own observation context.
#[allow(clippy::too_many_arguments)]
pub fn optimize_profiled_plan(
    opt: &Optimizer,
    catalog: Option<&ModelCatalog>,
    plan_profiles: &PlanProfiles,
    plan_guard: &PlanGuard,
    call_json: &str,
    now_us: i64,
) -> String {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let Some(catalog) = catalog else {
            return PASS_THROUGH_JSON.to_string();
        };
        if !opt.config().compose {
            let plan_json = optimize_plan(opt, call_json);
            let threshold = serde_json::from_str::<Plan>(&plan_json)
                .ok()
                .map(|plan| opt.divergence_threshold_for_plan(&plan))
                .unwrap_or(0.05);
            return guard_and_attach_observation_context(
                Some(catalog),
                plan_guard,
                call_json,
                &plan_json,
                threshold,
                now_us,
            );
        }

        let started = Instant::now();
        let max_overhead_us = (opt.config().max_overhead_ms * 1000.0) as u128;
        let Ok(call) = serde_json::from_str::<Call>(call_json) else {
            return PASS_THROUGH_JSON.to_string();
        };
        let reference_plan = Plan::PassThrough;
        let reference_threshold = opt.divergence_threshold_for_plan(&reference_plan);
        let Some(reference_descriptor) =
            describe_plan(catalog, &call, &reference_plan, reference_threshold)
        else {
            return PASS_THROUGH_JSON.to_string();
        };
        let reference = match CandidatePlan::new(
            reference_descriptor.spec.clone(),
            None,
            PlanAdmission {
                request_compatible: true,
                disabled: false,
                divergence_threshold: reference_threshold,
                divergence_exposure: 0.0,
            },
        ) {
            Ok(reference) => reference,
            Err(_) => return PASS_THROUGH_JSON.to_string(),
        };
        let fallback = || {
            encode_observation_context(
                serde_json::to_string(&reference_plan)
                    .unwrap_or_else(|_| PASS_THROUGH_JSON.to_string()),
                &reference_descriptor.context,
            )
        };
        if started.elapsed().as_micros() > max_overhead_us {
            let alternatives = Vec::new();
            let policy = opt.config().selection_policy(now_us);
            let selection = select_candidate(&reference, &alternatives, &policy);
            let mut diagnostics = PlannerDecisionDiagnostics::from_selection(
                opt.config(),
                &reference_descriptor.context.key.call_site_version,
                &reference,
                &alternatives,
                &selection,
            );
            diagnostics.force_fallback("planning_overhead_exceeded");
            return attach_planner_diagnostics(fallback(), &diagnostics);
        }

        let reference_profile = plan_profiles.get(
            &reference_descriptor.context.key,
            &reference_descriptor.context.runtime_version,
        );
        let shadow_rate = if opt.config().shadow_rate.is_finite() {
            f64::from(opt.config().shadow_rate.clamp(0.0, 1.0))
        } else {
            0.02
        };
        let mut candidates = Vec::new();
        let mut executable = Vec::new();
        let mut seen = HashSet::new();
        seen.insert(reference.id.clone());

        for plan in opt.candidate_plans(&call, catalog) {
            if matches!(plan, Plan::PassThrough | Plan::Parallel { .. }) {
                continue;
            }
            let threshold = opt.divergence_threshold_for_plan(&plan);
            let Some(descriptor) = describe_plan(catalog, &call, &plan, threshold) else {
                continue;
            };
            if !seen.insert(descriptor.context.key.execution_plan_id.clone()) {
                continue;
            }
            let admission = match plan_guard.admission(
                &descriptor.context.key,
                true,
                threshold,
                now_us,
            ) {
                Ok(admission) => admission,
                Err(_) => continue,
            };
            let estimate = plan_profiles
                .get(&descriptor.context.key, &descriptor.context.runtime_version)
                .and_then(|profile| {
                    reference_profile.as_ref().and_then(|reference_profile| {
                        estimate_plan(
                            &profile,
                            reference_profile,
                            shadow_rate,
                            f64::from(opt.config().max_overhead_ms.max(0.0)),
                        )
                    })
                });
            let candidate = match CandidatePlan::new(descriptor.spec, estimate, admission) {
                Ok(candidate) => candidate,
                Err(_) => continue,
            };
            executable.push((
                candidate.id.clone(),
                plan,
                descriptor.context,
            ));
            candidates.push(candidate);
            if started.elapsed().as_micros() > max_overhead_us {
                let policy = opt.config().selection_policy(now_us);
                let selection = select_candidate(&reference, &candidates, &policy);
                let mut diagnostics = PlannerDecisionDiagnostics::from_selection(
                    opt.config(),
                    &reference_descriptor.context.key.call_site_version,
                    &reference,
                    &candidates,
                    &selection,
                );
                diagnostics.force_fallback("planning_overhead_exceeded");
                return attach_planner_diagnostics(fallback(), &diagnostics);
            }
        }

        let policy = opt.config().selection_policy(now_us);
        let selection = select_candidate(&reference, &candidates, &policy);
        let mut diagnostics = PlannerDecisionDiagnostics::from_selection(
            opt.config(),
            &reference_descriptor.context.key.call_site_version,
            &reference,
            &candidates,
            &selection,
        );
        if selection.selected_reference || started.elapsed().as_micros() > max_overhead_us {
            if started.elapsed().as_micros() > max_overhead_us {
                diagnostics.force_fallback("planning_overhead_exceeded");
            }
            return attach_planner_diagnostics(fallback(), &diagnostics);
        }
        let selected_id = selection.selected.id.clone();
        let Some((_id, selected_plan, selected_context)) = executable
            .into_iter()
            .find(|(id, _, _)| *id == selected_id)
        else {
            diagnostics.force_fallback("selected_plan_not_executable");
            return attach_planner_diagnostics(fallback(), &diagnostics);
        };
        // Re-check immediately before exposure. A concurrent shadow result may
        // have disabled the plan after its admission snapshot was built.
        if plan_guard
            .decision(&selected_context.key, now_us)
            .blocks_user_visible()
        {
            diagnostics.force_fallback("guard_changed_before_dispatch");
            return attach_planner_diagnostics(fallback(), &diagnostics);
        }
        let plan_json =
            serde_json::to_string(&selected_plan).unwrap_or_else(|_| PASS_THROUGH_JSON.to_string());
        attach_planner_diagnostics(
            encode_observation_context(plan_json, &selected_context),
            &diagnostics,
        )
    }))
    .unwrap_or_else(|_| PASS_THROUGH_JSON.to_string())
}

/// Attach at most one durably reserved cold/refresh-plan counterfactual.
///
/// A successful lease deliberately selects the immutable reference primary,
/// even when an incumbent rewrite was admissible. This keeps another cold
/// plan from being stranded behind that incumbent and compares both outcomes
/// against the same original request. There are still at most two calls:
/// reference plus candidate, never incumbent plus reference plus candidate.
/// Without a lease, the admitted primary is returned unchanged. Python sees the
/// candidate only through `agentc_exploration_context`, executes it through
/// the same provider adapter after the reference response has been observed,
/// and reports completion with the opaque token. Tool-bearing and streaming
/// requests abstain until their divergence comparators can validate more than
/// assistant text. Every error returns `primary_plan_json` unchanged.
#[allow(clippy::too_many_arguments)]
pub fn reserve_profiled_exploration(
    opt: &Optimizer,
    catalog: Option<&ModelCatalog>,
    plan_profiles: &PlanProfiles,
    plan_guard: &PlanGuard,
    controller: &ExplorationController,
    conn: &mut Connection,
    call_json: &str,
    primary_plan_json: &str,
    now_us: i64,
) -> String {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let Ok(primary_plan) = serde_json::from_str::<Plan>(primary_plan_json) else {
            return primary_plan_json.to_string();
        };
        if !opt.config().enabled || !opt.config().exploration_enabled || !opt.config().compose
            || matches!(primary_plan, Plan::Cached { .. } | Plan::Parallel { .. })
        {
            return primary_plan_json.to_string();
        }
        let Some(catalog) = catalog else {
            return primary_plan_json.to_string();
        };
        let Ok(call) = serde_json::from_str::<Call>(call_json) else {
            return primary_plan_json.to_string();
        };
        let Some(requirements) = RequestRequirements::from_call(&call) else {
            return primary_plan_json.to_string();
        };
        // Text-only completion comparison is the only production comparator
        // currently implemented. Discarded tool calls cannot be scored by it.
        if requirements.streaming || requirements.tool_calling || !call.tools.is_empty() {
            return primary_plan_json.to_string();
        }
        let Ok(Some(primary_context)) = embedded_observation_context(primary_plan_json) else {
            return primary_plan_json.to_string();
        };
        let expected_primary_context = build_observation_context(
            catalog, &call, &primary_plan, opt.divergence_threshold_for_plan(&primary_plan),
        );
        if expected_primary_context.as_ref() != Some(&primary_context) {
            return primary_plan_json.to_string();
        }
        // An incumbent's context belongs to its rewritten target/cap. It must
        // never become the reference identity for candidate evidence.
        let Some(reference_context) = build_observation_context(
            catalog, &call, &Plan::PassThrough,
            opt.divergence_threshold_for_plan(&Plan::PassThrough),
        ) else {
            return primary_plan_json.to_string();
        };

        let selection_policy = opt.config().selection_policy(now_us);
        if selection_policy.validate().is_err() {
            return primary_plan_json.to_string();
        }
        let reference_profile = plan_profiles.get(
            &reference_context.key, &reference_context.runtime_version);
        let shadow_rate = if opt.config().shadow_rate.is_finite() {
            f64::from(opt.config().shadow_rate.clamp(0.0, 1.0))
        } else {
            0.02
        };

        let mut controller_candidates = Vec::new();
        let mut executable = Vec::new();
        let mut seen = HashSet::new();
        for plan in opt.candidate_plans(&call, catalog) {
            if matches!(plan, Plan::PassThrough | Plan::Cached { .. } | Plan::Parallel { .. }) {
                continue;
            }
            let threshold = opt.divergence_threshold_for_plan(&plan);
            let Some(descriptor) = describe_plan(catalog, &call, &plan, threshold) else {
                continue;
            };
            if !seen.insert(descriptor.context.key.execution_plan_id.clone()) {
                continue;
            }
            let Ok(admission) = plan_guard.admission(
                &descriptor.context.key,
                true,
                threshold,
                now_us,
            ) else {
                continue;
            };
            let profile = plan_profiles
                .get(&descriptor.context.key, &descriptor.context.runtime_version);
            let paired_observations = profile.as_ref()
                .map(|profile| profile.paired_observations)
                .unwrap_or(0);
            let estimate = profile.as_ref().and_then(|profile| {
                reference_profile.as_ref().and_then(|reference| {
                    estimate_plan(profile, reference, shadow_rate,
                        f64::from(opt.config().max_overhead_ms.max(0.0)))
                })
            });
            let refresh_required = CandidatePlan::new(
                descriptor.spec.clone(), estimate, admission.clone())
                .map(|candidate| matches!(rejection_reason(&candidate, &selection_policy),
                    Some(CandidateRejectionReason::StaleProfile { .. }
                        | CandidateRejectionReason::DivergenceExceeded { .. })))
                .unwrap_or(false);
            controller_candidates.push(ExplorationCandidate {
                key: descriptor.context.key.clone(),
                paired_observations,
                refresh_required,
                request_compatible: admission.request_compatible,
                forbidden: false,
                disabled: admission.disabled,
                divergence_threshold: admission.divergence_threshold,
                divergence_exposure: admission.divergence_exposure,
            });
            executable.push((descriptor.context.key.clone(), plan, descriptor.context));
        }

        let decision = controller.decide_and_reserve(
            conn,
            &reference_context.key.call_site_version,
            &reference_context.key.execution_plan_id,
            &controller_candidates,
            now_us,
        );
        let Some(lease) = decision.counterfactual else {
            return primary_plan_json.to_string();
        };
        let Some((_key, candidate_plan, candidate_context)) = executable
            .into_iter()
            .find(|(key, _, _)| *key == lease.key)
        else {
            let _ = controller.cancel_unstarted(conn, &lease);
            return primary_plan_json.to_string();
        };

        let candidate_plan_json = serde_json::to_string(&candidate_plan)
            .unwrap_or_else(|_| PASS_THROUGH_JSON.to_string());
        let candidate_plan_json = encode_observation_context(
            candidate_plan_json,
            &candidate_context,
        );
        let Ok(candidate_plan_value) = serde_json::from_str(&candidate_plan_json) else {
            let _ = controller.cancel_unstarted(conn, &lease);
            return primary_plan_json.to_string();
        };
        let token = OpaqueExplorationToken {
            schema_version: EXPLORATION_TOKEN_SCHEMA_VERSION,
            lease: lease.clone(),
            context: candidate_context,
            rules: plan_rules(&candidate_plan),
        };
        let Ok(lease_token) = serde_json::to_string(&token) else {
            let _ = controller.cancel_unstarted(conn, &lease);
            return primary_plan_json.to_string();
        };
        let context = EmbeddedExplorationContext {
            schema_version: EXPLORATION_TOKEN_SCHEMA_VERSION,
            lease_token,
            candidate_plan: candidate_plan_value,
        };
        let reference_plan_json = if matches!(primary_plan, Plan::PassThrough) {
            primary_plan_json.to_string()
        } else {
            let reference_json = encode_observation_context(
                PASS_THROUGH_JSON.to_string(), &reference_context,
            );
            if let Some(mut diagnostics) = extract_planner_diagnostics(primary_plan_json) {
                diagnostics.force_fallback("bounded_exploration_reference");
                diagnostics.selection_reason = "bounded_exploration_reference".to_string();
                attach_planner_diagnostics(reference_json, &diagnostics)
            } else {
                reference_json
            }
        };
        encode_exploration_context(&reference_plan_json, &context).unwrap_or_else(|| {
            let _ = controller.cancel_unstarted(conn, &lease);
            primary_plan_json.to_string()
        })
    }))
    .unwrap_or_else(|_| primary_plan_json.to_string())
}

/// Record one leased candidate outcome and its reference divergence.
///
/// The durable controller transition happens first. A crash can therefore
/// lose evidence but cannot erase or replay billed exploration spend. An
/// idempotent completion replay returns `Ok(None)` and never duplicates a
/// profile observation.
pub fn complete_profiled_exploration(
    plan_profiles: &PlanProfiles,
    controller: &ExplorationController,
    conn: &mut Connection,
    lease_token: &str,
    outcome_json: &str,
    divergence: f64,
    completed_at_us: i64,
) -> Result<Option<DivergenceAttribution>, String> {
    let token = decode_exploration_token(lease_token)?;
    let outcome: Outcome = serde_json::from_str(outcome_json).map_err(|error| error.to_string())?;
    if outcome.dispatch_fallback {
        return Err("counterfactual outcome cannot contain a reference fallback".to_string());
    }
    if outcome
        .call_site_id
        .as_deref()
        .is_some_and(|site| site != token.context.call_site_id)
    {
        return Err("counterfactual outcome is bound to a different call site".to_string());
    }
    if token.context.key != token.lease.key {
        return Err("exploration token plan binding does not match its lease".to_string());
    }
    let completion = controller
        .complete(
            conn,
            &token.lease,
            &CounterfactualFeedback {
                divergence,
                cost_usd: outcome.cost_usd,
                latency_ms: outcome.latency_ms,
                label: CounterfactualLabel::ObservationOnly,
            },
            completed_at_us,
        )
        .map_err(|error| error.to_string())?;
    if completion == ExplorationCompletion::AlreadyRecorded {
        return Ok(None);
    }

    let runtime_version = observed_runtime_version(&token.context.runtime_version, &outcome);
    let observation = plan_profiles
        .observe(PlanProfileUpdate {
            key: token.context.key,
            runtime_version,
            input_tokens: outcome.input_tokens,
            output_tokens: outcome.output_tokens,
            latency_ms: outcome.latency_ms,
            cost_usd: outcome.cost_usd,
            output_is_structured: outcome.output_is_structured,
            output_is_short: outcome.output_is_short,
            divergence: Some(divergence),
            dispatch_fallback: false,
            observed_at_us: Some(completed_at_us),
        })
        .map_err(|error| error.to_string())?;
    let solo_rule = match token.rules.as_slice() {
        [rule] => Some(rule.clone()),
        _ => None,
    };
    Ok(Some(DivergenceAttribution {
        call_site_id: token.context.call_site_id,
        solo_rule,
        rules: token.rules,
        plan_observation: Some(observation),
        guard_eligible: true,
        divergence_threshold: Some(token.context.divergence_threshold),
        newly_recorded: true,
    }))
}

/// Mark a reserved counterfactual failed. The attempt remains in the rolling
/// cap because provider work may have begun before the adapter observed the
/// failure.
pub fn fail_profiled_exploration(
    controller: &ExplorationController,
    conn: &mut Connection,
    lease_token: &str,
    completed_at_us: i64,
) -> Result<ExplorationCompletion, String> {
    let token = decode_exploration_token(lease_token)?;
    if token.context.key != token.lease.key {
        return Err("exploration token plan binding does not match its lease".to_string());
    }
    controller
        .fail(conn, &token.lease, completed_at_us)
        .map_err(|error| error.to_string())
}

/// Cancel the lease embedded in an envelope that never crossed the provider
/// dispatch boundary. The caller strips the candidate, so this reservation
/// must not consume the rolling exploration call cap.
pub fn cancel_embedded_exploration(
    controller: &ExplorationController,
    conn: &mut Connection,
    explored_plan_json: &str,
) -> Result<(), String> {
    let value: serde_json::Value =
        serde_json::from_str(explored_plan_json).map_err(|error| error.to_string())?;
    let context: EmbeddedExplorationContext = serde_json::from_value(
        value
            .get(EXPLORATION_CONTEXT_KEY)
            .cloned()
            .ok_or_else(|| "plan has no exploration context".to_string())?,
    )
    .map_err(|error| error.to_string())?;
    let token = decode_exploration_token(&context.lease_token)?;
    if token.context.key != token.lease.key {
        return Err("exploration token plan binding does not match its lease".to_string());
    }
    controller
        .cancel_unstarted(conn, &token.lease)
        .map_err(|error| error.to_string())
}

fn decode_exploration_token(value: &str) -> Result<OpaqueExplorationToken, String> {
    let token: OpaqueExplorationToken =
        serde_json::from_str(value).map_err(|error| error.to_string())?;
    if token.schema_version != EXPLORATION_TOKEN_SCHEMA_VERSION {
        return Err("unsupported exploration token schema version".to_string());
    }
    Ok(token)
}

fn encode_exploration_context(
    primary_plan_json: &str,
    context: &EmbeddedExplorationContext,
) -> Option<String> {
    let mut value = serde_json::from_str::<serde_json::Value>(primary_plan_json).ok()?;
    let object = value.as_object_mut()?;
    object.insert(
        EXPLORATION_CONTEXT_KEY.to_string(),
        serde_json::to_value(context).ok()?,
    );
    serde_json::to_string(&value).ok()
}

fn estimate_plan(
    profile: &PlanProfile,
    reference: &PlanProfile,
    shadow_rate: f64,
    planner_overhead_ms: f64,
) -> Option<PlanEstimate> {
    if profile.window_observations == 0 || reference.window_observations == 0 {
        return None;
    }
    let divergence_upper_p95 = profile.divergence_upper_p95?;
    let last_observed_at_us = profile.last_paired_at_us?;
    let fallback_rate = profile.dispatch_fallback_rate;
    let extra_reference_rate = shadow_rate + fallback_rate;
    Some(PlanEstimate {
        paired_observations: profile.paired_observations,
        expected_cost_usd: profile.cost_usd.mean,
        expected_latency_ms: profile.latency_ms.mean,
        divergence_upper_p95,
        expected_net_cost_savings_usd: reference.cost_usd.mean
            - profile.cost_usd.mean
            - extra_reference_rate * reference.cost_usd.mean,
        expected_net_latency_savings_ms: reference.latency_ms.mean
            - profile.latency_ms.mean
            - extra_reference_rate * reference.latency_ms.mean
            - planner_overhead_ms,
        last_observed_at_us,
    })
}

/// Attach a self-contained, content-free profile identity to a planned call.
///
/// The field is deliberately embedded in the JSON returned to Python instead
/// of stored in a side table: concurrent calls and delayed observations cannot
/// cross-wire, and provider adapters only have to return the original JSON.
/// Unsupported calls (notably multi-call Parallel plans) remain executable but
/// receive no decision profile until their identity schema is defined.
pub fn attach_observation_context(
    catalog: Option<&ModelCatalog>,
    call_json: &str,
    plan_json: &str,
    divergence_threshold: f64,
) -> String {
    if !divergence_threshold.is_finite() || !(0.0..=1.0).contains(&divergence_threshold) {
        return plan_json.to_string();
    }
    let Some(catalog) = catalog else {
        return plan_json.to_string();
    };
    let Ok(original_call) = serde_json::from_str::<Call>(call_json) else {
        return plan_json.to_string();
    };
    let Ok(plan) = serde_json::from_str::<Plan>(plan_json) else {
        return plan_json.to_string();
    };
    let Some(context) = build_observation_context(
        catalog,
        &original_call,
        &plan,
        divergence_threshold,
    ) else {
        return plan_json.to_string();
    };
    encode_observation_context(plan_json.to_string(), &context)
}

fn encode_observation_context(
    plan_json: String,
    context: &EmbeddedObservationContext,
) -> String {
    let Ok(mut value) = serde_json::from_str::<serde_json::Value>(&plan_json) else {
        return plan_json;
    };
    let Some(object) = value.as_object_mut() else {
        return plan_json;
    };
    let Ok(encoded) = serde_json::to_value(context) else {
        return plan_json;
    };
    object.insert(OBSERVATION_CONTEXT_KEY.to_string(), encoded);
    serde_json::to_string(&value).unwrap_or(plan_json)
}

/// Apply the persisted complete-plan guard and attach observation context to
/// the plan that will actually execute.
///
/// Active disables and cooldown-expired plans awaiting cold re-admission both
/// fall back to the immutable reference request. Because lookup uses the exact
/// [`PlanProfileKey`], a harmful interaction does not suppress either of its
/// solo constituents.
pub fn guard_and_attach_observation_context(
    catalog: Option<&ModelCatalog>,
    plan_guard: &PlanGuard,
    call_json: &str,
    plan_json: &str,
    divergence_threshold: f64,
    now_us: i64,
) -> String {
    let attached = attach_observation_context(
        catalog,
        call_json,
        plan_json,
        divergence_threshold,
    );
    let Ok(plan) = serde_json::from_str::<Plan>(&attached) else {
        return PASS_THROUGH_JSON.to_string();
    };
    // Parallel is currently an observe-only certificate: Python's driver has
    // already scheduled the calls, and executing this Plan falls through to
    // the unchanged request. A real multi-call plan needs its own identity
    // before it can become a guarded semantic alternative (bd-6m0o).
    if matches!(plan, Plan::PassThrough | Plan::Parallel { .. }) {
        return attached;
    }
    let Ok(Some(context)) = embedded_observation_context(&attached) else {
        return attach_observation_context(
            catalog,
            call_json,
            PASS_THROUGH_JSON,
            divergence_threshold,
        );
    };
    if plan_guard
        .decision(&context.key, now_us)
        .blocks_user_visible()
    {
        return attach_observation_context(
            catalog,
            call_json,
            PASS_THROUGH_JSON,
            divergence_threshold,
        );
    }
    attached
}

fn build_observation_context(
    catalog: &ModelCatalog,
    original_call: &Call,
    plan: &Plan,
    divergence_threshold: f64,
) -> Option<EmbeddedObservationContext> {
    describe_plan(catalog, original_call, plan, divergence_threshold)
        .map(|descriptor| descriptor.context)
}

fn describe_plan(
    catalog: &ModelCatalog,
    original_call: &Call,
    plan: &Plan,
    divergence_threshold: f64,
) -> Option<ResolvedPlanDescriptor> {
    let requirements = RequestRequirements::from_call(original_call)?;
    let selected_call = selected_call(original_call, plan)?;
    let target = catalog.resolve(
        &requirements.provider_protocol,
        &requirements.provider_namespace,
        &selected_call.model,
    )?;
    let target_model_id = selected_call.model.clone();
    let target_model_version = if selected_call.model == target.model_id {
        target.model_version.clone()
    } else {
        // The adapter must preserve a reference request exactly. When that
        // request names a mutable alias, do not pretend it dispatched the
        // catalog's immutable snapshot; use an explicit observation cohort
        // until the provider response supplies a stronger revision token.
        format!("{}@{}", selected_call.model, catalog.catalog_version)
    };

    let prompt_shape_version = prompt_shape_version(original_call, &requirements);
    let tool_schema_version = tool_schema_version(original_call)?;
    let application_config_version = application_config_version(original_call);
    let call_site_version = CallSiteVersion::from_spec(&CallSiteVersionSpec {
        call_site_id: original_call.call_site_id.clone(),
        prompt_shape_version,
        provider_protocol: requirements.provider_protocol.clone(),
        tool_schema_version,
        application_config_version: Some(application_config_version),
    })
    .ok()?;

    let rules = plan_rules(plan);
    let rewrites = rules
        .iter()
        // Model routing is represented by target_model_id, not duplicated as
        // a semantic rewrite in the same identity. Cache behavior is likewise
        // represented by cache_policy rather than a duplicate rewrite.
        .filter(|rule| !matches!(rule.as_str(), "ModelDowngrade" | "CacheHit"))
        .map(|rule| RewriteApplication {
            stable_name: rule.clone(),
            implementation_version: PLAN_IMPLEMENTATION_VERSION.to_string(),
            parameters: rewrite_parameters(rule, original_call, selected_call),
        })
        .collect();
    let is_cached = matches!(plan, Plan::Cached { .. });
    let has_alternative = !rules.is_empty() || is_cached;
    let spec = ExecutionPlanSpec {
        schema_version: EXECUTION_PLAN_SCHEMA_VERSION,
        provider_protocol: requirements.provider_protocol.clone(),
        requested_model_id: original_call.model.clone(),
        target_model_id: target_model_id.clone(),
        target_model_revision: Some(target_model_version.clone()),
        rewrite_ordering: RewriteOrdering::Ordered,
        rewrites,
        cache_policy: CachePolicy {
            stable_name: if is_cached {
                "memoized-response"
            } else {
                "provider-call"
            }
            .to_string(),
            implementation_version: PLAN_IMPLEMENTATION_VERSION.to_string(),
            parameters: serde_json::json!({}),
        },
        output_budget: selected_call.parameters.max_output_tokens,
        validation_policy: ValidationPolicy {
            stable_name: if has_alternative {
                "sampled-reference"
            } else {
                "none"
            }
            .to_string(),
            implementation_version: "1".to_string(),
            parameters: if has_alternative {
                validation_policy_parameters(divergence_threshold)
            } else {
                serde_json::json!({})
            },
        },
    };
    let execution_plan_id = spec.execution_plan_id().ok()?;
    Some(ResolvedPlanDescriptor {
        context: EmbeddedObservationContext {
            call_site_id: original_call.call_site_id.clone(),
            key: PlanProfileKey {
                call_site_version,
                execution_plan_id,
            },
            runtime_version: PlanRuntimeVersion {
                provider_protocol: requirements.provider_protocol,
                target_model_id,
                target_model_version,
                price_table_version: catalog.price_table_version.clone(),
            },
            divergence_threshold,
        },
        spec,
    })
}

fn selected_call<'a>(original_call: &'a Call, plan: &'a Plan) -> Option<&'a Call> {
    match plan {
        Plan::PassThrough | Plan::Cached { .. } => Some(original_call),
        Plan::Rewritten { call, .. } | Plan::Composed { call, .. } => Some(call),
        // ExecutionPlanSpec currently represents one provider call. Assigning
        // a multi-call plan the identity of its first branch would pool unlike
        // executions, so abstain until Parallel gets an explicit schema.
        Plan::Parallel { .. } => None,
    }
}

pub fn plan_rules(plan: &Plan) -> Vec<String> {
    plan.rule_names()
}

fn prompt_shape_version(call: &Call, requirements: &RequestRequirements) -> String {
    let roles: Vec<&str> = call
        .messages
        .iter()
        .map(|message| message.role.trim())
        .collect();
    let dependency_kinds: Vec<&'static str> = call
        .input_deps
        .iter()
        .map(|dependency| match dependency {
            DepSource::Literal => "literal",
            DepSource::UserInput { .. } => "user_input",
            DepSource::ToolOutput { .. } => "tool_output",
            DepSource::LlmOutput { .. } => "llm_output",
            DepSource::State { .. } => "state",
        })
        .collect();
    let structured_output_schema_version = call
        .parameters
        .extra
        .as_object()
        .and_then(|extra| extra.get(ROUTE_CONTEXT_KEY))
        .and_then(serde_json::Value::as_object)
        .and_then(|context| context.get("structured_output_schema_version"))
        .and_then(serde_json::Value::as_str);
    let value = serde_json::json!({
        "schema_version": PROMPT_SHAPE_SCHEMA_VERSION,
        "roles": roles,
        "dependency_kinds": dependency_kinds,
        "message_count": call.messages.len(),
        "dependency_count": call.input_deps.len(),
        "native_messages_opaque": call.has_opaque_native_messages(),
        "image_input": requirements.image_input,
        "structured_outputs": requirements.structured_outputs,
        "structured_output_schema_version": structured_output_schema_version,
        "streaming": requirements.streaming,
    });
    format!(
        "prompt-shape-v{PROMPT_SHAPE_SCHEMA_VERSION}:{}",
        content_hash(&canonical_json(&value))
    )
}

fn tool_schema_version(call: &Call) -> Option<Option<String>> {
    if call.tools.is_empty() {
        return Some(None);
    }
    let value = serde_json::to_value(&call.tools).ok()?;
    Some(Some(format!(
        "tool-schema-v1:{}",
        content_hash(&canonical_json(&value))
    )))
}

fn application_config_version(call: &Call) -> String {
    let value = serde_json::json!({
        "schema_version": 1,
        "temperature": call.parameters.temperature,
        "top_p": call.parameters.top_p,
        "stop": call.parameters.stop,
    });
    format!(
        "application-config-v1:{}",
        content_hash(&canonical_json(&value))
    )
}

fn rewrite_parameters(rule: &str, original_call: &Call, selected_call: &Call) -> serde_json::Value {
    match rule {
        "ContextCompress" => original_call
            .parameters
            .extra
            .as_object()
            .and_then(|extra| extra.get("dead_attention_epsilon"))
            .map(|epsilon| serde_json::json!({"dead_attention_epsilon": epsilon}))
            .unwrap_or_else(|| serde_json::json!({})),
        "OutputBudget" | "DeadOutputTruncation" => serde_json::json!({
            "max_output_tokens": selected_call.parameters.max_output_tokens,
        }),
        _ => serde_json::json!({}),
    }
}

fn validation_policy_parameters(divergence_threshold: f64) -> serde_json::Value {
    let rate = std::env::var("AGENTC_OPTIMIZE_SHADOW")
        .ok()
        .and_then(|value| value.trim().parse::<f64>().ok())
        .filter(|value| value.is_finite())
        .unwrap_or(0.02)
        .clamp(0.0, 1.0);
    let mode = std::env::var("AGENTC_SHADOW_DIVERGENCE_MODE")
        .unwrap_or_else(|_| "lexical".to_string())
        .trim()
        .to_ascii_lowercase();
    let mode = match mode.as_str() {
        "normalized" | "embedding" => mode,
        _ => "lexical".to_string(),
    };
    serde_json::json!({
        "divergence_threshold": {
            "source": "resolved-plan-policy",
            "value": divergence_threshold,
        },
        "divergence_mode": mode,
        "sampling_rate": rate,
    })
}

/// Fold the outcome of a dispatched plan into the cost model. Failures
/// are swallowed — the user's call already returned, so there's no way to
/// surface the error anyway.
pub fn optimize_observe(
    cost_model: &Arc<CostModel>,
    plan_profiles: &PlanProfiles,
    plan_json: &str,
    outcome_json: &str,
) -> Result<String, String> {
    let plan: Plan = serde_json::from_str(plan_json).map_err(|e| e.to_string())?;
    let outcome: Outcome = serde_json::from_str(outcome_json).map_err(|e| e.to_string())?;
    let observation_context = embedded_observation_context(plan_json)?;

    // Only Rewritten/Parallel/PassThrough actually carry a call worth
    // attributing; Cached is served from memoization's cache stats, not
    // the optimizer's cost model.
    let inferred_call_site_id = match &plan {
        Plan::Rewritten { call, .. } | Plan::Composed { call, .. } => call.call_site_id.clone(),
        Plan::Parallel { calls, .. } => calls
            .first()
            .map(|c| c.call_site_id.clone())
            .unwrap_or_default(),
        // For PassThrough / Cached the plan itself doesn't carry a Call,
        // so the caller must populate `outcome.call_site_id`. Without it
        // the cost model never warms up cold sites.
        Plan::PassThrough | Plan::Cached { .. } => outcome.call_site_id.clone().unwrap_or_default(),
    };
    if let Some(context) = observation_context.as_ref() {
        if !inferred_call_site_id.is_empty() && inferred_call_site_id != context.call_site_id {
            return Err("plan observation context is bound to a different call site".to_string());
        }
    }
    let call_site_id = observation_context
        .as_ref()
        .map(|context| context.call_site_id.clone())
        .unwrap_or(inferred_call_site_id);
    if !call_site_id.is_empty() {
        cost_model.observe(CostModelUpdate {
            call_site_id: call_site_id.clone(),
            input_tokens: outcome.input_tokens,
            output_tokens: outcome.output_tokens,
            latency_ms: outcome.latency_ms,
            cost_usd: outcome.cost_usd,
            output_is_structured: outcome.output_is_structured,
            output_is_short: outcome.output_is_short,
            now_us: None,
        });
    }

    // For composed plans, also record per-rule-set realized savings so the
    // cost model can track composition payoff vs. solo rules over time.
    if let Plan::Composed {
        rules,
        net_savings_usd,
        ..
    } = &plan
    {
        let rule_names: Vec<&str> = rules.iter().map(|r| r.rule.as_str()).collect();
        cost_model.observe_rule_set(&call_site_id, &rule_names, *net_savings_usd as f64);
    }

    let divergence_threshold = observation_context
        .as_ref()
        .map(|context| context.divergence_threshold);
    let plan_observation = observation_context
        .map(|context| {
            let runtime_version = observed_runtime_version(&context.runtime_version, &outcome);
            plan_profiles.observe(PlanProfileUpdate {
                key: context.key,
                runtime_version,
                input_tokens: outcome.input_tokens,
                output_tokens: outcome.output_tokens,
                latency_ms: outcome.latency_ms,
                cost_usd: outcome.cost_usd,
                output_is_structured: outcome.output_is_structured,
                output_is_short: outcome.output_is_short,
                divergence: None,
                dispatch_fallback: outcome.dispatch_fallback,
                observed_at_us: None,
            })
        })
        .transpose()
        .map_err(|error| error.to_string())?;
    let guard_eligible = plan_observation.is_some() && !matches!(&plan, Plan::PassThrough);
    serde_json::to_string(&OpaqueObservationToken {
        schema_version: OBSERVATION_TOKEN_SCHEMA_VERSION,
        plan_observation,
        call_site_id,
        guard_eligible,
        divergence_threshold,
        rules: plan_rules(&plan),
    })
    .map_err(|error| error.to_string())
}

fn observed_runtime_version(
    planned: &PlanRuntimeVersion,
    outcome: &Outcome,
) -> PlanRuntimeVersion {
    let mut observed = planned.clone();
    if !outcome.dispatch_fallback {
        if let Some(executed_model_id) = outcome
            .executed_model_id
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            if executed_model_id != planned.target_model_id {
                observed.target_model_version = executed_model_id.to_string();
            }
        }
    }
    observed
}

fn embedded_observation_context(
    plan_json: &str,
) -> Result<Option<EmbeddedObservationContext>, String> {
    let value: serde_json::Value = serde_json::from_str(plan_json).map_err(|e| e.to_string())?;
    value
        .get(OBSERVATION_CONTEXT_KEY)
        .cloned()
        .map(serde_json::from_value)
        .transpose()
        .map_err(|error| error.to_string())
}

/// Attach a delayed reference comparison to the exact execution identified by
/// an opaque observation token. The returned solo-rule attribution lets the
/// PyO3 adapter maintain the legacy guard without charging a composed result
/// independently to every constituent rule.
pub fn record_observation_divergence(
    plan_profiles: &PlanProfiles,
    observation_token: &str,
    divergence: f64,
) -> Result<DivergenceAttribution, String> {
    let token: OpaqueObservationToken =
        serde_json::from_str(observation_token).map_err(|error| error.to_string())?;
    if token.schema_version != OBSERVATION_TOKEN_SCHEMA_VERSION {
        return Err("unsupported observation token schema version".to_string());
    }
    if !divergence.is_finite() || !(0.0..=1.0).contains(&divergence) {
        return Err("divergence must be finite and in [0, 1]".to_string());
    }
    if token.guard_eligible
        && (token.plan_observation.is_none()
            || token
                .divergence_threshold
                .is_none_or(|value| !value.is_finite() || !(0.0..=1.0).contains(&value)))
    {
        return Err("guard-eligible token is missing its plan binding or threshold".to_string());
    }
    let newly_recorded = token
        .plan_observation
        .as_ref()
        .map(|observation| plan_profiles.record_divergence_once(observation, divergence, None))
        .transpose()
        .map_err(|error| error.to_string())?
        .unwrap_or(true);
    let solo_rule = match token.rules.as_slice() {
        [rule] => Some(rule.clone()),
        _ => None,
    };
    Ok(DivergenceAttribution {
        call_site_id: token.call_site_id,
        solo_rule,
        rules: token.rules,
        plan_observation: token.plan_observation,
        guard_eligible: token.guard_eligible,
        divergence_threshold: token.divergence_threshold,
        newly_recorded,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::budget::Budget;
    use crate::config::OptimizerConfig;
    use crate::cost_model::CostModelUpdate;
    use crate::dag::{Message, Parameters};
    use crate::exploration::ExplorationPolicy;
    use crate::model_catalog::{default_model_catalog, OPENAI_CHAT_COMPLETIONS_PROTOCOL};
    use crate::planner::{CostDriver, Proposal, RewriteRule};
    use crate::rules::ModelDowngradeRule;
    use crate::schema::ensure_cost_model_schema;
    use rusqlite::Connection;

    fn empty_optimizer() -> Optimizer {
        Optimizer::empty(Arc::new(CostModel::new()), OptimizerConfig::default())
    }

    fn observable_call(site: &str, content: &str) -> Call {
        Call {
            call_site_id: site.to_string(),
            trace_id: [0; 16],
            span_id: [0; 8],
            model: "gpt-4o".to_string(),
            messages: vec![Message {
                role: "user".to_string(),
                content: content.to_string(),
            }],
            parameters: Parameters {
                max_output_tokens: Some(128),
                extra: serde_json::json!({
                    "agentc_route_context": {
                        "provider_protocol": OPENAI_CHAT_COMPLETIONS_PROTOCOL,
                        "provider_namespace": "openai",
                        "input_tokens_upper_bound": 64,
                        "image_input": false,
                        "tool_calling": false,
                        "structured_outputs": false,
                        "streaming": false,
                    }
                }),
                ..Parameters::default()
            },
            tools: Vec::new(),
            input_deps: vec![DepSource::Literal],
            occurrence_ix: 0,
        }
    }

    fn outcome(site: &str) -> Outcome {
        Outcome {
            input_tokens: 100,
            output_tokens: 50,
            latency_ms: 100.0,
            cost_usd: 0.001,
            output_is_structured: false,
            output_is_short: true,
            call_site_id: Some(site.to_string()),
            ..Outcome::default()
        }
    }

    struct AlwaysCapsOutput;

    impl RewriteRule for AlwaysCapsOutput {
        fn name(&self) -> &'static str {
            "OutputBudget"
        }

        fn applies(&self, _: &Call, _: &crate::cost_model::CallSiteProfile) -> bool {
            true
        }

        fn propose(
            &self,
            call: &Call,
            _: &crate::cost_model::CallSiteProfile,
        ) -> Option<Proposal> {
            let mut rewritten = call.clone();
            rewritten.parameters.max_output_tokens = Some(64);
            Some(Proposal {
                rewritten: Plan::Rewritten {
                    rule: self.name().to_string(),
                    call: rewritten,
                    projected_savings_usd: 0.002,
                },
                projected_savings_usd: 0.002,
                cost_driver: CostDriver::OutputTokens,
                safety_check: Box::new(|_| true),
            })
        }

        fn accuracy_budget(&self) -> f32 {
            0.05
        }

        fn preserves_native_messages(&self) -> bool {
            true
        }
    }

    fn output_optimizer(site: &str) -> (Arc<ModelCatalog>, Optimizer) {
        let catalog = Arc::new(default_model_catalog().unwrap());
        let cost_model = Arc::new(CostModel::new());
        for observed_at_us in 1..=3 {
            cost_model.observe(CostModelUpdate {
                call_site_id: site.to_string(),
                input_tokens: 100,
                output_tokens: 50,
                latency_ms: 100.0,
                cost_usd: 0.01,
                output_is_structured: false,
                output_is_short: true,
                now_us: Some(observed_at_us),
            });
        }
        let optimizer = Optimizer::new(
            cost_model,
            vec![Box::new(AlwaysCapsOutput)],
            OptimizerConfig {
                shadow_rate: 0.0,
                ..OptimizerConfig::default()
            },
        );
        (catalog, optimizer)
    }

    #[test]
    fn cold_candidate_is_leased_without_replacing_the_reference_plan() {
        let site = "exploration-live-site";
        let (catalog, optimizer) = output_optimizer(site);
        let call = observable_call(site, "private reference prompt");
        let call_json = serde_json::to_string(&call).unwrap();
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let now_us = 2_000_000;
        let primary = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            now_us,
        );

        let planned = reserve_profiled_exploration(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &controller,
            &mut connection,
            &call_json,
            &primary,
            now_us,
        );

        assert!(matches!(
            serde_json::from_str::<Plan>(&planned).unwrap(),
            Plan::PassThrough
        ));
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let exploration: EmbeddedExplorationContext = serde_json::from_value(
            value.get(EXPLORATION_CONTEXT_KEY).unwrap().clone(),
        )
        .unwrap();
        assert_eq!(exploration.schema_version, EXPLORATION_TOKEN_SCHEMA_VERSION);
        assert!(matches!(
            serde_json::from_value::<Plan>(exploration.candidate_plan.clone()).unwrap(),
            Plan::Rewritten { .. }
        ));
        assert!(!exploration.lease_token.contains("private reference prompt"));

        let token = decode_exploration_token(&exploration.lease_token).unwrap();
        let snapshot = controller
            .snapshot(
                &connection,
                &token.lease.key.call_site_version,
                now_us,
            )
            .unwrap();
        assert_eq!(snapshot.calls_in_window, 1);
        assert_eq!(snapshot.active_leases, 1);
    }

    #[test]
    fn model_routing_default_threshold_survives_opaque_json_tokens() {
        let site = "routing-float-roundtrip";
        let catalog = Arc::new(default_model_catalog().unwrap());
        let cost_model = Arc::new(CostModel::new());
        for observed_at_us in 1..=3 {
            cost_model.observe(CostModelUpdate {
                call_site_id: site.to_string(), input_tokens: 100, output_tokens: 50,
                latency_ms: 100.0, cost_usd: 0.01, output_is_structured: false,
                output_is_short: true, now_us: Some(observed_at_us),
            });
        }
        let budget = Arc::new(Budget::new());
        let optimizer = Optimizer::with_budget(cost_model.clone(),
            vec![Box::new(ModelDowngradeRule::from_catalog(catalog.clone(), budget.clone()))],
            OptimizerConfig::default(), budget);
        let call_json = serde_json::to_string(&observable_call(site, "public request")).unwrap();
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let now_us = 2_000_000;
        let primary = optimize_profiled_plan(&optimizer, Some(&catalog), &profiles,
            &guard, &call_json, now_us);
        let planned = reserve_profiled_exploration(&optimizer, Some(&catalog), &profiles,
            &guard, &controller, &mut connection, &call_json, &primary, now_us);
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let exploration: EmbeddedExplorationContext = serde_json::from_value(
            value.get(EXPLORATION_CONTEXT_KEY).expect("routing candidate lease").clone()).unwrap();
        let token = decode_exploration_token(&exploration.lease_token).unwrap();
        let expected = f64::from(0.03_f32).to_bits();
        let durable: f64 = connection.query_row("SELECT divergence_threshold FROM execution_plan_exploration",
            [], |row| row.get(0)).unwrap();
        let candidate_json = serde_json::to_string(&exploration.candidate_plan).unwrap();
        let candidate_context = embedded_observation_context(&candidate_json).unwrap().unwrap();
        for threshold in [durable, token.lease.divergence_threshold,
            token.context.divergence_threshold, candidate_context.divergence_threshold] {
            assert_eq!(threshold.to_bits(), expected);
        }
        let outcome_json = serde_json::to_string(&outcome(site)).unwrap();
        // An actual mutation still fails: round-trip correctness does not relax validation.
        let mut tampered = token.clone();
        tampered.lease.divergence_threshold = f64::from_bits(expected + 1);
        assert!(complete_profiled_exploration(&profiles, &controller, &mut connection,
            &serde_json::to_string(&tampered).unwrap(), &outcome_json, 0.0, now_us + 1).is_err());
        assert!(complete_profiled_exploration(&profiles, &controller, &mut connection,
            &exploration.lease_token, &outcome_json, 0.0, now_us + 1).unwrap().is_some());
        assert!(complete_profiled_exploration(&profiles, &controller, &mut connection,
            &exploration.lease_token, &outcome_json, 0.0, now_us + 1).unwrap().is_none());
        let observed = optimize_observe(&cost_model, &PlanProfiles::new(), &candidate_json, &outcome_json).unwrap();
        let observed: OpaqueObservationToken = serde_json::from_str(&observed).unwrap();
        assert_eq!(observed.divergence_threshold.unwrap().to_bits(), expected);
    }

    fn seed_refresh_profiles(
        profiles: &PlanProfiles,
        catalog: &ModelCatalog,
        optimizer: &Optimizer,
        call: &Call,
        last_divergence: f64,
        now_us: i64,
    ) -> PlanProfileKey {
        let reference = describe_plan(catalog, call, &Plan::PassThrough, 0.05).unwrap();
        let candidate = optimizer.candidate_plans(call, catalog).into_iter()
            .find(|plan| matches!(plan, Plan::Rewritten { .. })).unwrap();
        let candidate = describe_plan(catalog, call, &candidate,
            optimizer.divergence_threshold_for_plan(&candidate)).unwrap();
        for i in 0..20 {
            for (descriptor, cost, divergence) in [
                (&reference, 0.01, 0.0),
                (&candidate, 0.0002, if i == 19 { last_divergence } else { 0.0 }),
            ] {
                profiles.observe(PlanProfileUpdate {
                    key: descriptor.context.key.clone(),
                    runtime_version: descriptor.context.runtime_version.clone(),
                    input_tokens: 100,
                    output_tokens: 50,
                    latency_ms: 25.0,
                    cost_usd: cost,
                    output_is_structured: false,
                    output_is_short: true,
                    divergence: Some(divergence),
                    dispatch_fallback: false,
                    observed_at_us: Some(now_us + i),
                }).unwrap();
            }
        }
        candidate.context.key
    }

    struct IncumbentFixture {
        catalog: Arc<ModelCatalog>,
        cost_model: Arc<CostModel>,
        optimizer: Optimizer,
        call: Call,
        profiles: PlanProfiles,
        joint_key: PlanProfileKey,
        reference_key: PlanProfileKey,
    }

    fn incumbent_fixture(exploration_enabled: bool) -> IncumbentFixture {
        let site = "incumbent-exploration-site";
        let catalog = Arc::new(default_model_catalog().unwrap());
        let cost_model = Arc::new(CostModel::new());
        for now_us in 1..=3 {
            cost_model.observe(CostModelUpdate {
                call_site_id: site.to_string(), input_tokens: 100, output_tokens: 50,
                latency_ms: 100.0, cost_usd: 0.01, output_is_structured: false,
                output_is_short: true, now_us: Some(now_us),
            });
        }
        let budget = Arc::new(Budget::new());
        let optimizer = Optimizer::with_budget(
            cost_model.clone(),
            vec![Box::new(AlwaysCapsOutput), Box::new(ModelDowngradeRule::from_catalog(
                catalog.clone(), budget.clone(),
            ))],
            OptimizerConfig {
                exploration_enabled,
                max_overhead_ms: 100.0,
                ..OptimizerConfig::default()
            },
            budget,
        );
        let call = observable_call(site, "private immutable original");
        let profiles = PlanProfiles::new();
        let mut joint_key = None;
        let mut reference_key = None;
        for plan in optimizer.candidate_plans(&call, &catalog) {
            let descriptor = describe_plan(&catalog, &call, &plan,
                optimizer.divergence_threshold_for_plan(&plan)).unwrap();
            let is_joint = matches!(&plan, Plan::Composed { call, .. }
                if call.model == "gpt-4o-mini-2024-07-18");
            let cost = match &plan {
                Plan::PassThrough => {
                    reference_key = Some(descriptor.context.key.clone());
                    0.01
                }
                Plan::Rewritten { rule, .. } if rule == "OutputBudget" => 0.005,
                _ if is_joint => {
                    joint_key = Some(descriptor.context.key.clone());
                    0.001
                }
                _ => 0.02,
            };
            for sequence in 0..if is_joint { 19 } else { 20 } {
                profiles.observe(PlanProfileUpdate {
                    key: descriptor.context.key.clone(),
                    runtime_version: descriptor.context.runtime_version.clone(),
                    input_tokens: 100, output_tokens: 50, latency_ms: 25.0,
                    cost_usd: cost, output_is_structured: false, output_is_short: true,
                    divergence: if matches!(plan, Plan::PassThrough) { None } else { Some(0.0) },
                    dispatch_fallback: false, observed_at_us: Some(2_000_000 + sequence),
                }).unwrap();
            }
        }
        IncumbentFixture { catalog, cost_model, optimizer, call, profiles,
            joint_key: joint_key.unwrap(), reference_key: reference_key.unwrap() }
    }

    #[test]
    fn admitted_incumbent_does_not_strand_joint_candidate_at_nineteen_pairs() {
        let f = incumbent_fixture(true);
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let call_json = serde_json::to_string(&f.call).unwrap();
        let primary = optimize_profiled_plan(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &call_json, 3_000_000);
        assert!(matches!(serde_json::from_str::<Plan>(&primary).unwrap(),
            Plan::Rewritten { rule, .. } if rule == "OutputBudget"));
        let planned = reserve_profiled_exploration(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &controller, &mut connection, &call_json, &primary, 3_000_000);
        assert!(matches!(serde_json::from_str::<Plan>(&planned).unwrap(), Plan::PassThrough),
            "a leased comparison must expose the original, not the incumbent");
        let reference = embedded_observation_context(&planned).unwrap().unwrap();
        assert_eq!(reference.key, f.reference_key);
        let diagnostics = crate::diagnostics::extract_planner_diagnostics(&planned).unwrap();
        assert!(diagnostics.selected_reference);
        assert_eq!(diagnostics.selected_plan_id, f.reference_key.execution_plan_id);
        assert_eq!(diagnostics.selection_reason, "bounded_exploration_reference");
        assert!(diagnostics.candidates.iter().all(|candidate| !candidate.selected));
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let exploration: EmbeddedExplorationContext = serde_json::from_value(
            value[EXPLORATION_CONTEXT_KEY].clone()).unwrap();
        let lease = decode_exploration_token(&exploration.lease_token).unwrap();
        assert_eq!(lease.lease.key, f.joint_key);
        assert_ne!(lease.lease.key, f.reference_key);
        assert_eq!(lease.context.divergence_threshold.to_bits(), f64::from(0.03_f32).to_bits());

        // A live lease must not replace another admitted primary or add a call.
        assert_eq!(reserve_profiled_exploration(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &controller, &mut connection, &call_json, &primary, 3_000_001), primary);
        let mut reference_outcome = outcome(&f.call.call_site_id);
        reference_outcome.cost_usd = 0.01;
        let observation = optimize_observe(&f.cost_model, &f.profiles, &planned,
            &serde_json::to_string(&reference_outcome).unwrap()).unwrap();
        let observation: OpaqueObservationToken = serde_json::from_str(&observation).unwrap();
        assert!(observation.rules.is_empty());
        assert!(!observation.guard_eligible);
        complete_profiled_exploration(&f.profiles, &controller, &mut connection,
            &exploration.lease_token, &serde_json::to_string(&outcome(&f.call.call_site_id)).unwrap(),
            0.0, 3_000_002).unwrap();
        assert_eq!(f.profiles.get_for_reporting(&f.joint_key).unwrap().paired_observations, 20);
        let selected = optimize_profiled_plan(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &call_json, 3_000_003);
        assert!(matches!(serde_json::from_str::<Plan>(&selected).unwrap(), Plan::Composed { .. }));
        assert_eq!(embedded_observation_context(&selected).unwrap().unwrap().key, f.joint_key);
        assert_eq!(reserve_profiled_exploration(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &controller, &mut connection, &call_json, &selected, 3_000_003), selected,
            "no eligible cold plan means the admitted joint primary remains selected");
        let snapshot = controller.snapshot(&connection, &f.joint_key.call_site_version, 3_000_003).unwrap();
        assert_eq!(snapshot.calls_in_window, 1);
        assert_eq!(snapshot.active_leases, 0);
        assert!((snapshot.counterfactual_cost_usd - 0.001).abs() < 1e-12);
    }

    #[test]
    fn disabled_exploration_preserves_admitted_incumbent_without_reservation() {
        let f = incumbent_fixture(false);
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let call_json = serde_json::to_string(&f.call).unwrap();
        let primary = optimize_profiled_plan(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &call_json, 3_000_000);
        assert!(matches!(serde_json::from_str::<Plan>(&primary).unwrap(), Plan::Rewritten { .. }));
        assert_eq!(reserve_profiled_exploration(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &controller, &mut connection, &call_json, &primary, 3_000_000), primary);
        let snapshot = controller.snapshot(&connection, &f.joint_key.call_site_version, 3_000_000).unwrap();
        assert_eq!(snapshot.calls_in_window, 0);
    }

    #[test]
    fn incumbent_exploration_cancellation_and_failed_cap_survive_profile_reload() {
        let f = incumbent_fixture(true);
        let guard = PlanGuard::new();
        let policy = ExplorationPolicy { max_calls_per_site: 1, ..ExplorationPolicy::default() };
        let controller = ExplorationController::with_policy(policy.clone()).unwrap();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let call_json = serde_json::to_string(&f.call).unwrap();
        let primary = optimize_profiled_plan(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &call_json, 3_000_000);
        let first = reserve_profiled_exploration(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &controller, &mut connection, &call_json, &primary, 3_000_000);
        assert_ne!(first, primary);
        cancel_embedded_exploration(&controller, &mut connection, &first).unwrap();
        assert_eq!(controller.snapshot(&connection, &f.joint_key.call_site_version,
            3_000_001).unwrap().calls_in_window, 0);
        let retry = reserve_profiled_exploration(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &controller, &mut connection, &call_json, &primary, 3_000_002);
        let value: serde_json::Value = serde_json::from_str(&retry).unwrap();
        let exploration: EmbeddedExplorationContext = serde_json::from_value(
            value[EXPLORATION_CONTEXT_KEY].clone()).unwrap();
        let token = decode_exploration_token(&exploration.lease_token).unwrap();
        assert_eq!(token.lease.sequence, 2, "cancellation must not reuse an issuance sequence");
        fail_profiled_exploration(&controller, &mut connection,
            &exploration.lease_token, 3_000_003).unwrap();
        f.profiles.flush_dirty(&mut connection).unwrap();
        let reloaded = PlanProfiles::new();
        reloaded.warm_from_db(&connection).unwrap();
        let restarted = ExplorationController::with_policy(policy).unwrap();
        let selected = optimize_profiled_plan(&f.optimizer, Some(&f.catalog), &reloaded,
            &guard, &call_json, 3_000_004);
        assert_eq!(selected, primary);
        assert_eq!(reserve_profiled_exploration(&f.optimizer, Some(&f.catalog), &reloaded,
            &guard, &restarted, &mut connection, &call_json, &selected, 3_000_004), selected);
        let snapshot = restarted.snapshot(&connection, &f.joint_key.call_site_version, 3_000_004).unwrap();
        assert_eq!(snapshot.calls_in_window, 1);
        assert_eq!(snapshot.failed_calls, 1);
        assert_eq!(snapshot.active_leases, 0);
        assert_eq!(reloaded.get_for_reporting(&f.joint_key).unwrap().paired_observations, 19);
    }

    #[test]
    fn incumbent_does_not_override_a_disabled_cold_candidate_guard() {
        let f = incumbent_fixture(true);
        let guard = PlanGuard::new();
        let profile = f.profiles.get_for_reporting(&f.joint_key).unwrap();
        for i in 0..2 {
            let token = f.profiles.observe(PlanProfileUpdate {
                key: f.joint_key.clone(), runtime_version: profile.runtime_version.clone(),
                input_tokens: 100, output_tokens: 50, latency_ms: 25.0, cost_usd: 0.001,
                output_is_structured: false, output_is_short: true, divergence: None,
                dispatch_fallback: false, observed_at_us: Some(3_000_000 + i),
            }).unwrap();
            guard.record_sample(&token, 1.0, f64::from(0.03_f32), Some(3_000_000 + i)).unwrap();
        }
        assert!(guard.decision(&f.joint_key, 3_000_002).blocks_user_visible());
        assert_eq!(f.profiles.get_for_reporting(&f.joint_key).unwrap().paired_observations, 19);
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let call_json = serde_json::to_string(&f.call).unwrap();
        let primary = optimize_profiled_plan(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &call_json, 3_000_002);
        assert!(matches!(serde_json::from_str::<Plan>(&primary).unwrap(), Plan::Rewritten { .. }));
        assert_eq!(reserve_profiled_exploration(&f.optimizer, Some(&f.catalog), &f.profiles,
            &guard, &controller, &mut connection, &call_json, &primary, 3_000_002), primary);
        assert_eq!(controller.snapshot(&connection, &f.joint_key.call_site_version,
            3_000_002).unwrap().calls_in_window, 0);
    }

    #[test]
    fn evidence_complete_stale_plan_reenters_exploration_after_profile_reload() {
        let site = "stale-refresh-site";
        let (catalog, optimizer) = output_optimizer(site);
        let call = observable_call(site, "reference prompt");
        let call_json = serde_json::to_string(&call).unwrap();
        let old_time = 2_000_000;
        let profiles = PlanProfiles::new();
        let key = seed_refresh_profiles(&profiles, &catalog, &optimizer, &call, 0.0, old_time);
        let guard = PlanGuard::new();
        let fresh = optimize_profiled_plan(&optimizer, Some(&catalog), &profiles, &guard, &call_json, old_time + 20);
        assert!(matches!(serde_json::from_str::<Plan>(&fresh).unwrap(), Plan::Rewritten { .. }));

        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        profiles.flush_dirty(&mut connection).unwrap();
        let restarted = PlanProfiles::new();
        restarted.warm_from_db(&connection).unwrap();
        let now_us = old_time + optimizer.config().selection_policy(old_time).max_profile_age_us + 100;
        let controller = ExplorationController::with_policy(ExplorationPolicy {
            max_calls_per_site: 1,
            ..ExplorationPolicy::default()
        }).unwrap();
        let primary = optimize_profiled_plan(&optimizer, Some(&catalog), &restarted, &guard, &call_json, now_us);
        assert!(matches!(serde_json::from_str::<Plan>(&primary).unwrap(), Plan::PassThrough));
        let planned = reserve_profiled_exploration(&optimizer, Some(&catalog), &restarted, &guard,
            &controller, &mut connection, &call_json, &primary, now_us);
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let exploration: EmbeddedExplorationContext = serde_json::from_value(
            value.get(EXPLORATION_CONTEXT_KEY).expect("stale evidence-complete plan needs fresh probe").clone()).unwrap();
        assert!(matches!(serde_json::from_str::<Plan>(&planned).unwrap(), Plan::PassThrough));
        assert_eq!(restarted.get_for_reporting(&key).unwrap().paired_observations, 20);
        let capped = reserve_profiled_exploration(&optimizer, Some(&catalog), &restarted, &guard,
            &controller, &mut connection, &call_json, &primary, now_us + 1);
        assert_eq!(capped, primary, "refresh cannot bypass the rolling spend cap");
        let mut result = outcome(site);
        result.cost_usd = 0.0002;
        complete_profiled_exploration(&restarted, &controller, &mut connection,
            &exploration.lease_token, &serde_json::to_string(&result).unwrap(), 0.0, now_us + 2).unwrap();
        let refreshed = optimize_profiled_plan(&optimizer, Some(&catalog), &restarted, &guard, &call_json, now_us + 3);
        assert!(matches!(serde_json::from_str::<Plan>(&refreshed).unwrap(), Plan::Rewritten { .. }));
        let profile = restarted.get_for_reporting(&key).unwrap();
        assert_eq!(profile.paired_observations, 21);
        assert_eq!(profile.last_paired_at_us, Some(now_us + 2));
    }

    #[test]
    fn evidence_complete_divergence_rejection_can_collect_bounded_recovery_evidence() {
        let site = "divergence-refresh-site";
        let (catalog, optimizer) = output_optimizer(site);
        let call = observable_call(site, "reference prompt");
        let call_json = serde_json::to_string(&call).unwrap();
        let profiles = PlanProfiles::new();
        let key = seed_refresh_profiles(&profiles, &catalog, &optimizer, &call, 2.0 / 3.0, 2_000_000);
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let mut probes = 0;
        for i in 0..21 {
            let now_us = 3_000_000 + i * 10;
            let primary = optimize_profiled_plan(&optimizer, Some(&catalog), &profiles, &guard, &call_json, now_us);
            if matches!(serde_json::from_str::<Plan>(&primary).unwrap(), Plan::Rewritten { .. }) {
                assert!(probes > 0);
                assert!(probes <= 20);
                assert_eq!(profiles.get_for_reporting(&key).unwrap().paired_observations, 20 + probes);
                return;
            }
            let planned = reserve_profiled_exploration(&optimizer, Some(&catalog), &profiles, &guard,
                &controller, &mut connection, &call_json, &primary, now_us);
            let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
            let exploration: EmbeddedExplorationContext = serde_json::from_value(
                value.get(EXPLORATION_CONTEXT_KEY).expect("rejected bound must not freeze learning at20 pairs").clone()).unwrap();
            let mut result = outcome(site);
            result.cost_usd = 0.0002;
            complete_profiled_exploration(&profiles, &controller, &mut connection,
                &exploration.lease_token, &serde_json::to_string(&result).unwrap(), 0.0, now_us + 1).unwrap();
            probes += 1;
        }
        panic!("bounded fresh evidence should permit re-admission after the outlier leaves p95");
    }

    #[test]
    fn over_budget_envelope_can_be_cancelled_before_candidate_dispatch() {
        let site = "exploration-overhead-site";
        let (catalog, optimizer) = output_optimizer(site);
        let call = observable_call(site, "private reference prompt");
        let call_json = serde_json::to_string(&call).unwrap();
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let controller = ExplorationController::with_policy(ExplorationPolicy {
            max_calls_per_site: 1,
            ..ExplorationPolicy::default()
        })
        .unwrap();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let now_us = 2_000_000;
        let primary = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            now_us,
        );
        let planned = reserve_profiled_exploration(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &controller,
            &mut connection,
            &call_json,
            &primary,
            now_us,
        );
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let exploration: EmbeddedExplorationContext = serde_json::from_value(
            value.get(EXPLORATION_CONTEXT_KEY).unwrap().clone(),
        )
        .unwrap();
        let token = decode_exploration_token(&exploration.lease_token).unwrap();

        cancel_embedded_exploration(&controller, &mut connection, &planned).unwrap();
        let snapshot = controller
            .snapshot(
                &connection,
                &token.lease.key.call_site_version,
                now_us + 1,
            )
            .unwrap();
        assert_eq!(snapshot.active_leases, 0);
        assert_eq!(snapshot.failed_calls, 0);
        assert_eq!(snapshot.calls_in_window, 0);

        let retry = reserve_profiled_exploration(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &controller,
            &mut connection,
            &call_json,
            &primary,
            now_us + 2,
        );
        assert_ne!(retry, primary);
    }

    #[test]
    fn completed_counterfactual_updates_only_its_exact_paired_profile_once() {
        let site = "exploration-feedback-site";
        let (catalog, optimizer) = output_optimizer(site);
        let call = observable_call(site, "private reference prompt");
        let call_json = serde_json::to_string(&call).unwrap();
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let now_us = 2_000_000;
        let primary = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            now_us,
        );
        let planned = reserve_profiled_exploration(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &controller,
            &mut connection,
            &call_json,
            &primary,
            now_us,
        );
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let exploration: EmbeddedExplorationContext = serde_json::from_value(
            value.get(EXPLORATION_CONTEXT_KEY).unwrap().clone(),
        )
        .unwrap();
        let token = decode_exploration_token(&exploration.lease_token).unwrap();
        let mut candidate_outcome = outcome(site);
        candidate_outcome.cost_usd = 0.0002;
        candidate_outcome.latency_ms = 25.0;
        let outcome_json = serde_json::to_string(&candidate_outcome).unwrap();

        let attribution = complete_profiled_exploration(
            &profiles,
            &controller,
            &mut connection,
            &exploration.lease_token,
            &outcome_json,
            0.01,
            now_us + 10,
        )
        .unwrap()
        .expect("first completion records evidence");
        assert_eq!(attribution.plan_observation.as_ref().unwrap().key(), &token.lease.key);
        let profile = profiles
            .get_for_reporting(&token.lease.key)
            .expect("candidate profile");
        assert_eq!(profile.window_observations, 1);
        assert_eq!(profile.paired_observations, 1);
        assert_eq!(profile.cost_usd.mean, 0.0002);

        let replay = complete_profiled_exploration(
            &profiles,
            &controller,
            &mut connection,
            &exploration.lease_token,
            &outcome_json,
            0.01,
            now_us + 10,
        )
        .unwrap();
        assert!(replay.is_none());
        assert_eq!(
            profiles
                .get_for_reporting(&token.lease.key)
                .unwrap()
                .window_observations,
            1
        );
        let snapshot = controller
            .snapshot(
                &connection,
                &token.lease.key.call_site_version,
                now_us + 10,
            )
            .unwrap();
        assert_eq!(snapshot.active_leases, 0);
        assert_eq!(snapshot.completed_calls, 1);
        assert_eq!(snapshot.counterfactual_cost_usd, 0.0002);
    }

    #[test]
    fn tool_bearing_request_never_receives_a_text_only_counterfactual() {
        let site = "exploration-tool-site";
        let (catalog, optimizer) = output_optimizer(site);
        let mut call = observable_call(site, "use the tool");
        call.tools.push(crate::dag::Tool {
            name: "mutate_state".to_string(),
            schema: serde_json::json!({"type": "object"}),
        });
        call.parameters.extra[ROUTE_CONTEXT_KEY]["tool_calling"] =
            serde_json::Value::Bool(true);
        let call_json = serde_json::to_string(&call).unwrap();
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let primary = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            2_000_000,
        );

        let planned = reserve_profiled_exploration(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &controller,
            &mut connection,
            &call_json,
            &primary,
            2_000_000,
        );

        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        assert!(value.get(EXPLORATION_CONTEXT_KEY).is_none());
        let count: i64 = connection
            .query_row("SELECT COUNT(*) FROM execution_plan_exploration", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn live_profiled_path_selects_only_the_exact_evidenced_joint_plan() {
        let catalog = Arc::new(default_model_catalog().unwrap());
        let cost_model = Arc::new(CostModel::new());
        for observed_at_us in 1..=3 {
            cost_model.observe(CostModelUpdate {
                call_site_id: "joint-live-site".to_string(),
                input_tokens: 100,
                output_tokens: 50,
                latency_ms: 100.0,
                cost_usd: 0.01,
                output_is_structured: false,
                output_is_short: true,
                now_us: Some(observed_at_us),
            });
        }
        let budget = Arc::new(Budget::new());
        let optimizer = Optimizer::with_budget(
            cost_model,
            vec![
                Box::new(AlwaysCapsOutput),
                Box::new(ModelDowngradeRule::from_catalog(
                    catalog.clone(),
                    budget.clone(),
                )),
            ],
            OptimizerConfig {
                shadow_rate: 0.0,
                ..OptimizerConfig::default()
            },
            budget,
        );
        let call = observable_call("joint-live-site", "private value");
        let call_json = serde_json::to_string(&call).unwrap();
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let now_us = 2_000_000;

        let cold = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            now_us,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&cold).unwrap(),
            Plan::PassThrough
        ));
        let cold_diagnostics = crate::diagnostics::extract_planner_diagnostics(&cold)
            .expect("cold joint decision diagnostics");
        assert!(cold_diagnostics.selected_reference);
        assert_eq!(
            cold_diagnostics.fallback_reason.as_deref(),
            Some("no_admissible_alternative")
        );
        assert!(cold_diagnostics
            .candidates
            .iter()
            .all(|candidate| candidate.rejection_reason.as_deref() == Some("missing_estimate")));

        let plans = optimizer.candidate_plans(&call, &catalog);
        let joint_plan = plans
            .into_iter()
            .find(|plan| {
                matches!(
                    plan,
                    Plan::Composed { rules, call, .. }
                        if rules.iter().any(|rule| rule.rule == "OutputBudget")
                            && rules.iter().any(|rule| rule.rule == "ModelDowngrade")
                            && call.model == "gpt-4o-mini-2024-07-18"
                )
            })
            .expect("joint model-plus-rewrite candidate");
        let reference_descriptor = describe_plan(
            &catalog,
            &call,
            &Plan::PassThrough,
            optimizer.divergence_threshold_for_plan(&Plan::PassThrough),
        )
        .unwrap();
        let joint_threshold = optimizer.divergence_threshold_for_plan(&joint_plan);
        let joint_descriptor =
            describe_plan(&catalog, &call, &joint_plan, joint_threshold).unwrap();

        for sequence in 0..20 {
            let observed_at_us = now_us - 20 + sequence;
            profiles
                .observe(PlanProfileUpdate {
                    key: reference_descriptor.context.key.clone(),
                    runtime_version: reference_descriptor.context.runtime_version.clone(),
                    input_tokens: 100,
                    output_tokens: 50,
                    latency_ms: 100.0,
                    cost_usd: 0.01,
                    output_is_structured: false,
                    output_is_short: true,
                    divergence: None,
                    dispatch_fallback: false,
                    observed_at_us: Some(observed_at_us),
                })
                .unwrap();
            profiles
                .observe(PlanProfileUpdate {
                    key: joint_descriptor.context.key.clone(),
                    runtime_version: joint_descriptor.context.runtime_version.clone(),
                    input_tokens: 60,
                    output_tokens: 40,
                    latency_ms: 40.0,
                    cost_usd: 0.001,
                    output_is_structured: false,
                    output_is_short: true,
                    divergence: Some(0.0),
                    dispatch_fallback: false,
                    observed_at_us: Some(observed_at_us),
                })
                .unwrap();
        }

        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        profiles.flush_dirty(&mut connection).unwrap();
        let restarted_profiles = PlanProfiles::new();
        restarted_profiles.warm_from_db(&connection).unwrap();

        let selected = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &restarted_profiles,
            &guard,
            &call_json,
            now_us,
        );
        let selected_plan: Plan = serde_json::from_str(&selected).unwrap();
        match selected_plan {
            Plan::Composed { rules, call, .. } => {
                assert!(rules.iter().any(|rule| rule.rule == "OutputBudget"));
                assert!(rules.iter().any(|rule| rule.rule == "ModelDowngrade"));
                assert_eq!(call.model, "gpt-4o-mini-2024-07-18");
                assert_eq!(call.parameters.max_output_tokens, Some(64));
            }
            other => panic!("expected exact evidenced joint plan, got {other:?}"),
        }
        let selected_context = embedded_observation_context(&selected).unwrap().unwrap();
        assert_eq!(selected_context.key, joint_descriptor.context.key);
        let diagnostics = crate::diagnostics::extract_planner_diagnostics(&selected)
            .expect("selected joint decision diagnostics");
        assert!(!diagnostics.selected_reference);
        assert_eq!(diagnostics.selected_plan_id, joint_descriptor.context.key.execution_plan_id);
        assert!(diagnostics
            .candidates
            .iter()
            .any(|candidate| candidate.selected && candidate.evidence_confidence == 1.0));
    }

    #[test]
    fn malformed_call_json_yields_pass_through() {
        let s = optimize_plan(&empty_optimizer(), "not json");
        assert_eq!(s, PASS_THROUGH_JSON);
    }

    #[test]
    fn valid_call_cold_site_yields_pass_through() {
        let call = serde_json::json!({
            "call_site_id": "site-x",
            "trace_id": "00".repeat(16),
            "span_id": "00".repeat(8),
            "model": "gpt-4o",
            "messages": [],
        });
        let s = optimize_plan(&empty_optimizer(), &call.to_string());
        // Valid round-trip, but cold ⇒ still pass_through.
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["kind"], "pass_through");
    }

    #[test]
    fn observe_on_pass_through_without_site_is_noop() {
        let cm = Arc::new(CostModel::new());
        let profiles = PlanProfiles::new();
        let plan = Plan::PassThrough;
        let outcome = Outcome {
            input_tokens: 1,
            output_tokens: 1,
            latency_ms: 1.0,
            cost_usd: 0.001,
            output_is_structured: false,
            output_is_short: true,
            call_site_id: None,
            ..Outcome::default()
        };
        let ok = optimize_observe(
            &cm,
            &profiles,
            &serde_json::to_string(&plan).unwrap(),
            &serde_json::to_string(&outcome).unwrap(),
        );
        assert!(ok.is_ok());
        assert!(cm.get("anything").is_none());
    }

    #[test]
    fn observe_on_pass_through_with_site_updates_cost_model() {
        let cm = Arc::new(CostModel::new());
        let profiles = PlanProfiles::new();
        let plan = Plan::PassThrough;
        let outcome = Outcome {
            input_tokens: 100,
            output_tokens: 50,
            latency_ms: 100.0,
            cost_usd: 0.001,
            output_is_structured: false,
            output_is_short: true,
            call_site_id: Some("site-warm".to_string()),
            ..Outcome::default()
        };
        let ok = optimize_observe(
            &cm,
            &profiles,
            &serde_json::to_string(&plan).unwrap(),
            &serde_json::to_string(&outcome).unwrap(),
        );
        assert!(ok.is_ok());
        let prof = cm.get("site-warm").expect("site warmed");
        assert_eq!(prof.n_observations, 1);
    }

    #[test]
    fn observation_context_is_stable_across_prompt_values_of_the_same_shape() {
        let catalog = default_model_catalog().unwrap();
        let plan = serde_json::to_string(&Plan::PassThrough).unwrap();
        let first = observable_call("site", "first private value");
        let second = observable_call("site", "different private value");

        let first_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&first).unwrap(),
            &plan,
            0.05,
        );
        let second_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&second).unwrap(),
            &plan,
            0.05,
        );
        let first_context = embedded_observation_context(&first_json).unwrap().unwrap();
        let second_context = embedded_observation_context(&second_json).unwrap().unwrap();

        assert_eq!(first_context.key, second_context.key);
        assert!(!first_json.contains("first private value"));
        assert!(!second_json.contains("different private value"));
    }

    #[test]
    fn resolved_divergence_threshold_is_identity_bearing() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("threshold-site", "private value");
        let plan = Plan::Rewritten {
            rule: "OutputBudget".to_string(),
            call: original.clone(),
            projected_savings_usd: 0.01,
        };
        let call_json = serde_json::to_string(&original).unwrap();
        let plan_json = serde_json::to_string(&plan).unwrap();

        let first = attach_observation_context(Some(&catalog), &call_json, &plan_json, 0.05);
        let second = attach_observation_context(Some(&catalog), &call_json, &plan_json, 0.10);
        let first = embedded_observation_context(&first).unwrap().unwrap();
        let second = embedded_observation_context(&second).unwrap().unwrap();

        assert_eq!(first.divergence_threshold, 0.05);
        assert_eq!(second.divergence_threshold, 0.10);
        assert_ne!(first.key.execution_plan_id, second.key.execution_plan_id);
    }

    #[test]
    fn ordered_message_structure_changes_call_site_version() {
        let catalog = default_model_catalog().unwrap();
        let plan = serde_json::to_string(&Plan::PassThrough).unwrap();
        let first = observable_call("site", "private value");
        let mut second = first.clone();
        second.messages.insert(
            0,
            Message {
                role: "system".to_string(),
                content: "another private value".to_string(),
            },
        );
        second.input_deps.insert(0, DepSource::Literal);

        let first_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&first).unwrap(),
            &plan,
            0.05,
        );
        let second_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&second).unwrap(),
            &plan,
            0.05,
        );
        let first_context = embedded_observation_context(&first_json).unwrap().unwrap();
        let second_context = embedded_observation_context(&second_json).unwrap().unwrap();

        assert_ne!(
            first_context.key.call_site_version,
            second_context.key.call_site_version
        );
        assert!(!second_json.contains("another private value"));
    }

    #[test]
    fn structured_output_contract_changes_call_site_version() {
        let catalog = default_model_catalog().unwrap();
        let plan = serde_json::to_string(&Plan::PassThrough).unwrap();
        let mut first = observable_call("site", "private value");
        let mut second = first.clone();
        first.parameters.extra[ROUTE_CONTEXT_KEY]["structured_outputs"] =
            serde_json::Value::Bool(true);
        first.parameters.extra[ROUTE_CONTEXT_KEY]["structured_output_schema_version"] =
            serde_json::Value::String("structured-output-v1:first".to_string());
        second.parameters.extra[ROUTE_CONTEXT_KEY]["structured_outputs"] =
            serde_json::Value::Bool(true);
        second.parameters.extra[ROUTE_CONTEXT_KEY]["structured_output_schema_version"] =
            serde_json::Value::String("structured-output-v1:second".to_string());

        let first_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&first).unwrap(),
            &plan,
            0.05,
        );
        let second_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&second).unwrap(),
            &plan,
            0.05,
        );
        let first_context = embedded_observation_context(&first_json).unwrap().unwrap();
        let second_context = embedded_observation_context(&second_json).unwrap().unwrap();

        assert_ne!(
            first_context.key.call_site_version,
            second_context.key.call_site_version
        );
    }

    #[test]
    fn optimized_plan_without_canonical_identity_abstains() {
        let catalog = default_model_catalog().unwrap();
        let mut original = observable_call("unknown-model-site", "private value");
        original.model = "provider-model-not-in-catalog".to_string();
        let plan = Plan::Rewritten {
            rule: "ContextCompress".to_string(),
            call: original.clone(),
            projected_savings_usd: 0.01,
        };

        let guarded = guard_and_attach_observation_context(
            Some(&catalog),
            &PlanGuard::new(),
            &serde_json::to_string(&original).unwrap(),
            &serde_json::to_string(&plan).unwrap(),
            0.05,
            1,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&guarded).unwrap(),
            Plan::PassThrough
        ));
        assert!(embedded_observation_context(&guarded).unwrap().is_none());
    }

    #[test]
    fn observe_returns_token_that_correlates_one_exact_plan_divergence() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("profiled-site", "private value");
        let selected = original.clone();
        let plan = Plan::Rewritten {
            rule: "OutputBudget".to_string(),
            call: selected,
            projected_savings_usd: 0.01,
        };
        let observable_plan = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            &serde_json::to_string(&plan).unwrap(),
            0.125,
        );
        let cost_model = Arc::new(CostModel::new());
        let profiles = PlanProfiles::new();

        let token_json = optimize_observe(
            &cost_model,
            &profiles,
            &observable_plan,
            &serde_json::to_string(&outcome("profiled-site")).unwrap(),
        )
        .unwrap();
        let token: OpaqueObservationToken = serde_json::from_str(&token_json).unwrap();
        let observation = token.plan_observation.as_ref().unwrap();
        let before = profiles.get_for_reporting(observation.key()).unwrap();
        assert_eq!(before.window_observations, 1);
        assert_eq!(before.paired_observations, 0);

        let first = record_observation_divergence(&profiles, &token_json, 0.1).unwrap();
        let replay = record_observation_divergence(&profiles, &token_json, 0.1).unwrap();
        let after = profiles.get_for_reporting(observation.key()).unwrap();

        assert_eq!(first.solo_rule.as_deref(), Some("OutputBudget"));
        assert_eq!(first.divergence_threshold, Some(0.125));
        assert!(first.newly_recorded);
        assert!(!replay.newly_recorded);
        assert_eq!(after.paired_observations, 1);
        assert_eq!(after.n_paired_observations, 1);
    }

    #[test]
    fn stale_runtime_binding_is_rejected() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("stale-site", "private value");
        let plan = Plan::Rewritten {
            rule: "OutputBudget".to_string(),
            call: original.clone(),
            projected_savings_usd: 0.01,
        };
        let observable_plan = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            &serde_json::to_string(&plan).unwrap(),
            0.05,
        );
        let profiles = PlanProfiles::new();
        let token = optimize_observe(
            &Arc::new(CostModel::new()),
            &profiles,
            &observable_plan,
            &serde_json::to_string(&outcome("stale-site")).unwrap(),
        )
        .unwrap();
        let mut tampered: serde_json::Value = serde_json::from_str(&token).unwrap();
        tampered["plan_observation"]["runtime_version"]["target_model_version"] =
            serde_json::Value::String("stale-model-version".to_string());

        let error =
            record_observation_divergence(&profiles, &tampered.to_string(), 0.1).unwrap_err();
        assert!(error.contains("no longer current"));
    }

    #[test]
    fn provider_reported_model_revision_versions_the_observation() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("alias-site", "private value");
        let plan = serde_json::to_string(&Plan::PassThrough).unwrap();
        let observable_plan = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            &plan,
            0.05,
        );
        let context = embedded_observation_context(&observable_plan)
            .unwrap()
            .unwrap();
        assert_eq!(context.runtime_version.target_model_id, "gpt-4o");
        assert_eq!(
            context.runtime_version.target_model_version,
            format!("gpt-4o@{}", catalog.catalog_version)
        );
        let mut observed_outcome = outcome("alias-site");
        observed_outcome.executed_model_id = Some("gpt-4o-provider-revision".to_string());

        let token = optimize_observe(
            &Arc::new(CostModel::new()),
            &PlanProfiles::new(),
            &observable_plan,
            &serde_json::to_string(&observed_outcome).unwrap(),
        )
        .unwrap();
        let token: OpaqueObservationToken = serde_json::from_str(&token).unwrap();
        let observation = token.plan_observation.unwrap();

        assert_eq!(
            observation.runtime_version().target_model_version,
            "gpt-4o-provider-revision"
        );
    }

    #[test]
    fn composed_token_has_no_fabricated_solo_rule_attribution() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("composed-site", "private value");
        let plan = Plan::Composed {
            rules: vec![
                crate::planner::RuleApplication {
                    rule: "ContextCompress".to_string(),
                    projected_savings_usd: 0.01,
                    cost_driver: CostDriver::InputTokens,
                },
                crate::planner::RuleApplication {
                    rule: "OutputBudget".to_string(),
                    projected_savings_usd: 0.01,
                    cost_driver: CostDriver::OutputTokens,
                },
            ],
            call: original.clone(),
            net_savings_usd: 0.02,
        };
        let observable_plan = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            &serde_json::to_string(&plan).unwrap(),
            0.0,
        );
        let profiles = PlanProfiles::new();
        let token = optimize_observe(
            &Arc::new(CostModel::new()),
            &profiles,
            &observable_plan,
            &serde_json::to_string(&outcome("composed-site")).unwrap(),
        )
        .unwrap();

        let attribution = record_observation_divergence(&profiles, &token, 0.2).unwrap();
        assert!(attribution.solo_rule.is_none());
        assert!(attribution.newly_recorded);

        let guard = PlanGuard::with_limits(0.1, 100, 100).unwrap();
        guard
            .record_sample(
                attribution.plan_observation.as_ref().unwrap(),
                0.2,
                0.0,
                Some(1),
            )
            .unwrap();
        let original_json = serde_json::to_string(&original).unwrap();
        let plan_json = serde_json::to_string(&plan).unwrap();
        let blocked = guard_and_attach_observation_context(
            Some(&catalog),
            &guard,
            &original_json,
            &plan_json,
            0.0,
            1,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&blocked).unwrap(),
            Plan::PassThrough
        ));

        let solo = Plan::Rewritten {
            rule: "ContextCompress".to_string(),
            call: original.clone(),
            projected_savings_usd: 0.01,
        };
        let allowed = guard_and_attach_observation_context(
            Some(&catalog),
            &guard,
            &original_json,
            &serde_json::to_string(&solo).unwrap(),
            0.0,
            1,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&allowed).unwrap(),
            Plan::Rewritten { .. }
        ));
    }
}
