//! Content-free diagnostics for one complete-plan selection attempt.
//!
//! The planner embeds this record beside its internal observation context. The
//! Python adapters carry it opaquely, and the audit writer persists it for
//! `agentc optimize inspect`. No prompt, response, tool arguments, credentials,
//! or provider-native payloads enter the record.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;

use crate::config::OptimizerConfig;
use crate::execution_plan::{
    CandidatePlan, CandidateRejectionReason, ExecutionPlanId, PlanEstimate, Selection,
    SelectionObjective, SelectionReason,
};
use crate::plan_profile::CallSiteVersion;

pub const PLANNER_DIAGNOSTICS_KEY: &str = "agentc_planner_diagnostics";
pub const PLANNER_DIAGNOSTICS_SCHEMA_VERSION: u16 = 1;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlannerRiskContract {
    pub objective: SelectionObjective,
    pub min_paired_observations: u32,
    pub profile_freshness_hours: f64,
    pub max_rewrite_depth: usize,
    pub shadow_rate: f32,
    pub exploration_enabled: bool,
    pub exploration_calls_per_site_24h: u32,
    pub max_concurrent_counterfactuals: u32,
    pub divergence_exposure_budget: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub global_divergence_threshold: Option<f64>,
    pub evaluation_task_damage_budget: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub evaluation_non_inferiority_margin: Option<f64>,
    pub task_quality_scope: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub configuration_error: Option<String>,
}

