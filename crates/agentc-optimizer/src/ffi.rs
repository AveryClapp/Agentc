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
use crate::execution_plan::{
    rejection_reason, select_candidate, CachePolicy, CandidatePlan, CandidateRejectionReason,
    ExecutionPlanSpec, PlanAdmission, PlanEstimate, RewriteApplication, RewriteOrdering, Selection,
    SelectionPolicy, ValidationPolicy, EXECUTION_PLAN_SCHEMA_VERSION,
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
use crate::planner::{CandidateSearchStatus, Optimizer, Plan};

/// Canonical PassThrough JSON, returned whenever anything goes sideways.
pub const PASS_THROUGH_JSON: &str = "{\"kind\":\"pass_through\"}";
/// Text-action comparator whose declarations participate in profile identity.
pub const TEXT_ACTION_COMPARATOR_VERSION: &str = "mini-swe-bash-v1";

const OBSERVATION_CONTEXT_KEY: &str = "agentc_observation_context";
const EXPLORATION_CONTEXT_KEY: &str = "agentc_exploration_context";
const SELECTION_AUDIT_KEY: &str = "agentc_selection_audit";
const OBSERVATION_TOKEN_SCHEMA_VERSION: u16 = 2;
const EXPLORATION_TOKEN_SCHEMA_VERSION: u16 = 1;
const PROMPT_SHAPE_SCHEMA_VERSION: u16 = 1;
const PLAN_IMPLEMENTATION_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Execute a frozen evaluation policy without consulting live interaction
/// profiles. Apply the ordinary guard and issue native observation identities.
pub fn optimize_frozen_policy(
    opt: &Optimizer,
    catalog: Option<&ModelCatalog>,
    guard: &PlanGuard,
    call_json: &str,
    mode: crate::calibrated_policy::Mode,
    now_us: i64,
    audit_enabled: bool,
) -> String {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let started = Instant::now();
        let fallback = |reason| {
            unavailable_selection_audit(
                PASS_THROUGH_JSON.to_string(),
                mode.as_str(),
                reason,
                audit_enabled,
            )
        };
        let Some(catalog) = catalog else {
            return fallback("missing_model_catalog");
        };
        let Ok(call) = serde_json::from_str::<Call>(call_json) else {
            return fallback("invalid_call");
        };
        let compiled = match crate::calibrated_policy::compile(catalog, &call, mode) {
            Ok(compiled) => compiled,
            Err(reason) => return fallback(reason),
        };
        let Ok(plan_json) = serde_json::to_string(&compiled.plan) else {
            return fallback("invalid_compiled_policy");
        };
        let Some(reference) = describe_plan(
            catalog,
            &call,
            &Plan::PassThrough,
            compiled.divergence_threshold,
        ) else {
            return fallback("missing_reference_identity");
        };
        let Some(selected) = describe_plan(
            catalog,
            &call,
            &compiled.plan,
            compiled.divergence_threshold,
        ) else {
            return fallback("missing_selected_identity");
        };
        let guarded = guard_and_attach_observation_context(
            Some(catalog),
            guard,
            call_json,
            &plan_json,
            compiled.divergence_threshold,
            now_us,
        );
        if !opt.config().max_overhead_ms.is_finite()
            || opt.config().max_overhead_ms <= 0.0
            || started.elapsed().as_secs_f64() * 1000.0 > opt.config().max_overhead_ms as f64
        {
            return unavailable_selection_audit(
                attach_observation_context(
                    Some(catalog),
                    call_json,
                    PASS_THROUGH_JSON,
                    compiled.divergence_threshold,
                ),
                mode.as_str(),
                "planning_overhead_budget",
                audit_enabled,
            );
        }
        if !audit_enabled {
            return guarded;
        }
        let effective: serde_json::Value = serde_json::from_str(&guarded).unwrap_or_default();
        let blocked = effective[OBSERVATION_CONTEXT_KEY]["key"]["execution_plan_id"]
            != serde_json::json!(selected.context.key.execution_plan_id);
        encode_selection_audit(
            guarded,
            serde_json::json!({
                "schema_version": 1, "planner_mode": mode.as_str(),
                "candidate_count": null, "enumeration_complete": false,
                "search_status": "frozen_policy_not_enumerated",
                "reference": {"id": reference.context.key.execution_plan_id}, "alternatives": [],
                "policy": compiled.decision,
                "selection": {"id": selected.context.key.execution_plan_id},
                "abstention_reason": if blocked { Some("complete_plan_guard") } else { None },
            }),
        )
    }))
    .unwrap_or_else(|_| PASS_THROUGH_JSON.to_string())
}

/// Identity and runtime metadata embedded in a returned Plan as an internal
/// JSON field. Python carries this field opaquely in ``Plan.raw_json``; it does
/// not need to understand or reconstruct any profile key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct EmbeddedObservationContext {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    evidence_cohort_started_at_us: Option<i64>,
    #[serde(default)]
    provider_alias: bool,
    call_site_id: String,
    key: PlanProfileKey,
    runtime_version: PlanRuntimeVersion,
    divergence_threshold: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    validation_contract: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    sampling_rate: Option<f64>,
}

/// The production binding snapshots sampling at bootstrap, before identity and
/// dispatch. Native-only legacy helpers retain their existing undeclared path.
pub fn bind_sampling_policy(call_json: &str, rate: f32) -> String {
    let rate = serde_json::from_str::<f64>(&rate.to_string()).unwrap_or(0.0);
    bind_issued_sampling_policy(call_json, rate)
}

fn bind_issued_sampling_policy(call_json: &str, rate: f64) -> String {
    let Ok(mut call) = serde_json::from_str::<Call>(call_json) else {
        return call_json.into();
    };
    let Some(context) = call
        .parameters
        .extra
        .get_mut(ROUTE_CONTEXT_KEY)
        .and_then(serde_json::Value::as_object_mut)
    else {
        return call_json.into();
    };
    context.insert("sampling_rate".into(), serde_json::json!(rate));
    serde_json::to_string(&call).unwrap_or_else(|_| call_json.into())
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
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    cached_response: bool,
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
    optimize_profiled_plan_with_audit(
        opt,
        catalog,
        plan_profiles,
        plan_guard,
        call_json,
        now_us,
        false,
    )
}

/// Opt-in acquisition telemetry for the exact selection performed in this
/// invocation. The default entrypoint returns byte-identical plan envelopes
/// without allocating or serializing an audit snapshot.
#[allow(clippy::too_many_arguments)]
pub fn optimize_profiled_plan_with_audit(
    opt: &Optimizer,
    catalog: Option<&ModelCatalog>,
    plan_profiles: &PlanProfiles,
    plan_guard: &PlanGuard,
    call_json: &str,
    now_us: i64,
    audit_enabled: bool,
) -> String {
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let Some(catalog) = catalog else {
            return unavailable_selection_audit(
                PASS_THROUGH_JSON.to_string(),
                "joint_guarded",
                "catalog_unavailable",
                audit_enabled,
            );
        };
        if !opt.config().compose {
            let plan_json = optimize_plan(opt, call_json);
            let threshold = serde_json::from_str::<Plan>(&plan_json)
                .ok()
                .map(|plan| opt.divergence_threshold_for_plan(&plan))
                .unwrap_or(0.05);
            let guarded = guard_and_attach_observation_context(
                Some(catalog),
                plan_guard,
                call_json,
                &plan_json,
                threshold,
                now_us,
            );
            return unavailable_selection_audit(
                guarded,
                "projected_greedy",
                "complete_plans_not_enumerated",
                audit_enabled,
            );
        }

        let started = Instant::now();
        let max_overhead_us = (opt.config().max_overhead_ms * 1000.0) as u128;
        let Ok(call) = serde_json::from_str::<Call>(call_json) else {
            return unavailable_selection_audit(
                PASS_THROUGH_JSON.to_string(),
                "joint_guarded",
                "invalid_request",
                audit_enabled,
            );
        };
        let reference_plan = Plan::PassThrough;
        let reference_threshold = opt.divergence_threshold_for_plan(&reference_plan);
        let Some(reference_descriptor) =
            describe_plan(catalog, &call, &reference_plan, reference_threshold)
        else {
            return unavailable_selection_audit(
                PASS_THROUGH_JSON.to_string(),
                "joint_guarded",
                "reference_identity_unavailable",
                audit_enabled,
            );
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
            Err(_) => {
                return unavailable_selection_audit(
                    PASS_THROUGH_JSON.to_string(),
                    "joint_guarded",
                    "reference_identity_invalid",
                    audit_enabled,
                )
            }
        };
        let fallback = || {
            encode_observation_context(
                serde_json::to_string(&reference_plan)
                    .unwrap_or_else(|_| PASS_THROUGH_JSON.to_string()),
                &reference_descriptor.context,
            )
        };
        let selection_policy = profiled_selection_policy(opt, plan_guard, now_us);
        let audited = |plan_json: String,
                       candidates: &[CandidatePlan],
                       search_status,
                       complete,
                       selection: Option<&Selection<'_>>,
                       abstention: Option<&str>| {
            if !audit_enabled {
                return plan_json;
            }
            let snapshot = serde_json::json!({
                "schema_version": 1,
                "planner_mode": "joint_guarded",
                "candidate_count": selection.map(|_| candidates.len() + 1),
                "selector_invoked": selection.is_some(),
                "enumeration_complete": complete,
                "search_status": search_status,
                "reference": reference,
                "alternatives": candidates,
                "policy": selection_policy,
                "selection": selection.map(|selection| serde_json::json!({
                    "selected_plan_id": selection.selected.id,
                    "selected_reference": selection.selected_reference,
                    "reason": selection.reason,
                    "admissible_alternatives": selection.admissible_alternatives,
                    "rejections": selection.rejections,
                })),
                "abstention_reason": abstention,
            });
            encode_selection_audit(plan_json, snapshot)
        };
        if started.elapsed().as_micros() > max_overhead_us {
            return audited(
                fallback(),
                &[],
                CandidateSearchStatus::OverheadBudget,
                false,
                None,
                Some("selection_overhead_budget"),
            );
        }

        let reference_profile = decision_profile(plan_profiles, &reference_descriptor.context);
        let shadow_rate = if opt.config().shadow_rate.is_finite() {
            f64::from(opt.config().shadow_rate.clamp(0.0, 1.0))
        } else {
            0.02
        };
        let mut candidates = Vec::new();
        let mut executable = Vec::new();
        let mut seen = HashSet::new();
        seen.insert(reference.id.clone());

        let search = opt.candidate_search(&call, catalog);
        for plan in search.plans {
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
            prepare_cold_readmission(plan_profiles, plan_guard, &descriptor.context, now_us);
            let mut admission =
                match plan_guard.admission(&descriptor.context.key, true, threshold, now_us) {
                    Ok(admission) => admission,
                    Err(_) => continue,
                };
            let estimate =
                decision_profile(plan_profiles, &descriptor.context).and_then(|profile| {
                    reference_profile.as_ref().and_then(|reference_profile| {
                        estimate_plan(
                            &profile,
                            reference_profile,
                            shadow_rate,
                            f64::from(opt.config().max_overhead_ms.max(0.0)),
                        )
                    })
                });
            if matches!(
                plan_guard.decision(&descriptor.context.key, now_us),
                crate::plan_guard::PlanGuardDecision::ColdReadmissionRequired(_)
            ) {
                let mut provisional = admission.clone();
                provisional.disabled = false;
                if CandidatePlan::new(descriptor.spec.clone(), estimate.clone(), provisional)
                    .is_ok_and(|candidate| {
                        rejection_reason(&candidate, &selection_policy).is_none()
                    })
                    && plan_guard
                        .mark_readmitted_with_fresh_profile(&descriptor.context.key, now_us)
                        .is_ok()
                {
                    admission = match plan_guard.admission(
                        &descriptor.context.key,
                        true,
                        threshold,
                        now_us,
                    ) {
                        Ok(admission) => admission,
                        Err(_) => continue,
                    };
                }
            }
            let candidate = match CandidatePlan::new(descriptor.spec, estimate, admission) {
                Ok(candidate) => candidate,
                Err(_) => continue,
            };
            executable.push((candidate.id.clone(), plan, descriptor.context));
            candidates.push(candidate);
            if started.elapsed().as_micros() > max_overhead_us {
                return audited(
                    fallback(),
                    &candidates,
                    search.status,
                    false,
                    None,
                    Some("selection_overhead_budget"),
                );
            }
        }

        let selection = select_candidate(&reference, &candidates, &selection_policy);
        let complete = search.status == CandidateSearchStatus::Complete;
        if started.elapsed().as_micros() > max_overhead_us {
            return audited(
                fallback(),
                &candidates,
                search.status,
                complete,
                Some(&selection),
                Some("selection_overhead_budget"),
            );
        }
        if selection.selected_reference {
            return audited(
                fallback(),
                &candidates,
                search.status,
                complete,
                Some(&selection),
                Some("no_admissible_alternative"),
            );
        }
        let selected_id = selection.selected.id.clone();
        let Some((_id, selected_plan, selected_context)) =
            executable.into_iter().find(|(id, _, _)| *id == selected_id)
        else {
            return audited(
                fallback(),
                &candidates,
                search.status,
                complete,
                Some(&selection),
                Some("selected_plan_unavailable"),
            );
        };
        // Re-check immediately before exposure. A concurrent shadow result may
        // have disabled the plan after its admission snapshot was built.
        if plan_guard
            .decision(&selected_context.key, now_us)
            .blocks_user_visible()
        {
            return audited(
                fallback(),
                &candidates,
                search.status,
                complete,
                Some(&selection),
                Some("disabled_before_dispatch"),
            );
        }
        let plan_json =
            serde_json::to_string(&selected_plan).unwrap_or_else(|_| PASS_THROUGH_JSON.to_string());
        audited(
            encode_observation_context(plan_json, &selected_context),
            &candidates,
            search.status,
            complete,
            Some(&selection),
            None,
        )
    }))
    .unwrap_or_else(|_| {
        unavailable_selection_audit(
            PASS_THROUGH_JSON.to_string(),
            "joint_guarded",
            "planning_failed",
            audit_enabled,
        )
    });
    bind_evidence_cohort(result, plan_profiles)
}

fn bind_evidence_cohort(plan_json: String, profiles: &PlanProfiles) -> String {
    let Ok(Some(mut context)) = embedded_observation_context(&plan_json) else {
        return plan_json;
    };
    context.evidence_cohort_started_at_us = profiles.evidence_cohort_started_at_us(&context.key);
    encode_observation_context(plan_json, &context)
}

/// Cooldown expiry opens only bounded counterfactual learning. It does not
/// clear the disable or the guard's rolling exposure history.
fn prepare_cold_readmission(
    profiles: &PlanProfiles,
    guard: &PlanGuard,
    context: &EmbeddedObservationContext,
    now_us: i64,
) -> bool {
    let crate::plan_guard::PlanGuardDecision::ColdReadmissionRequired(disabled) =
        guard.decision(&context.key, now_us)
    else {
        return false;
    };
    profiles.begin_readmission(
        &context.key,
        &context.runtime_version,
        disabled.reenable_at_us,
    );
    true
}

/// An unavailable search remains explicit and does not become a measured
/// zero-candidate opportunity. No native plan identity is invented.
pub fn unavailable_selection_audit(
    plan_json: String,
    mode: &str,
    reason: &str,
    enabled: bool,
) -> String {
    if !enabled {
        return plan_json;
    }
    encode_selection_audit(
        plan_json,
        serde_json::json!({
            "schema_version": 1, "planner_mode": mode, "candidate_count": null,
            "selector_invoked": false, "enumeration_complete": false,
            "search_status": "unavailable", "reference": null, "alternatives": [],
            "policy": null, "selection": null, "abstention_reason": reason,
        }),
    )
}

fn encode_selection_audit(plan_json: String, mut audit: serde_json::Value) -> String {
    if !audit.is_object() {
        return plan_json;
    }
    let Ok(mut value) = serde_json::from_str::<serde_json::Value>(&plan_json) else {
        return plan_json;
    };
    let effective = value[OBSERVATION_CONTEXT_KEY]["key"]["execution_plan_id"].clone();
    audit["effective_selected_plan_id"] = effective;
    if let Some(object) = value.as_object_mut() {
        object.insert(SELECTION_AUDIT_KEY.to_string(), audit);
    }
    serde_json::to_string(&value).unwrap_or(plan_json)
}

/// Exploration may hold an admitted incumbent off-path and force its immutable
/// reference. Preserve the selector's original decision and update the actual
/// primary identity independently; observation handles stay opaque and unchanged.
fn carry_selection_audit(primary_plan_json: &str, effective_plan_json: String) -> String {
    let Some(audit) = serde_json::from_str::<serde_json::Value>(primary_plan_json)
        .ok()
        .and_then(|value| value.get(SELECTION_AUDIT_KEY).cloned())
    else {
        return effective_plan_json;
    };
    encode_selection_audit(effective_plan_json, audit)
}

fn profiled_selection_policy(
    opt: &Optimizer,
    plan_guard: &PlanGuard,
    now_us: i64,
) -> SelectionPolicy {
    SelectionPolicy {
        now_us,
        divergence_exposure_budget: plan_guard.exposure_budget(),
        ..opt.config().selection_policy(now_us)
    }
}

