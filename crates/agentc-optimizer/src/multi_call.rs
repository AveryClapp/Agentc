//! Profiles for an observed, ordered map of real SDK calls.
//!
//! A ParallelBranch certificate is deliberately not an input: its synthetic
//! peer has no execution receipt. This module accepts complete branch plans
//! and physical outcomes from the driver that actually scheduled the work.
//! It records economics only; observing a concurrent run does not establish
//! that an arbitrary application callback is safe to reorder or replay.

use agentc_core::storage::{canonical_json, content_hash};
use serde::{Deserialize, Serialize};

use crate::dag::Outcome;
use crate::execution_plan::{ExecutionPlanId, ExecutionPlanSpec};
use crate::plan_profile::{
    CallSiteVersion, CallSiteVersionSpec, PlanObservationToken, PlanProfileKey, PlanProfileUpdate,
    PlanProfiles, PlanRuntimeVersion,
};

pub const MULTI_CALL_VERSION: &str = "agentc.ordered-map.v1";
pub const MULTI_CALL_RUNTIME_VERSION: &str = "agentc.multi-call-runtime.v1";
pub const EXACT_MAP_COMPARISON_VERSION: &str = "exact-ordered-map-results-v1";
pub const PREPARED_PROVIDER_BATCH_VERSION: &str = "independent-provider-requests-v1";

/// Position is assigned before dispatch, never from completion order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallPosition {
    pub item: u32,
    pub call: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MultiCallBranch {
    pub position: CallPosition,
    pub call_site_version: CallSiteVersion,
    pub plan: ExecutionPlanSpec,
}

/// Complete shape of a map invocation. Each item contains one or more calls
/// in program order; the result list is always assembled in input order.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MultiCallPlanSpec {
    pub application_version: String,
    pub callback_version: String,
    pub item_count: u32,
    pub max_workers: u32,
    pub branches: Vec<MultiCallBranch>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comparison_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub execution_contract: Option<String>,
}

impl MultiCallPlanSpec {
    fn canonical_branches(&self) -> Result<Vec<&MultiCallBranch>, String> {
        if self
            .execution_contract
            .as_deref()
            .is_some_and(|version| version != PREPARED_PROVIDER_BATCH_VERSION)
        {
            return Err("unsupported provider batch execution contract".into());
        }
        if self
            .comparison_version
            .as_deref()
            .is_some_and(|version| version != EXACT_MAP_COMPARISON_VERSION)
        {
            return Err("unsupported map comparison version".into());
        }
        if self.application_version.trim().is_empty() || self.callback_version.trim().is_empty() {
            return Err("map application and callback versions are required".into());
        }
        if self.item_count == 0 || self.max_workers == 0 || self.max_workers > self.item_count {
            return Err("map item/worker counts are invalid".into());
        }
        let mut branches: Vec<_> = self.branches.iter().collect();
        branches.sort_by_key(|branch| branch.position);
        let mut item = 0;
        let mut call = 0;
        for branch in &branches {
            if branch.position.item == item + 1 && call > 0 {
                item += 1;
                call = 0;
            }
            if branch.position != (CallPosition { item, call }) || item >= self.item_count {
                return Err("map calls contain missing, duplicate, or invalid positions".into());
            }
            branch
                .plan
                .execution_plan_id()
                .map_err(|error| error.to_string())?;
            call = call.checked_add(1).ok_or("map call index overflow")?;
        }
        if call == 0 || item != self.item_count - 1 {
            return Err("every map item must contain a recorded SDK call".into());
        }
        if self.execution_contract.is_some()
            && branches.iter().any(|branch| branch.position.call != 0)
        {
            return Err(
                "prepared provider batches require exactly one complete call per item".into(),
            );
        }
        Ok(branches)
    }