impl From<&OptimizerConfig> for PlannerRiskContract {
    fn from(config: &OptimizerConfig) -> Self {
        Self {
            objective: config.objective,
            min_paired_observations: config.min_plan_evidence,
            profile_freshness_hours: config.plan_profile_freshness_hours,
            max_rewrite_depth: config.max_rewrite_depth,
            shadow_rate: config.shadow_rate,
            exploration_enabled: config.exploration_enabled,
            exploration_calls_per_site_24h: config.exploration_calls_per_site_24h,
            max_concurrent_counterfactuals: config.max_concurrent_counterfactuals,
            divergence_exposure_budget: config.divergence_exposure_budget,
            global_divergence_threshold: config.global_divergence_threshold,
            evaluation_task_damage_budget: config.evaluation_task_damage_budget,
            evaluation_non_inferiority_margin: config.evaluation_non_inferiority_margin,
            task_quality_scope: "evaluation_only".to_string(),
            configuration_error: config.configuration_error.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CandidateDecisionDiagnostic {
    pub plan_id: ExecutionPlanId,
    pub provider_protocol: String,
    pub target_model_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_model_revision: Option<String>,
    pub ordered_rewrites: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_budget: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub estimate: Option<PlanEstimate>,
    pub evidence_confidence: f64,
    pub divergence_threshold: f64,
    pub divergence_exposure: f64,
    pub selected: bool,
    pub admissible: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rejection_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlannerDecisionDiagnostics {
    pub schema_version: u16,
    pub call_site_version: CallSiteVersion,
    pub risk: PlannerRiskContract,
    pub reference_plan_id: ExecutionPlanId,
    pub selected_plan_id: ExecutionPlanId,
    pub selected_reference: bool,
    pub selection_reason: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fallback_reason: Option<String>,
    pub candidates: Vec<CandidateDecisionDiagnostic>,
}

impl PlannerDecisionDiagnostics {
    pub fn from_selection(
        config: &OptimizerConfig,
        call_site_version: &CallSiteVersion,
        reference: &CandidatePlan,
        alternatives: &[CandidatePlan],
        selection: &Selection<'_>,
    ) -> Self {
        let rejections: HashMap<&str, String> = selection
            .rejections
            .iter()
            .map(|rejection| {
                (
                    rejection.plan_id.as_str(),
                    rejection_reason_code(&rejection.reason),
                )
            })
            .collect();
        let candidates = alternatives
            .iter()
            .map(|candidate| {
                let rejection_reason = rejections.get(candidate.id.as_str()).cloned();
                CandidateDecisionDiagnostic {
                    plan_id: candidate.id.clone(),
                    provider_protocol: candidate.spec.provider_protocol.clone(),
                    target_model_id: candidate.spec.target_model_id.clone(),
                    target_model_revision: candidate.spec.target_model_revision.clone(),
                    ordered_rewrites: candidate
                        .spec
                        .rewrites
                        .iter()
                        .map(|rewrite| rewrite.stable_name.clone())
                        .collect(),
                    output_budget: candidate.spec.output_budget,
                    estimate: candidate.estimate.clone(),
                    evidence_confidence: candidate
                        .estimate
                        .as_ref()
                        .map(|estimate| {
                            f64::from(estimate.paired_observations)
                                / f64::from(config.min_plan_evidence)
                        })
                        .unwrap_or(0.0)
                        .clamp(0.0, 1.0),
                    divergence_threshold: candidate.admission.divergence_threshold,
                    divergence_exposure: candidate.admission.divergence_exposure,
                    selected: !selection.selected_reference
                        && candidate.id == selection.selected.id,
                    admissible: rejection_reason.is_none(),
                    rejection_reason,
                }
            })
            .collect();
        let selection_reason = selection_reason_code(&selection.reason);
        let fallback_reason = if selection.selected_reference {
            Some(if config.configuration_error.is_some() {
                "invalid_configuration".to_string()
            } else {
                selection_reason.clone()
            })
        } else {
            None
        };
        Self {
            schema_version: PLANNER_DIAGNOSTICS_SCHEMA_VERSION,
            call_site_version: call_site_version.clone(),
            risk: PlannerRiskContract::from(config),
            reference_plan_id: reference.id.clone(),
            selected_plan_id: selection.selected.id.clone(),
            selected_reference: selection.selected_reference,
            selection_reason,
            fallback_reason,
            candidates,
        }
    }

    pub fn force_fallback(&mut self, reason: impl Into<String>) {
        self.selected_plan_id = self.reference_plan_id.clone();
        self.selected_reference = true;
        self.fallback_reason = Some(reason.into());
        for candidate in &mut self.candidates {
            candidate.selected = false;
        }
    }
}

pub fn attach_planner_diagnostics(
    plan_json: String,
    diagnostics: &PlannerDecisionDiagnostics,
) -> String {
    let Ok(mut value) = serde_json::from_str::<serde_json::Value>(&plan_json) else {
        return plan_json;
    };
    let Some(object) = value.as_object_mut() else {
        return plan_json;
    };
    let Ok(encoded) = serde_json::to_value(diagnostics) else {
        return plan_json;
    };
    object.insert(PLANNER_DIAGNOSTICS_KEY.to_string(), encoded);
    serde_json::to_string(&value).unwrap_or(plan_json)
}

pub fn extract_planner_diagnostics(plan_json: &str) -> Option<PlannerDecisionDiagnostics> {
    serde_json::from_str::<serde_json::Value>(plan_json)
        .ok()?
        .get(PLANNER_DIAGNOSTICS_KEY)
        .cloned()
        .and_then(|value| serde_json::from_value(value).ok())
        .filter(|diagnostics: &PlannerDecisionDiagnostics| {
            diagnostics.schema_version == PLANNER_DIAGNOSTICS_SCHEMA_VERSION
        })
}

/// Extract the exact embedded diagnostics JSON after validating its versioned
/// typed schema. Audit persistence uses this instead of serializing the typed
/// value again: a JSON -> floating-point -> JSON round trip can change the
/// shortest decimal representation and would make the audit differ from the
/// decision actually returned to the adapter.
pub fn extract_planner_diagnostics_json(plan_json: &str) -> Option<String> {
    #[derive(Deserialize)]
    struct DiagnosticsEnvelope<'a> {
        #[serde(borrow)]
        agentc_planner_diagnostics: Option<&'a RawValue>,
    }

    let envelope: DiagnosticsEnvelope<'_> = serde_json::from_str(plan_json).ok()?;
    let embedded = envelope.agentc_planner_diagnostics?;
    let diagnostics: PlannerDecisionDiagnostics = serde_json::from_str(embedded.get()).ok()?;
    if diagnostics.schema_version != PLANNER_DIAGNOSTICS_SCHEMA_VERSION {
        return None;
    }
    Some(embedded.get().to_string())
}

fn selection_reason_code(reason: &SelectionReason) -> String {
    match reason {
        SelectionReason::BestAdmissibleAlternative => "best_admissible_alternative".to_string(),
        SelectionReason::NoAdmissibleAlternative => "no_admissible_alternative".to_string(),
        SelectionReason::InvalidPolicy(error) => format!("invalid_policy:{error}"),
    }
}

fn rejection_reason_code(reason: &CandidateRejectionReason) -> String {
    match reason {
        CandidateRejectionReason::RequestIncompatible => "request_incompatible".to_string(),
        CandidateRejectionReason::Disabled => "disabled".to_string(),
        CandidateRejectionReason::MissingEstimate => "missing_estimate".to_string(),
        CandidateRejectionReason::InsufficientEvidence { observed, required } => {
            format!("insufficient_evidence:{observed}/{required}")
        }
        CandidateRejectionReason::NonFiniteEstimate { field } => {
            format!("non_finite_estimate:{field}")
        }
        CandidateRejectionReason::NegativeEstimate { field } => {
            format!("negative_estimate:{field}")
        }
        CandidateRejectionReason::DivergenceOutOfRange => "divergence_out_of_range".to_string(),
        CandidateRejectionReason::DivergenceExceeded {
            upper_p95,
            threshold,
        } => format!("divergence_exceeded:{upper_p95:.6}>{threshold:.6}"),
        CandidateRejectionReason::InvalidAdmissionThreshold => {
            "invalid_admission_threshold".to_string()
        }
        CandidateRejectionReason::InvalidExposure => "invalid_exposure".to_string(),
        CandidateRejectionReason::ExposureBudgetExceeded { exposure, budget } => {
            format!("exposure_budget_exceeded:{exposure:.6}>={budget:.6}")
        }
        CandidateRejectionReason::ObservationFromFuture => "observation_from_future".to_string(),
        CandidateRejectionReason::StaleProfile { age_us, max_age_us } => {
            format!("stale_profile:{age_us}>{max_age_us}")
        }
        CandidateRejectionReason::NonPositiveNetBenefit { objective, value } => {
            let objective = match objective {
                SelectionObjective::Cost => "cost",
                SelectionObjective::Latency => "latency",
            };
            format!("non_positive_net_{objective}_benefit:{value:.6}")
        }
        CandidateRejectionReason::CostLimitExceeded { expected, limit } => {
            format!("cost_limit_exceeded:{expected:.6}>{limit:.6}")
        }
        CandidateRejectionReason::LatencyLimitExceeded { expected, limit } => {
            format!("latency_limit_exceeded:{expected:.6}>{limit:.6}")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::execution_plan::{
        CachePolicy, ExecutionPlanSpec, PlanAdmission, RewriteOrdering, SelectionPolicy,
        ValidationPolicy, EXECUTION_PLAN_SCHEMA_VERSION,
    };

    fn candidate(target: &str, estimate: Option<PlanEstimate>) -> CandidatePlan {
        CandidatePlan::new(
            ExecutionPlanSpec {
                schema_version: EXECUTION_PLAN_SCHEMA_VERSION,
                provider_protocol: "openai.chat.completions.v1".to_string(),
                requested_model_id: "strong".to_string(),
                target_model_id: target.to_string(),
                target_model_revision: Some("v1".to_string()),
                rewrite_ordering: RewriteOrdering::Ordered,
                rewrites: Vec::new(),
                cache_policy: CachePolicy {
                    stable_name: "provider-call".to_string(),
                    implementation_version: "1".to_string(),
                    parameters: serde_json::json!({}),
                },
                output_budget: None,
                validation_policy: ValidationPolicy {
                    stable_name: "sampled-reference".to_string(),
                    implementation_version: "1".to_string(),
                    parameters: serde_json::json!({}),
                },
            },
            estimate,
            PlanAdmission {
                request_compatible: true,
                disabled: false,
                divergence_threshold: 0.1,
                divergence_exposure: 0.0,
            },
        )
        .unwrap()
    }

    #[test]
    fn diagnostics_explain_selection_and_rejection_without_content() {
        let reference = candidate("strong", None);
        let admitted = candidate(
            "cheap-a",
            Some(PlanEstimate {
                paired_observations: 20,
                expected_cost_usd: 0.01,
                expected_latency_ms: 10.0,
                divergence_upper_p95: 0.02,
                expected_net_cost_savings_usd: 0.02,
                expected_net_latency_savings_ms: 5.0,
                last_observed_at_us: 90,
            }),
        );
        let cold = candidate("cheap-b", None);
        let alternatives = vec![admitted, cold];
        let selection = crate::execution_plan::select_candidate(
            &reference,
            &alternatives,
            &SelectionPolicy {
                now_us: 100,
                ..SelectionPolicy::default()
            },
        );
        let diagnostics = PlannerDecisionDiagnostics::from_selection(
            &OptimizerConfig::default(),
            &CallSiteVersion::parse("c".repeat(64)).unwrap(),
            &reference,
            &alternatives,
            &selection,
        );
        assert!(!diagnostics.selected_reference);
        assert_eq!(diagnostics.candidates.len(), 2);
        assert!(diagnostics.candidates[0].selected);
        assert_eq!(diagnostics.candidates[0].evidence_confidence, 1.0);
        assert_eq!(
            diagnostics.candidates[1].rejection_reason.as_deref(),
            Some("missing_estimate")
        );

        let encoded =
            attach_planner_diagnostics("{\"kind\":\"pass_through\"}".to_string(), &diagnostics);
        assert_eq!(extract_planner_diagnostics(&encoded), Some(diagnostics));
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(
                &extract_planner_diagnostics_json(&encoded).unwrap()
            )
            .unwrap(),
            serde_json::from_str::<serde_json::Value>(&encoded).unwrap()[PLANNER_DIAGNOSTICS_KEY]
        );
        assert!(!encoded.contains("prompt"));
        assert!(!encoded.contains("response"));
    }
}
