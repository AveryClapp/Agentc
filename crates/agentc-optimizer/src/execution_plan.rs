//! Canonical execution-plan identity and constrained plan selection.
//!
//! This module is deliberately provider-free. Candidate generation turns a
//! concrete request into [`ExecutionPlanSpec`] values, profile storage attaches
//! [`PlanEstimate`] values, and this module makes one deterministic decision.
//! The immutable reference request is passed separately and wins whenever the
//! policy is invalid or no alternative is admissible.

use std::cmp::Ordering;
use std::error::Error;
use std::fmt;

use agentc_core::storage::{canonical_json, content_hash};
use serde::{de::Error as _, Deserialize, Deserializer, Serialize};

/// Schema version included in every new execution-plan identity.
pub const EXECUTION_PLAN_SCHEMA_VERSION: u16 = 1;

/// Stable SHA-256 identity for one complete model-and-rewrite plan.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct ExecutionPlanId(String);

impl ExecutionPlanId {
    /// Parse a canonical lowercase SHA-256 digest.
    pub fn parse(value: impl Into<String>) -> Result<Self, PlanIdentityError> {
        let value = value.into();
        if value.len() != 64
            || !value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err(PlanIdentityError::InvalidDigest);
        }
        Ok(Self(value))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl<'de> Deserialize<'de> for ExecutionPlanId {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        Self::parse(value).map_err(D::Error::custom)
    }
}

impl fmt::Display for ExecutionPlanId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// Whether rewrite order carries semantic meaning for this plan.
///
/// `CanonicalCommuting` is an assertion made by the candidate generator after
/// it proves that all listed operations commute. The identity layer does not
/// attempt to infer commutativity from rewrite names or cost drivers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RewriteOrdering {
    Ordered,
    CanonicalCommuting,
}

/// One semantic transformation and the implementation that produced it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RewriteApplication {
    pub stable_name: String,
    pub implementation_version: String,
    #[serde(default)]
    pub parameters: serde_json::Value,
}

/// Cache behavior is part of plan semantics even when no model call occurs.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CachePolicy {
    pub stable_name: String,
    pub implementation_version: String,
    #[serde(default)]
    pub parameters: serde_json::Value,
}

/// Counterfactual/exploration behavior attached to a plan.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ValidationPolicy {
    pub stable_name: String,
    pub implementation_version: String,
    #[serde(default)]
    pub parameters: serde_json::Value,
}

/// Complete semantic identity of an executable alternative.
///
/// Price is intentionally absent: it belongs to versioned observation
/// metadata and may change without changing what the plan executes.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExecutionPlanSpec {
    pub schema_version: u16,
    pub provider_protocol: String,
    pub requested_model_id: String,
    pub target_model_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_model_revision: Option<String>,
    pub rewrite_ordering: RewriteOrdering,
    #[serde(default)]
    pub rewrites: Vec<RewriteApplication>,
    pub cache_policy: CachePolicy,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_budget: Option<u32>,
    pub validation_policy: ValidationPolicy,
}

impl ExecutionPlanSpec {
    /// Compute a stable identity after validating all identity-bearing fields.
    pub fn execution_plan_id(&self) -> Result<ExecutionPlanId, PlanIdentityError> {
        self.validate_identity_fields()?;

        let mut canonical = self.clone();
        if canonical.rewrite_ordering == RewriteOrdering::CanonicalCommuting {
            canonical.rewrites.sort_by(canonical_rewrite_cmp);
        }
        let value = serde_json::to_value(canonical)
            .map_err(|error| PlanIdentityError::Serialization(error.to_string()))?;
        Ok(ExecutionPlanId(content_hash(&canonical_json(&value))))
    }