    pub fn profile_key(&self) -> Result<PlanProfileKey, String> {
        let branches = self.canonical_branches()?;
        let plans: Vec<_> = branches.iter().map(|branch| {
            Ok(serde_json::json!({
                "position": branch.position,
                "execution_plan_id": branch.plan.execution_plan_id().map_err(|error| error.to_string())?,
            }))
        }).collect::<Result<_, String>>()?;
        let mut identity = serde_json::json!({
            "version": MULTI_CALL_VERSION,
            "application_version": self.application_version,
            "callback_version": self.callback_version,
            "item_count": self.item_count,
            "max_workers": self.max_workers,
            "result_aggregation": "input-order-v1",
            "validation": self.comparison_version.as_deref().unwrap_or("observe-only-v1"),
            "branches": plans,
        });
        if let Some(contract) = &self.execution_contract {
            identity["execution_contract"] = serde_json::json!(contract);
        }
        let sites: Vec<_> = branches
            .iter()
            .map(|branch| {
                serde_json::json!({
                    "position": branch.position, "call_site_version": branch.call_site_version,
                })
            })
            .collect();
        let mut shape = serde_json::json!({
            "version": MULTI_CALL_VERSION, "item_count": self.item_count,
            "callback_version": self.callback_version, "branches": sites,
        });
        if let Some(contract) = &self.execution_contract {
            shape["execution_contract"] = serde_json::json!(contract);
        }
        Ok(PlanProfileKey {
            execution_plan_id: ExecutionPlanId::parse(content_hash(&canonical_json(&identity)))
                .map_err(|error| error.to_string())?,
            call_site_version: CallSiteVersion::from_spec(&CallSiteVersionSpec {
                call_site_id: MULTI_CALL_VERSION.into(),
                prompt_shape_version: content_hash(&canonical_json(&shape)),
                provider_protocol: MULTI_CALL_RUNTIME_VERSION.into(),
                tool_schema_version: None,
                application_config_version: Some(self.application_version.clone()),
            })
            .map_err(|error| error.to_string())?,
        })
    }
}

impl crate::execution_plan::PlanSemantics for MultiCallPlanSpec {
    fn execution_plan_id(
        &self,
    ) -> Result<ExecutionPlanId, crate::execution_plan::PlanIdentityError> {
        self.profile_key()
            .map(|key| key.execution_plan_id)
            .map_err(crate::execution_plan::PlanIdentityError::InvalidMultiCallPlan)
    }

    fn mutation_count(&self) -> usize {
        usize::from(self.max_workers > 1)
            + self
                .branches
                .iter()
                .map(|branch| {
                    usize::from(branch.plan.target_model_id != branch.plan.requested_model_id)
                        + branch.plan.rewrites.len()
                })
                .sum::<usize>()
    }
}

/// A heterogeneous vector, not the runtime version of the first branch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MultiCallRuntimeBranch {
    pub position: CallPosition,
    pub runtime: PlanRuntimeVersion,
}