/// Attach at most one durably reserved cold or refresh-plan counterfactual.
///
/// A successful lease selects the immutable reference primary, including when
/// an incumbent rewrite was admitted. This compares another cold candidate to
/// the original request with at most two calls: reference plus candidate.
/// Without a lease the admitted primary remains unchanged. Python sees the
/// candidate only through `agentc_exploration_context`, executes it through
/// a provider rewrite or cached SDK response after the reference is observed,
/// and reports completion with the opaque token. Client function tools require
/// an adapter-declared, identity-bound typed action comparator. Streaming and
/// unsupported tool seams abstain. Every error returns `primary_plan_json` unchanged.
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
        if !opt.config().enabled
            || !opt.config().compose
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
        if requirements.streaming
            || ((requirements.tool_calling || !call.tools.is_empty())
                && !supports_tool_exploration(&call, &requirements))
            || (has_text_action_contract(&call)
                && !supports_text_action_exploration(&call, &requirements))
        {
            return primary_plan_json.to_string();
        }
        let Ok(Some(primary_context)) = embedded_observation_context(primary_plan_json) else {
            return primary_plan_json.to_string();
        };
        let mut expected_primary_context = build_observation_context(
            catalog,
            &call,
            &primary_plan,
            opt.divergence_threshold_for_plan(&primary_plan),
        );
        if let Some(context) = expected_primary_context.as_mut() {
            context.evidence_cohort_started_at_us =
                plan_profiles.evidence_cohort_started_at_us(&context.key);
        }
        if expected_primary_context.as_ref() != Some(&primary_context) {
            return primary_plan_json.to_string();
        }
        // The incumbent's identity describes its rewritten target and budget.
        // Candidate evidence must remain bound to the immutable source cohort.
        let Some(reference_context) = build_observation_context(
            catalog,
            &call,
            &Plan::PassThrough,
            opt.divergence_threshold_for_plan(&Plan::PassThrough),
        ) else {
            return primary_plan_json.to_string();
        };

        let selection_policy = profiled_selection_policy(opt, plan_guard, now_us);
        if selection_policy.validate().is_err() {
            return primary_plan_json.to_string();
        }
        let reference_profile = decision_profile(plan_profiles, &reference_context);
        let shadow_rate = if opt.config().shadow_rate.is_finite() {
            f64::from(opt.config().shadow_rate.clamp(0.0, 1.0))
        } else {
            0.02
        };

        let mut controller_candidates = Vec::new();
        let mut executable = Vec::new();
        let mut seen = HashSet::new();
        for plan in opt.candidate_plans(&call, catalog) {
            if matches!(plan, Plan::PassThrough | Plan::Parallel { .. }) {
                continue;
            }
            let threshold = opt.divergence_threshold_for_plan(&plan);
            let Some(mut descriptor) = describe_plan(catalog, &call, &plan, threshold) else {
                continue;
            };
            if !seen.insert(descriptor.context.key.execution_plan_id.clone()) {
                continue;
            }
            let readmission =
                prepare_cold_readmission(plan_profiles, plan_guard, &descriptor.context, now_us);
            let Ok(mut admission) =
                plan_guard.admission(&descriptor.context.key, true, threshold, now_us)
            else {
                continue;
            };
            if readmission && admission.divergence_exposure < plan_guard.exposure_budget() {
                admission.disabled = false;
            }
            descriptor.context.evidence_cohort_started_at_us =
                plan_profiles.evidence_cohort_started_at_us(&descriptor.context.key);
            let profile = decision_profile(plan_profiles, &descriptor.context);
            let paired_observations = profile
                .as_ref()
                .map(|profile| profile.paired_observations)
                .unwrap_or(0);
            let estimate = profile.as_ref().and_then(|profile| {
                reference_profile.as_ref().and_then(|reference| {
                    estimate_plan(
                        profile,
                        reference,
                        shadow_rate,
                        f64::from(opt.config().max_overhead_ms.max(0.0)),
                    )
                })
            });
            let refresh_required =
                CandidatePlan::new(descriptor.spec.clone(), estimate, admission.clone())
                    .map(|candidate| {
                        matches!(
                            rejection_reason(&candidate, &selection_policy),
                            Some(
                                CandidateRejectionReason::StaleProfile { .. }
                                    | CandidateRejectionReason::DivergenceExceeded { .. }
                            )
                        )
                    })
                    .unwrap_or(false);
            controller_candidates.push(ExplorationCandidate {
                key: descriptor.context.key.clone(),
                paired_observations,
                refresh_required,
                projected_savings_usd: match &plan {
                    Plan::Rewritten {
                        projected_savings_usd,
                        ..
                    } => f64::from(*projected_savings_usd).max(0.0),
                    Plan::Composed {
                        net_savings_usd, ..
                    } => f64::from(*net_savings_usd).max(0.0),
                    Plan::Cached { .. } => reference_profile
                        .as_ref()
                        .map(|profile| profile.cost_usd.mean)
                        .unwrap_or(0.0)
                        .max(0.0),
                    _ => 0.0,
                },
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
        let Some((_key, candidate_plan, candidate_context)) =
            executable.into_iter().find(|(key, _, _)| *key == lease.key)
        else {
            let _ = controller.cancel_unstarted(conn, &lease);
            return primary_plan_json.to_string();
        };

        let Ok(candidate_plan_json) = serde_json::to_string(&candidate_plan) else {
            let _ = controller.cancel_unstarted(conn, &lease);
            return primary_plan_json.to_string();
        };
        let candidate_plan_json =
            encode_observation_context(candidate_plan_json, &candidate_context);
        let Ok(candidate_plan_value) = serde_json::from_str(&candidate_plan_json) else {
            let _ = controller.cancel_unstarted(conn, &lease);
            return primary_plan_json.to_string();
        };
        let token = OpaqueExplorationToken {
            schema_version: EXPLORATION_TOKEN_SCHEMA_VERSION,
            lease: lease.clone(),
            context: candidate_context,
            rules: plan_rules(&candidate_plan),
            cached_response: matches!(candidate_plan, Plan::Cached { .. }),
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
            encode_observation_context(PASS_THROUGH_JSON.to_string(), &reference_context)
        };
        let explored =
            encode_exploration_context(&reference_plan_json, &context).unwrap_or_else(|| {
                let _ = controller.cancel_unstarted(conn, &lease);
                primary_plan_json.to_string()
            });
        carry_selection_audit(primary_plan_json, explored)
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
    let mut outcome: Outcome =
        serde_json::from_str(outcome_json).map_err(|error| error.to_string())?;
    if outcome.inference_cost_complete == Some(false) {
        return Err("counterfactual inference cost is incomplete".into());
    }
    if outcome.dispatch_fallback {
        return Err("counterfactual outcome cannot contain a reference fallback".to_string());
    }
    if token.cached_response {
        if token.rules != ["CacheHit"] {
            return Err("cached exploration token has incompatible rule attribution".to_string());
        }
        outcome.input_tokens = 0;
        outcome.output_tokens = 0;
        outcome.cost_usd = 0.0;
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
    let outcome = outcome.including_additional_inference()?;
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
    // A lease issued before this cohort may still finish late. Its provider
    // cost remains in the controller ledger, but it cannot supply fresh evidence.
    if !plan_profiles
        .accepts_execution_started_at(&token.context.key, Some(token.lease.started_at_us))
    {
        return Ok(None);
    }

    let runtime_version = observed_runtime_version(&token.context.runtime_version, &outcome);
    let update = PlanProfileUpdate {
        cost_evidence: outcome.cost_evidence(),
        key: token.context.key.clone(),
        runtime_version,
        input_tokens: outcome.input_tokens,
        output_tokens: outcome.output_tokens,
        latency_ms: outcome.latency_ms,
        cost_usd: outcome.cost_usd,
        output_is_structured: outcome.output_is_structured,
        output_is_short: outcome.output_is_short,
        divergence: Some(divergence),
        dispatch_fallback: outcome.validation_rejected,
        observed_at_us: Some(completed_at_us),
    };
    let observation = if token.cached_response {
        // The response model identifies historical cached content. Only the
        // primary reference call can resolve the provider's current alias.
        plan_profiles.observe(update)
    } else {
        record_provider_outcome(plan_profiles, update, &token.context)
    }
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

/// Fail the lease embedded in a planned exploration envelope.
///
/// Use this when provider work may have begun. The durable failed attempt
/// conservatively remains charged to the rolling exploration cap.
pub fn fail_embedded_exploration(
    controller: &ExplorationController,
    conn: &mut Connection,
    explored_plan_json: &str,
    completed_at_us: i64,
) -> Result<ExplorationCompletion, String> {
    let value: serde_json::Value =
        serde_json::from_str(explored_plan_json).map_err(|error| error.to_string())?;
    let context: EmbeddedExplorationContext = serde_json::from_value(
        value
            .get(EXPLORATION_CONTEXT_KEY)
            .cloned()
            .ok_or_else(|| "plan has no exploration context".to_string())?,
    )
    .map_err(|error| error.to_string())?;
    fail_profiled_exploration(controller, conn, &context.lease_token, completed_at_us)
}

/// Cancel an envelope discarded inside the native planner before dispatch.
/// This is deliberately not exposed through the Python adapter API.
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
    if !profile.measured_cost_complete() || !reference.measured_cost_complete() {
        return None;
    }
    let divergence_upper_p95 = profile.divergence_upper_p95?;
    let last_observed_at_us = profile.last_paired_at_us?;
    let fallback_rate = profile.dispatch_fallback_rate;
    let extra_reference_rate = shadow_rate + fallback_rate;
    let expected_cost_usd = profile.cost_usd.mean + extra_reference_rate * reference.cost_usd.mean
        - profile.charged_reference_cost_usd.mean;
    let expected_latency_ms = profile.latency_ms.mean
        + extra_reference_rate * reference.latency_ms.mean
        - profile.charged_reference_latency_ms.mean
        + planner_overhead_ms;
    Some(PlanEstimate {
        paired_observations: profile.paired_observations,
        expected_cost_usd,
        expected_latency_ms,
        divergence_upper_p95,
        expected_net_cost_savings_usd: reference.cost_usd.mean - expected_cost_usd,
        expected_net_latency_savings_ms: reference.latency_ms.mean - expected_latency_ms,
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
    let Some(context) =
        build_observation_context(catalog, &original_call, &plan, divergence_threshold)
    else {
        return plan_json.to_string();
    };
    encode_observation_context(plan_json.to_string(), &context)
}

fn encode_observation_context(plan_json: String, context: &EmbeddedObservationContext) -> String {
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
    let attached = attach_observation_context(catalog, call_json, plan_json, divergence_threshold);
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

fn decision_profile(
    profiles: &PlanProfiles,
    context: &EmbeddedObservationContext,
) -> Option<PlanProfile> {
    if context.provider_alias {
        profiles.get_provider_alias(&context.key, &context.runtime_version)
    } else {
        profiles.get(&context.key, &context.runtime_version)
    }
}

fn record_provider_outcome(
    profiles: &PlanProfiles,
    update: PlanProfileUpdate,
    context: &EmbeddedObservationContext,
) -> Result<PlanObservationToken, crate::plan_profile::PlanProfileUpdateError> {
    record_provider_outcome_with_charge(
        profiles,
        update,
        context,
        crate::plan_profile::ChargedReferenceFallback::default(),
    )
}

fn record_provider_outcome_with_charge(
    profiles: &PlanProfiles,
    update: PlanProfileUpdate,
    context: &EmbeddedObservationContext,
    charge: crate::plan_profile::ChargedReferenceFallback,
) -> Result<PlanObservationToken, crate::plan_profile::PlanProfileUpdateError> {
    if context.provider_alias {
        profiles.observe_provider_outcome_with_charged_reference(
            update,
            &context.runtime_version,
            charge,
        )
    } else {
        profiles.observe_with_charged_reference(update, charge)
    }
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
    let rule_configuration = match plan {
        Plan::Cached { value } => value.get(crate::planner::RULE_IDENTITIES_KEY),
        Plan::Parallel { calls, .. } => calls.first().and_then(|call| {
            call.parameters
                .extra
                .get(crate::planner::RULE_IDENTITIES_KEY)
        }),
        _ => selected_call
            .parameters
            .extra
            .get(crate::planner::RULE_IDENTITIES_KEY),
    };
    let rule_configuration = match rule_configuration {
        None => None, // Direct legacy plans remain in their separate old cohort.
        Some(declaration) => {
            if declaration
                .get("schema_version")
                .and_then(serde_json::Value::as_u64)
                != Some(1)
            {
                return None;
            }
            let configured = declaration.get("rules")?.as_object()?;
            if configured.len() != rules.len() {
                return None;
            }
            for name in &rules {
                let identity: crate::planner::RuleIdentity =
                    serde_json::from_value(configured.get(name)?.clone()).ok()?;
                if identity.implementation_version.is_empty() || !identity.parameters.is_object() {
                    return None;
                }
            }
            Some(configured)
        }
    };
    let rewrites = rules
        .iter()
        // Model routing is represented by target_model_id, not duplicated as
        // a semantic rewrite in the same identity. Cache behavior is likewise
        // represented by cache_policy rather than a duplicate rewrite.
        .filter(|rule| !matches!(rule.as_str(), "ModelDowngrade" | "CacheHit"))
        .map(|rule| RewriteApplication {
            stable_name: rule.clone(),
            implementation_version: if rule == "ContextCompress" {
                crate::rules::context_compress::IMPLEMENTATION_VERSION.to_string()
            } else if rule == "StateDrop" {
                crate::rules::state_drop::IMPLEMENTATION_VERSION.to_string()
            } else {
                PLAN_IMPLEMENTATION_VERSION.to_string()
            },
            parameters: rewrite_parameters(rule, original_call, selected_call),
        })
        .collect();
    let is_cached = matches!(plan, Plan::Cached { .. });
    let cache_parameters = match plan {
        Plan::Cached { value } => crate::rules::cache_hit::source_policy(value)?,
        _ => serde_json::json!({}),
    };
    let has_alternative = !rules.is_empty() || is_cached;
    let mut spec = ExecutionPlanSpec {
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
            implementation_version: if is_cached {
                crate::rules::cache_hit::SOURCE_POLICY_VERSION
            } else {
                PLAN_IMPLEMENTATION_VERSION
            }
            .to_string(),
            parameters: cache_parameters,
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
                let mut parameters = validation_policy_parameters(divergence_threshold);
                if let Some(rate) = original_call
                    .parameters
                    .extra
                    .get(ROUTE_CONTEXT_KEY)
                    .and_then(|context| context.get("sampling_rate"))
                {
                    parameters["sampling_rate"] = rate.clone();
                }
                if let Some(configuration) = rule_configuration {
                    parameters["rule_configuration"] = serde_json::json!(configuration);
                }
                parameters
            } else {
                serde_json::json!({})
            },
        },
    };
    if has_alternative {
        let mode = spec
            .validation_policy
            .parameters
            .get("divergence_mode")?
            .as_str()?;
        spec.validation_policy.implementation_version = divergence_metric_version(mode).to_string();
        if let Some(contract) = original_call
            .parameters
            .extra
            .get(crate::model_catalog::ROUTE_CONTEXT_KEY)
            .and_then(|context| context.get(crate::model_catalog::OUTPUT_VALIDATION_KEY))
        {
            spec.validation_policy.stable_name = "sampled-reference-plus-pre-release".into();
            spec.validation_policy.parameters["output_validation"] = contract.clone();
        }
    }
    let execution_plan_id = spec.execution_plan_id().ok()?;
    Some(ResolvedPlanDescriptor {
        context: EmbeddedObservationContext {
            sampling_rate: original_call
                .parameters
                .extra
                .get(ROUTE_CONTEXT_KEY)
                .and_then(|context| context.get("sampling_rate"))
                .and_then(serde_json::Value::as_f64),
            evidence_cohort_started_at_us: None,
            validation_contract: has_alternative
                .then(|| {
                    original_call
                        .parameters
                        .extra
                        .get(ROUTE_CONTEXT_KEY)
                        .and_then(|context| {
                            context.get(crate::model_catalog::OUTPUT_VALIDATION_KEY)
                        })
                        .cloned()
                })
                .flatten(),
            provider_alias: selected_call.model != target.model_id
                || target.revision_kind
                    == crate::model_catalog::ModelRevisionKind::CatalogObservation,
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
    let mut value = serde_json::json!({
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
    // Undeclared text and legacy identities stay unchanged. An explicit action
    // comparator and its settings define a separate request cohort.
    if let Some(context) = call
        .parameters
        .extra
        .get(ROUTE_CONTEXT_KEY)
        .and_then(serde_json::Value::as_object)
    {
        for field in [
            "tool_action_comparator_version",
            "tool_settings_version",
            "text_action_comparator_version",
            "text_action_settings_version",
            "text_action_contract_supported",
        ] {
            if let Some(declaration) = context.get(field) {
                value[field] = declaration.clone();
            }
        }
    }
    format!(
        "prompt-shape-v{PROMPT_SHAPE_SCHEMA_VERSION}:{}",
        content_hash(&canonical_json(&value))
    )
}

fn supports_tool_exploration(call: &Call, requirements: &RequestRequirements) -> bool {
    use crate::model_catalog::{LITELLM_COMPLETION_PROTOCOL, OPENAI_CHAT_COMPLETIONS_PROTOCOL};
    if !requirements.tool_calling
        || call.tools.is_empty()
        || !matches!(
            requirements.provider_protocol.as_str(),
            OPENAI_CHAT_COMPLETIONS_PROTOCOL | LITELLM_COMPLETION_PROTOCOL
        )
        || call
            .tools
            .iter()
            .any(|tool| tool.name.is_empty() || !tool.schema.is_object())
    {
        return false;
    }
    let Some(context) = call
        .parameters
        .extra
        .get(ROUTE_CONTEXT_KEY)
        .and_then(serde_json::Value::as_object)
    else {
        return false;
    };
    let settings = context
        .get("tool_settings_version")
        .and_then(serde_json::Value::as_str)
        .and_then(|version| version.strip_prefix("tool-settings-v1:"));
    context
        .get("tool_action_comparator_version")
        .and_then(serde_json::Value::as_str)
        == Some("typed-actions-v1")
        && settings.is_some_and(|hash| {
            hash.len() == 64 && hash.bytes().all(|byte| byte.is_ascii_hexdigit())
        })
}

fn has_text_action_contract(call: &Call) -> bool {
    call.parameters
        .extra
        .get(ROUTE_CONTEXT_KEY)
        .is_some_and(|context| {
            [
                "text_action_comparator_version",
                "text_action_settings_version",
                "text_action_contract_supported",
            ]
            .iter()
            .any(|field| context.get(field).is_some())
        })
}

fn supports_text_action_exploration(call: &Call, requirements: &RequestRequirements) -> bool {
    use crate::model_catalog::{LITELLM_COMPLETION_PROTOCOL, OPENAI_CHAT_COMPLETIONS_PROTOCOL};
    let context = &call.parameters.extra[ROUTE_CONTEXT_KEY];
    let settings = context
        .get("text_action_settings_version")
        .and_then(serde_json::Value::as_str)
        .and_then(|value| value.strip_prefix("text-action-settings-v1:"));
    !requirements.tool_calling
        && call.tools.is_empty()
        && !requirements.structured_outputs
        && !requirements.streaming
        && matches!(
            requirements.provider_protocol.as_str(),
            OPENAI_CHAT_COMPLETIONS_PROTOCOL | LITELLM_COMPLETION_PROTOCOL
        )
        && context
            .get("text_action_contract_supported")
            .and_then(serde_json::Value::as_bool)
            == Some(true)
        && context
            .get("text_action_comparator_version")
            .and_then(serde_json::Value::as_str)
            == Some(TEXT_ACTION_COMPARATOR_VERSION)
        && settings.is_some_and(|hash| {
            hash.len() == 64
                && hash
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
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
    let mut value = serde_json::json!({
        "schema_version": 1,
        "temperature": call.parameters.temperature,
        "top_p": call.parameters.top_p,
        "stop": call.parameters.stop,
    });
    if let Some(declaration) = call
        .parameters
        .extra
        .get(ROUTE_CONTEXT_KEY)
        .and_then(|context| context.get(crate::model_catalog::APPLICATION_VERSION_KEY))
    {
        value[crate::model_catalog::APPLICATION_VERSION_KEY] = declaration.clone();
    }
    if let Some(declaration) = call
        .parameters
        .extra
        .get(ROUTE_CONTEXT_KEY)
        .and_then(|context| context.get(crate::model_catalog::PROVIDER_CONFIGURATION_KEY))
    {
        value[crate::model_catalog::PROVIDER_CONFIGURATION_KEY] = declaration.clone();
    }
    if let Some(endpoint) = call
        .parameters
        .extra
        .get(ROUTE_CONTEXT_KEY)
        .and_then(|context| context.get(crate::model_catalog::PROVIDER_ENDPOINT_KEY))
    {
        value[crate::model_catalog::PROVIDER_ENDPOINT_KEY] = endpoint.clone();
    }
    if let Some(policy) = call
        .parameters
        .extra
        .get(ROUTE_CONTEXT_KEY)
        .and_then(|context| context.get(crate::calibrated_policy::CONTEXT_KEY))
    {
        value[crate::calibrated_policy::CONTEXT_KEY] = policy.clone();
    }
    format!(
        "application-config-v1:{}",
        content_hash(&canonical_json(&value))
    )
}

fn rewrite_parameters(rule: &str, original_call: &Call, selected_call: &Call) -> serde_json::Value {
    if let Some(parameters) = selected_call
        .parameters
        .extra
        .get(crate::calibrated_policy::REWRITE_PARAMETERS_KEY)
        .and_then(|rules| rules.get(rule))
    {
        return parameters.clone();
    }
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

/// Version the metric together with its fallback. Lexical identities retain
/// their existing contract; normalized containment evidence cannot be reused.
pub fn divergence_metric_version(mode: &str) -> &'static str {
    match mode.trim().to_ascii_lowercase().as_str() {
        "normalized" => "normalized-jaccard-v2",
        "embedding" => "embedding-cosine-normalized-jaccard-v2",
        _ => "1",
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
    let mut outcome: Outcome = serde_json::from_str(outcome_json).map_err(|e| e.to_string())?;
    if outcome.inference_cost_complete == Some(false) {
        return Err("plan inference cost is incomplete".into());
    }
    let cache_hit = matches!(&plan, Plan::Cached { .. }) && !outcome.dispatch_fallback;
    if cache_hit {
        outcome.input_tokens = 0;
        outcome.output_tokens = 0;
        outcome.cost_usd = 0.0;
        outcome.cost_source = crate::multi_call::InferenceCostSource::NoInference;
    } else if outcome.cost_source == crate::multi_call::InferenceCostSource::NoInference {
        return Err("non-cached inference cannot declare no inference".into());
    }
    let total_outcome = outcome.including_additional_inference()?;
    let reference_charge = if outcome.dispatch_fallback {
        crate::plan_profile::ChargedReferenceFallback {
            cost_usd: outcome.cost_usd,
            latency_ms: outcome.reference_fallback_latency_ms.unwrap_or(0.0),
        }
    } else {
        crate::plan_profile::ChargedReferenceFallback::default()
    };
    let observation_context = embedded_observation_context(plan_json)?;
    if observation_context.as_ref().is_some_and(|context| {
        plan_profiles.evidence_cohort_started_at_us(&context.key)
            != context.evidence_cohort_started_at_us
    }) {
        return Err("plan execution predates its readmission evidence cohort".to_string());
    }

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
    if !call_site_id.is_empty() && !cache_hit {
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
    // The legacy output distribution describes the returned provider response.
    // Plan economics, however, include every piece of physical inference work.
    let outcome = total_outcome;
    let plan_observation = observation_context
        .map(|context| {
            let runtime_version = observed_runtime_version(&context.runtime_version, &outcome);
            let update = PlanProfileUpdate {
                cost_evidence: outcome.cost_evidence(),
                key: context.key.clone(),
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
            };
            if cache_hit {
                // A cached model label is historical, not an observation of
                // what the provider's mutable alias currently resolves to.
                plan_profiles.observe(update)
            } else {
                record_provider_outcome_with_charge(
                    plan_profiles,
                    update,
                    &context,
                    reference_charge,
                )
            }
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

/// Driver-issued receipts for calls that actually ran in an ordered map.
/// Source requests remain transient; only content-free identities and metrics
/// enter PlanProfiles. Synthetic ParallelBranch peers are never inspected.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ObservedMapCall {
    position: crate::multi_call::CallPosition,
    source_call_json: String,
    plan_json: String,
    cost_source: crate::multi_call::InferenceCostSource,
    outcome: Outcome,
    #[serde(default)]
    scalar_observation_token: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ObservedMap {
    application_version: String,
    callback_version: String,
    item_count: u32,
    max_workers: u32,
    wall_latency_ms: f64,
    calls: Vec<ObservedMapCall>,
    #[serde(default)]
    comparison: Option<MapComparisonFingerprint>,
    #[serde(default)]
    comparison_requested: bool,
    #[serde(default)]
    execution_contract: Option<String>,
    #[serde(default)]
    comparison_version: Option<String>,
    #[serde(default)]
    provider_receipt_batch_id: Option<String>,
    #[serde(default)]
    graph_comparison_requested: bool,
    #[serde(default)]
    graph_preparation_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MapComparisonFingerprint {
    version: String,
    input_sha256: String,
    output_sha256: String,
}

pub const PREPARED_PROVIDER_DECLARATION_KEY: &str = "agentc_prepared_provider_batch";

fn validate_prepared_provider_call(call: &Call) -> Result<(), String> {
    let requirements =
        RequestRequirements::from_call(call).ok_or("prepared request contract is unsupported")?;
    crate::wiring::sdk_response_cache_key(call)
        .ok_or("prepared request fingerprint is incomplete")?;
    let declaration = call
        .parameters
        .extra
        .get(PREPARED_PROVIDER_DECLARATION_KEY)
        .and_then(serde_json::Value::as_object)
        .ok_or("prepared request declaration is missing")?;
    if declaration.len() != 5
        || declaration
            .get("version")
            .and_then(serde_json::Value::as_str)
            != Some(crate::multi_call::PREPARED_PROVIDER_BATCH_VERSION)
        || declaration
            .get("provider_protocol")
            .and_then(serde_json::Value::as_str)
            != Some(requirements.provider_protocol.as_str())
        || declaration
            .get("retry_limit")
            .and_then(serde_json::Value::as_u64)
            != Some(0)
        || declaration
            .get("application_callbacks")
            .and_then(serde_json::Value::as_bool)
            != Some(false)
        || declaration
            .get("remote_store")
            .and_then(serde_json::Value::as_bool)
            != Some(false)
        || requirements.streaming
        || requirements.image_input
    {
        return Err("prepared request is not a frozen independent stateless completion".into());
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreparedProviderBatchRequest {
    pub application_version: String,
    pub callback_version: String,
    pub calls: Vec<Call>,
}

pub struct PreparedProviderBatch {
    pub spec: crate::multi_call::MultiCallPlanSpec,
    pub runtime: PlanRuntimeVersion,
    pub plans: Vec<serde_json::Value>,
    pub aliases_resolved: bool,
    pub catalog_qualified: bool,
}

/// Bind original work without inventing catalog facts, rates or admission.
fn describe_accounting_original(call: &Call) -> Option<ResolvedPlanDescriptor> {
    let requirements = RequestRequirements::from_call(call)?;
    let version = "unqualified-original-accounting-v1";
    let site = CallSiteVersion::from_spec(&CallSiteVersionSpec {
        call_site_id: call.call_site_id.clone(),
        prompt_shape_version: prompt_shape_version(call, &requirements),
        provider_protocol: requirements.provider_protocol.clone(),
        tool_schema_version: tool_schema_version(call)?,
        application_config_version: Some(application_config_version(call)),
    })
    .ok()?;
    let spec = ExecutionPlanSpec {
        schema_version: EXECUTION_PLAN_SCHEMA_VERSION,
        provider_protocol: requirements.provider_protocol.clone(),
        requested_model_id: call.model.clone(),
        target_model_id: call.model.clone(),
        target_model_revision: Some(version.into()),
        rewrite_ordering: RewriteOrdering::Ordered,
        rewrites: vec![],
        output_budget: call.parameters.max_output_tokens,
        cache_policy: CachePolicy {
            stable_name: "provider-call".into(),
            implementation_version: version.into(),
            parameters: serde_json::json!({}),
        },
        validation_policy: ValidationPolicy {
            stable_name: "none".into(),
            implementation_version: "1".into(),
            parameters: serde_json::json!({}),
        },
    };
    Some(ResolvedPlanDescriptor {
        context: EmbeddedObservationContext {
            evidence_cohort_started_at_us: None,
            provider_alias: false,
            call_site_id: call.call_site_id.clone(),
            key: PlanProfileKey {
                call_site_version: site,
                execution_plan_id: spec.execution_plan_id().ok()?,
            },
            runtime_version: PlanRuntimeVersion {
                provider_protocol: requirements.provider_protocol,
                target_model_id: call.model.clone(),
                target_model_version: version.into(),
                price_table_version: version.into(),
            },
            divergence_threshold: 0.0,
            validation_contract: None,
            sampling_rate: None,
        },
        spec,
    })
}

/// Materialize original leaf plans without allocating probes or executing
/// application code. The trusted SDK driver owns the frozen request declaration.
pub fn prepare_provider_batch(
    catalog: &ModelCatalog,
    profiles: &PlanProfiles,
    request: PreparedProviderBatchRequest,
) -> Result<PreparedProviderBatch, String> {
    use crate::multi_call::{
        CallPosition, MultiCallBranch, MultiCallPlanSpec, MultiCallRuntimeBranch,
    };
    let item_count =
        u32::try_from(request.calls.len()).map_err(|_| "prepared request count overflow")?;
    let mut branches = Vec::new();
    let mut runtimes = Vec::new();
    let mut plans = Vec::new();
    let mut aliases_resolved = true;
    let mut catalog_qualified = true;
    for (item, call) in request.calls.iter().enumerate() {
        validate_prepared_provider_call(call)?;
        if call
            .parameters
            .extra
            .get(ROUTE_CONTEXT_KEY)
            .and_then(|context| context.get(crate::model_catalog::APPLICATION_VERSION_KEY))
            .and_then(|value| value.get("sha256"))
            .and_then(serde_json::Value::as_str)
            != Some(request.application_version.as_str())
        {
            return Err("prepared requests require one declared application revision".into());
        }
        let descriptor =
            if let Some(descriptor) = describe_plan(catalog, call, &Plan::PassThrough, 0.0) {
                descriptor
            } else {
                catalog_qualified = false;
                aliases_resolved = false;
                describe_accounting_original(call)
                    .ok_or("prepared request has no original accounting descriptor")?
            };
        let resolved = decision_profile(profiles, &descriptor.context);
        if descriptor.context.provider_alias && resolved.is_none() {
            aliases_resolved = false;
        }
        let runtime = resolved
            .map(|profile| profile.runtime_version)
            .unwrap_or_else(|| descriptor.context.runtime_version.clone());
        let position = CallPosition {
            item: item as u32,
            call: 0,
        };
        branches.push(MultiCallBranch {
            position,
            call_site_version: descriptor.context.key.call_site_version.clone(),
            plan: descriptor.spec,
        });
        runtimes.push(MultiCallRuntimeBranch { position, runtime });
        let plan = encode_observation_context(PASS_THROUGH_JSON.into(), &descriptor.context);
        plans.push(serde_json::from_str(&plan).map_err(|error| error.to_string())?);
    }
    let spec = MultiCallPlanSpec {
        application_version: request.application_version,
        callback_version: request.callback_version,
        item_count,
        max_workers: 1,
        branches,
        comparison_version: Some(crate::multi_call::EXACT_MAP_COMPARISON_VERSION.into()),
        execution_contract: Some(crate::multi_call::PREPARED_PROVIDER_BATCH_VERSION.into()),
    };
    let runtime = crate::multi_call::profile_runtime(&spec, &runtimes)?;
    Ok(PreparedProviderBatch {
        spec,
        runtime,
        plans,
        aliases_resolved,
        catalog_qualified,
    })
}

pub fn optimize_observe_multi_call(
    catalog: &ModelCatalog,
    profiles: &PlanProfiles,
    receipt_json: &str,
) -> Result<String, String> {
    observe_multi_call_receipt(catalog, profiles, None, receipt_json)
}

pub fn optimize_observe_multi_call_with_comparison(
    catalog: &ModelCatalog,
    profiles: &PlanProfiles,
    conn: &mut Connection,
    receipt_json: &str,
) -> Result<String, String> {
    observe_multi_call_receipt(catalog, profiles, Some(conn), receipt_json)
}

fn observe_multi_call_receipt(
    catalog: &ModelCatalog,
    profiles: &PlanProfiles,
    mut conn: Option<&mut Connection>,
    receipt_json: &str,
) -> Result<String, String> {
    use crate::multi_call::{
        observe_multi_call, MultiCallBranch, MultiCallOutcome, MultiCallPlanSpec,
        MultiCallRuntimeBranch,
    };
    let receipt: ObservedMap =
        serde_json::from_str(receipt_json).map_err(|error| error.to_string())?;
    let graph_input_sha256 = content_hash(&canonical_json(
        &serde_json::from_str::<serde_json::Value>(receipt_json).map_err(|e| e.to_string())?,
    ));
    if let Some(database) = conn.as_deref_mut() {
        crate::provider_publication::recover_pending(database, profiles)
            .map_err(|error| error.to_string())?;
    }
    if receipt.comparison_requested && conn.is_none() {
        return Err("map acquisition needs a durable store".into());
    }
    if let Some(comparison) = &receipt.comparison {
        let version = if receipt.graph_comparison_requested {
            crate::provider_graph::COMPARISON_VERSION
        } else {
            crate::multi_call::EXACT_MAP_COMPARISON_VERSION
        };
        if conn.is_none() || comparison.version != version {
            return Err("map comparison needs a supported contract and durable store".into());
        }
        crate::execution_plan::ExecutionPlanId::parse(&comparison.input_sha256)
            .map_err(|error| error.to_string())?;
        crate::execution_plan::ExecutionPlanId::parse(&comparison.output_sha256)
            .map_err(|error| error.to_string())?;
    }
    let mut branches = Vec::new();
    let mut versions = Vec::new();
    let mut outcomes = Vec::new();
    let mut request_fingerprints = Vec::new();
    let mut physical_requests = std::collections::BTreeMap::new();
    let mut scalar_provenance = Vec::new();
    let mut realized_alternative = false;
    let mut graph_sources = Vec::new();
    for measured in receipt.calls {
        let mut original: Call =
            serde_json::from_str(&measured.source_call_json).map_err(|error| error.to_string())?;
        if receipt.execution_contract.is_some() {
            validate_prepared_provider_call(&original)?;
            physical_requests.insert(
                measured.position,
                crate::provider_receipts::request_fingerprint(&original)
                    .ok_or("prepared request fingerprint is unavailable")?,
            );
        }
        if let Some(hash) = crate::provider_receipts::request_fingerprint(&original) {
            physical_requests.insert(measured.position, hash);
        }
        if receipt.comparison.is_some() && !receipt.graph_comparison_requested {
            let key = crate::wiring::sdk_response_cache_key(&original)
                .ok_or("map comparison request lacks a complete SDK request fingerprint")?;
            request_fingerprints.push((
                measured.position,
                serde_json::json!({
                    "prompt_hash":key.prompt_hash,"parameters_hash":key.parameters_hash,
                    "model":key.model,"call_site_id":key.call_site_id,
                }),
            ));
        }
        let declared = original
            .parameters
            .extra
            .get(ROUTE_CONTEXT_KEY)
            .and_then(|context| context.get(crate::model_catalog::APPLICATION_VERSION_KEY));
        if declared
            .and_then(|value| value.get("sha256"))
            .and_then(serde_json::Value::as_str)
            != Some(receipt.application_version.as_str())
        {
            return Err("map call does not share its declared application version".into());
        }
        let value: serde_json::Value =
            serde_json::from_str(&measured.plan_json).map_err(|error| error.to_string())?;
        let plan: Plan =
            serde_json::from_value(value.clone()).map_err(|error| error.to_string())?;
        let alternative = matches!(plan, Plan::Rewritten { .. } | Plan::Composed { .. });
        if matches!(plan, Plan::Cached { .. })
            || value.get(EXPLORATION_CONTEXT_KEY).is_some()
            || alternative && (conn.is_none() || receipt.execution_contract.is_some())
        {
            return Err(
                "map alternatives and probes require explicit physical call receipts".into(),
            );
        }
        realized_alternative |= alternative;
        let scalar_observation = measured
            .scalar_observation_token
            .as_deref()
            .map(serde_json::from_str::<OpaqueObservationToken>)
            .transpose()
            .map_err(|error| error.to_string())?
            .and_then(|token| token.plan_observation);
        if let Some(token) = &scalar_observation {
            let profile = profiles
                .get_for_reporting(token.key())
                .ok_or("original scalar observation is unavailable")?;
            let sample = profile
                .retained_samples()
                .iter()
                .find(|sample| sample.sequence == token.sequence())
                .ok_or("original scalar observation has retired")?;
            if sample.runtime_version != *token.runtime_version()
                || sample.input_tokens != measured.outcome.input_tokens
                || sample.output_tokens != measured.outcome.output_tokens
                || sample.cost_usd != measured.outcome.cost_usd
                || sample.latency_ms != measured.outcome.latency_ms
            {
                return Err("scalar observation differs from original batch outcome".into());
            }
        }
        scalar_provenance.push(crate::provider_publication::ScalarProvenance {
            position: measured.position,
            cost_source: measured.cost_source.clone(),
            observation: scalar_observation,
        });
        let mut outcome = measured.outcome;
        if matches!(plan, Plan::Parallel { .. }) {
            if !outcome.dispatch_fallback
                || outcome.dispatch_fallback_reason.as_deref() != Some("parallel_observe_only")
            {
                return Err("map parallel certificate was not executed observe-only".into());
            }
            // This is the original request, not an attempted optimized call
            // followed by a retry. Preserve ordinary fallback rejection.
            outcome.dispatch_fallback = false;
            outcome.dispatch_fallback_reason = None;
            outcome.reference_fallback_latency_ms = None;
        }
        let descriptor = if alternative || receipt.graph_comparison_requested {
            let issued = embedded_observation_context(&measured.plan_json)?
                .ok_or("graph alternative has no issued observation context")?;
            // The native planner snapshots sampling before issuing a plan.
            // Python's transient source predates that native-only field; use
            // the issued snapshot rather than today's sampling configuration.
            if let Some(rate) = issued.sampling_rate {
                original = serde_json::from_str(&bind_issued_sampling_policy(
                    &serde_json::to_string(&original).map_err(|e| e.to_string())?,
                    rate,
                ))
                .map_err(|e| e.to_string())?;
            }
            let described = if alternative {
                &plan
            } else {
                &Plan::PassThrough
            };
            let descriptor =
                describe_plan(catalog, &original, described, issued.divergence_threshold)
                    .ok_or("graph alternative has no supported native descriptor")?;
            if issued.key != descriptor.context.key {
                return Err("graph alternative identity differs from its issued plan".into());
            }
            if issued.runtime_version != descriptor.context.runtime_version {
                return Err("graph alternative runtime differs from its issued plan".into());
            }
            if scalar_provenance
                .last()
                .and_then(|p| p.observation.as_ref())
                .is_none_or(|token| token.key() != &issued.key)
            {
                return Err("graph alternative differs from its issued scalar observation".into());
            }
            descriptor
        } else {
            describe_plan(catalog, &original, &Plan::PassThrough, 0.0)
                .ok_or("map original call has no supported native descriptor")?
        };
        let planned = &descriptor.context.runtime_version;
        if outcome.provider_protocol.as_deref() != Some(planned.provider_protocol.as_str())
            || outcome
                .target_model_id
                .as_deref()
                .is_some_and(|model| model != planned.target_model_id)
            || outcome
                .price_table_version
                .as_deref()
                .is_some_and(|price| price != planned.price_table_version)
            || outcome
                .executed_model_id
                .as_deref()
                .is_none_or(|model| model.trim().is_empty())
        {
            return Err("map outcome lacks matching protocol, model, or price metadata".into());
        }
        versions.push(MultiCallRuntimeBranch {
            position: measured.position,
            runtime: observed_runtime_version(planned, &outcome),
        });
        branches.push(MultiCallBranch {
            position: measured.position,
            call_site_version: descriptor.context.key.call_site_version,
            plan: descriptor.spec,
        });
        outcomes.push(MultiCallOutcome {
            position: measured.position,
            cost_source: measured.cost_source,
            outcome,
        });
        graph_sources.push((measured.position, original));
    }
    let spec = MultiCallPlanSpec {
        application_version: receipt.application_version,
        callback_version: receipt.callback_version,
        item_count: receipt.item_count,
        max_workers: receipt.max_workers,
        branches,
        comparison_version: receipt.comparison_version.clone().or_else(|| {
            receipt
                .comparison
                .as_ref()
                .map(|comparison| comparison.version.clone())
        }),
        execution_contract: receipt.execution_contract,
    };
    if realized_alternative || receipt.graph_comparison_requested {
        if spec.execution_contract.is_some() {
            return Err("prepared original batches cannot use a graph comparison contract".into());
        }
        let mut graph_calls = spec;
        // Original-map pairing cannot attest a different model/rewrite graph.
        // Retain realized economics now; whole-result pairing has its own contract.
        graph_calls.comparison_version = None;
        let mut edges = std::collections::BTreeSet::new();
        let mut spans = std::collections::BTreeMap::new();
        for (position, source) in &graph_sources {
            if spans.insert(source.span_id, *position).is_some() {
                return Err("graph source spans are duplicated".into());
            }
            if position.call > 0 {
                edges.insert(crate::provider_graph::Dependency {
                    producer: crate::multi_call::CallPosition {
                        item: position.item,
                        call: position.call - 1,
                    },
                    consumer: *position,
                });
            }
        }
        for (position, source) in &graph_sources {
            for dependency in &source.input_deps {
                if let crate::DepSource::LlmOutput { span_id } = dependency {
                    if let Some(producer) = spans.get(span_id) {
                        edges.insert(crate::provider_graph::Dependency {
                            producer: *producer,
                            consumer: *position,
                        });
                    }
                }
            }
        }
        let graph = crate::provider_graph::Spec {
            version: crate::provider_graph::VERSION.into(),
            results: crate::provider_graph::Results::CallbackItems {
                items: (0..graph_calls.item_count).collect(),
            },
            calls: graph_calls,
            dependencies: edges.into_iter().collect(),
        };
        let mut source_contracts = Vec::new();
        if receipt.graph_comparison_requested && receipt.comparison.is_some() {
            for (position, source) in &graph_sources {
                // Frozen model/rewrite policy is the treatment being compared.
                // All original provider controls, schemas and transport namespace
                // remain in the native content-free request key.
                let mut original = source.clone();
                if let Some(context) = original
                    .parameters
                    .extra
                    .get_mut(ROUTE_CONTEXT_KEY)
                    .and_then(serde_json::Value::as_object_mut)
                {
                    context.remove(crate::calibrated_policy::CONTEXT_KEY);
                }
                let controls = crate::wiring::sdk_response_cache_key(&original)
                    .ok_or("graph original request controls are unsupported")?;
                source_contracts.push(crate::provider_graph::SourceContract {position:*position,sha256:content_hash(&canonical_json(&serde_json::json!({
                    "version":"provider-graph-source-controls-v1","model":original.model,"parameters":controls.parameters_hash,
                })))});
            }
        }
        let database = conn
            .as_deref_mut()
            .ok_or("graph provider journal is unavailable")?;
        let batch_id = receipt
            .provider_receipt_batch_id
            .as_deref()
            .ok_or("graph provider batch identity is unavailable")?;
        let execution = crate::provider_graph::publish(
            database,
            profiles,
            crate::provider_graph::PublishRequest {
                batch_id,
                spec: &graph,
                versions: &versions,
                outcomes: &outcomes,
                requests: &physical_requests,
                wall_latency_ms: receipt.wall_latency_ms,
                input_sha256: &graph_input_sha256,
                observed_at_us: std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_err(|e| e.to_string())?
                    .as_micros()
                    .min(i64::MAX as u128) as i64,
                comparison: if receipt.graph_comparison_requested {
                    receipt
                        .comparison
                        .as_ref()
                        .map(|c| crate::provider_graph::Comparison {
                            version: c.version.clone(),
                            input_sha256: c.input_sha256.clone(),
                            output_sha256: c.output_sha256.clone(),
                        })
                } else {
                    None
                },
                source_contracts: &source_contracts,
                preparation_id: receipt.graph_preparation_id.as_deref(),
            },
        )
        .map_err(|e| e.to_string())?;
        return serde_json::to_string(&serde_json::json!({
            "status":if execution.cost_source.is_measured() { "recorded" } else { "pending_billing" }, "admission":"observe_only", "key":execution.key,
            "runtime_version":execution.runtime,"totals":execution.totals,
            "cost_source":execution.cost_source, "graph_execution_id":execution.execution_id,
            "comparison_token":null,
            "graph_comparison_token": execution.comparison.as_ref().map(|_|&execution.execution_id),
            "comparison_reason": (receipt.comparison_requested && !receipt.graph_comparison_requested).then_some("whole_graph_comparison_contract_required"),
        })).map_err(|e|e.to_string());
    }
    if spec.execution_contract.is_some() {
        let id = receipt
            .provider_receipt_batch_id
            .as_deref()
            .ok_or("prepared batch provider journal is required")?;
        let database = conn
            .as_deref()
            .ok_or("prepared batch provider journal store is unavailable")?;
        let billing_pending = outcomes.iter().any(|outcome| {
            outcome.cost_source != crate::multi_call::InferenceCostSource::ProviderReported
        });
        let totals = if billing_pending {
            crate::multi_call::aggregate_original_evidence(
                &spec,
                &outcomes,
                receipt.wall_latency_ms,
            )?
        } else {
            crate::multi_call::aggregate_outcomes(&spec, &outcomes, receipt.wall_latency_ms)?
        };
        crate::provider_receipts::verify_original_evidence(
            database,
            id,
            &spec,
            &totals,
            &physical_requests,
            !billing_pending,
        )
        .map_err(|error| error.to_string())?;
        crate::provider_receipts::verify_observed_outcomes(database, id, &outcomes)
            .map_err(|error| error.to_string())?;
        let runtime_version = crate::multi_call::profile_runtime(&spec, &versions)?;
        let mut reference = spec.clone();
        reference.max_workers = 1;
        request_fingerprints.sort_by_key(|(position, _)| *position);
        let comparison = receipt
            .comparison
            .as_ref()
            .map(|comparison| {
                Ok::<_, String>(crate::provider_publication::ComparisonInput {
                    reference_plan_id: reference.profile_key()?.execution_plan_id,
                    max_workers: spec.max_workers,
                    input_fingerprint: content_hash(&canonical_json(&serde_json::json!({
                        "version": comparison.version, "map_inputs": comparison.input_sha256,
                        "requests": request_fingerprints,
                    }))),
                    output_fingerprint: comparison.output_sha256.clone(),
                })
            })
            .transpose()?;
        let database = conn
            .as_deref_mut()
            .ok_or("provider receipt store is unavailable")?;
        // Commit the predecessor before reserving recovery input. The batch
        // publication itself stages only its key and cannot roll back peers.
        profiles
            .flush_dirty(database)
            .map_err(|error| error.to_string())?;
        let key = spec.profile_key()?;
        let original_graph = crate::provider_publication::DeferredEvidence {
            spec,
            runtimes: versions,
            request_fingerprints: physical_requests.into_iter().collect(),
            scalar_provenance,
        };
        let (deferred_evidence, original_evidence) = if billing_pending {
            (Some(original_graph), None)
        } else {
            (None, Some(original_graph))
        };
        let input = crate::provider_publication::PublicationInput {
            version: "provider-profile-publication-v1".into(),
            journal_sha256: crate::provider_publication::journal_fingerprint(database, id)
                .map_err(|error| error.to_string())?,
            predecessor: profiles.publication_boundary(&key),
            retained_window_size: profiles.retained_window_size(),
            update: crate::plan_profile::PlanProfileUpdate {
                cost_evidence: crate::cost_evidence::CostEvidence {
                    source: if billing_pending {
                        crate::cost_evidence::CostSource::Estimated
                    } else {
                        crate::cost_evidence::CostSource::ProviderReported
                    },
                    physical_invocations: outcomes
                        .iter()
                        .flat_map(|outcome| outcome.outcome.physical_invocations.iter().cloned())
                        .collect(),
                },
                key,
                runtime_version,
                input_tokens: totals.input_tokens,
                output_tokens: totals.output_tokens,
                latency_ms: totals.wall_latency_ms,
                cost_usd: totals.cost_usd,
                output_is_structured: outcomes
                    .iter()
                    .all(|value| value.outcome.output_is_structured),
                output_is_short: totals.output_tokens <= 128,
                dispatch_fallback: false,
                divergence: None,
                observed_at_us: Some(
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map_err(|error| error.to_string())?
                        .as_micros()
                        .min(i64::MAX as u128) as i64,
                ),
            },
            totals,
            charge_acquisition: receipt.comparison_requested || receipt.comparison.is_some(),
            comparison,
            deferred_evidence,
            original_evidence,
        };
        if let Some(result) =
            crate::provider_publication::reserve(database, id, receipt_json, &input)
                .map_err(|error| error.to_string())?
        {
            return Ok(result);
        }
        return crate::provider_publication::publish(database, profiles, id)
            .map_err(|error| error.to_string());
    }
    let (observation, totals) = observe_multi_call(
        profiles,
        &spec,
        &versions,
        &outcomes,
        receipt.wall_latency_ms,
        None,
    )?;
    if receipt.comparison_requested || receipt.comparison.is_some() {
        let database = conn
            .as_deref_mut()
            .ok_or("map acquisition store is unavailable")?;
        profiles
            .flush_dirty(database)
            .map_err(|error| error.to_string())?;
        crate::multi_call_comparison::record_acquisition(database, profiles, &observation)
            .map_err(|error| error.to_string())?;
    }
    let comparison_token = if let Some(comparison) = receipt.comparison {
        request_fingerprints.sort_by_key(|(position, _)| *position);
        let input_fingerprint = content_hash(&canonical_json(&serde_json::json!({
            "version": comparison.version, "map_inputs": comparison.input_sha256,
            "requests": request_fingerprints,
        })));
        let conn = conn.ok_or("map comparison store is unavailable")?;
        profiles
            .flush_dirty(conn)
            .map_err(|error| error.to_string())?;
        Some(
            crate::multi_call_comparison::register_execution(
                conn,
                profiles,
                &spec,
                &observation,
                input_fingerprint,
                comparison.output_sha256,
            )
            .map_err(|error| error.to_string())?,
        )
    } else {
        None
    };
    let result = serde_json::to_string(&serde_json::json!({
        "status": "recorded", "admission": "observe_only",
        "key": observation.key(), "runtime_version": observation.runtime_version(),
        "totals": totals,
        "comparison_token": comparison_token,
    }))
    .map_err(|error| error.to_string())?;
    Ok(result)
}

/// Issue one bounded discovery instruction; it never selects a visible candidate.
pub fn discover_graph_node(
    opt: &Optimizer,
    catalog: &ModelCatalog,
    source_json: &str,
    reference: bool,
) -> Result<String, String> {
    let bound = bind_sampling_policy(source_json, opt.config().shadow_rate);
    let source: Call = serde_json::from_str(&bound).map_err(|e| e.to_string())?;
    let mut plans = if reference {
        vec![Plan::PassThrough]
    } else {
        opt.candidate_plans(&source, catalog)
    };
    let savings = |p: &Plan| match p {
        Plan::Rewritten {
            projected_savings_usd,
            ..
        } => *projected_savings_usd,
        Plan::Composed {
            net_savings_usd, ..
        } => *net_savings_usd,
        _ => 0.0,
    };
    plans.sort_by(|a, b| savings(b).total_cmp(&savings(a)));
    for plan in plans {
        if matches!(plan, Plan::Cached { .. } | Plan::Parallel { .. }) {
            continue;
        }
        let Some(d) = describe_plan(
            catalog,
            &source,
            &plan,
            opt.divergence_threshold_for_plan(&plan),
        ) else {
            continue;
        };
        return Ok(encode_observation_context(
            serde_json::to_string(&plan).map_err(|e| e.to_string())?,
            &d.context,
        ));
    }
    Err("discovery has no supported native provider instruction".into())
}

/// Materialize an exact frozen template without invoking the ordinary selector
/// or allocating probes. The preparation caller owns workflow authorization.
pub fn materialize_graph_node(
    opt: &Optimizer,
    catalog: &ModelCatalog,
    template: &crate::provider_graph_preparation::Template,
    position: crate::multi_call::CallPosition,
    source_json: &str,
    observation_only: bool,
) -> Result<String, String> {
    let desired = template
        .spec
        .calls
        .branches
        .iter()
        .find(|b| b.position == position)
        .ok_or("template has no such logical node")?;
    let runtime = &template
        .runtime_branches
        .iter()
        .find(|b| b.position == position)
        .ok_or("template runtime is missing")?
        .runtime;
    let mut source: Call = serde_json::from_str(source_json).map_err(|e| e.to_string())?;
    let alternative =
        desired.plan.target_model_id != source.model || !desired.plan.rewrites.is_empty();
    if alternative && (observation_only || !opt.config().enabled) {
        return Err("excluded SDK calls cannot execute an alternative graph template".into());
    }
    if let Some(rate) = desired
        .plan
        .validation_policy
        .parameters
        .get("sampling_rate")
        .and_then(serde_json::Value::as_f64)
    {
        source = serde_json::from_str(&bind_issued_sampling_policy(
            &serde_json::to_string(&source).map_err(|e| e.to_string())?,
            rate,
        ))
        .map_err(|e| e.to_string())?;
    }
    let threshold = desired
        .plan
        .validation_policy
        .parameters
        .get("divergence_threshold")
        .and_then(|v| v.get("value"))
        .and_then(serde_json::Value::as_f64)
        .unwrap_or(0.0);
    let mut plans = vec![Plan::PassThrough];
    if alternative {
        plans.extend(opt.candidate_plans(&source, catalog));
        if let Some(mode) = source
            .parameters
            .extra
            .get(ROUTE_CONTEXT_KEY)
            .and_then(|v| v.get(crate::calibrated_policy::CONTEXT_KEY))
            .and_then(|v| v.get("mode"))
        {
            if let Ok(mode) = serde_json::from_value(mode.clone()) {
                if let Ok(compiled) = crate::calibrated_policy::compile(catalog, &source, mode) {
                    plans.push(compiled.plan);
                }
            }
        }
        if desired.plan.rewrites.is_empty() {
            if let Some(requirements) = RequestRequirements::from_call(&source) {
                if let Some(target) = catalog.resolve(
                    &requirements.provider_protocol,
                    &requirements.provider_namespace,
                    &desired.plan.target_model_id,
                ) {
                    if let Ok(metadata) = catalog.routed_target(&source, target) {
                        let mut routed = source.clone();
                        if metadata.annotate_call(&mut routed).is_ok() {
                            routed.model = desired.plan.target_model_id.clone();
                            let mut plan = Plan::Rewritten {
                                rule: "ModelDowngrade".into(),
                                call: routed,
                                projected_savings_usd: 0.0,
                            };
                            if desired
                                .plan
                                .validation_policy
                                .parameters
                                .get("rule_configuration")
                                .is_some()
                            {
                                opt.bind_rule_identities(&mut plan);
                            }
                            plans.push(plan);
                        }
                    }
                }
            }
        }
    }
    for plan in plans {
        let Some(descriptor) = describe_plan(catalog, &source, &plan, threshold) else {
            continue;
        };
        if descriptor.spec != desired.plan
            || descriptor.context.key.call_site_version != desired.call_site_version
            || descriptor.context.runtime_version != *runtime
        {
            continue;
        }
        let raw = encode_observation_context(
            serde_json::to_string(&plan).map_err(|e| e.to_string())?,
            &descriptor.context,
        );
        let node = prepared_graph_node(catalog, position, source_json, &raw)?;
        crate::provider_graph_preparation::validate_template_node(template, &node)
            .map_err(|e| e.to_string())?;
        return Ok(raw);
    }
    Err("current SDK request cannot materialize the exact frozen graph plan and runtime".into())
}

/// Reconstruct a single issued node at the SDK seam, before any provider call.
pub fn prepared_graph_node(
    catalog: &ModelCatalog,
    position: crate::multi_call::CallPosition,
    source_json: &str,
    plan_json: &str,
) -> Result<crate::provider_graph_preparation::Node, String> {
    let mut source: Call = serde_json::from_str(source_json).map_err(|e| e.to_string())?;
    let plan: Plan = serde_json::from_str(plan_json).map_err(|e| e.to_string())?;
    let value: serde_json::Value = serde_json::from_str(plan_json).map_err(|e| e.to_string())?;
    if matches!(plan, Plan::Cached { .. } | Plan::Parallel { .. })
        || value.get(EXPLORATION_CONTEXT_KEY).is_some()
    {
        return Err("prepared graph node contains unsupported dispatch roles".into());
    }
    let issued = embedded_observation_context(plan_json)?
        .ok_or("prepared graph node has no native identity")?;
    if let Some(rate) = issued.sampling_rate {
        source = serde_json::from_str(&bind_issued_sampling_policy(
            &serde_json::to_string(&source).map_err(|e| e.to_string())?,
            rate,
        ))
        .map_err(|e| e.to_string())?;
    }
    let descriptor = describe_plan(catalog, &source, &plan, issued.divergence_threshold)
        .ok_or("prepared graph node has no supported descriptor")?;
    if descriptor.context.key != issued.key
        || descriptor.context.runtime_version != issued.runtime_version
    {
        return Err("prepared graph node differs from its native issue".into());
    }
    let logical = crate::provider_receipts::request_fingerprint(&source)
        .ok_or("prepared graph original request is unsupported")?;
    if let Some(context) = source
        .parameters
        .extra
        .get_mut(ROUTE_CONTEXT_KEY)
        .and_then(serde_json::Value::as_object_mut)
    {
        context.remove(crate::calibrated_policy::CONTEXT_KEY);
    }
    let controls = crate::wiring::sdk_response_cache_key(&source)
        .ok_or("prepared graph original controls are unsupported")?;
    let source_contract_sha256 = content_hash(&canonical_json(
        &serde_json::json!({"version":"provider-graph-source-controls-v1","model":source.model,"parameters":controls.parameters_hash}),
    ));
    Ok(crate::provider_graph_preparation::Node {
        position,
        key: issued.key,
        plan: descriptor.spec,
        runtime: issued.runtime_version,
        source_contract_sha256,
        logical_request_sha256: logical,
    })
}

/// Persist a pre-release rejection against the exact issued plan. A verdict
/// is not a divergence comparison and cannot mutate paired profile evidence.
pub fn record_output_validation_rejection(
    profiles: &PlanProfiles,
    guard: &PlanGuard,
    conn: &mut Connection,
    plan_json: &str,
    contract_json: &str,
    execution_id: &str,
    observed_at_us: i64,
) -> Result<(), String> {
    let plan: Plan = serde_json::from_str(plan_json).map_err(|e| e.to_string())?;
    if matches!(plan, Plan::PassThrough | Plan::Parallel { .. }) {
        return Err("validation rejection requires a scalar alternative".into());
    }
    let context = embedded_observation_context(plan_json)?
        .ok_or("validation rejection is missing native plan context")?;
    let contract: serde_json::Value =
        serde_json::from_str(contract_json).map_err(|e| e.to_string())?;
    if context.validation_contract.as_ref() != Some(&contract)
        || contract.get("version").and_then(serde_json::Value::as_str)
            != Some(crate::model_catalog::OUTPUT_VALIDATION_VERSION)
    {
        return Err("validation rejection contract does not match its issued plan".into());
    }
    if profiles.evidence_cohort_started_at_us(&context.key) != context.evidence_cohort_started_at_us
    {
        return Err("validation rejection predates the current evidence cohort".into());
    }
    guard
        .record_validation_rejection(
            &context.key,
            &context.runtime_version,
            context.divergence_threshold,
            execution_id,
            observed_at_us,
        )
        .map_err(|e| e.to_string())?;
    guard.flush_dirty(conn).map_err(|e| e.to_string())?;
    Ok(())
}

fn observed_runtime_version(planned: &PlanRuntimeVersion, outcome: &Outcome) -> PlanRuntimeVersion {
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

/// Schema of the [`propose_rule`] result envelope.
pub const RULE_PROPOSAL_SCHEMA_VERSION: u16 = 1;

/// Measurement-only: return one named rule's proposed rewrite of `call_json`
/// using the live call-site profile, via [`Optimizer::propose_single_rule`].
/// No admission, hot threshold, guard, audit row or observation context is
/// involved, and nothing is recorded; the result cannot be executed as a plan.
///
/// Result (always valid JSON, panics included):
/// `{"schema_version":1,"status":"proposed","rule":..,"call":<Call>,
///   "retained_message_indices":[..]|null,"profile":{..}}` or
/// `{"schema_version":1,"status":"not_applicable","rule":..,"reason":..}`.
/// `retained_message_indices` lists the original index of each message in the
/// rewritten call, so callers can remove native messages by identity.
pub fn propose_rule(opt: &Optimizer, call_json: &str, rule_name: &str) -> String {
    let refuse = |reason: &str| {
        serde_json::json!({
            "schema_version": RULE_PROPOSAL_SCHEMA_VERSION,
            "status": "not_applicable",
            "rule": rule_name,
            "reason": reason,
        })
        .to_string()
    };
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let Ok(mut call) = serde_json::from_str::<Call>(call_json) else {
            return refuse("invalid_call");
        };
        let count = call.messages.len();
        if !call.parameters.extra.is_object() && !call.parameters.extra.is_null() {
            return refuse("invalid_call");
        }
        call.parameters.extra[crate::dag::MESSAGE_INDEX_KEY] = (0..count).collect();
        let (mut rewritten, profile) = match opt.propose_single_rule(&call, rule_name) {
            Ok(result) => result,
            Err(reason) => return refuse(reason),
        };
        let indices = rewritten
            .parameters
            .extra
            .as_object_mut()
            .and_then(|extra| extra.remove(crate::dag::MESSAGE_INDEX_KEY))
            .and_then(|value| serde_json::from_value::<Vec<usize>>(value).ok())
            .filter(|indices| {
                indices.len() == rewritten.messages.len() && indices.iter().all(|i| *i < count)
            });
        serde_json::to_string(&serde_json::json!({
            "schema_version": RULE_PROPOSAL_SCHEMA_VERSION,
            "status": "proposed",
            "rule": rule_name,
            "call": rewritten,
            "retained_message_indices": indices,
            "profile": {
                "call_site_id": profile.call_site_id,
                "n_observations": profile.n_observations,
                "window_observations": profile.window_observations,
                "output_token_p95": profile.output_token_p95,
                "output_token_p99": profile.output_token_p99,
            },
        }))
        .unwrap_or_else(|_| refuse("serialization_failed"))
    }))
    .unwrap_or_else(|_| refuse("panic"))
}

#[cfg(test)]
mod propose_rule_tests {
    use std::sync::Arc;

    use super::*;
    use crate::config::OptimizerConfig;
    use crate::cost_model::CostModelUpdate;
    use crate::rules::{ContextCompressRule, OutputBudgetRule};

    fn optimizer(observations: u32) -> Optimizer {
        let cost_model = Arc::new(CostModel::new());
        for _ in 0..observations {
            cost_model.observe(CostModelUpdate {
                call_site_id: "site".into(),
                input_tokens: 1000,
                output_tokens: 100,
                latency_ms: 10.0,
                cost_usd: 0.01,
                output_is_structured: false,
                output_is_short: true,
                now_us: Some(0),
            });
        }
        Optimizer::new(
            cost_model,
            vec![
                Box::new(OutputBudgetRule::default()),
                Box::new(ContextCompressRule::default()),
            ],
            OptimizerConfig {
                // The study entry point must ignore these production gates.
                enabled: false,
                hot_threshold: 1_000,
                ..OptimizerConfig::default()
            },
        )
    }

    fn call(messages: serde_json::Value, extra: serde_json::Value) -> String {
        serde_json::json!({
            "call_site_id": "site", "trace_id": "00".repeat(16), "span_id": "00".repeat(8),
            "model": "gpt-4o", "messages": messages, "parameters": {"extra": extra},
        })
        .to_string()
    }

    fn parse(json: &str) -> serde_json::Value {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn output_budget_proposes_cap_without_hot_threshold_or_master_switch() {
        let out = parse(&propose_rule(
            &optimizer(1),
            &call(
                serde_json::json!([{"role": "user", "content": "hi"}]),
                serde_json::Value::Null,
            ),
            "OutputBudget",
        ));
        assert_eq!(out["status"], "proposed", "{out}");
        let cap = out["call"]["parameters"]["max_output_tokens"]
            .as_u64()
            .unwrap();
        assert!((120..=121).contains(&cap), "cap={cap}");
        assert_eq!(out["retained_message_indices"], serde_json::json!([0]));
        assert!(out["call"]["parameters"]["extra"]
            .get(crate::dag::MESSAGE_INDEX_KEY)
            .is_none());
        assert_eq!(out["profile"]["window_observations"], 1);
    }

    #[test]
    fn cold_profile_is_not_applicable() {
        let out = parse(&propose_rule(
            &optimizer(0),
            &call(
                serde_json::json!([{"role": "user", "content": "hi"}]),
                serde_json::Value::Null,
            ),
            "OutputBudget",
        ));
        assert_eq!(out["status"], "not_applicable");
        assert_eq!(out["reason"], "rule_does_not_apply");
    }

    #[test]
    fn unknown_rule_and_invalid_call_are_refused() {
        let opt = optimizer(1);
        assert_eq!(
            parse(&propose_rule(&opt, "{}", "OutputBudget"))["reason"],
            "invalid_call"
        );
        let valid = call(serde_json::json!([]), serde_json::Value::Null);
        assert_eq!(
            parse(&propose_rule(&opt, &valid, "Nope"))["reason"],
            "rule_not_registered"
        );
    }

    #[test]
    fn context_compress_reports_retained_original_indices_for_opaque_messages() {
        let big = "lorem ipsum ".repeat(1_000);
        let messages = serde_json::json!([
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": big},
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": "recent"},
        ]);
        let extra = serde_json::json!({
            "attention_scores": [1.0, 1.0, 0.0, 0.0, 0.0, 0.0],
            "agentc_native_messages_opaque": true,
        });
        let out = parse(&propose_rule(
            &optimizer(0),
            &call(messages, extra),
            "ContextCompress",
        ));
        assert_eq!(out["status"], "proposed", "{out}");
        assert_eq!(
            out["retained_message_indices"],
            serde_json::json!([0, 1, 4, 5])
        );
        assert_eq!(out["call"]["messages"].as_array().unwrap().len(), 4);
        assert_eq!(
            out["call"]["parameters"]["extra"]["attention_scores"],
            serde_json::json!([1.0, 1.0, 0.0, 0.0])
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::budget::Budget;
    use crate::config::OptimizerConfig;
    use crate::cost_model::CostModelUpdate;
    use crate::dag::{Message, Parameters};
    use crate::model_catalog::{default_model_catalog, OPENAI_CHAT_COMPLETIONS_PROTOCOL};
    use crate::planner::{CostDriver, Proposal, RewriteRule};
    use crate::rules::ModelDowngradeRule;
    use crate::schema::ensure_cost_model_schema;
    use rusqlite::Connection;

    fn empty_optimizer() -> Optimizer {
        Optimizer::empty(Arc::new(CostModel::new()), OptimizerConfig::default())
    }

    fn materialization_template(
        source: &Call,
        plan: &Plan,
        catalog: &ModelCatalog,
    ) -> crate::provider_graph_preparation::Template {
        let d = describe_plan(catalog, source, plan, 0.05).unwrap();
        let position = crate::multi_call::CallPosition { item: 0, call: 0 };
        let raw = encode_observation_context(serde_json::to_string(plan).unwrap(), &d.context);
        let node = prepared_graph_node(
            catalog,
            position,
            &serde_json::to_string(source).unwrap(),
            &raw,
        )
        .unwrap();
        crate::provider_graph_preparation::Template {
            execution_id: "a".repeat(64),
            head_sha256: "b".repeat(64),
            spec: crate::provider_graph::Spec {
                version: crate::provider_graph::VERSION.into(),
                calls: crate::multi_call::MultiCallPlanSpec {
                    application_version: "c".repeat(64),
                    callback_version: "materialization-test-v1".into(),
                    item_count: 1,
                    max_workers: 1,
                    branches: vec![crate::multi_call::MultiCallBranch {
                        position,
                        call_site_version: d.context.key.call_site_version,
                        plan: d.spec,
                    }],
                    execution_contract: None,
                    comparison_version: None,
                },
                dependencies: vec![],
                results: crate::provider_graph::Results::CallbackItems { items: vec![0] },
            },
            runtime_branches: vec![crate::multi_call::MultiCallRuntimeBranch {
                position,
                runtime: d.context.runtime_version,
            }],
            source_contracts: vec![crate::provider_graph::SourceContract {
                position,
                sha256: node.source_contract_sha256,
            }],
        }
    }

    fn materialization_source() -> Call {
        let mut call = observable_call("sdk.materialization", "private training input");
        call.model = "gpt-4o-2024-11-20".into();
        call.parameters.extra[ROUTE_CONTEXT_KEY]["provider_configuration"] = serde_json::json!({"version":"provider-request-config-v1","supported":true,"sha256":"a".repeat(64)});
        call.parameters.extra[ROUTE_CONTEXT_KEY]["sdk_response_cache"] = serde_json::json!({"version":crate::wiring::SDK_RESPONSE_CACHE_VERSION,"supported":true,"message_sha256":"c".repeat(64),"shape_sha256":"d".repeat(64)});
        call
    }

    fn materialization_route(source: &Call, catalog: &ModelCatalog) -> Plan {
        let target = catalog
            .resolve(
                OPENAI_CHAT_COMPLETIONS_PROTOCOL,
                "openai",
                "gpt-4o-mini-2024-07-18",
            )
            .unwrap();
        let mut call = source.clone();
        catalog
            .routed_target(source, target)
            .unwrap()
            .annotate_call(&mut call)
            .unwrap();
        call.model = target.model_id.clone();
        Plan::Rewritten {
            rule: "ModelDowngrade".into(),
            call,
            projected_savings_usd: 0.0,
        }
    }

    #[test]
    fn template_materialization_rebuilds_original_and_routed_requests_with_new_contents() {
        let catalog = default_model_catalog().unwrap();
        let source = materialization_source();
        let pos = crate::multi_call::CallPosition { item: 0, call: 0 };
        for plan in [Plan::PassThrough, materialization_route(&source, &catalog)] {
            let template = materialization_template(&source, &plan, &catalog);
            let mut next = source.clone();
            next.messages[0].content = "private later response-derived input".into();
            next.parameters.extra[ROUTE_CONTEXT_KEY]["sdk_response_cache"]["message_sha256"] =
                serde_json::json!(content_hash(&next.messages[0].content));
            let raw = materialize_graph_node(
                &empty_optimizer(),
                &catalog,
                &template,
                pos,
                &serde_json::to_string(&next).unwrap(),
                false,
            )
            .unwrap();
            let node =
                prepared_graph_node(&catalog, pos, &serde_json::to_string(&next).unwrap(), &raw)
                    .unwrap();
            assert_eq!(node.plan, template.spec.calls.branches[0].plan);
            assert_ne!(
                node.logical_request_sha256,
                prepared_graph_node(
                    &catalog,
                    pos,
                    &serde_json::to_string(&source).unwrap(),
                    &encode_observation_context(
                        serde_json::to_string(&plan).unwrap(),
                        &describe_plan(&catalog, &source, &plan, 0.05)
                            .unwrap()
                            .context,
                    ),
                )
                .unwrap()
                .logical_request_sha256
            );
            let rebuilt: Plan = serde_json::from_str(&raw).unwrap();
            let call = selected_call(&next, &rebuilt).unwrap();
            assert_eq!(call.messages[0].content, next.messages[0].content);
            assert_eq!(
                call.model,
                template.spec.calls.branches[0].plan.target_model_id
            );
        }
    }

    #[test]
    fn template_materialization_rechecks_source_controls_positions_and_runtime() {
        let catalog = default_model_catalog().unwrap();
        let source = materialization_source();
        let plan = materialization_route(&source, &catalog);
        let template = materialization_template(&source, &plan, &catalog);
        let pos = crate::multi_call::CallPosition { item: 0, call: 0 };
        for case in 0..5 {
            let mut source = source.clone();
            let mut template = template.clone();
            let mut position = pos;
            match case {
                0 => source.parameters.max_output_tokens = Some(256),
                1 => source.call_site_id = "changed-source".into(),
                2 => {
                    template.runtime_branches[0].runtime.target_model_version =
                        "changed-runtime".into()
                }
                3 => position.call = 1,
                _ => source.model = "gpt-4o-mini-2024-07-18".into(),
            }
            assert!(
                materialize_graph_node(
                    &empty_optimizer(),
                    &catalog,
                    &template,
                    position,
                    &serde_json::to_string(&source).unwrap(),
                    false
                )
                .is_err(),
                "{case}"
            );
        }
    }

    #[test]
    fn template_materialization_cannot_route_excluded_or_globally_disabled_requests() {
        let catalog = default_model_catalog().unwrap();
        let source = materialization_source();
        let pos = crate::multi_call::CallPosition { item: 0, call: 0 };
        let routed =
            materialization_template(&source, &materialization_route(&source, &catalog), &catalog);
        assert!(materialize_graph_node(
            &empty_optimizer(),
            &catalog,
            &routed,
            pos,
            &serde_json::to_string(&source).unwrap(),
            true
        )
        .is_err());
        let disabled = Optimizer::empty(
            Arc::new(CostModel::new()),
            OptimizerConfig {
                enabled: false,
                ..Default::default()
            },
        );
        assert!(materialize_graph_node(
            &disabled,
            &catalog,
            &routed,
            pos,
            &serde_json::to_string(&source).unwrap(),
            false
        )
        .is_err());
        let original = materialization_template(&source, &Plan::PassThrough, &catalog);
        assert!(materialize_graph_node(
            &disabled,
            &catalog,
            &original,
            pos,
            &serde_json::to_string(&source).unwrap(),
            true
        )
        .is_ok());
    }

    #[test]
    fn template_materialization_does_not_claim_an_unreproducible_rewrite_or_configuration() {
        let catalog = default_model_catalog().unwrap();
        let source = materialization_source();
        let pos = crate::multi_call::CallPosition { item: 0, call: 0 };
        let mut template =
            materialization_template(&source, &materialization_route(&source, &catalog), &catalog);
        template.spec.calls.branches[0]
            .plan
            .rewrites
            .push(RewriteApplication {
                stable_name: "MissingRewrite".into(),
                implementation_version: "1".into(),
                parameters: serde_json::json!({}),
            });
        assert!(materialize_graph_node(
            &empty_optimizer(),
            &catalog,
            &template,
            pos,
            &serde_json::to_string(&source).unwrap(),
            false
        )
        .is_err());
        let mut template =
            materialization_template(&source, &materialization_route(&source, &catalog), &catalog);
        template.spec.calls.branches[0]
            .plan
            .validation_policy
            .parameters["rule_configuration"] = serde_json::json!({"ModelDowngrade":{"implementation_version":"different","parameters":{}}});
        assert!(materialize_graph_node(
            &empty_optimizer(),
            &catalog,
            &template,
            pos,
            &serde_json::to_string(&source).unwrap(),
            false
        )
        .is_err());
    }

    #[test]
    fn template_materialization_preserves_the_issued_sampling_policy() {
        let catalog = default_model_catalog().unwrap();
        for rate in [0.0, 0.02, 0.123456789012345] {
            let source: Call = serde_json::from_str(&bind_issued_sampling_policy(
                &serde_json::to_string(&materialization_source()).unwrap(),
                rate,
            ))
            .unwrap();
            let template = materialization_template(
                &source,
                &materialization_route(&source, &catalog),
                &catalog,
            );
            let next = bind_sampling_policy(&serde_json::to_string(&source).unwrap(), 0.75);
            let raw = materialize_graph_node(
                &empty_optimizer(),
                &catalog,
                &template,
                crate::multi_call::CallPosition { item: 0, call: 0 },
                &next,
                false,
            )
            .unwrap();
            assert_eq!(
                embedded_observation_context(&raw)
                    .unwrap()
                    .unwrap()
                    .sampling_rate,
                Some(rate)
            );
        }
    }

    #[test]
    fn observed_map_profiles_actual_calls_and_ignores_synthetic_peers() {
        let catalog = default_model_catalog().unwrap();
        let profiles = PlanProfiles::new();
        let mut calls = Vec::new();
        for item in 0..2 {
            let mut original = observable_call(
                &format!("map-site-{item}"),
                "private prompt must not persist",
            );
            original.parameters.extra[ROUTE_CONTEXT_KEY]
                [crate::model_catalog::APPLICATION_VERSION_KEY] =
                serde_json::json!({"version":"application-version-v1", "sha256":"a".repeat(64)});
            let descriptor = describe_plan(&catalog, &original, &Plan::PassThrough, 0.0).unwrap();
            let mut measured = outcome(&original.call_site_id);
            measured.provider_protocol =
                Some(descriptor.context.runtime_version.provider_protocol.clone());
            measured.executed_model_id = Some(original.model.clone());
            measured.dispatch_fallback = true;
            measured.dispatch_fallback_reason = Some("parallel_observe_only".into());
            let mut fake_peer = original.clone();
            fake_peer.model = "synthetic-peer-not-a-model".into();
            fake_peer.call_site_id = "synthetic-peer-not-a-call-site".into();
            let plan = serde_json::json!({"kind":"parallel", "calls":[original, fake_peer], "rule":"ParallelBranch", "projected_savings_usd":0});
            calls.push(serde_json::json!({
                "position":{"item":item,"call":0},
                "source_call_json":serde_json::to_string(&original).unwrap(),
                "plan_json":plan.to_string(), "cost_source":"provider_reported", "outcome":measured,
            }));
        }
        let receipt = serde_json::json!({
            "application_version":"a".repeat(64), "callback_version":"callback-v1",
            "item_count":2,"max_workers":2,"wall_latency_ms":120.0,"calls":calls,
        });
        let result: serde_json::Value = serde_json::from_str(
            &optimize_observe_multi_call(&catalog, &profiles, &receipt.to_string()).unwrap(),
        )
        .unwrap();
        assert_eq!(result["status"], "recorded");
        assert_eq!(result["admission"], "observe_only");
        assert_eq!(result["totals"]["call_count"], 2);
        assert_eq!(result["totals"]["cost_usd"], 0.002);
        let key: PlanProfileKey = serde_json::from_value(result["key"].clone()).unwrap();
        let profile = profiles.get_for_reporting(&key).unwrap();
        assert_eq!(profile.paired_observations, 0);
        assert!(!profile
            .runtime_version
            .target_model_version
            .contains("synthetic-peer"));
        let mut conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        profiles.flush_dirty(&mut conn).unwrap();
        let durable: String = conn
            .query_row(
                "SELECT target_model_version FROM execution_plan_profile",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(!durable.contains("private prompt"));
        let mut incomplete = receipt.clone();
        incomplete["calls"][1]["cost_source"] = serde_json::json!("estimated");
        assert!(optimize_observe_multi_call(&catalog, &profiles, &incomplete.to_string()).is_err());
        assert_eq!(profiles.get_for_reporting(&key).unwrap().n_observations, 1);
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
            cost_source: crate::multi_call::InferenceCostSource::ProviderReported,
            physical_invocations: Vec::new(),
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

    #[test]
    fn cost_sensitive_estimates_require_measured_candidate_and_reference_windows() {
        use crate::cost_evidence::{CostEvidence, CostSource};
        for (candidate_source, reference_source, expected) in [
            (
                CostSource::ProviderReported,
                CostSource::ProviderReported,
                true,
            ),
            (CostSource::Estimated, CostSource::ProviderReported, false),
            (CostSource::ProviderReported, CostSource::Estimated, false),
            (CostSource::Unknown, CostSource::ProviderReported, false),
            (CostSource::ProviderReported, CostSource::Unknown, false),
        ] {
            let profiles = PlanProfiles::new();
            let runtime = PlanRuntimeVersion {
                provider_protocol: "fixture".into(),
                target_model_id: "model".into(),
                target_model_version: "v1".into(),
                price_table_version: "prices-v1".into(),
            };
            let make = |id: &str, source: CostSource| PlanProfileUpdate {
                cost_evidence: CostEvidence {
                    source,
                    physical_invocations: Vec::new(),
                },
                key: PlanProfileKey {
                    call_site_version: CallSiteVersion::parse("a".repeat(64)).unwrap(),
                    execution_plan_id: crate::ExecutionPlanId::parse(id.repeat(64)).unwrap(),
                },
                runtime_version: runtime.clone(),
                input_tokens: 2,
                output_tokens: 1,
                cost_usd: 0.01,
                latency_ms: 10.0,
                output_is_structured: false,
                output_is_short: true,
                divergence: Some(0.0),
                dispatch_fallback: false,
                observed_at_us: Some(1),
            };
            let candidate = make("b", candidate_source);
            let reference = make("c", reference_source);
            profiles.observe(candidate.clone()).unwrap();
            profiles.observe(reference.clone()).unwrap();
            let candidate = profiles.get_for_reporting(&candidate.key).unwrap();
            let reference = profiles.get_for_reporting(&reference.key).unwrap();
            assert_eq!(
                estimate_plan(&candidate, &reference, 0.0, 0.0).is_some(),
                expected
            );
            assert_eq!(candidate.paired_observations, 1);
            assert_eq!(candidate.window_observations, 1);
        }
    }

    #[test]
    fn output_validation_identity_changes_only_the_alternative() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("validator-cohort", "private prompt");
        let rewritten = Plan::Rewritten {
            rule: "OutputBudget".into(),
            call: original.clone(),
            projected_savings_usd: 0.01,
        };
        let reference = describe_plan(&catalog, &original, &Plan::PassThrough, 0.05).unwrap();
        let alternative = describe_plan(&catalog, &original, &rewritten, 0.05).unwrap();
        let mut validated = original.clone();
        let declaration = serde_json::json!({
            "version": crate::model_catalog::OUTPUT_VALIDATION_VERSION,
            "validator_id": "fixture-v1", "configuration_sha256": "a".repeat(64),
            "model_version": "validator-pinned-v1", "max_cost_usd": 0.002,
            "supported": true,
        });
        validated.parameters.extra[ROUTE_CONTEXT_KEY]["output_validation"] = declaration;
        let updated = describe_plan(&catalog, &validated, &rewritten, 0.05).unwrap();
        assert_ne!(alternative.context.key, updated.context.key);
        assert_eq!(
            reference.context.key,
            describe_plan(&catalog, &validated, &Plan::PassThrough, 0.05)
                .unwrap()
                .context
                .key
        );
        for field in [
            "validator_id",
            "configuration_sha256",
            "model_version",
            "max_cost_usd",
        ] {
            let mut changed = validated.clone();
            changed.parameters.extra[ROUTE_CONTEXT_KEY]["output_validation"][field] = match field {
                "max_cost_usd" => serde_json::json!(0.003),
                "configuration_sha256" => serde_json::json!("b".repeat(64)),
                _ => serde_json::json!("changed-v2"),
            };
            assert_ne!(
                updated.context.key,
                describe_plan(&catalog, &changed, &rewritten, 0.05)
                    .unwrap()
                    .context
                    .key
            );
        }
        assert!(!serde_json::to_string(&updated.spec)
            .unwrap()
            .contains("private prompt"));
        validated.parameters.extra[ROUTE_CONTEXT_KEY]["output_validation"]["supported"] =
            serde_json::json!(false);
        assert!(describe_plan(&catalog, &validated, &rewritten, 0.05).is_none());
    }

    #[test]
    fn additional_inference_is_profiled_without_poisoning_output_distribution() {
        let catalog = default_model_catalog().unwrap();
        let call = observable_call("validator-cost", "task");
        let plan = Plan::Rewritten {
            rule: "OutputBudget".into(),
            call: call.clone(),
            projected_savings_usd: 0.01,
        };
        let descriptor = describe_plan(&catalog, &call, &plan, 0.05).unwrap();
        let encoded =
            encode_observation_context(serde_json::to_string(&plan).unwrap(), &descriptor.context);
        let cm = Arc::new(CostModel::new());
        let profiles = PlanProfiles::new();
        let mut measured = outcome(&call.call_site_id);
        measured.additional_inference = Some(crate::dag::AdditionalInference {
            cost_source: crate::multi_call::InferenceCostSource::ProviderReported,
            input_tokens: 200,
            output_tokens: 300,
            cost_usd: 0.002,
        });
        optimize_observe(
            &cm,
            &profiles,
            &encoded,
            &serde_json::to_string(&measured).unwrap(),
        )
        .unwrap();
        let profile = profiles.get_for_reporting(&descriptor.context.key).unwrap();
        assert_eq!(profile.input_tokens.mean, 300.0);
        assert_eq!(profile.output_tokens.mean, 350.0);
        assert_eq!(profile.cost_usd.mean, 0.003);
        assert_eq!(profile.paired_observations, 0);
        let legacy = cm.get(&call.call_site_id).unwrap();
        assert_eq!(legacy.output_token_p99, 50.0);
        assert_eq!(legacy.cost_usd.mean, 0.001);
        measured.inference_cost_complete = Some(false);
        assert!(optimize_observe(
            &cm,
            &profiles,
            &encoded,
            &serde_json::to_string(&measured).unwrap()
        )
        .is_err());
        assert_eq!(cm.get(&call.call_site_id).unwrap().n_observations, 1);
        measured.inference_cost_complete = Some(true);
        measured.additional_inference.as_mut().unwrap().cost_usd = -1.0;
        assert!(optimize_observe(
            &cm,
            &profiles,
            &encoded,
            &serde_json::to_string(&measured).unwrap()
        )
        .is_err());
        assert_eq!(
            profiles
                .get_for_reporting(&descriptor.context.key)
                .unwrap()
                .n_observations,
            1
        );
    }

    #[test]
    fn visible_and_counterfactual_fallback_economics_charge_reference_once() {
        use crate::plan_profile::ChargedReferenceFallback;
        let catalog = default_model_catalog().unwrap();
        let call = observable_call("fallback-economics", "task");
        let plan = Plan::Rewritten {
            rule: "OutputBudget".into(),
            call: call.clone(),
            projected_savings_usd: 0.01,
        };
        let candidate = describe_plan(&catalog, &call, &plan, 0.05).unwrap();
        let reference = describe_plan(&catalog, &call, &Plan::PassThrough, 0.05).unwrap();
        let profiles = PlanProfiles::new();
        let mk = |descriptor: &ResolvedPlanDescriptor, cost, latency, fallback, paired| {
            PlanProfileUpdate {
                cost_evidence: crate::cost_evidence::CostEvidence {
                    source: crate::cost_evidence::CostSource::ProviderReported,
                    physical_invocations: Vec::new(),
                },
                key: descriptor.context.key.clone(),
                runtime_version: descriptor.context.runtime_version.clone(),
                input_tokens: 10,
                output_tokens: 10,
                latency_ms: latency,
                cost_usd: cost,
                output_is_structured: false,
                output_is_short: true,
                divergence: paired,
                dispatch_fallback: fallback,
                observed_at_us: Some(1),
            }
        };
        profiles
            .observe(mk(&reference, 0.01, 30.0, false, None))
            .unwrap();
        profiles
            .observe(mk(&candidate, 0.002, 20.0, false, Some(0.0)))
            .unwrap();
        profiles
            .observe_with_charged_reference(
                mk(&candidate, 0.012, 50.0, true, Some(0.0)),
                ChargedReferenceFallback {
                    cost_usd: 0.01,
                    latency_ms: 30.0,
                },
            )
            .unwrap();
        let compare = |profiles: &PlanProfiles| {
            let profile = profiles.get_for_reporting(&candidate.context.key).unwrap();
            let reference = profiles.get_for_reporting(&reference.context.key).unwrap();
            let estimate = estimate_plan(&profile, &reference, 0.02, 1.0).unwrap();
            assert!((estimate.expected_cost_usd - 0.0072).abs() < 1e-12);
            assert!((estimate.expected_net_cost_savings_usd - 0.0028).abs() < 1e-12);
            assert!((estimate.expected_latency_ms - 36.6).abs() < 1e-12);
            assert!((estimate.expected_net_latency_savings_ms + 6.6).abs() < 1e-12);
        };
        compare(&profiles);
        let mut conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        profiles.flush_dirty(&mut conn).unwrap();
        let restarted = PlanProfiles::new();
        restarted.warm_from_db(&conn).unwrap();
        compare(&restarted);
        // A background rejection did not run the reference as part of its
        // candidate work, so its future fallback still needs projection.
        let counterfactuals = PlanProfiles::new();
        counterfactuals
            .observe(mk(&reference, 0.01, 30.0, false, None))
            .unwrap();
        counterfactuals
            .observe(mk(&candidate, 0.002, 20.0, false, Some(0.0)))
            .unwrap();
        counterfactuals
            .observe(mk(&candidate, 0.002, 20.0, true, Some(0.0)))
            .unwrap();
        compare(&counterfactuals);
    }

    #[test]
    fn validation_rejection_ffi_binds_contract_and_preserves_reference_evidence() {
        let catalog = default_model_catalog().unwrap();
        let mut call = observable_call("durable-validation", "private prompt");
        let contract = serde_json::json!({"version": crate::model_catalog::OUTPUT_VALIDATION_VERSION,
            "validator_id": "fixture-v1", "configuration_sha256": "a".repeat(64),
            "model_version": "pinned-v1", "max_cost_usd": 0.001, "supported": true});
        call.parameters.extra[ROUTE_CONTEXT_KEY]["output_validation"] = contract.clone();
        let plan = Plan::Rewritten {
            rule: "OutputBudget".into(),
            call: call.clone(),
            projected_savings_usd: 0.01,
        };
        let descriptor = describe_plan(&catalog, &call, &plan, 0.05).unwrap();
        let encoded =
            encode_observation_context(serde_json::to_string(&plan).unwrap(), &descriptor.context);
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let mut conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_micros() as i64;
        let first = "1".repeat(32);
        record_output_validation_rejection(
            &profiles,
            &guard,
            &mut conn,
            &encoded,
            &contract.to_string(),
            &first,
            now,
        )
        .unwrap();
        assert!(profiles.is_empty());
        let restarted = PlanGuard::new();
        restarted.warm_from_db(&conn).unwrap();
        let mut wrong = contract.clone();
        wrong["model_version"] = serde_json::json!("wrong-model");
        assert!(record_output_validation_rejection(
            &profiles,
            &restarted,
            &mut conn,
            &encoded,
            &wrong.to_string(),
            &"2".repeat(32),
            now + 1
        )
        .is_err());
        assert_eq!(
            restarted
                .get(&descriptor.context.key)
                .unwrap()
                .validation_rejection_count(),
            1
        );
        record_output_validation_rejection(
            &profiles,
            &restarted,
            &mut conn,
            &encoded,
            &contract.to_string(),
            &"2".repeat(32),
            now + 2,
        )
        .unwrap();
        assert!(restarted
            .decision(&descriptor.context.key, now + 2)
            .blocks_user_visible());
        assert_eq!(
            restarted
                .get(&descriptor.context.key)
                .unwrap()
                .divergence_exposure,
            0.0
        );
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM execution_plan_profile", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            conn.query_row(
                "SELECT COUNT(*) FROM execution_plan_guard_observation",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            0
        );
        let fresh_guard = PlanGuard::new();
        let mut failed_db = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&failed_db).unwrap();
        failed_db.execute_batch("CREATE TRIGGER reject_guard_write BEFORE INSERT ON execution_plan_validation_rejection BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END;").unwrap();
        assert!(record_output_validation_rejection(
            &profiles,
            &fresh_guard,
            &mut failed_db,
            &encoded,
            &contract.to_string(),
            &first,
            now
        )
        .is_err());
        assert_eq!(
            failed_db
                .query_row("SELECT COUNT(*) FROM execution_plan_guard", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(fresh_guard.dirty_len(), 1);
        failed_db
            .execute_batch("DROP TRIGGER reject_guard_write;")
            .unwrap();
        record_output_validation_rejection(
            &profiles,
            &fresh_guard,
            &mut failed_db,
            &encoded,
            &contract.to_string(),
            &first,
            now + 1,
        )
        .unwrap();
        assert_eq!(
            fresh_guard
                .get(&descriptor.context.key)
                .unwrap()
                .validation_rejection_count(),
            1
        );
    }

    #[test]
    fn ffi_fallback_charge_is_measured_from_reference_and_invalid_duration_is_atomic() {
        let catalog = default_model_catalog().unwrap();
        let call = observable_call("physical-reference", "task");
        let plan = Plan::Rewritten {
            rule: "OutputBudget".into(),
            call: call.clone(),
            projected_savings_usd: 0.01,
        };
        let descriptor = describe_plan(&catalog, &call, &plan, 0.05).unwrap();
        let encoded =
            encode_observation_context(serde_json::to_string(&plan).unwrap(), &descriptor.context);
        let cm = Arc::new(CostModel::new());
        let profiles = PlanProfiles::new();
        let mut measured = outcome(&call.call_site_id);
        measured.dispatch_fallback = true;
        measured.cost_usd = 0.01;
        measured.reference_fallback_latency_ms = Some(30.0);
        measured.additional_inference = Some(crate::dag::AdditionalInference {
            cost_source: crate::multi_call::InferenceCostSource::ProviderReported,
            input_tokens: 100,
            output_tokens: 50,
            cost_usd: 0.002,
        });
        optimize_observe(
            &cm,
            &profiles,
            &encoded,
            &serde_json::to_string(&measured).unwrap(),
        )
        .unwrap();
        let profile = profiles.get_for_reporting(&descriptor.context.key).unwrap();
        assert_eq!(profile.cost_usd.mean, 0.012);
        assert_eq!(profile.charged_reference_cost_usd.mean, 0.01);
        assert_eq!(profile.charged_reference_latency_ms.mean, 30.0);
        measured.reference_fallback_latency_ms = Some(101.0);
        assert!(optimize_observe(
            &cm,
            &profiles,
            &encoded,
            &serde_json::to_string(&measured).unwrap()
        )
        .is_err());
        assert_eq!(cm.get(&call.call_site_id).unwrap().n_observations, 1);
        assert_eq!(
            profiles
                .get_for_reporting(&descriptor.context.key)
                .unwrap()
                .n_observations,
            1
        );
    }

    struct AlwaysCapsOutput;

    impl RewriteRule for AlwaysCapsOutput {
        fn identity(&self) -> Option<crate::planner::RuleIdentity> {
            crate::planner::RuleIdentity::new(
                "test-always-cap-v1",
                self.accuracy_budget(),
                serde_json::json!({"cap": 64}),
            )
        }

        fn name(&self) -> &'static str {
            "OutputBudget"
        }

        fn applies(&self, _: &Call, _: &crate::cost_model::CallSiteProfile) -> bool {
            true
        }

        fn propose(&self, call: &Call, _: &crate::cost_model::CallSiteProfile) -> Option<Proposal> {
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
    fn selection_audit_separates_cold_search_from_no_opportunity() {
        let catalog = default_model_catalog().unwrap();
        let call_json =
            serde_json::to_string(&observable_call("audit.cold", "private input")).unwrap();
        let planned = optimize_profiled_plan_with_audit(
            &empty_optimizer(),
            Some(&catalog),
            &PlanProfiles::new(),
            &PlanGuard::new(),
            &call_json,
            3_000_000,
            true,
        );
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let audit = &value[SELECTION_AUDIT_KEY];
        assert_eq!(audit["candidate_count"], 1);
        assert_eq!(audit["search_status"], "cold_call_site");
        assert_eq!(audit["enumeration_complete"], false);
        assert_eq!(audit["selector_invoked"], true);
        assert_eq!(audit["selection"]["selected_reference"], true);
        assert_eq!(
            audit["effective_selected_plan_id"],
            value[OBSERVATION_CONTEXT_KEY]["key"]["execution_plan_id"]
        );
        assert!(
            !planned.contains("private input"),
            "selection audit must not archive request content"
        );
    }

    #[test]
    fn selection_audit_reports_actual_missing_estimate_rejection_and_preserves_identity() {
        let (catalog, optimizer) = output_optimizer("audit.hot");
        let call_json =
            serde_json::to_string(&observable_call("audit.hot", "private input")).unwrap();
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let plain = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            3_000_000,
        );
        let explicit_disabled = optimize_profiled_plan_with_audit(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            3_000_000,
            false,
        );
        assert_eq!(plain, explicit_disabled);
        let audited = optimize_profiled_plan_with_audit(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            3_000_000,
            true,
        );
        let mut value: serde_json::Value = serde_json::from_str(&audited).unwrap();
        let audit = value
            .as_object_mut()
            .unwrap()
            .remove(SELECTION_AUDIT_KEY)
            .unwrap();
        assert_eq!(
            value,
            serde_json::from_str::<serde_json::Value>(&plain).unwrap()
        );
        assert_eq!(audit["candidate_count"], 2);
        assert_eq!(audit["alternatives"].as_array().unwrap().len(), 1);
        assert_eq!(audit["enumeration_complete"], true);
        assert_eq!(audit["search_status"], "complete");
        assert_eq!(audit["policy"]["objective"], "cost");
        assert_eq!(audit["policy"]["min_paired_observations"], 20);
        assert_eq!(
            audit["selection"]["rejections"][0]["reason"]["reason"],
            "missing_estimate"
        );
        assert_eq!(
            audit["selection"]["rejections"][0]["plan_id"],
            audit["alternatives"][0]["id"]
        );
    }

    #[test]
    fn selection_audit_does_not_invent_a_count_when_budget_stops_before_selection() {
        let f = incumbent_fixture();
        let optimizer = Optimizer::new(
            f.cost_model.clone(),
            vec![Box::new(AlwaysCapsOutput)],
            OptimizerConfig {
                max_overhead_ms: 0.0,
                ..OptimizerConfig::default()
            },
        );
        let planned = optimize_profiled_plan_with_audit(
            &optimizer,
            Some(&f.catalog),
            &f.profiles,
            &PlanGuard::new(),
            &serde_json::to_string(&f.call).unwrap(),
            3_000_000,
            true,
        );
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let audit = &value[SELECTION_AUDIT_KEY];
        assert_eq!(value["kind"], "pass_through");
        assert!(audit["candidate_count"].is_null());
        assert_eq!(audit["selector_invoked"], false);
        assert_eq!(audit["enumeration_complete"], false);
        assert_eq!(audit["abstention_reason"], "selection_overhead_budget");
    }

    #[test]
    fn unavailable_selection_audit_keeps_unknown_models_executable_without_inventing_identity() {
        let catalog = default_model_catalog().unwrap();
        let mut call = observable_call("unknown.model", "private original input");
        call.model = "unregistered-fixture-model".to_string();
        let planned = optimize_profiled_plan_with_audit(
            &empty_optimizer(),
            Some(&catalog),
            &PlanProfiles::new(),
            &PlanGuard::new(),
            &serde_json::to_string(&call).unwrap(),
            3_000_000,
            true,
        );
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        assert_eq!(value["kind"], "pass_through");
        let audit = &value[SELECTION_AUDIT_KEY];
        assert!(audit["candidate_count"].is_null());
        assert!(audit["effective_selected_plan_id"].is_null());
        assert_eq!(audit["selector_invoked"], false);
        assert_eq!(audit["abstention_reason"], "reference_identity_unavailable");
        assert!(!planned.contains("private original input"));
    }

    #[test]
    fn exploration_keeps_the_policy_choice_and_updates_the_effective_primary_audit() {
        let f = incumbent_fixture();
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let call_json = serde_json::to_string(&f.call).unwrap();
        let primary = optimize_profiled_plan_with_audit(
            &f.optimizer,
            Some(&f.catalog),
            &f.profiles,
            &guard,
            &call_json,
            3_000_000,
            true,
        );
        let primary_value: serde_json::Value = serde_json::from_str(&primary).unwrap();
        assert_ne!(primary_value["kind"], "pass_through");
        let explored = reserve_profiled_exploration(
            &f.optimizer,
            Some(&f.catalog),
            &f.profiles,
            &guard,
            &controller,
            &mut connection,
            &call_json,
            &primary,
            3_000_000,
        );
        let value: serde_json::Value = serde_json::from_str(&explored).unwrap();
        assert_eq!(value["kind"], "pass_through");
        assert_eq!(
            value[SELECTION_AUDIT_KEY]["selection"],
            primary_value[SELECTION_AUDIT_KEY]["selection"]
        );
        assert_eq!(
            value[SELECTION_AUDIT_KEY]["candidate_count"],
            primary_value[SELECTION_AUDIT_KEY]["candidate_count"]
        );
        assert_eq!(
            value[SELECTION_AUDIT_KEY]["effective_selected_plan_id"],
            serde_json::json!(f.reference_key.execution_plan_id)
        );
        assert_ne!(
            value[SELECTION_AUDIT_KEY]["effective_selected_plan_id"],
            value[SELECTION_AUDIT_KEY]["selection"]["selected_plan_id"]
        );
        assert!(!value[EXPLORATION_CONTEXT_KEY]["lease_token"].is_null());
    }

    struct AlwaysCachedResponse;
    impl RewriteRule for AlwaysCachedResponse {
        fn name(&self) -> &'static str {
            "CacheHit"
        }
        fn identity(&self) -> Option<crate::planner::RuleIdentity> {
            crate::planner::RuleIdentity::new(
                "cached-controller-fixture-v1",
                0.01,
                serde_json::json!({"fixture":true}),
            )
        }
        fn applies(&self, _: &Call, _: &crate::CallSiteProfile) -> bool {
            true
        }
        fn propose(&self, _: &Call, _: &crate::CallSiteProfile) -> Option<Proposal> {
            Some(Proposal {
                rewritten: Plan::Cached {
                    value: serde_json::json!({"source":{"kind":"exact"},"output_content_id":"controller-fixture"}),
                },
                projected_savings_usd: 0.01,
                cost_driver: CostDriver::CallElimination,
                safety_check: Box::new(|_| true),
            })
        }
        fn accuracy_budget(&self) -> f32 {
            0.01
        }
    }

    fn cached_optimizer(site: &str) -> (Arc<ModelCatalog>, Optimizer, Arc<CostModel>) {
        let cost_model = Arc::new(CostModel::new());
        for i in 0..3 {
            cost_model.observe(CostModelUpdate {
                call_site_id: site.into(),
                input_tokens: 100,
                output_tokens: 50,
                latency_ms: 100.0,
                cost_usd: 0.01,
                output_is_structured: false,
                output_is_short: true,
                now_us: Some(i),
            });
        }
        let opt = Optimizer::new(
            cost_model.clone(),
            vec![Box::new(AlwaysCachedResponse)],
            OptimizerConfig {
                shadow_rate: 0.0,
                ..OptimizerConfig::default()
            },
        );
        (Arc::new(default_model_catalog().unwrap()), opt, cost_model)
    }

    #[test]
    fn cached_exploration_normalizes_historical_usage_and_cannot_resolve_aliases() {
        cached_exploration_accounting(false);
    }

    #[test]
    fn cached_exploration_charges_validator_work_exactly_once() {
        cached_exploration_accounting(true);
    }

    fn cached_exploration_accounting(with_validator: bool) {
        let site = "cached-counterfactual-accounting";
        let (catalog, optimizer, costs) = cached_optimizer(site);
        let call = observable_call(site, "private cache prompt");
        let call_json = serde_json::to_string(&call).unwrap();
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        let primary = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            2_000_000,
        );
        let mut reference = outcome(site);
        reference.executed_model_id = Some("provider-current".into());
        optimize_observe(
            &costs,
            &profiles,
            &primary,
            &serde_json::to_string(&reference).unwrap(),
        )
        .unwrap();
        let planned = reserve_profiled_exploration(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &controller,
            &mut conn,
            &call_json,
            &primary,
            2_000_001,
        );
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let exploration: EmbeddedExplorationContext =
            serde_json::from_value(value[EXPLORATION_CONTEXT_KEY].clone()).unwrap();
        let token = decode_exploration_token(&exploration.lease_token).unwrap();
        assert!(token.cached_response);
        let mut historical = outcome(site);
        historical.executed_model_id = Some("provider-old-cached".into());
        historical.cost_usd = 100.0;
        historical.latency_ms = 7.0;
        if with_validator {
            historical.additional_inference = Some(crate::dag::AdditionalInference {
                cost_source: crate::multi_call::InferenceCostSource::ProviderReported,
                input_tokens: 7,
                output_tokens: 3,
                cost_usd: 0.002,
            });
            historical.validation_rejected = true;
        }
        let payload = serde_json::to_string(&historical).unwrap();
        let attribution = complete_profiled_exploration(
            &profiles,
            &controller,
            &mut conn,
            &exploration.lease_token,
            &payload,
            0.0,
            2_000_002,
        )
        .unwrap()
        .unwrap();
        assert_eq!(attribution.solo_rule.as_deref(), Some("CacheHit"));
        let profile = profiles.get_for_reporting(&token.lease.key).unwrap();
        assert_eq!(
            (
                profile.input_tokens.mean,
                profile.output_tokens.mean,
                profile.cost_usd.mean
            ),
            if with_validator {
                (7.0, 3.0, 0.002)
            } else {
                (0.0, 0.0, 0.0)
            }
        );
        assert_eq!(
            profile.dispatch_fallback_rate,
            if with_validator { 1.0 } else { 0.0 }
        );
        assert_eq!(profile.latency_ms.mean, 7.0);
        assert_eq!(profile.paired_observations, 1);
        let reference_context = embedded_observation_context(&primary).unwrap().unwrap();
        assert_eq!(
            decision_profile(&profiles, &reference_context)
                .unwrap()
                .runtime_version
                .target_model_version,
            "provider-current"
        );
        assert!(complete_profiled_exploration(
            &profiles,
            &controller,
            &mut conn,
            &exploration.lease_token,
            &payload,
            0.0,
            2_000_003
        )
        .unwrap()
        .is_none());
        let snapshot = controller
            .snapshot(&conn, &token.lease.key.call_site_version, 2_000_004)
            .unwrap();
        assert_eq!(snapshot.completed_calls, 1);
        assert_eq!(
            snapshot.counterfactual_cost_usd,
            if with_validator { 0.002 } else { 0.0 }
        );
        assert_eq!(snapshot.active_leases, 0);
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
        let exploration: EmbeddedExplorationContext =
            serde_json::from_value(value.get(EXPLORATION_CONTEXT_KEY).unwrap().clone()).unwrap();
        assert_eq!(exploration.schema_version, EXPLORATION_TOKEN_SCHEMA_VERSION);
        assert!(matches!(
            serde_json::from_value::<Plan>(exploration.candidate_plan.clone()).unwrap(),
            Plan::Rewritten { .. }
        ));
        assert!(!exploration.lease_token.contains("private reference prompt"));

        let token = decode_exploration_token(&exploration.lease_token).unwrap();
        let snapshot = controller
            .snapshot(&connection, &token.lease.key.call_site_version, now_us)
            .unwrap();
        assert_eq!(snapshot.calls_in_window, 1);
        assert_eq!(snapshot.active_leases, 1);
    }

    #[test]
    fn late_pre_readmission_execution_is_billed_without_becoming_fresh_evidence() {
        let site = "late-readmission-site";
        let (catalog, optimizer) = output_optimizer(site);
        let call_json = serde_json::to_string(&observable_call(site, "reference prompt")).unwrap();
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
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
            &mut conn,
            &call_json,
            &primary,
            2_000_000,
        );
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let exploration: EmbeddedExplorationContext =
            serde_json::from_value(value[EXPLORATION_CONTEXT_KEY].clone()).unwrap();
        let token = decode_exploration_token(&exploration.lease_token).unwrap();
        profiles.begin_readmission(&token.lease.key, &token.context.runtime_version, 2_000_001);
        let mut result = outcome(site);
        result.cost_usd = 0.25;
        let result_json = serde_json::to_string(&result).unwrap();
        assert!(complete_profiled_exploration(
            &profiles,
            &controller,
            &mut conn,
            &exploration.lease_token,
            &result_json,
            0.0,
            2_000_002
        )
        .unwrap()
        .is_none());
        assert_eq!(
            profiles
                .get_for_reporting(&token.lease.key)
                .unwrap()
                .paired_observations,
            0
        );
        assert_eq!(
            controller
                .snapshot(&conn, &token.lease.key.call_site_version, 2_000_002)
                .unwrap()
                .counterfactual_cost_usd,
            0.25
        );
        assert!(optimize_observe(
            &Arc::new(CostModel::new()),
            &profiles,
            &serde_json::to_string(&exploration.candidate_plan).unwrap(),
            &result_json
        )
        .is_err());
    }

    #[test]
    fn cooldown_readmission_cannot_probe_while_rolling_exposure_remains_exhausted() {
        let site = "cooldown-exposure-site";
        let (catalog, optimizer) = output_optimizer(site);
        let call = observable_call(site, "reference prompt");
        let profiles = PlanProfiles::new();
        let key = seed_refresh_profiles(&profiles, &catalog, &optimizer, &call, 0.0, 2_000_000);
        let profile = profiles.get_for_reporting(&key).unwrap();
        let observation: PlanObservationToken = serde_json::from_value(serde_json::json!({
            "key": key, "runtime_version": profile.runtime_version, "sequence": 20
        }))
        .unwrap();
        let threshold = optimizer.divergence_threshold_for_plan(
            &optimizer
                .candidate_plans(&call, &catalog)
                .into_iter()
                .find(|plan| matches!(plan, Plan::Rewritten { .. }))
                .unwrap(),
        );
        let guard = PlanGuard::with_limits(0.5, 100, 20).unwrap();
        guard
            .record_sample(&observation, 1.0, threshold, Some(3_000_000))
            .unwrap();
        let call_json = serde_json::to_string(&call).unwrap();
        let now_us = 3_000_020;
        let primary = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            now_us,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&primary).unwrap(),
            Plan::PassThrough
        ));
        let mut conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        let controller = ExplorationController::new();
        assert_eq!(
            reserve_profiled_exploration(
                &optimizer,
                Some(&catalog),
                &profiles,
                &guard,
                &controller,
                &mut conn,
                &call_json,
                &primary,
                now_us
            ),
            primary
        );
        assert!(guard.get(&key).unwrap().divergence_exposure >= 0.5);
        assert!(guard.decision(&key, now_us).blocks_user_visible());
        assert_eq!(
            controller
                .snapshot(&conn, &key.call_site_version, now_us)
                .unwrap()
                .calls_in_window,
            0
        );
    }

    #[test]
    fn cooldown_readmission_requires_twenty_new_pairs_and_survives_restart() {
        assert_cooldown_readmission(false);
    }

    #[test]
    fn cached_cooldown_readmission_requires_twenty_fresh_pairs_and_survives_restart() {
        assert_cooldown_readmission(true);
    }

    fn assert_cooldown_readmission(cached: bool) {
        let site = "cooldown-readmission-site";
        let (catalog, optimizer) = if cached {
            let (catalog, optimizer, _) = cached_optimizer(site);
            (catalog, optimizer)
        } else {
            output_optimizer(site)
        };
        let call = observable_call(site, "reference prompt");
        let call_json = serde_json::to_string(&call).unwrap();
        let profiles = PlanProfiles::new();
        let key = seed_refresh_profiles(&profiles, &catalog, &optimizer, &call, 0.0, 2_000_000);
        let guard = PlanGuard::with_limits(0.5, 10, 20).unwrap();
        let profile = profiles.get_for_reporting(&key).unwrap();
        let observation: PlanObservationToken = serde_json::from_value(serde_json::json!({
            "key": key, "runtime_version": profile.runtime_version, "sequence": 20
        }))
        .unwrap();
        let threshold = optimizer.divergence_threshold_for_plan(
            &optimizer
                .candidate_plans(&call, &catalog)
                .into_iter()
                .find(|plan| matches!(plan, Plan::Rewritten { .. } | Plan::Cached { .. }))
                .unwrap(),
        );
        guard
            .record_sample(&observation, 1.0, threshold, Some(3_000_000))
            .unwrap();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let blocked = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            3_000_019,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&blocked).unwrap(),
            Plan::PassThrough
        ));
        assert_eq!(
            reserve_profiled_exploration(
                &optimizer,
                Some(&catalog),
                &profiles,
                &guard,
                &controller,
                &mut connection,
                &call_json,
                &blocked,
                3_000_019
            ),
            blocked
        );
        for pairs in 0..20 {
            let now_us = 3_000_020 + pairs * 10;
            let primary = optimize_profiled_plan(
                &optimizer,
                Some(&catalog),
                &profiles,
                &guard,
                &call_json,
                now_us,
            );
            assert!(matches!(
                serde_json::from_str::<Plan>(&primary).unwrap(),
                Plan::PassThrough
            ));
            assert!(guard.decision(&key, now_us).blocks_user_visible());
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
                value
                    .get(EXPLORATION_CONTEXT_KEY)
                    .expect("cooldown must allow a bounded fresh probe")
                    .clone(),
            )
            .unwrap();
            let mut result = outcome(site);
            result.cost_usd = 0.0002;
            complete_profiled_exploration(
                &profiles,
                &controller,
                &mut connection,
                &exploration.lease_token,
                &serde_json::to_string(&result).unwrap(),
                0.0,
                now_us + 1,
            )
            .unwrap();
            assert_eq!(
                profiles
                    .get_for_reporting(&key)
                    .unwrap()
                    .paired_observations,
                (pairs + 1) as u32
            );
            profiles.flush_dirty(&mut connection).unwrap();
            guard.flush_dirty(&mut connection).unwrap();
            let restarted = PlanProfiles::new();
            restarted.warm_from_db(&connection).unwrap();
            let restarted_guard = PlanGuard::with_limits(0.5, 10, 20).unwrap();
            restarted_guard.warm_from_db(&connection).unwrap();
            let next = optimize_profiled_plan(
                &optimizer,
                Some(&catalog),
                &restarted,
                &restarted_guard,
                &call_json,
                now_us + 2,
            );
            assert_eq!(
                matches!(
                    serde_json::from_str::<Plan>(&next).unwrap(),
                    Plan::Rewritten { .. } | Plan::Cached { .. }
                ),
                pairs == 19
            );
            assert_eq!(
                restarted_guard
                    .decision(&key, now_us + 2)
                    .blocks_user_visible(),
                pairs != 19
            );
        }
        // Re-admission itself, as well as its fresh evidence boundary, is durable.
        optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            3_000_300,
        );
        guard.flush_dirty(&mut connection).unwrap();
        let restarted_guard = PlanGuard::with_limits(0.5, 10, 20).unwrap();
        restarted_guard.warm_from_db(&connection).unwrap();
        assert!(!restarted_guard
            .decision(&key, 3_000_301)
            .blocks_user_visible());
    }

    fn seed_refresh_profiles(
        profiles: &PlanProfiles,
        catalog: &ModelCatalog,
        optimizer: &Optimizer,
        call: &Call,
        last_divergence: f64,
        now_us: i64,
    ) -> PlanProfileKey {
        let reference = describe_plan(
            catalog,
            call,
            &Plan::PassThrough,
            optimizer.divergence_threshold_for_plan(&Plan::PassThrough),
        )
        .unwrap();
        let plan = optimizer
            .candidate_plans(call, catalog)
            .into_iter()
            .find(|plan| matches!(plan, Plan::Rewritten { .. } | Plan::Cached { .. }))
            .unwrap();
        let candidate = describe_plan(
            catalog,
            call,
            &plan,
            optimizer.divergence_threshold_for_plan(&plan),
        )
        .unwrap();
        // Controlled-clock synthetic histories isolate eligibility. Real
        // retained counts and reference/candidate identities stay separate.
        for i in 0..20 {
            for (descriptor, cost, divergence) in [
                (&reference, 0.01, None),
                (
                    &candidate,
                    if matches!(plan, Plan::Cached { .. }) {
                        0.0
                    } else {
                        0.0002
                    },
                    Some(if i == 19 { last_divergence } else { 0.0 }),
                ),
            ] {
                profiles
                    .observe(PlanProfileUpdate {
                        cost_evidence: crate::cost_evidence::CostEvidence {
                            source: crate::cost_evidence::CostSource::ProviderReported,
                            physical_invocations: Vec::new(),
                        },
                        key: descriptor.context.key.clone(),
                        runtime_version: descriptor.context.runtime_version.clone(),
                        input_tokens: if divergence.is_some() && matches!(plan, Plan::Cached { .. })
                        {
                            0
                        } else {
                            100
                        },
                        output_tokens: if divergence.is_some()
                            && matches!(plan, Plan::Cached { .. })
                        {
                            0
                        } else {
                            50
                        },
                        latency_ms: 25.0,
                        cost_usd: cost,
                        output_is_structured: false,
                        output_is_short: true,
                        divergence,
                        dispatch_fallback: false,
                        observed_at_us: Some(now_us + i),
                    })
                    .unwrap();
            }
        }
        candidate.context.key
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
        let controller = ExplorationController::new();
        let boundary_time = old_time + 19 + SelectionPolicy::default().max_profile_age_us;
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let boundary = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            boundary_time,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&boundary).unwrap(),
            Plan::Rewritten { .. }
        ));
        assert_eq!(
            reserve_profiled_exploration(
                &optimizer,
                Some(&catalog),
                &profiles,
                &guard,
                &controller,
                &mut connection,
                &call_json,
                &boundary,
                boundary_time
            ),
            boundary,
            "a current admitted profile must not receive a refresh probe"
        );
        profiles.flush_dirty(&mut connection).unwrap();
        let restarted = PlanProfiles::new();
        restarted.warm_from_db(&connection).unwrap();
        let now_us = boundary_time + 1;
        let controller =
            ExplorationController::with_policy(crate::exploration::ExplorationPolicy {
                max_calls_per_site: 1,
                ..crate::exploration::ExplorationPolicy::default()
            })
            .unwrap();
        let primary = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &restarted,
            &guard,
            &call_json,
            now_us,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&primary).unwrap(),
            Plan::PassThrough
        ));
        let planned = reserve_profiled_exploration(
            &optimizer,
            Some(&catalog),
            &restarted,
            &guard,
            &controller,
            &mut connection,
            &call_json,
            &primary,
            now_us,
        );
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let exploration: EmbeddedExplorationContext = serde_json::from_value(
            value
                .get(EXPLORATION_CONTEXT_KEY)
                .expect("stale complete evidence needs a fresh probe")
                .clone(),
        )
        .unwrap();
        assert_eq!(
            decode_exploration_token(&exploration.lease_token)
                .unwrap()
                .lease
                .key,
            key
        );
        assert_eq!(
            restarted
                .get_for_reporting(&key)
                .unwrap()
                .paired_observations,
            20
        );
        assert_eq!(
            reserve_profiled_exploration(
                &optimizer,
                Some(&catalog),
                &restarted,
                &guard,
                &controller,
                &mut connection,
                &call_json,
                &primary,
                now_us + 1
            ),
            primary,
            "refresh must respect the persisted attempt cap"
        );
        let mut result = outcome(site);
        result.cost_usd = 0.0002;
        complete_profiled_exploration(
            &restarted,
            &controller,
            &mut connection,
            &exploration.lease_token,
            &serde_json::to_string(&result).unwrap(),
            0.0,
            now_us + 2,
        )
        .unwrap();
        restarted.flush_dirty(&mut connection).unwrap();
        let reloaded = PlanProfiles::new();
        reloaded.warm_from_db(&connection).unwrap();
        let refreshed = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &reloaded,
            &guard,
            &call_json,
            now_us + 3,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&refreshed).unwrap(),
            Plan::Rewritten { .. }
        ));
        let profile = reloaded.get_for_reporting(&key).unwrap();
        assert_eq!(profile.paired_observations, 21);
        assert_eq!(profile.last_paired_at_us, Some(now_us + 2));
        assert_eq!(
            controller
                .snapshot(&connection, &key.call_site_version, now_us + 3)
                .unwrap()
                .calls_in_window,
            1
        );
    }

    #[test]
    fn evidence_complete_divergence_rejection_can_collect_bounded_recovery_evidence() {
        let site = "divergence-refresh-site";
        let (catalog, optimizer) = output_optimizer(site);
        let call = observable_call(site, "reference prompt");
        let call_json = serde_json::to_string(&call).unwrap();
        let profiles = PlanProfiles::new();
        let key =
            seed_refresh_profiles(&profiles, &catalog, &optimizer, &call, 2.0 / 3.0, 2_000_000);
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        for probes in 0..21 {
            let now_us = 3_000_000 + probes * 10;
            let primary = optimize_profiled_plan(
                &optimizer,
                Some(&catalog),
                &profiles,
                &guard,
                &call_json,
                now_us,
            );
            if matches!(
                serde_json::from_str::<Plan>(&primary).unwrap(),
                Plan::Rewritten { .. }
            ) {
                assert_eq!(
                    probes, 19,
                    "the original upper-p95 admission gate must still apply"
                );
                let profile = profiles.get_for_reporting(&key).unwrap();
                assert_eq!(profile.paired_observations, 39);
                assert!(
                    profile
                        .retained_divergence_samples()
                        .iter()
                        .any(|sample| sample.divergence > 0.5),
                    "refresh must retain rather than delete the adverse sample"
                );
                assert_eq!(
                    reserve_profiled_exploration(
                        &optimizer,
                        Some(&catalog),
                        &profiles,
                        &guard,
                        &controller,
                        &mut connection,
                        &call_json,
                        &primary,
                        now_us
                    ),
                    primary
                );
                assert_eq!(
                    controller
                        .snapshot(&connection, &key.call_site_version, now_us)
                        .unwrap()
                        .calls_in_window,
                    19
                );
                return;
            }
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
                value
                    .get(EXPLORATION_CONTEXT_KEY)
                    .expect("rejected bound must not freeze learning at 20 pairs")
                    .clone(),
            )
            .unwrap();
            let mut result = outcome(site);
            result.cost_usd = 0.0002;
            complete_profiled_exploration(
                &profiles,
                &controller,
                &mut connection,
                &exploration.lease_token,
                &serde_json::to_string(&result).unwrap(),
                0.0,
                now_us + 1,
            )
            .unwrap();
        }
        panic!("bounded fresh pairs should permit readmission when the retained p95 recovers");
    }

    #[test]
    fn evidence_complete_cost_or_future_rejections_do_not_request_refresh() {
        for future in [false, true] {
            let site = "no-refresh-rejection-site";
            let (catalog, optimizer) = output_optimizer(site);
            let call = observable_call(site, "reference prompt");
            let call_json = serde_json::to_string(&call).unwrap();
            let profiles = PlanProfiles::new();
            let key = seed_refresh_profiles(&profiles, &catalog, &optimizer, &call, 0.0, 2_000_000);
            let now_us = if future { 2_000_018 } else { 3_000_000 };
            if !future {
                let profile = profiles.get_for_reporting(&key).unwrap();
                profiles
                    .observe(PlanProfileUpdate {
                        cost_evidence: crate::cost_evidence::CostEvidence {
                            source: crate::cost_evidence::CostSource::ProviderReported,
                            physical_invocations: Vec::new(),
                        },
                        key: key.clone(),
                        runtime_version: profile.runtime_version,
                        input_tokens: 100,
                        output_tokens: 50,
                        latency_ms: 25.0,
                        cost_usd: 100.0,
                        output_is_structured: false,
                        output_is_short: true,
                        divergence: Some(0.0),
                        dispatch_fallback: false,
                        observed_at_us: Some(now_us - 1),
                    })
                    .unwrap();
            }
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
                now_us,
            );
            assert!(matches!(
                serde_json::from_str::<Plan>(&primary).unwrap(),
                Plan::PassThrough
            ));
            assert_eq!(
                reserve_profiled_exploration(
                    &optimizer,
                    Some(&catalog),
                    &profiles,
                    &guard,
                    &controller,
                    &mut connection,
                    &call_json,
                    &primary,
                    now_us
                ),
                primary
            );
            assert_eq!(
                controller
                    .snapshot(&connection, &key.call_site_version, now_us)
                    .unwrap()
                    .calls_in_window,
                0
            );
        }
    }

    #[test]
    fn over_budget_envelope_cancellation_preserves_provider_capacity() {
        let site = "exploration-overhead-site";
        let (catalog, optimizer) = output_optimizer(site);
        let call = observable_call(site, "private reference prompt");
        let call_json = serde_json::to_string(&call).unwrap();
        let profiles = PlanProfiles::new();
        let guard = PlanGuard::new();
        let controller =
            ExplorationController::with_policy(crate::exploration::ExplorationPolicy {
                max_calls_per_site: 1,
                ..crate::exploration::ExplorationPolicy::default()
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
        let exploration: EmbeddedExplorationContext =
            serde_json::from_value(value.get(EXPLORATION_CONTEXT_KEY).unwrap().clone()).unwrap();
        let token = decode_exploration_token(&exploration.lease_token).unwrap();

        assert_eq!(
            cancel_embedded_exploration(&controller, &mut connection, &planned),
            Ok(()),
        );
        let snapshot = controller
            .snapshot(&connection, &token.lease.key.call_site_version, now_us + 1)
            .unwrap();
        assert_eq!(snapshot.active_leases, 0);
        assert_eq!(snapshot.failed_calls, 0);
        assert_eq!(snapshot.calls_in_window, 0);
        assert_ne!(
            reserve_profiled_exploration(
                &optimizer,
                Some(&catalog),
                &profiles,
                &guard,
                &controller,
                &mut connection,
                &call_json,
                &primary,
                now_us + 2,
            ),
            primary
        );
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
        let exploration: EmbeddedExplorationContext =
            serde_json::from_value(value.get(EXPLORATION_CONTEXT_KEY).unwrap().clone()).unwrap();
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
        assert_eq!(
            attribution.plan_observation.as_ref().unwrap().key(),
            &token.lease.key
        );
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
            .snapshot(&connection, &token.lease.key.call_site_version, now_us + 10)
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
        call.parameters.extra[ROUTE_CONTEXT_KEY]["tool_calling"] = serde_json::Value::Bool(true);
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
            .query_row(
                "SELECT COUNT(*) FROM execution_plan_exploration",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn declared_action_comparator_leases_tools_under_a_new_profile_identity() {
        use crate::model_catalog::LITELLM_COMPLETION_PROTOCOL;
        for (protocol, model) in [
            (OPENAI_CHAT_COMPLETIONS_PROTOCOL, "gpt-4o"),
            (LITELLM_COMPLETION_PROTOCOL, "openai/gpt-4o"),
        ] {
            let site = "exploration-typed-tool-site";
            let (catalog, optimizer) = output_optimizer(site);
            let mut call = observable_call(site, "use the tool");
            call.model = model.to_string();
            call.tools.push(crate::dag::Tool {
                name: "mutate_state".to_string(),
                schema: serde_json::json!({"type": "object"}),
            });
            call.parameters.extra[ROUTE_CONTEXT_KEY]["provider_protocol"] = protocol.into();
            call.parameters.extra[ROUTE_CONTEXT_KEY]["tool_calling"] = true.into();
            call.parameters.extra[crate::dag::NATIVE_MESSAGES_OPAQUE_KEY] = true.into();
            let requirements = RequestRequirements::from_call(&call).unwrap();
            let legacy_shape = prompt_shape_version(&call, &requirements);
            call.parameters.extra[ROUTE_CONTEXT_KEY]["tool_action_comparator_version"] =
                "typed-actions-v1".into();
            call.parameters.extra[ROUTE_CONTEXT_KEY]["tool_settings_version"] =
                format!("tool-settings-v1:{}", "0".repeat(64)).into();
            assert!(supports_tool_exploration(&call, &requirements));
            assert_ne!(legacy_shape, prompt_shape_version(&call, &requirements));
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
            assert_eq!(value["kind"], "pass_through");
            let candidate = &value[EXPLORATION_CONTEXT_KEY]["candidate_plan"];
            assert_eq!(
                candidate["call"]["tools"],
                serde_json::to_value(&call.tools).unwrap()
            );
            assert_eq!(
                candidate["call"]["messages"],
                serde_json::to_value(&call.messages).unwrap()
            );
            assert_eq!(candidate["call"]["parameters"]["max_output_tokens"], 64);
            assert_eq!(
                fail_embedded_exploration(&controller, &mut connection, &planned, 2_000_001)
                    .unwrap(),
                ExplorationCompletion::Recorded
            );
        }
    }

    #[test]
    fn text_action_contracts_require_the_exact_supported_seam_and_new_identity() {
        use crate::model_catalog::LITELLM_COMPLETION_PROTOCOL;
        for (protocol, model) in [
            (OPENAI_CHAT_COMPLETIONS_PROTOCOL, "gpt-4o"),
            (LITELLM_COMPLETION_PROTOCOL, "openai/gpt-4o"),
        ] {
            for change in [
                "valid",
                "unknown",
                "missing_settings",
                "bad_settings",
                "unsupported",
                "stream",
                "structured",
                "tools",
            ] {
                let site = "text-action-contract-control";
                let (catalog, optimizer) = output_optimizer(site);
                let mut call = observable_call(site, "produce one bash command");
                call.model = model.into();
                call.parameters.extra[ROUTE_CONTEXT_KEY]["provider_protocol"] = protocol.into();
                let requirements = RequestRequirements::from_call(&call).unwrap();
                let old_shape = prompt_shape_version(&call, &requirements);
                let context = &mut call.parameters.extra[ROUTE_CONTEXT_KEY];
                context["text_action_comparator_version"] = TEXT_ACTION_COMPARATOR_VERSION.into();
                context["text_action_settings_version"] =
                    format!("text-action-settings-v1:{}", "a".repeat(64)).into();
                context["text_action_contract_supported"] = true.into();
                match change {
                    "unknown" => context["text_action_comparator_version"] = "unknown".into(),
                    "missing_settings" => {
                        context
                            .as_object_mut()
                            .unwrap()
                            .remove("text_action_settings_version");
                    }
                    "bad_settings" => context["text_action_settings_version"] = "invalid".into(),
                    "unsupported" => context["text_action_contract_supported"] = false.into(),
                    "stream" => context["streaming"] = true.into(),
                    "structured" => context["structured_outputs"] = true.into(),
                    "tools" => {
                        context["tool_calling"] = true.into();
                        call.tools.push(crate::dag::Tool {
                            name: "tool".into(),
                            schema: serde_json::json!({"type":"object"}),
                        });
                    }
                    _ => {}
                }
                let requirements = RequestRequirements::from_call(&call).unwrap();
                assert_ne!(old_shape, prompt_shape_version(&call, &requirements));
                assert_eq!(
                    supports_text_action_exploration(&call, &requirements),
                    change == "valid"
                );
                let profiles = PlanProfiles::new();
                let guard = PlanGuard::new();
                let controller = ExplorationController::new();
                let mut connection = Connection::open_in_memory().unwrap();
                ensure_cost_model_schema(&connection).unwrap();
                let call_json = serde_json::to_string(&call).unwrap();
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
                assert_eq!(
                    value.get(EXPLORATION_CONTEXT_KEY).is_some(),
                    change == "valid"
                );
                if change == "valid" {
                    assert_eq!(
                        fail_embedded_exploration(
                            &controller,
                            &mut connection,
                            &planned,
                            2_000_001
                        )
                        .unwrap(),
                        ExplorationCompletion::Recorded
                    );
                    let shape = prompt_shape_version(&call, &requirements);
                    call.parameters.extra[ROUTE_CONTEXT_KEY]["text_action_settings_version"] =
                        format!("text-action-settings-v1:{}", "b".repeat(64)).into();
                    assert_ne!(shape, prompt_shape_version(&call, &requirements));
                }
            }
        }
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

    fn incumbent_fixture() -> IncumbentFixture {
        let site = "incumbent-exploration-site";
        let catalog = Arc::new(default_model_catalog().unwrap());
        let cost_model = Arc::new(CostModel::new());
        for now_us in 1..=3 {
            cost_model.observe(CostModelUpdate {
                call_site_id: site.to_string(),
                input_tokens: 100,
                output_tokens: 50,
                latency_ms: 100.0,
                cost_usd: 0.01,
                output_is_structured: false,
                output_is_short: true,
                now_us: Some(now_us),
            });
        }
        let budget = Arc::new(Budget::new());
        let optimizer = Optimizer::with_budget(
            cost_model.clone(),
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
        let call = observable_call(site, "private immutable original");
        let profiles = PlanProfiles::new();
        let mut joint_key = None;
        let mut reference_key = None;
        // Fixture profiles use synthetic outcomes to isolate the admission
        // transition. They are not workload quality or savings evidence.
        for plan in optimizer.candidate_plans(&call, &catalog) {
            let context = build_observation_context(
                &catalog,
                &call,
                &plan,
                optimizer.divergence_threshold_for_plan(&plan),
            )
            .unwrap();
            let is_joint = matches!(&plan, Plan::Composed { call, .. }
                if call.model == "gpt-4o-mini-2024-07-18");
            let cost = match &plan {
                Plan::PassThrough => {
                    reference_key = Some(context.key.clone());
                    0.01
                }
                Plan::Rewritten { rule, .. } if rule == "OutputBudget" => 0.005,
                _ if is_joint => {
                    joint_key = Some(context.key.clone());
                    0.001
                }
                _ => 0.02,
            };
            for sequence in 0..if is_joint { 19 } else { 20 } {
                profiles
                    .observe(PlanProfileUpdate {
                        cost_evidence: crate::cost_evidence::CostEvidence {
                            source: crate::cost_evidence::CostSource::ProviderReported,
                            physical_invocations: Vec::new(),
                        },
                        key: context.key.clone(),
                        runtime_version: context.runtime_version.clone(),
                        input_tokens: 100,
                        output_tokens: 50,
                        latency_ms: 25.0,
                        cost_usd: cost,
                        output_is_structured: false,
                        output_is_short: true,
                        divergence: if matches!(plan, Plan::PassThrough) {
                            None
                        } else {
                            Some(0.0)
                        },
                        dispatch_fallback: false,
                        observed_at_us: Some(2_000_000 + sequence),
                    })
                    .unwrap();
            }
        }
        IncumbentFixture {
            catalog,
            cost_model,
            optimizer,
            call,
            profiles,
            joint_key: joint_key.unwrap(),
            reference_key: reference_key.unwrap(),
        }
    }

    #[test]
    fn admitted_incumbent_does_not_strand_joint_candidate_at_nineteen_pairs() {
        let f = incumbent_fixture();
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let call_json = serde_json::to_string(&f.call).unwrap();
        let primary = optimize_profiled_plan(
            &f.optimizer,
            Some(&f.catalog),
            &f.profiles,
            &guard,
            &call_json,
            3_000_000,
        );
        assert!(matches!(serde_json::from_str::<Plan>(&primary).unwrap(),
            Plan::Rewritten { rule, .. } if rule == "OutputBudget"));
        let planned = reserve_profiled_exploration(
            &f.optimizer,
            Some(&f.catalog),
            &f.profiles,
            &guard,
            &controller,
            &mut connection,
            &call_json,
            &primary,
            3_000_000,
        );
        assert!(
            matches!(
                serde_json::from_str::<Plan>(&planned).unwrap(),
                Plan::PassThrough
            ),
            "a leased comparison must expose the immutable source request"
        );
        let reference = embedded_observation_context(&planned).unwrap().unwrap();
        assert_eq!(reference.key, f.reference_key);
        assert_ne!(
            reference.key,
            embedded_observation_context(&primary).unwrap().unwrap().key
        );
        let value: serde_json::Value = serde_json::from_str(&planned).unwrap();
        let exploration: EmbeddedExplorationContext =
            serde_json::from_value(value[EXPLORATION_CONTEXT_KEY].clone()).unwrap();
        let token = decode_exploration_token(&exploration.lease_token).unwrap();
        assert_eq!(token.lease.key, f.joint_key);
        assert_eq!(
            token.context.divergence_threshold.to_bits(),
            f64::from(0.03_f32).to_bits()
        );

        assert_eq!(
            reserve_profiled_exploration(
                &f.optimizer,
                Some(&f.catalog),
                &f.profiles,
                &guard,
                &controller,
                &mut connection,
                &call_json,
                &primary,
                3_000_001
            ),
            primary,
            "a live lease must preserve the incumbent without an extra probe"
        );
        let mut reference_outcome = outcome(&f.call.call_site_id);
        reference_outcome.cost_usd = 0.01;
        let observation = optimize_observe(
            &f.cost_model,
            &f.profiles,
            &planned,
            &serde_json::to_string(&reference_outcome).unwrap(),
        )
        .unwrap();
        let observation: OpaqueObservationToken = serde_json::from_str(&observation).unwrap();
        assert!(observation.rules.is_empty());
        assert!(!observation.guard_eligible);
        complete_profiled_exploration(
            &f.profiles,
            &controller,
            &mut connection,
            &exploration.lease_token,
            &serde_json::to_string(&outcome(&f.call.call_site_id)).unwrap(),
            0.0,
            3_000_002,
        )
        .unwrap();
        assert_eq!(
            f.profiles
                .get_for_reporting(&f.joint_key)
                .unwrap()
                .paired_observations,
            20
        );
        f.profiles.flush_dirty(&mut connection).unwrap();
        let reloaded = PlanProfiles::new();
        reloaded.warm_from_db(&connection).unwrap();
        let selected = optimize_profiled_plan(
            &f.optimizer,
            Some(&f.catalog),
            &reloaded,
            &guard,
            &call_json,
            3_000_003,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&selected).unwrap(),
            Plan::Composed { .. }
        ));
        assert_eq!(
            embedded_observation_context(&selected)
                .unwrap()
                .unwrap()
                .key,
            f.joint_key
        );
        assert_eq!(
            reserve_profiled_exploration(
                &f.optimizer,
                Some(&f.catalog),
                &reloaded,
                &guard,
                &controller,
                &mut connection,
                &call_json,
                &selected,
                3_000_003
            ),
            selected,
            "no cold candidates must leave the admitted joint primary selected"
        );
        let snapshot = controller
            .snapshot(&connection, &f.joint_key.call_site_version, 3_000_003)
            .unwrap();
        assert_eq!(snapshot.calls_in_window, 1);
        assert_eq!(snapshot.active_leases, 0);
        assert_eq!(snapshot.completed_calls, 1);
        assert!((snapshot.counterfactual_cost_usd - 0.001).abs() < 1e-12);
    }

    #[test]
    fn incumbent_probe_failures_preserve_the_default_cap_after_profile_reload() {
        let f = incumbent_fixture();
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let call_json = serde_json::to_string(&f.call).unwrap();
        let primary = optimize_profiled_plan(
            &f.optimizer,
            Some(&f.catalog),
            &f.profiles,
            &guard,
            &call_json,
            3_000_000,
        );
        for sequence in 0..20 {
            let planned = reserve_profiled_exploration(
                &f.optimizer,
                Some(&f.catalog),
                &f.profiles,
                &guard,
                &controller,
                &mut connection,
                &call_json,
                &primary,
                3_000_000 + sequence,
            );
            assert!(matches!(
                serde_json::from_str::<Plan>(&planned).unwrap(),
                Plan::PassThrough
            ));
            fail_embedded_exploration(&controller, &mut connection, &planned, 3_000_000 + sequence)
                .unwrap();
        }
        f.profiles.flush_dirty(&mut connection).unwrap();
        let reloaded = PlanProfiles::new();
        reloaded.warm_from_db(&connection).unwrap();
        let restarted = ExplorationController::new();
        let selected = optimize_profiled_plan(
            &f.optimizer,
            Some(&f.catalog),
            &reloaded,
            &guard,
            &call_json,
            3_000_021,
        );
        assert_eq!(selected, primary);
        assert_eq!(
            reserve_profiled_exploration(
                &f.optimizer,
                Some(&f.catalog),
                &reloaded,
                &guard,
                &restarted,
                &mut connection,
                &call_json,
                &selected,
                3_000_021
            ),
            selected
        );
        let snapshot = restarted
            .snapshot(&connection, &f.joint_key.call_site_version, 3_000_021)
            .unwrap();
        assert_eq!(snapshot.calls_in_window, 20);
        assert_eq!(snapshot.failed_calls, 20);
        assert_eq!(snapshot.active_leases, 0);
        assert_eq!(
            reloaded
                .get_for_reporting(&f.joint_key)
                .unwrap()
                .paired_observations,
            19
        );
    }

    #[test]
    fn incumbent_cannot_override_a_disabled_cold_candidate_guard() {
        let f = incumbent_fixture();
        let guard = PlanGuard::new();
        let profile = f.profiles.get_for_reporting(&f.joint_key).unwrap();
        for i in 0..2 {
            let token = f
                .profiles
                .observe(PlanProfileUpdate {
                    cost_evidence: crate::cost_evidence::CostEvidence {
                        source: crate::cost_evidence::CostSource::ProviderReported,
                        physical_invocations: Vec::new(),
                    },
                    key: f.joint_key.clone(),
                    runtime_version: profile.runtime_version.clone(),
                    input_tokens: 100,
                    output_tokens: 50,
                    latency_ms: 25.0,
                    cost_usd: 0.001,
                    output_is_structured: false,
                    output_is_short: true,
                    divergence: None,
                    dispatch_fallback: false,
                    observed_at_us: Some(3_000_000 + i),
                })
                .unwrap();
            guard
                .record_sample(&token, 1.0, f64::from(0.03_f32), Some(3_000_000 + i))
                .unwrap();
        }
        assert!(guard
            .decision(&f.joint_key, 3_000_002)
            .blocks_user_visible());
        assert_eq!(
            f.profiles
                .get_for_reporting(&f.joint_key)
                .unwrap()
                .paired_observations,
            19
        );
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let call_json = serde_json::to_string(&f.call).unwrap();
        let primary = optimize_profiled_plan(
            &f.optimizer,
            Some(&f.catalog),
            &f.profiles,
            &guard,
            &call_json,
            3_000_002,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&primary).unwrap(),
            Plan::Rewritten { .. }
        ));
        assert_eq!(
            reserve_profiled_exploration(
                &f.optimizer,
                Some(&f.catalog),
                &f.profiles,
                &guard,
                &controller,
                &mut connection,
                &call_json,
                &primary,
                3_000_002
            ),
            primary
        );
        assert_eq!(
            controller
                .snapshot(&connection, &f.joint_key.call_site_version, 3_000_002)
                .unwrap()
                .calls_in_window,
            0
        );
    }

    #[test]
    fn incumbent_exploration_rejects_mismatched_primary_or_original_identities() {
        let f = incumbent_fixture();
        let guard = PlanGuard::new();
        let controller = ExplorationController::new();
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let call_json = serde_json::to_string(&f.call).unwrap();
        let primary = optimize_profiled_plan(
            &f.optimizer,
            Some(&f.catalog),
            &f.profiles,
            &guard,
            &call_json,
            3_000_000,
        );
        let primary_value: serde_json::Value = serde_json::from_str(&primary).unwrap();
        let mut altered_identity = primary_value.clone();
        altered_identity[OBSERVATION_CONTEXT_KEY]["key"]["execution_plan_id"] =
            serde_json::Value::String("f".repeat(64));
        let mut altered_call = primary_value.clone();
        altered_call["call"]["parameters"]["max_output_tokens"] = serde_json::json!(65);
        let mut altered_threshold = primary_value.clone();
        altered_threshold[OBSERVATION_CONTEXT_KEY]["divergence_threshold"] = serde_json::json!(0.5);
        for value in [altered_identity, altered_call, altered_threshold] {
            let mismatched = serde_json::to_string(&value).unwrap();
            assert_eq!(
                reserve_profiled_exploration(
                    &f.optimizer,
                    Some(&f.catalog),
                    &f.profiles,
                    &guard,
                    &controller,
                    &mut connection,
                    &call_json,
                    &mismatched,
                    3_000_000
                ),
                mismatched
            );
        }
        let mut another_site = f.call.clone();
        another_site.call_site_id = "another-original-site".to_string();
        assert_eq!(
            reserve_profiled_exploration(
                &f.optimizer,
                Some(&f.catalog),
                &f.profiles,
                &guard,
                &controller,
                &mut connection,
                &serde_json::to_string(&another_site).unwrap(),
                &primary,
                3_000_000
            ),
            primary
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT COUNT(*) FROM execution_plan_exploration",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
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
                    cost_evidence: crate::cost_evidence::CostEvidence {
                        source: crate::cost_evidence::CostSource::ProviderReported,
                        physical_invocations: Vec::new(),
                    },
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
                    cost_evidence: crate::cost_evidence::CostEvidence {
                        source: crate::cost_evidence::CostSource::ProviderReported,
                        physical_invocations: Vec::new(),
                    },
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
    }

    #[test]
    fn undeclared_duplicate_or_invalid_rule_configuration_has_no_production_identity() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("invalid-rule-identity", "task");
        for rules in [
            vec![
                Box::new(crate::rules::ContextCompressRule::new(8192, f32::NAN))
                    as Box<dyn RewriteRule>,
            ],
            vec![
                Box::new(AlwaysCapsOutput) as Box<dyn RewriteRule>,
                Box::new(AlwaysCapsOutput),
            ],
            vec![],
        ] {
            let optimizer = Optimizer::new(
                Arc::new(CostModel::new()),
                rules,
                OptimizerConfig::default(),
            );
            let mut plan = Plan::Rewritten {
                rule: if optimizer.accuracy_budget_for("ContextCompress").is_some() {
                    "ContextCompress".into()
                } else {
                    "OutputBudget".into()
                },
                call: original.clone(),
                projected_savings_usd: 0.01,
            };
            optimizer.bind_rule_identities(&mut plan);
            assert!(describe_plan(&catalog, &original, &plan, 0.05).is_none());
        }
    }

    #[test]
    fn configured_rule_profile_isolated_after_restart_and_in_composed_plans() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("rule-profile-cohort", "task");
        let build = |fraction| {
            Optimizer::new(
                Arc::new(CostModel::new()),
                vec![
                    Box::new(crate::rules::ContextCompressRule::new(8192, fraction)),
                    Box::new(crate::rules::OutputBudgetRule::default()),
                ],
                OptimizerConfig::default(),
            )
        };
        let left = build(0.3);
        let right = build(0.6);
        let make = || Plan::Composed {
            rules: vec![
                crate::planner::RuleApplication {
                    rule: "ContextCompress".into(),
                    projected_savings_usd: 0.01,
                    cost_driver: CostDriver::InputTokens,
                },
                crate::planner::RuleApplication {
                    rule: "OutputBudget".into(),
                    projected_savings_usd: 0.01,
                    cost_driver: CostDriver::OutputTokens,
                },
            ],
            call: original.clone(),
            net_savings_usd: 0.02,
        };
        let mut a = make();
        let mut b = make();
        left.bind_rule_identities(&mut a);
        right.bind_rule_identities(&mut b);
        let a = describe_plan(&catalog, &original, &a, 0.05).unwrap();
        let b = describe_plan(&catalog, &original, &b, 0.05).unwrap();
        let profiles = PlanProfiles::new();
        for sequence in 1..=20 {
            profiles
                .observe(PlanProfileUpdate {
                    cost_evidence: crate::cost_evidence::CostEvidence {
                        source: crate::cost_evidence::CostSource::ProviderReported,
                        physical_invocations: Vec::new(),
                    },
                    key: a.context.key.clone(),
                    runtime_version: a.context.runtime_version.clone(),
                    input_tokens: 100,
                    output_tokens: 20,
                    latency_ms: 10.0,
                    cost_usd: 0.001,
                    output_is_structured: false,
                    output_is_short: true,
                    divergence: Some(0.0),
                    dispatch_fallback: false,
                    observed_at_us: Some(sequence),
                })
                .unwrap();
        }
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        profiles.flush_dirty(&mut connection).unwrap();
        let restarted = PlanProfiles::new();
        restarted.warm_from_db(&connection).unwrap();
        assert_eq!(
            decision_profile(&restarted, &a.context)
                .unwrap()
                .paired_observations,
            20
        );
        assert!(decision_profile(&restarted, &b.context).is_none());
        let mut rebuilt = make();
        build(0.3).bind_rule_identities(&mut rebuilt);
        assert_eq!(
            describe_plan(&catalog, &original, &rebuilt, 0.05)
                .unwrap()
                .context
                .key,
            a.context.key
        );
    }

    #[test]
    fn cache_observations_do_not_spend_provider_tokens_or_resolve_provider_aliases() {
        let catalog = default_model_catalog().unwrap();
        let call = observable_call("cache-accounting", "task");
        let json = serde_json::to_string(&call).unwrap();
        let reference = attach_observation_context(Some(&catalog), &json, PASS_THROUGH_JSON, 0.05);
        let cost_model = Arc::new(CostModel::new());
        let profiles = PlanProfiles::new();
        let mut real = outcome("cache-accounting");
        real.executed_model_id = Some("gpt-4o-provider-current".into());
        optimize_observe(
            &cost_model,
            &profiles,
            &reference,
            &serde_json::to_string(&real).unwrap(),
        )
        .unwrap();
        let cached = Plan::Cached {
            value: serde_json::json!({"source":{"kind":"exact"}}),
        };
        let cached = attach_observation_context(
            Some(&catalog),
            &json,
            &serde_json::to_string(&cached).unwrap(),
            0.05,
        );
        let mut old = real.clone();
        old.executed_model_id = Some("gpt-4o-cache-historical".into());
        let token = optimize_observe(
            &cost_model,
            &profiles,
            &cached,
            &serde_json::to_string(&old).unwrap(),
        )
        .unwrap();
        let issued: OpaqueObservationToken = serde_json::from_str(&token).unwrap();
        let profile = profiles
            .get_for_reporting(issued.plan_observation.as_ref().unwrap().key())
            .unwrap();
        assert_eq!(profile.cost_usd.mean, 0.0);
        assert_eq!(profile.input_tokens.mean, 0.0);
        assert_eq!(profile.output_tokens.mean, 0.0);
        assert_eq!(
            cost_model.get("cache-accounting").unwrap().n_observations,
            1
        );
        let context = embedded_observation_context(&reference).unwrap().unwrap();
        assert_eq!(
            decision_profile(&profiles, &context)
                .unwrap()
                .runtime_version
                .target_model_version,
            "gpt-4o-provider-current"
        );
        let attribution = record_observation_divergence(&profiles, &token, 0.8).unwrap();
        assert_eq!(attribution.solo_rule.as_deref(), Some("CacheHit"));
        assert!(attribution.guard_eligible && attribution.newly_recorded);
        old.dispatch_fallback = true;
        optimize_observe(
            &cost_model,
            &profiles,
            &cached,
            &serde_json::to_string(&old).unwrap(),
        )
        .unwrap();
        assert_eq!(
            cost_model.get("cache-accounting").unwrap().n_observations,
            2
        );
        assert_eq!(
            cost_model.get("cache-accounting").unwrap().cost_usd.mean,
            real.cost_usd
        );
    }

    #[test]
    fn invalid_cache_sources_cannot_obtain_a_guarded_plan_identity() {
        let catalog = default_model_catalog().unwrap();
        let call = observable_call("invalid-cache-source", "task");
        for source in [
            serde_json::Value::Null,
            serde_json::json!({"kind":"unknown"}),
            serde_json::json!({"kind":"lsh","similarity":0.5}),
            serde_json::json!({"kind":"lsh","similarity":1.01}),
            serde_json::json!({"kind":"exact","unrecognized":true}),
        ] {
            let plan = Plan::Cached {
                value: serde_json::json!({"source":source}),
            };
            assert!(describe_plan(&catalog, &call, &plan, 0.05).is_none());
        }
    }

    #[test]
    fn cache_sources_have_separate_content_free_evidence_cohorts() {
        let catalog = default_model_catalog().unwrap();
        let call = observable_call("cache-source-cohort", "private prompt");
        let make = |source| Plan::Cached {
            value: serde_json::json!({
                "source": source, "output_content_id": "private-content-id", "age_micros": 500,
            }),
        };
        let exact = describe_plan(
            &catalog,
            &call,
            &make(serde_json::json!({"kind":"exact"})),
            0.05,
        )
        .unwrap();
        let lsh = describe_plan(
            &catalog,
            &call,
            &make(serde_json::json!({"kind":"lsh","similarity":0.96})),
            0.05,
        )
        .unwrap();
        let closer = describe_plan(
            &catalog,
            &call,
            &make(serde_json::json!({"kind":"lsh","similarity":0.99})),
            0.05,
        )
        .unwrap();
        assert_ne!(exact.context.key, lsh.context.key);
        assert_eq!(lsh.context.key, closer.context.key);
        let public = serde_json::to_string(&lsh.spec).unwrap();
        assert!(
            !public.contains("private prompt")
                && !public.contains("private-content-id")
                && !public.contains("age_micros")
        );
    }

    #[test]
    fn configured_rule_semantics_version_only_the_affected_complete_plan() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("configured-rule-identity", "private task");
        let make = |min_bytes, fraction| {
            Optimizer::new(
                Arc::new(CostModel::new()),
                vec![
                    Box::new(crate::rules::ContextCompressRule::new(min_bytes, fraction)),
                    Box::new(crate::rules::OutputBudgetRule::default()),
                ],
                OptimizerConfig::default(),
            )
        };
        let left = make(8192, 0.3);
        let right = make(16384, 0.6);
        for name in ["ContextCompress", "OutputBudget"] {
            let mut a = Plan::Rewritten {
                rule: name.into(),
                call: original.clone(),
                projected_savings_usd: 0.01,
            };
            let mut b = a.clone();
            left.bind_rule_identities(&mut a);
            right.bind_rule_identities(&mut b);
            let a = describe_plan(&catalog, &original, &a, 0.05).unwrap();
            let b = describe_plan(&catalog, &original, &b, 0.05).unwrap();
            assert_eq!(
                a.context.key.call_site_version,
                b.context.key.call_site_version
            );
            assert_eq!(a.context.runtime_version, b.context.runtime_version);
            assert_eq!(
                a.context.key == b.context.key,
                name == "OutputBudget",
                "{name}"
            );
            assert!(!serde_json::to_string(&a.spec)
                .unwrap()
                .contains("private task"));
        }
    }

    #[test]
    fn metric_contract_versions_isolate_old_normalized_profiles_and_retain_lexical() {
        let catalog = default_model_catalog().unwrap();
        let call = observable_call("metric-version-cohort", "private task");
        let plan = Plan::Rewritten {
            rule: "OutputBudget".into(),
            call: call.clone(),
            projected_savings_usd: 0.01,
        };
        let descriptor = describe_plan(&catalog, &call, &plan, 0.05).unwrap();
        for mode in ["lexical", "normalized", "embedding"] {
            let mut old = descriptor.spec.clone();
            old.validation_policy.parameters["divergence_mode"] = serde_json::json!(mode);
            old.validation_policy.implementation_version = "1".into();
            let old_key = PlanProfileKey {
                call_site_version: descriptor.context.key.call_site_version.clone(),
                execution_plan_id: old.execution_plan_id().unwrap(),
            };
            let mut current = old.clone();
            current.validation_policy.implementation_version =
                divergence_metric_version(mode).into();
            let new_key = PlanProfileKey {
                execution_plan_id: current.execution_plan_id().unwrap(),
                ..old_key.clone()
            };
            assert_eq!(old_key == new_key, mode == "lexical");
            let profiles = PlanProfiles::new();
            for i in 1..=20 {
                profiles
                    .observe(PlanProfileUpdate {
                        cost_evidence: crate::cost_evidence::CostEvidence {
                            source: crate::cost_evidence::CostSource::ProviderReported,
                            physical_invocations: Vec::new(),
                        },
                        key: old_key.clone(),
                        runtime_version: descriptor.context.runtime_version.clone(),
                        input_tokens: 100,
                        output_tokens: 50,
                        latency_ms: 25.0,
                        cost_usd: 0.001,
                        output_is_structured: false,
                        output_is_short: true,
                        divergence: Some(0.0),
                        dispatch_fallback: false,
                        observed_at_us: Some(i),
                    })
                    .unwrap();
            }
            let mut conn = Connection::open_in_memory().unwrap();
            ensure_cost_model_schema(&conn).unwrap();
            profiles.flush_dirty(&mut conn).unwrap();
            let restarted = PlanProfiles::new();
            restarted.warm_from_db(&conn).unwrap();
            assert_eq!(
                restarted
                    .get(&new_key, &descriptor.context.runtime_version)
                    .is_some(),
                mode == "lexical"
            );
            assert_eq!(
                restarted
                    .get(&old_key, &descriptor.context.runtime_version)
                    .unwrap()
                    .paired_observations,
                20
            );
        }
        assert_eq!(
            divergence_metric_version(" NORMALIZED "),
            "normalized-jaccard-v2"
        );
        assert_eq!(divergence_metric_version("unknown"), "1");
    }

    #[test]
    fn metric_contract_spec_uses_resolved_mode() {
        if let Ok(expected) = std::env::var("AGENTC_TEST_METRIC_VERSION") {
            let catalog = default_model_catalog().unwrap();
            let call = observable_call("metric-spec-version", "task");
            let plan = Plan::Rewritten {
                rule: "OutputBudget".into(),
                call: call.clone(),
                projected_savings_usd: 0.01,
            };
            let descriptor = describe_plan(&catalog, &call, &plan, 0.05).unwrap();
            assert_eq!(
                descriptor.spec.validation_policy.implementation_version,
                expected
            );
            let reference = describe_plan(&catalog, &call, &Plan::PassThrough, 0.05).unwrap();
            assert_eq!(reference.spec.validation_policy.implementation_version, "1");
            return;
        }
        // Set process environment only in isolated children, avoiding races
        // with other native tests that construct execution-plan identities.
        for (mode, version) in [
            ("lexical", "1"),
            ("normalized", "normalized-jaccard-v2"),
            ("embedding", "embedding-cosine-normalized-jaccard-v2"),
        ] {
            let result = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "ffi::tests::metric_contract_spec_uses_resolved_mode",
                ])
                .env("AGENTC_SHADOW_DIVERGENCE_MODE", mode)
                .env("AGENTC_TEST_METRIC_VERSION", version)
                .output()
                .unwrap();
            assert!(
                result.status.success(),
                "{}",
                String::from_utf8_lossy(&result.stderr)
            );
        }
    }

    #[test]
    fn compression_version_changes_solo_routed_and_composed_identities_only() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("exchange-version", "task");
        for routed in [false, true] {
            let mut selected_call = original.clone();
            if routed {
                selected_call.model = "gpt-4o-mini-2024-07-18".to_string();
            }
            for rules in [
                vec!["ContextCompress"],
                vec!["OutputBudget"],
                vec!["ContextCompress", "OutputBudget"],
                vec!["PromptDedup", "OutputBudget"],
            ] {
                let plan = if rules.len() == 1 {
                    Plan::Rewritten {
                        rule: rules[0].to_string(),
                        call: selected_call.clone(),
                        projected_savings_usd: 0.01,
                    }
                } else {
                    Plan::Composed {
                        rules: rules
                            .iter()
                            .map(|rule| crate::planner::RuleApplication {
                                rule: rule.to_string(),
                                projected_savings_usd: 0.01,
                                cost_driver: if *rule == "OutputBudget" {
                                    CostDriver::OutputTokens
                                } else {
                                    CostDriver::InputTokens
                                },
                            })
                            .collect(),
                        call: selected_call.clone(),
                        net_savings_usd: 0.02,
                    }
                };
                let descriptor = describe_plan(&catalog, &original, &plan, 0.05).unwrap();
                let mut legacy = descriptor.spec.clone();
                for rewrite in &mut legacy.rewrites {
                    rewrite.implementation_version = PLAN_IMPLEMENTATION_VERSION.to_string();
                }
                let old_id = legacy.execution_plan_id().unwrap();
                let new_id = descriptor.spec.execution_plan_id().unwrap();
                if rules.contains(&"ContextCompress") {
                    assert_ne!(old_id, new_id, "old deletion evidence must stay separate");
                } else {
                    assert_eq!(old_id, new_id, "unrelated plans retain their identity");
                }
            }
        }
    }

    #[test]
    fn legacy_compression_evidence_stays_cold_for_new_semantics_after_restart() {
        let site = "exchange-profile-version";
        let (catalog, _) = output_optimizer(site);
        let cost_model = Arc::new(CostModel::new());
        for observed_at_us in 1..=3 {
            cost_model.observe(CostModelUpdate {
                call_site_id: site.to_string(),
                input_tokens: 4000,
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
            vec![Box::new(crate::rules::ContextCompressRule::default())],
            OptimizerConfig {
                shadow_rate: 0.0,
                ..OptimizerConfig::default()
            },
        );
        let mut call = observable_call(site, "task");
        call.messages = vec![
            Message {
                role: "system".into(),
                content: "instructions".into(),
            },
            Message {
                role: "user".into(),
                content: "task".into(),
            },
            Message {
                role: "assistant".into(),
                content: "old action".into(),
            },
            Message {
                role: "user".into(),
                content: "obsolete output ".repeat(1000),
            },
            Message {
                role: "assistant".into(),
                content: "current action".into(),
            },
            Message {
                role: "user".into(),
                content: "".into(),
            },
        ];
        call.input_deps = vec![DepSource::Literal; 6];
        call.parameters.extra["attention_scores"] = serde_json::json!(vec![0.0; 6]);
        call.parameters.extra["message_deps"] = serde_json::to_value(&call.input_deps).unwrap();
        let plan = optimizer
            .candidate_plans(&call, &catalog)
            .into_iter()
            .find(|plan| matches!(plan, Plan::Rewritten { rule, .. } if rule == "ContextCompress"))
            .expect("exchange-preserving compression candidate");
        let threshold = optimizer.divergence_threshold_for_plan(&plan);
        let descriptor = describe_plan(&catalog, &call, &plan, threshold).unwrap();
        let reference = describe_plan(
            &catalog,
            &call,
            &Plan::PassThrough,
            optimizer.divergence_threshold_for_plan(&Plan::PassThrough),
        )
        .unwrap();
        let mut legacy_spec = descriptor.spec.clone();
        legacy_spec.rewrites[0].implementation_version = PLAN_IMPLEMENTATION_VERSION.into();
        let mut legacy_key = descriptor.context.key.clone();
        legacy_key.execution_plan_id = legacy_spec.execution_plan_id().unwrap();
        let profiles = PlanProfiles::new();
        let now_us = 2_000_000;
        let record = |key: PlanProfileKey, runtime_version: PlanRuntimeVersion, candidate: bool| {
            for sequence in 0..20 {
                profiles
                    .observe(PlanProfileUpdate {
                        cost_evidence: crate::cost_evidence::CostEvidence {
                            source: crate::cost_evidence::CostSource::ProviderReported,
                            physical_invocations: Vec::new(),
                        },
                        key: key.clone(),
                        runtime_version: runtime_version.clone(),
                        input_tokens: if candidate { 100 } else { 4000 },
                        output_tokens: 50,
                        latency_ms: 40.0,
                        cost_usd: if candidate { 0.001 } else { 0.01 },
                        output_is_structured: false,
                        output_is_short: true,
                        divergence: candidate.then_some(0.0),
                        dispatch_fallback: false,
                        observed_at_us: Some(now_us - 20 + sequence),
                    })
                    .unwrap();
            }
        };
        record(
            reference.context.key,
            reference.context.runtime_version,
            false,
        );
        record(
            legacy_key.clone(),
            descriptor.context.runtime_version.clone(),
            true,
        );
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        profiles.flush_dirty(&mut connection).unwrap();
        let restarted = PlanProfiles::new();
        restarted.warm_from_db(&connection).unwrap();
        assert_eq!(
            restarted
                .get_for_reporting(&legacy_key)
                .unwrap()
                .paired_observations,
            20
        );
        assert!(restarted
            .get_for_reporting(&descriptor.context.key)
            .is_none());
        let call_json = serde_json::to_string(&call).unwrap();
        let guard = PlanGuard::new();
        let selected = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &restarted,
            &guard,
            &call_json,
            now_us,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&selected).unwrap(),
            Plan::PassThrough
        ));
        record(
            descriptor.context.key.clone(),
            descriptor.context.runtime_version.clone(),
            true,
        );
        profiles.flush_dirty(&mut connection).unwrap();
        let fresh = PlanProfiles::new();
        fresh.warm_from_db(&connection).unwrap();
        let selected = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &fresh,
            &guard,
            &call_json,
            now_us,
        );
        assert!(matches!(serde_json::from_str::<Plan>(&selected).unwrap(),
            Plan::Rewritten { rule, .. } if rule == "ContextCompress"));
        assert_eq!(
            embedded_observation_context(&selected)
                .unwrap()
                .unwrap()
                .key,
            descriptor.context.key
        );
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
            cost_source: crate::multi_call::InferenceCostSource::ProviderReported,
            physical_invocations: Vec::new(),
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
            cost_source: crate::multi_call::InferenceCostSource::ProviderReported,
            physical_invocations: Vec::new(),
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
    fn provider_configuration_versions_exact_call_site_evidence() {
        let catalog = default_model_catalog().unwrap();
        let legacy = observable_call("provider-settings-site", "same input");
        let legacy_context = describe_plan(&catalog, &legacy, &Plan::PassThrough, 0.05).unwrap();
        let mut first = legacy.clone();
        first.parameters.extra[ROUTE_CONTEXT_KEY]["provider_configuration"] = serde_json::json!({
            "version": "provider-request-config-v1", "supported": true, "sha256": "a".repeat(64)
        });
        let mut second = first.clone();
        second.parameters.extra[ROUTE_CONTEXT_KEY]["provider_configuration"]["sha256"] =
            serde_json::json!("b".repeat(64));
        let first_context = describe_plan(&catalog, &first, &Plan::PassThrough, 0.05).unwrap();
        let second_context = describe_plan(&catalog, &second, &Plan::PassThrough, 0.05).unwrap();
        assert_ne!(
            legacy_context.context.key.call_site_version,
            first_context.context.key.call_site_version
        );
        assert_ne!(
            first_context.context.key.call_site_version,
            second_context.context.key.call_site_version
        );
        assert_eq!(
            first_context.context.key.execution_plan_id,
            second_context.context.key.execution_plan_id
        );
    }

    #[test]
    fn unsupported_provider_configuration_cannot_receive_profiled_evidence() {
        let catalog = default_model_catalog().unwrap();
        let mut call = observable_call("unsupported-settings-site", "same input");
        for declaration in [
            serde_json::json!({"version": "provider-request-config-v1", "supported": false, "sha256": null}),
            serde_json::json!({"version": "unknown-v2", "supported": true, "sha256": "a".repeat(64)}),
            serde_json::json!({"version": "provider-request-config-v1", "supported": true, "sha256": "not-a-hash"}),
        ] {
            call.parameters.extra[ROUTE_CONTEXT_KEY]["provider_configuration"] = declaration;
            assert!(describe_plan(&catalog, &call, &Plan::PassThrough, 0.05).is_none());
        }
    }

    #[test]
    fn unsupported_provider_configuration_abstains_in_greedy_planner() {
        let (_catalog, optimizer) = output_optimizer("unsupported-greedy-site");
        let mut call = observable_call("unsupported-greedy-site", "same input");
        assert!(matches!(optimizer.plan(&call), Plan::Rewritten { .. }));
        call.parameters.extra[ROUTE_CONTEXT_KEY]["provider_configuration"] = serde_json::json!({
            "version": "provider-request-config-v1", "supported": false, "sha256": null
        });
        assert!(matches!(optimizer.plan(&call), Plan::PassThrough));
    }

    #[test]
    fn changing_provider_configuration_cannot_borrow_an_admitted_plan() {
        let site = "provider-settings-admitted-site";
        let (catalog, optimizer) = output_optimizer(site);
        let mut call = observable_call(site, "same input");
        call.parameters.extra[ROUTE_CONTEXT_KEY]["provider_configuration"] = serde_json::json!({
            "version": "provider-request-config-v1", "supported": true, "sha256": "a".repeat(64)
        });
        let profiles = PlanProfiles::new();
        seed_refresh_profiles(&profiles, &catalog, &optimizer, &call, 0.0, 2_000_000);
        let guard = PlanGuard::new();
        let before = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &serde_json::to_string(&call).unwrap(),
            2_000_020,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&before).unwrap(),
            Plan::Rewritten { .. }
        ));
        call.parameters.extra[ROUTE_CONTEXT_KEY]["provider_configuration"]["sha256"] =
            serde_json::json!("b".repeat(64));
        let after = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &serde_json::to_string(&call).unwrap(),
            2_000_020,
        );
        assert!(matches!(
            serde_json::from_str::<Plan>(&after).unwrap(),
            Plan::PassThrough
        ));
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
    fn application_version_separates_legacy_and_declared_template_evidence() {
        let catalog = default_model_catalog().unwrap();
        let legacy = observable_call("application-version-site", "private prompt");
        let legacy_key = describe_plan(&catalog, &legacy, &Plan::PassThrough, 0.05)
            .unwrap()
            .context
            .key;
        let mut first = legacy.clone();
        first.parameters.extra[ROUTE_CONTEXT_KEY]["application_version"] =
            serde_json::json!({"version": "application-version-v1", "sha256": "a".repeat(64)});
        let first_key = describe_plan(&catalog, &first, &Plan::PassThrough, 0.05)
            .unwrap()
            .context
            .key;
        let mut second = first.clone();
        second.parameters.extra[ROUTE_CONTEXT_KEY]["application_version"]["sha256"] =
            serde_json::json!("b".repeat(64));
        let second_key = describe_plan(&catalog, &second, &Plan::PassThrough, 0.05)
            .unwrap()
            .context
            .key;
        assert_ne!(legacy_key.call_site_version, first_key.call_site_version);
        assert_ne!(first_key.call_site_version, second_key.call_site_version);
        assert_eq!(first_key.execution_plan_id, second_key.execution_plan_id);
        first.messages[0].content = "another private task".into();
        assert_eq!(
            first_key,
            describe_plan(&catalog, &first, &Plan::PassThrough, 0.05)
                .unwrap()
                .context
                .key
        );
    }

    #[test]
    fn malformed_application_version_abstains_in_all_planners() {
        let (catalog, optimizer) = output_optimizer("invalid-application-version");
        let mut call = observable_call("invalid-application-version", "private prompt");
        assert!(matches!(optimizer.plan(&call), Plan::Rewritten { .. }));
        for declaration in [
            serde_json::Value::Null,
            serde_json::json!({"version": "future", "sha256": "a".repeat(64)}),
            serde_json::json!({"version": "application-version-v1", "sha256": "bad"}),
            serde_json::json!({"version": "application-version-v1", "sha256": "a".repeat(64), "extra": true}),
        ] {
            call.parameters.extra[ROUTE_CONTEXT_KEY]["application_version"] = declaration;
            assert!(describe_plan(&catalog, &call, &Plan::PassThrough, 0.05).is_none());
            assert!(matches!(optimizer.plan(&call), Plan::PassThrough));
            let candidates = optimizer.candidate_plans(&call, &catalog);
            assert!(matches!(candidates.as_slice(), [Plan::PassThrough]));
        }
    }

    #[test]
    fn application_version_cold_starts_admission_after_profile_restart() {
        let site = "application-version-restart";
        let (catalog, optimizer) = output_optimizer(site);
        let legacy = observable_call(site, "private prompt");
        let mut first = legacy.clone();
        first.parameters.extra[ROUTE_CONTEXT_KEY]["application_version"] =
            serde_json::json!({"version": "application-version-v1", "sha256": "a".repeat(64)});
        let mut second = first.clone();
        second.parameters.extra[ROUTE_CONTEXT_KEY]["application_version"]["sha256"] =
            serde_json::json!("b".repeat(64));
        for admitted in [&legacy, &first] {
            let profiles = PlanProfiles::new();
            seed_refresh_profiles(&profiles, &catalog, &optimizer, admitted, 0.0, 2_000_000);
            let mut conn = Connection::open_in_memory().unwrap();
            ensure_cost_model_schema(&conn).unwrap();
            profiles.flush_dirty(&mut conn).unwrap();
            let restarted = PlanProfiles::new();
            restarted.warm_from_db(&conn).unwrap();
            let guard = PlanGuard::new();
            for candidate in [&legacy, &first, &second] {
                let selected = optimize_profiled_plan(
                    &optimizer,
                    Some(&catalog),
                    &restarted,
                    &guard,
                    &serde_json::to_string(candidate).unwrap(),
                    2_000_020,
                );
                let expected = std::ptr::eq(candidate, admitted);
                assert_eq!(
                    matches!(
                        serde_json::from_str::<Plan>(&selected).unwrap(),
                        Plan::Rewritten { .. }
                    ),
                    expected
                );
            }
        }
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
    fn provider_resolved_alias_is_available_to_subsequent_planning_and_reload() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("alias-lookup-site", "same input");
        let descriptor = describe_plan(&catalog, &original, &Plan::PassThrough, 0.05).unwrap();
        let plan = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            PASS_THROUGH_JSON,
            0.05,
        );
        let profiles = PlanProfiles::new();
        let mut observed = outcome("alias-lookup-site");
        observed.executed_model_id = Some("gpt-4o-2024-11-20".to_string());
        optimize_observe(
            &Arc::new(CostModel::new()),
            &profiles,
            &plan,
            &serde_json::to_string(&observed).unwrap(),
        )
        .unwrap();
        let resolved = profiles
            .get_provider_alias(&descriptor.context.key, &descriptor.context.runtime_version)
            .expect("resolved reference must be usable on the next request");
        assert_eq!(
            resolved.runtime_version.target_model_version,
            "gpt-4o-2024-11-20"
        );
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        profiles.flush_dirty(&mut connection).unwrap();
        let restarted = PlanProfiles::new();
        restarted.warm_from_db(&connection).unwrap();
        assert!(restarted
            .get_provider_alias(&descriptor.context.key, &descriptor.context.runtime_version)
            .is_some());
    }

    #[test]
    fn a_revision_observed_by_one_alias_plan_invalidates_other_plan_lookups() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("alias-shared-site", "same input");
        let reference = describe_plan(&catalog, &original, &Plan::PassThrough, 0.05).unwrap();
        let reference_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            PASS_THROUGH_JSON,
            0.05,
        );
        let mut changed = original.clone();
        changed.parameters.max_output_tokens = Some(64);
        let candidate = Plan::Rewritten {
            rule: "OutputBudget".into(),
            call: changed,
            projected_savings_usd: 0.001,
        };
        let candidate_context = describe_plan(&catalog, &original, &candidate, 0.05).unwrap();
        let candidate_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            &serde_json::to_string(&candidate).unwrap(),
            0.05,
        );
        let profiles = PlanProfiles::new();
        let costs = Arc::new(CostModel::new());
        let mut first = outcome("alias-shared-site");
        first.executed_model_id = Some("revision-A".into());
        optimize_observe(
            &costs,
            &profiles,
            &reference_json,
            &serde_json::to_string(&first).unwrap(),
        )
        .unwrap();
        assert!(profiles
            .get_provider_alias(&reference.context.key, &reference.context.runtime_version)
            .is_some());
        first.executed_model_id = Some("revision-B".into());
        optimize_observe(
            &costs,
            &profiles,
            &candidate_json,
            &serde_json::to_string(&first).unwrap(),
        )
        .unwrap();
        assert!(profiles
            .get_provider_alias(&reference.context.key, &reference.context.runtime_version)
            .is_none());
        assert!(profiles
            .get_provider_alias(
                &candidate_context.context.key,
                &candidate_context.context.runtime_version
            )
            .is_some());
        // A fallback cannot change what the provider alias was observed to mean.
        first.executed_model_id = Some("revision-C".into());
        first.dispatch_fallback = true;
        optimize_observe(
            &costs,
            &profiles,
            &reference_json,
            &serde_json::to_string(&first).unwrap(),
        )
        .unwrap();
        assert!(profiles
            .get_provider_alias(
                &candidate_context.context.key,
                &candidate_context.context.runtime_version
            )
            .is_some());
    }

    #[test]
    fn alias_resolution_allows_empirical_selection_after_restart() {
        let site = "alias-empirical-site";
        let (catalog, optimizer) = output_optimizer(site);
        let call = observable_call(site, "same input");
        let reference_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&call).unwrap(),
            PASS_THROUGH_JSON,
            0.05,
        );
        let candidate = optimizer.plan(&call);
        assert!(matches!(candidate, Plan::Rewritten { .. }));
        let candidate_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&call).unwrap(),
            &serde_json::to_string(&candidate).unwrap(),
            optimizer.divergence_threshold_for_plan(&candidate),
        );
        let profiles = PlanProfiles::new();
        let costs = Arc::new(CostModel::new());
        for _ in 0..20 {
            let mut reference = outcome(site);
            reference.executed_model_id = Some("gpt-4o-2024-11-20".into());
            reference.cost_usd = 0.03;
            optimize_observe(
                &costs,
                &profiles,
                &reference_json,
                &serde_json::to_string(&reference).unwrap(),
            )
            .unwrap();
            reference.cost_usd = 0.005;
            let token = optimize_observe(
                &costs,
                &profiles,
                &candidate_json,
                &serde_json::to_string(&reference).unwrap(),
            )
            .unwrap();
            record_observation_divergence(&profiles, &token, 0.0).unwrap();
        }
        let context = embedded_observation_context(&candidate_json)
            .unwrap()
            .unwrap();
        let now = profiles
            .get_for_reporting(&context.key)
            .unwrap()
            .last_paired_at_us
            .unwrap()
            + 1;
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        profiles.flush_dirty(&mut connection).unwrap();
        let restarted = PlanProfiles::new();
        restarted.warm_from_db(&connection).unwrap();
        let result = optimize_profiled_plan_with_audit(
            &optimizer,
            Some(&catalog),
            &restarted,
            &PlanGuard::new(),
            &serde_json::to_string(&call).unwrap(),
            now,
            true,
        );
        assert!(
            matches!(
                serde_json::from_str::<Plan>(&result).unwrap(),
                Plan::Rewritten { .. }
            ),
            "{result}"
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