    fn validate_identity_fields(&self) -> Result<(), PlanIdentityError> {
        if self.schema_version == 0 {
            return Err(PlanIdentityError::ZeroSchemaVersion);
        }
        require_nonempty("provider_protocol", &self.provider_protocol)?;
        require_nonempty("requested_model_id", &self.requested_model_id)?;
        require_nonempty("target_model_id", &self.target_model_id)?;
        if let Some(revision) = &self.target_model_revision {
            require_nonempty("target_model_revision", revision)?;
        }
        require_nonempty("cache_policy.stable_name", &self.cache_policy.stable_name)?;
        require_nonempty(
            "cache_policy.implementation_version",
            &self.cache_policy.implementation_version,
        )?;
        require_nonempty(
            "validation_policy.stable_name",
            &self.validation_policy.stable_name,
        )?;
        require_nonempty(
            "validation_policy.implementation_version",
            &self.validation_policy.implementation_version,
        )?;
        for rewrite in &self.rewrites {
            require_nonempty("rewrite.stable_name", &rewrite.stable_name)?;
            require_nonempty(
                "rewrite.implementation_version",
                &rewrite.implementation_version,
            )?;
        }
        Ok(())
    }
}

fn canonical_rewrite_cmp(left: &RewriteApplication, right: &RewriteApplication) -> Ordering {
    left.stable_name
        .cmp(&right.stable_name)
        .then_with(|| {
            left.implementation_version
                .cmp(&right.implementation_version)
        })
        .then_with(|| canonical_json(&left.parameters).cmp(&canonical_json(&right.parameters)))
}

fn require_nonempty(field: &'static str, value: &str) -> Result<(), PlanIdentityError> {
    if value.trim().is_empty() {
        Err(PlanIdentityError::EmptyField(field))
    } else {
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PlanIdentityError {
    ZeroSchemaVersion,
    EmptyField(&'static str),
    InvalidDigest,
    Serialization(String),
}

impl fmt::Display for PlanIdentityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ZeroSchemaVersion => formatter.write_str("execution-plan schema version is zero"),
            Self::EmptyField(field) => write!(formatter, "execution-plan field {field} is empty"),
            Self::InvalidDigest => formatter
                .write_str("execution-plan ID must be a 64-character lowercase SHA-256 digest"),
            Self::Serialization(message) => {
                write!(formatter, "execution-plan serialization failed: {message}")
            }
        }
    }
}

impl Error for PlanIdentityError {}

/// Observed estimate for this exact plan at one versioned call site.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlanEstimate {
    pub paired_observations: u32,
    pub expected_cost_usd: f64,
    pub expected_latency_ms: f64,
    pub divergence_upper_p95: f64,
    pub expected_net_cost_savings_usd: f64,
    pub expected_net_latency_savings_ms: f64,
    pub last_observed_at_us: i64,
}

/// Current compatibility and guard state for a candidate.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlanAdmission {
    pub request_compatible: bool,
    pub disabled: bool,
    pub divergence_threshold: f64,
    pub divergence_exposure: f64,
}

/// A complete alternative passed to the selector.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CandidatePlan {
    pub id: ExecutionPlanId,
    pub spec: ExecutionPlanSpec,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub estimate: Option<PlanEstimate>,
    pub admission: PlanAdmission,
}

impl CandidatePlan {
    pub fn new(
        spec: ExecutionPlanSpec,
        estimate: Option<PlanEstimate>,
        admission: PlanAdmission,
    ) -> Result<Self, PlanIdentityError> {
        let id = spec.execution_plan_id()?;
        Ok(Self {
            id,
            spec,
            estimate,
            admission,
        })
    }