/// Encode the full vector in the existing durable version columns. The
/// protocol is explicitly a map-runtime schema; model and price columns are
/// canonical JSON arrays. No single-call key or provider alias can match it.
/// Keeping the full vector (rather than just its hash) permits offline audits
/// without changing the single-call identity or SQLite schema.
pub fn profile_runtime(
    spec: &MultiCallPlanSpec,
    versions: &[MultiCallRuntimeBranch],
) -> Result<PlanRuntimeVersion, String> {
    let branches = spec.canonical_branches()?;
    let mut versions: Vec<_> = versions.iter().collect();
    versions.sort_by_key(|version| version.position);
    if versions.len() != branches.len() {
        return Err("map runtime vector does not cover every call".into());
    }
    for (branch, version) in branches.iter().zip(&versions) {
        let runtime = &version.runtime;
        if branch.position != version.position
            || branch.plan.provider_protocol != runtime.provider_protocol
            || branch.plan.target_model_id != runtime.target_model_id
            || runtime.target_model_version.trim().is_empty()
            || runtime.price_table_version.trim().is_empty()
        {
            return Err("map runtime does not match its branch plan".into());
        }
    }
    let models: Vec<_> = versions
        .iter()
        .map(|version| &version.runtime.target_model_id)
        .collect();
    let prices: Vec<_> = versions
        .iter()
        .map(|version| &version.runtime.price_table_version)
        .collect();
    let encode = |value: serde_json::Value| canonical_json(&value);
    Ok(PlanRuntimeVersion {
        provider_protocol: MULTI_CALL_RUNTIME_VERSION.into(),
        target_model_id: encode(serde_json::json!(models)),
        target_model_version: encode(serde_json::json!(versions)),
        price_table_version: encode(serde_json::json!(prices)),
    })
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InferenceCostSource {
    ProviderReported,
    NoInference,
    Estimated,
    #[default]
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MultiCallOutcome {
    pub position: CallPosition,
    pub cost_source: InferenceCostSource,
    pub outcome: Outcome,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MultiCallTotals {
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub cost_usd: f64,
    pub wall_latency_ms: f64,
    pub summed_call_latency_ms: f64,
    pub call_count: u32,
}

/// Reject partial accounting before touching the profile store. Wall time is
/// measured by the map driver, including queueing and callback/result work;
/// neither the maximum nor the sum of individual call durations replaces it.
pub fn aggregate_outcomes(
    spec: &MultiCallPlanSpec,
    outcomes: &[MultiCallOutcome],
    wall_latency_ms: f64,
) -> Result<MultiCallTotals, String> {
    aggregate_metrics(spec, outcomes, wall_latency_ms, true)
}

/// Retain original execution metrics while billing is unresolved. This does
/// not observe a profile or declare the estimated cost complete.
pub(crate) fn aggregate_original_evidence(
    spec: &MultiCallPlanSpec,
    outcomes: &[MultiCallOutcome],
    wall_latency_ms: f64,
) -> Result<MultiCallTotals, String> {
    aggregate_metrics(spec, outcomes, wall_latency_ms, false)
}

fn aggregate_metrics(
    spec: &MultiCallPlanSpec,
    outcomes: &[MultiCallOutcome],
    wall_latency_ms: f64,
    require_billing: bool,
) -> Result<MultiCallTotals, String> {
    let branches = spec.canonical_branches()?;
    if !wall_latency_ms.is_finite() || wall_latency_ms < 0.0 {
        return Err("map wall latency must be finite and nonnegative".into());
    }
    let mut outcomes: Vec<_> = outcomes.iter().collect();
    outcomes.sort_by_key(|outcome| outcome.position);
    if outcomes.len() != branches.len() {
        return Err("map accounting does not cover every call".into());
    }
    let mut totals = MultiCallTotals {
        input_tokens: 0,
        output_tokens: 0,
        cost_usd: 0.0,
        wall_latency_ms,
        summed_call_latency_ms: 0.0,
        call_count: u32::try_from(branches.len()).map_err(|_| "map call count overflow")?,
    };
    for (branch, measured) in branches.iter().zip(outcomes) {
        if branch.position != measured.position {
            return Err("map accounting contains missing or duplicate call positions".into());
        }
        let mut original = measured.outcome.clone();
        if !require_billing
            && original.additional_inference.is_none()
            && matches!(
                measured.cost_source,
                InferenceCostSource::Estimated | InferenceCostSource::Unknown
            )
        {
            original.inference_cost_complete = None;
        }
        let outcome = original.including_additional_inference()?;
        match measured.cost_source {
            InferenceCostSource::ProviderReported => {}
            InferenceCostSource::Estimated | InferenceCostSource::Unknown if !require_billing => {}
            InferenceCostSource::NoInference
                if branch.plan.cache_policy.stable_name == "memoized-response"
                    && outcome.input_tokens == 0
                    && outcome.output_tokens == 0
                    && outcome.cost_usd == 0.0 => {}
            _ => {
                return Err("map inference billing is not complete provider-reported usage".into())
            }
        }
        // The first adapter path accepts only original requests. Auxiliary
        // calls need their own versioned receipts, not unversioned extra cost.
        if measured.outcome.additional_inference.is_some()
            || outcome.dispatch_fallback
            || outcome.validation_rejected
        {
            return Err("map branch requires explicit auxiliary/fallback call receipts".into());
        }
        if outcome.latency_ms > wall_latency_ms + 1.0 {
            return Err("map call duration exceeds measured map wall time".into());
        }
        totals.input_tokens = totals
            .input_tokens
            .checked_add(outcome.input_tokens)
            .ok_or("map input tokens overflow")?;
        totals.output_tokens = totals
            .output_tokens
            .checked_add(outcome.output_tokens)
            .ok_or("map output tokens overflow")?;
        totals.cost_usd += outcome.cost_usd;
        totals.summed_call_latency_ms += outcome.latency_ms;
        if !totals.cost_usd.is_finite() || !totals.summed_call_latency_ms.is_finite() {
            return Err("map metric sum overflow".into());
        }
    }
    Ok(totals)
}

/// A measured batch is unpaired evidence. No observation handle is exposed to
/// the single-call divergence API, and no pair/admission is manufactured.
pub fn observe_multi_call(
    profiles: &PlanProfiles,
    spec: &MultiCallPlanSpec,
    versions: &[MultiCallRuntimeBranch],
    outcomes: &[MultiCallOutcome],
    wall_latency_ms: f64,
    observed_at_us: Option<i64>,
) -> Result<(PlanObservationToken, MultiCallTotals), String> {
    let key = spec.profile_key()?;
    let runtime_version = profile_runtime(spec, versions)?;
    let totals = aggregate_outcomes(spec, outcomes, wall_latency_ms)?;
    let observation = profiles
        .observe(PlanProfileUpdate {
            cost_evidence: crate::cost_evidence::CostEvidence {
                source: outcomes.iter().fold(
                    crate::cost_evidence::CostSource::NoInference,
                    |source, measured| {
                        source.combine(crate::cost_evidence::CostSource::from(
                            &measured.cost_source,
                        ))
                    },
                ),
                physical_invocations: outcomes
                    .iter()
                    .flat_map(|measured| measured.outcome.physical_invocations.iter().cloned())
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
            observed_at_us,
        })
        .map_err(|error| error.to_string())?;
    Ok((observation, totals))
}