    pub fn mutation_count(&self) -> usize {
        let model_mutation = usize::from(self.spec.target_model_id != self.spec.requested_model_id);
        model_mutation + self.spec.rewrites.len()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SelectionObjective {
    Cost,
    Latency,
}

/// Global constraints applied uniformly to every alternative.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SelectionPolicy {
    pub objective: SelectionObjective,
    pub min_paired_observations: u32,
    pub now_us: i64,
    pub max_profile_age_us: i64,
    pub divergence_exposure_budget: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_expected_cost_usd: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_expected_latency_ms: Option<f64>,
}

impl Default for SelectionPolicy {
    fn default() -> Self {
        Self {
            objective: SelectionObjective::Cost,
            min_paired_observations: 20,
            now_us: 0,
            max_profile_age_us: 24 * 60 * 60 * 1_000_000,
            divergence_exposure_budget: 1.0,
            max_expected_cost_usd: None,
            max_expected_latency_ms: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum SelectionPolicyError {
    ZeroEvidenceFloor,
    NegativeClock,
    NegativeFreshnessHorizon,
    InvalidExposureBudget,
    InvalidCostLimit,
    InvalidLatencyLimit,
}

impl fmt::Display for SelectionPolicyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ZeroEvidenceFloor => formatter.write_str("minimum evidence must be positive"),
            Self::NegativeClock => formatter.write_str("selection clock must be non-negative"),
            Self::NegativeFreshnessHorizon => {
                formatter.write_str("profile freshness horizon must be non-negative")
            }
            Self::InvalidExposureBudget => {
                formatter.write_str("divergence exposure budget must be finite and non-negative")
            }
            Self::InvalidCostLimit => {
                formatter.write_str("cost limit must be finite and non-negative")
            }
            Self::InvalidLatencyLimit => {
                formatter.write_str("latency limit must be finite and non-negative")
            }
        }
    }
}

impl Error for SelectionPolicyError {}

impl SelectionPolicy {
    pub fn validate(&self) -> Result<(), SelectionPolicyError> {
        if self.min_paired_observations == 0 {
            return Err(SelectionPolicyError::ZeroEvidenceFloor);
        }
        if self.now_us < 0 {
            return Err(SelectionPolicyError::NegativeClock);
        }
        if self.max_profile_age_us < 0 {
            return Err(SelectionPolicyError::NegativeFreshnessHorizon);
        }
        if !is_nonnegative_finite(self.divergence_exposure_budget) {
            return Err(SelectionPolicyError::InvalidExposureBudget);
        }
        if self
            .max_expected_cost_usd
            .is_some_and(|value| !is_nonnegative_finite(value))
        {
            return Err(SelectionPolicyError::InvalidCostLimit);
        }
        if self
            .max_expected_latency_ms
            .is_some_and(|value| !is_nonnegative_finite(value))
        {
            return Err(SelectionPolicyError::InvalidLatencyLimit);
        }
        Ok(())
    }
}

fn is_nonnegative_finite(value: f64) -> bool {
    value.is_finite() && value >= 0.0
}

#[derive(Debug, Clone, PartialEq)]
pub enum CandidateRejectionReason {
    RequestIncompatible,
    Disabled,
    MissingEstimate,
    InsufficientEvidence {
        observed: u32,
        required: u32,
    },
    NonFiniteEstimate {
        field: &'static str,
    },
    NegativeEstimate {
        field: &'static str,
    },
    DivergenceOutOfRange,
    DivergenceExceeded {
        upper_p95: f64,
        threshold: f64,
    },
    InvalidAdmissionThreshold,
    InvalidExposure,
    ExposureBudgetExceeded {
        exposure: f64,
        budget: f64,
    },
    ObservationFromFuture,
    StaleProfile {
        age_us: i64,
        max_age_us: i64,
    },
    NonPositiveNetBenefit {
        objective: SelectionObjective,
        value: f64,
    },
    CostLimitExceeded {
        expected: f64,
        limit: f64,
    },
    LatencyLimitExceeded {
        expected: f64,
        limit: f64,
    },
}

#[derive(Debug, Clone, PartialEq)]
pub struct CandidateRejection {
    pub plan_id: ExecutionPlanId,
    pub reason: CandidateRejectionReason,
}

#[derive(Debug, Clone, PartialEq)]
pub enum SelectionReason {
    BestAdmissibleAlternative,
    NoAdmissibleAlternative,
    InvalidPolicy(SelectionPolicyError),
}

/// Deterministic selector output. `selected` always points to either one of the
/// supplied alternatives or the separately supplied immutable reference plan.
#[derive(Debug)]
pub struct Selection<'a> {
    pub selected: &'a CandidatePlan,
    pub selected_reference: bool,
    pub reason: SelectionReason,
    pub rejections: Vec<CandidateRejection>,
    pub admissible_alternatives: usize,
}

/// Select the best admissible alternative, or return `reference` unchanged.
pub fn select_candidate<'a>(
    reference: &'a CandidatePlan,
    alternatives: &'a [CandidatePlan],
    policy: &SelectionPolicy,
) -> Selection<'a> {
    if let Err(error) = policy.validate() {
        return Selection {
            selected: reference,
            selected_reference: true,
            reason: SelectionReason::InvalidPolicy(error),
            rejections: Vec::new(),
            admissible_alternatives: 0,
        };
    }

    let mut rejections = Vec::new();
    let mut admissible = Vec::new();
    for candidate in alternatives {
        match rejection_reason(candidate, policy) {
            Some(reason) => rejections.push(CandidateRejection {
                plan_id: candidate.id.clone(),
                reason,
            }),
            None => admissible.push(candidate),
        }
    }

    let selected = admissible
        .iter()
        .copied()
        .min_by(|left, right| compare_admissible(left, right, policy.objective));

    match selected {
        Some(candidate) => Selection {
            selected: candidate,
            selected_reference: false,
            reason: SelectionReason::BestAdmissibleAlternative,
            rejections,
            admissible_alternatives: admissible.len(),
        },
        None => Selection {
            selected: reference,
            selected_reference: true,
            reason: SelectionReason::NoAdmissibleAlternative,
            rejections,
            admissible_alternatives: 0,
        },
    }
}

/// Shared admission classification; callers must validate the policy first.
pub(crate) fn rejection_reason(
    candidate: &CandidatePlan,
    policy: &SelectionPolicy,
) -> Option<CandidateRejectionReason> {
    if !candidate.admission.request_compatible {
        return Some(CandidateRejectionReason::RequestIncompatible);
    }
    if candidate.admission.disabled {
        return Some(CandidateRejectionReason::Disabled);
    }
    if !candidate.admission.divergence_threshold.is_finite()
        || !(0.0..=1.0).contains(&candidate.admission.divergence_threshold)
    {
        return Some(CandidateRejectionReason::InvalidAdmissionThreshold);
    }
    if !is_nonnegative_finite(candidate.admission.divergence_exposure) {
        return Some(CandidateRejectionReason::InvalidExposure);
    }
    if candidate.admission.divergence_exposure >= policy.divergence_exposure_budget {
        return Some(CandidateRejectionReason::ExposureBudgetExceeded {
            exposure: candidate.admission.divergence_exposure,
            budget: policy.divergence_exposure_budget,
        });
    }

    let Some(estimate) = candidate.estimate.as_ref() else {
        return Some(CandidateRejectionReason::MissingEstimate);
    };

    if estimate.paired_observations < policy.min_paired_observations {
        return Some(CandidateRejectionReason::InsufficientEvidence {
            observed: estimate.paired_observations,
            required: policy.min_paired_observations,
        });
    }

    for (field, value) in [
        ("expected_cost_usd", estimate.expected_cost_usd),
        ("expected_latency_ms", estimate.expected_latency_ms),
        ("divergence_upper_p95", estimate.divergence_upper_p95),
        (
            "expected_net_cost_savings_usd",
            estimate.expected_net_cost_savings_usd,
        ),
        (
            "expected_net_latency_savings_ms",
            estimate.expected_net_latency_savings_ms,
        ),
    ] {
        if !value.is_finite() {
            return Some(CandidateRejectionReason::NonFiniteEstimate { field });
        }
    }
    for (field, value) in [
        ("expected_cost_usd", estimate.expected_cost_usd),
        ("expected_latency_ms", estimate.expected_latency_ms),
    ] {
        if value < 0.0 {
            return Some(CandidateRejectionReason::NegativeEstimate { field });
        }
    }
    if !(0.0..=1.0).contains(&estimate.divergence_upper_p95) {
        return Some(CandidateRejectionReason::DivergenceOutOfRange);
    }
    if estimate.divergence_upper_p95 > candidate.admission.divergence_threshold {
        return Some(CandidateRejectionReason::DivergenceExceeded {
            upper_p95: estimate.divergence_upper_p95,
            threshold: candidate.admission.divergence_threshold,
        });
    }
    if estimate.last_observed_at_us > policy.now_us {
        return Some(CandidateRejectionReason::ObservationFromFuture);
    }
    let age_us = policy.now_us - estimate.last_observed_at_us;
    if age_us > policy.max_profile_age_us {
        return Some(CandidateRejectionReason::StaleProfile {
            age_us,
            max_age_us: policy.max_profile_age_us,
        });
    }

    let net_benefit = match policy.objective {
        SelectionObjective::Cost => estimate.expected_net_cost_savings_usd,
        SelectionObjective::Latency => estimate.expected_net_latency_savings_ms,
    };
    if net_benefit <= 0.0 {
        return Some(CandidateRejectionReason::NonPositiveNetBenefit {
            objective: policy.objective,
            value: net_benefit,
        });
    }
    if let Some(limit) = policy.max_expected_cost_usd {
        if estimate.expected_cost_usd > limit {
            return Some(CandidateRejectionReason::CostLimitExceeded {
                expected: estimate.expected_cost_usd,
                limit,
            });
        }
    }
    if let Some(limit) = policy.max_expected_latency_ms {
        if estimate.expected_latency_ms > limit {
            return Some(CandidateRejectionReason::LatencyLimitExceeded {
                expected: estimate.expected_latency_ms,
                limit,
            });
        }
    }
    None
}

fn compare_admissible(
    left: &CandidatePlan,
    right: &CandidatePlan,
    objective: SelectionObjective,
) -> Ordering {
    let left_estimate = left.estimate.as_ref().expect("admitted plan has estimate");
    let right_estimate = right.estimate.as_ref().expect("admitted plan has estimate");
    let primary = match objective {
        SelectionObjective::Cost => left_estimate
            .expected_cost_usd
            .total_cmp(&right_estimate.expected_cost_usd),
        SelectionObjective::Latency => left_estimate
            .expected_latency_ms
            .total_cmp(&right_estimate.expected_latency_ms),
    };
    primary
        .then_with(|| {
            right_estimate
                .paired_observations
                .cmp(&left_estimate.paired_observations)
        })
        .then_with(|| {
            left_estimate
                .divergence_upper_p95
                .total_cmp(&right_estimate.divergence_upper_p95)
        })
        .then_with(|| left.mutation_count().cmp(&right.mutation_count()))
        .then_with(|| left.id.cmp(&right.id))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const NOW_US: i64 = 2_000_000;

    fn rewrite(name: &str) -> RewriteApplication {
        RewriteApplication {
            stable_name: name.to_string(),
            implementation_version: "1".to_string(),
            parameters: json!({"threshold": 0.2, "mode": "extractive"}),
        }
    }

    fn spec(target: &str, rewrites: Vec<RewriteApplication>) -> ExecutionPlanSpec {
        ExecutionPlanSpec {
            schema_version: EXECUTION_PLAN_SCHEMA_VERSION,
            provider_protocol: "openai-chat".to_string(),
            requested_model_id: "strong".to_string(),
            target_model_id: target.to_string(),
            target_model_revision: Some("2026-03-17".to_string()),
            rewrite_ordering: RewriteOrdering::Ordered,
            rewrites,
            cache_policy: CachePolicy {
                stable_name: "bypass".to_string(),
                implementation_version: "1".to_string(),
                parameters: json!({}),
            },
            output_budget: Some(512),
            validation_policy: ValidationPolicy {
                stable_name: "paired-shadow".to_string(),
                implementation_version: "1".to_string(),
                parameters: json!({"rate": 0.02}),
            },
        }
    }

    fn estimate(cost: f64, latency: f64) -> PlanEstimate {
        PlanEstimate {
            paired_observations: 20,
            expected_cost_usd: cost,
            expected_latency_ms: latency,
            divergence_upper_p95: 0.1,
            expected_net_cost_savings_usd: 0.01,
            expected_net_latency_savings_ms: 10.0,
            last_observed_at_us: NOW_US - 100,
        }
    }

    fn admission() -> PlanAdmission {
        PlanAdmission {
            request_compatible: true,
            disabled: false,
            divergence_threshold: 0.15,
            divergence_exposure: 0.0,
        }
    }

    fn candidate(
        target: &str,
        rewrites: Vec<RewriteApplication>,
        cost: f64,
        latency: f64,
    ) -> CandidatePlan {
        CandidatePlan::new(
            spec(target, rewrites),
            Some(estimate(cost, latency)),
            admission(),
        )
        .unwrap()
    }

    fn reference() -> CandidatePlan {
        let mut reference = candidate("strong", vec![], 0.03, 1_000.0);
        reference.estimate = None;
        reference
    }

    fn policy(objective: SelectionObjective) -> SelectionPolicy {
        SelectionPolicy {
            objective,
            now_us: NOW_US,
            ..SelectionPolicy::default()
        }
    }

    #[test]
    fn identity_is_stable_across_json_object_insertion_order() {
        let mut left = spec("cheap", vec![rewrite("ContextCompress")]);
        let mut right = left.clone();
        left.rewrites[0].parameters = json!({"alpha": 1, "beta": {"x": 2, "y": 3}});
        right.rewrites[0].parameters = json!({"beta": {"y": 3, "x": 2}, "alpha": 1});
        assert_eq!(
            left.execution_plan_id().unwrap(),
            right.execution_plan_id().unwrap()
        );
    }

    #[test]
    fn ordered_rewrites_have_order_sensitive_identity() {
        let left = spec("cheap", vec![rewrite("A"), rewrite("B")]);
        let right = spec("cheap", vec![rewrite("B"), rewrite("A")]);
        assert_ne!(
            left.execution_plan_id().unwrap(),
            right.execution_plan_id().unwrap()
        );
    }

    #[test]
    fn proven_commuting_rewrites_have_order_independent_identity() {
        let mut left = spec("cheap", vec![rewrite("A"), rewrite("B")]);
        left.rewrite_ordering = RewriteOrdering::CanonicalCommuting;
        let mut right = spec("cheap", vec![rewrite("B"), rewrite("A")]);
        right.rewrite_ordering = RewriteOrdering::CanonicalCommuting;
        assert_eq!(
            left.execution_plan_id().unwrap(),
            right.execution_plan_id().unwrap()
        );
    }

    #[test]
    fn model_and_validation_policy_are_identity_bearing() {
        let base = spec("cheap-a", vec![rewrite("ContextCompress")]);
        let mut other_model = base.clone();
        other_model.target_model_id = "cheap-b".to_string();
        let mut other_validation = base.clone();
        other_validation.validation_policy.parameters = json!({"rate": 0.05});
        assert_ne!(
            base.execution_plan_id().unwrap(),
            other_model.execution_plan_id().unwrap()
        );
        assert_ne!(
            base.execution_plan_id().unwrap(),
            other_validation.execution_plan_id().unwrap()
        );
    }

    #[test]
    fn invalid_identity_fields_fail_closed() {
        let mut invalid = spec("cheap", vec![]);
        invalid.provider_protocol = " ".to_string();
        assert_eq!(
            invalid.execution_plan_id(),
            Err(PlanIdentityError::EmptyField("provider_protocol"))
        );
    }

    #[test]
    fn plan_id_parser_and_deserializer_reject_noncanonical_digests() {
        let valid = "a".repeat(64);
        assert_eq!(ExecutionPlanId::parse(&valid).unwrap().as_str(), valid);
        assert_eq!(
            ExecutionPlanId::parse("A".repeat(64)),
            Err(PlanIdentityError::InvalidDigest)
        );
        assert_eq!(
            ExecutionPlanId::parse("abc"),
            Err(PlanIdentityError::InvalidDigest)
        );
        assert!(serde_json::from_str::<ExecutionPlanId>("\"not-a-digest\"").is_err());
    }

    #[test]
    fn cost_objective_selects_cheapest_joint_plan() {
        let reference = reference();
        let alternatives = vec![
            candidate("cheap", vec![], 0.020, 600.0),
            candidate("cheap", vec![rewrite("ContextCompress")], 0.010, 800.0),
            candidate("strong", vec![rewrite("ContextCompress")], 0.015, 500.0),
        ];
        let selected =
            select_candidate(&reference, &alternatives, &policy(SelectionObjective::Cost));
        assert_eq!(selected.selected.id, alternatives[1].id);
        assert_eq!(selected.selected.spec.target_model_id, "cheap");
        assert_eq!(
            selected.selected.spec.rewrites[0].stable_name,
            "ContextCompress"
        );
        assert!(!selected.selected_reference);
    }

    #[test]
    fn latency_objective_selects_fastest_plan() {
        let reference = reference();
        let alternatives = vec![
            candidate("cheap", vec![], 0.005, 700.0),
            candidate("strong", vec![rewrite("OutputBudget")], 0.020, 300.0),
        ];
        let selected = select_candidate(
            &reference,
            &alternatives,
            &policy(SelectionObjective::Latency),
        );
        assert_eq!(selected.selected.id, alternatives[1].id);
    }

    #[test]
    fn missing_joint_profile_is_not_synthesized_from_solo_profiles() {
        let reference = reference();
        let route_only = candidate("cheap", vec![], 0.020, 700.0);
        let rewrite_only = candidate("strong", vec![rewrite("ContextCompress")], 0.018, 650.0);
        let mut joint = candidate("cheap", vec![rewrite("ContextCompress")], 0.005, 300.0);
        joint.estimate = None;
        let alternatives = vec![route_only, rewrite_only, joint];
        let selected =
            select_candidate(&reference, &alternatives, &policy(SelectionObjective::Cost));
        assert_ne!(selected.selected.id, alternatives[2].id);
        assert!(selected.rejections.iter().any(|rejection| {
            rejection.plan_id == alternatives[2].id
                && rejection.reason == CandidateRejectionReason::MissingEstimate
        }));
    }

    #[test]
    fn stale_under_evidenced_and_non_finite_plans_are_rejected() {
        let reference = reference();
        let mut stale = candidate("stale", vec![], 0.01, 100.0);
        stale.estimate.as_mut().unwrap().last_observed_at_us = 0;
        let mut cold = candidate("cold", vec![], 0.01, 100.0);
        cold.estimate.as_mut().unwrap().paired_observations = 19;
        let mut invalid = candidate("invalid", vec![], 0.01, 100.0);
        invalid.estimate.as_mut().unwrap().expected_cost_usd = f64::NAN;
        let alternatives = vec![stale, cold, invalid];
        let selected = select_candidate(
            &reference,
            &alternatives,
            &SelectionPolicy {
                max_profile_age_us: 1_000,
                ..policy(SelectionObjective::Cost)
            },
        );
        assert!(selected.selected_reference);
        assert_eq!(selected.rejections.len(), 3);
        assert!(matches!(
            selected.rejections[0].reason,
            CandidateRejectionReason::StaleProfile { .. }
        ));
        assert!(matches!(
            selected.rejections[1].reason,
            CandidateRejectionReason::InsufficientEvidence { .. }
        ));
        assert!(matches!(
            selected.rejections[2].reason,
            CandidateRejectionReason::NonFiniteEstimate { .. }
        ));
    }

    #[test]
    fn compatibility_divergence_exposure_and_hard_limits_are_enforced() {
        let reference = reference();
        let mut incompatible = candidate("incompatible", vec![], 0.01, 100.0);
        incompatible.admission.request_compatible = false;
        let mut divergent = candidate("divergent", vec![], 0.01, 100.0);
        divergent.estimate.as_mut().unwrap().divergence_upper_p95 = 0.2;
        let mut exposed = candidate("exposed", vec![], 0.01, 100.0);
        exposed.admission.divergence_exposure = 1.0;
        let expensive = candidate("expensive", vec![], 0.03, 100.0);
        let alternatives = vec![incompatible, divergent, exposed, expensive];
        let selected = select_candidate(
            &reference,
            &alternatives,
            &SelectionPolicy {
                max_expected_cost_usd: Some(0.02),
                ..policy(SelectionObjective::Cost)
            },
        );
        assert!(selected.selected_reference);
        assert_eq!(selected.rejections.len(), 4);
    }

    #[test]
    fn non_positive_net_benefit_rejects_for_selected_objective() {
        let reference = reference();
        let mut no_cost_gain = candidate("cheap", vec![], 0.01, 100.0);
        no_cost_gain
            .estimate
            .as_mut()
            .unwrap()
            .expected_net_cost_savings_usd = 0.0;
        let cost_selection = select_candidate(
            &reference,
            std::slice::from_ref(&no_cost_gain),
            &policy(SelectionObjective::Cost),
        );
        assert!(cost_selection.selected_reference);

        no_cost_gain
            .estimate
            .as_mut()
            .unwrap()
            .expected_net_cost_savings_usd = 0.01;
        no_cost_gain
            .estimate
            .as_mut()
            .unwrap()
            .expected_net_latency_savings_ms = -1.0;
        let latency_selection = select_candidate(
            &reference,
            std::slice::from_ref(&no_cost_gain),
            &policy(SelectionObjective::Latency),
        );
        assert!(latency_selection.selected_reference);
    }

    #[test]
    fn ties_prefer_evidence_then_divergence_then_mutation_count_then_id() {
        let reference = reference();
        let mut less_evidence = candidate("a", vec![], 0.01, 100.0);
        less_evidence.estimate.as_mut().unwrap().paired_observations = 20;
        let mut more_evidence = candidate("b", vec![rewrite("R")], 0.01, 100.0);
        more_evidence.estimate.as_mut().unwrap().paired_observations = 21;
        let alternatives = vec![less_evidence, more_evidence];
        let selection =
            select_candidate(&reference, &alternatives, &policy(SelectionObjective::Cost));
        assert_eq!(selection.selected.id, alternatives[1].id);

        let mut high_divergence = alternatives[1].clone();
        high_divergence
            .estimate
            .as_mut()
            .unwrap()
            .divergence_upper_p95 = 0.12;
        let mut low_divergence = candidate("c", vec![rewrite("R")], 0.01, 100.0);
        low_divergence
            .estimate
            .as_mut()
            .unwrap()
            .paired_observations = 21;
        low_divergence
            .estimate
            .as_mut()
            .unwrap()
            .divergence_upper_p95 = 0.05;
        let alternatives = vec![high_divergence, low_divergence];
        let selection =
            select_candidate(&reference, &alternatives, &policy(SelectionObjective::Cost));
        assert_eq!(selection.selected.id, alternatives[1].id);

        let one_mutation = candidate("cheap", vec![], 0.01, 100.0);
        let two_mutations = candidate("cheap", vec![rewrite("R")], 0.01, 100.0);
        let alternatives = vec![two_mutations, one_mutation];
        let selection =
            select_candidate(&reference, &alternatives, &policy(SelectionObjective::Cost));
        assert_eq!(selection.selected.id, alternatives[1].id);

        let left = candidate("tie-a", vec![], 0.01, 100.0);
        let right = candidate("tie-b", vec![], 0.01, 100.0);
        let expected = std::cmp::min(left.id.clone(), right.id.clone());
        let alternatives = vec![left, right];
        let selection =
            select_candidate(&reference, &alternatives, &policy(SelectionObjective::Cost));
        assert_eq!(selection.selected.id, expected);
    }

    #[test]
    fn no_admissible_candidate_returns_reference() {
        let mut candidate = candidate("cheap", vec![], 0.01, 100.0);
        candidate.admission.disabled = true;
        let reference = reference();
        let selection = select_candidate(
            &reference,
            std::slice::from_ref(&candidate),
            &policy(SelectionObjective::Cost),
        );
        assert!(selection.selected_reference);
        assert_eq!(selection.selected.id, reference.id);
        assert_eq!(selection.reason, SelectionReason::NoAdmissibleAlternative);
    }

    #[test]
    fn invalid_policy_fails_to_reference() {
        let alternative = candidate("cheap", vec![], 0.01, 100.0);
        let reference = reference();
        let selection = select_candidate(
            &reference,
            std::slice::from_ref(&alternative),
            &SelectionPolicy {
                divergence_exposure_budget: f64::NAN,
                ..policy(SelectionObjective::Cost)
            },
        );
        assert!(selection.selected_reference);
        assert_eq!(
            selection.reason,
            SelectionReason::InvalidPolicy(SelectionPolicyError::InvalidExposureBudget)
        );
    }
}
