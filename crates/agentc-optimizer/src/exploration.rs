//! Persistent, bounded exploration for cold execution plans.
//!
//! The controller never chooses a user-visible candidate. Every decision
//! returns the immutable reference result and may additionally lease one cold
//! plan for counterfactual execution. A lease is committed to SQLite before
//! provider work starts, so crashes cannot erase spend from the rolling cap.
//! Output divergence and optional evaluation-only task-quality labels remain
//! separate throughout the API and durable schema.

use std::error::Error;
use std::fmt;

use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::plan_profile::{CallSiteVersion, PlanProfileKey};

/// Rolling horizon for the per-call-site exploration budget.
pub const EXPLORATION_WINDOW_US: i64 = 24 * 60 * 60 * 1_000_000;
/// Maximum candidate calls leased at one call site in the rolling horizon.
pub const DEFAULT_EXPLORATION_CALL_CAP: u32 = 20;
/// Maximum live counterfactuals at one call site.
pub const DEFAULT_CONCURRENT_COUNTERFACTUAL_CAP: u32 = 1;
/// Evidence count at which a plan is no longer a cold exploration candidate.
pub const DEFAULT_EXPLORATION_EVIDENCE_TARGET: u32 = 20;
/// Conservative duration after which a lost lease no longer consumes a
/// concurrency slot. It still consumes one call from the rolling spend cap.
pub const DEFAULT_EXPLORATION_LEASE_US: i64 = 10 * 60 * 1_000_000;
/// Stable production seed. Experiments should set an explicit seed and record
/// it in their manifest.
pub const DEFAULT_EXPLORATION_SEED: u64 = 0x4147_454e_5443_0001;

/// Limits shared by all candidate plans at a call site.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExplorationPolicy {
    pub seed: u64,
    pub max_calls_per_site: u32,
    pub max_concurrent_per_site: u32,
    pub evidence_target: u32,
    pub window_us: i64,
    pub lease_duration_us: i64,
    pub divergence_exposure_budget: f64,
    /// Evaluation-only budget over labeled task outcomes. Production leaves
    /// this as `None` because output divergence is not a task-quality label.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_damage_budget: Option<f64>,
}

impl Default for ExplorationPolicy {
    fn default() -> Self {
        Self {
            seed: DEFAULT_EXPLORATION_SEED,
            max_calls_per_site: DEFAULT_EXPLORATION_CALL_CAP,
            max_concurrent_per_site: DEFAULT_CONCURRENT_COUNTERFACTUAL_CAP,
            evidence_target: DEFAULT_EXPLORATION_EVIDENCE_TARGET,
            window_us: EXPLORATION_WINDOW_US,
            lease_duration_us: DEFAULT_EXPLORATION_LEASE_US,
            divergence_exposure_budget: crate::plan_guard::DEFAULT_PLAN_EXPOSURE_BUDGET,
            task_damage_budget: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExplorationPolicyError {
    ZeroCallCap,
    ZeroConcurrencyCap,
    ZeroEvidenceTarget,
    InvalidWindow,
    InvalidLeaseDuration,
    InvalidDivergenceExposureBudget,
    InvalidTaskDamageBudget,
}

impl fmt::Display for ExplorationPolicyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ZeroCallCap => formatter.write_str("exploration call cap must be positive"),
            Self::ZeroConcurrencyCap => {
                formatter.write_str("counterfactual concurrency cap must be positive")
            }
            Self::ZeroEvidenceTarget => {
                formatter.write_str("exploration evidence target must be positive")
            }
            Self::InvalidWindow => {
                formatter.write_str("exploration window must be positive")
            }
            Self::InvalidLeaseDuration => formatter.write_str(
                "exploration lease duration must be positive and no larger than the window",
            ),
            Self::InvalidDivergenceExposureBudget => formatter
                .write_str("divergence exposure budget must be finite and non-negative"),
            Self::InvalidTaskDamageBudget => {
                formatter.write_str("task damage budget must be finite and non-negative")
            }
        }
    }
}

impl Error for ExplorationPolicyError {}

impl ExplorationPolicy {
    pub fn validate(&self) -> Result<(), ExplorationPolicyError> {
        if self.max_calls_per_site == 0 {
            return Err(ExplorationPolicyError::ZeroCallCap);
        }
        if self.max_concurrent_per_site == 0 {
            return Err(ExplorationPolicyError::ZeroConcurrencyCap);
        }
        if self.evidence_target == 0 {
            return Err(ExplorationPolicyError::ZeroEvidenceTarget);
        }
        if self.window_us <= 0 {
            return Err(ExplorationPolicyError::InvalidWindow);
        }
        if self.lease_duration_us <= 0 || self.lease_duration_us > self.window_us {
            return Err(ExplorationPolicyError::InvalidLeaseDuration);
        }
        if !is_nonnegative_finite(self.divergence_exposure_budget) {
            return Err(ExplorationPolicyError::InvalidDivergenceExposureBudget);
        }
        if self
            .task_damage_budget
            .is_some_and(|value| !is_nonnegative_finite(value))
        {
            return Err(ExplorationPolicyError::InvalidTaskDamageBudget);
        }
        Ok(())
    }
}

/// Decision-time facts for one complete execution plan. The controller stores
/// only its content-free identity; the candidate call remains with the planner.
#[derive(Debug, Clone, PartialEq)]
pub struct ExplorationCandidate {
    pub key: PlanProfileKey,
    pub paired_observations: u32,
    pub request_compatible: bool,
    pub forbidden: bool,
    pub disabled: bool,
    pub divergence_threshold: f64,
    /// Current complete-plan exposure from the plan guard. The controller also
    /// derives exposure from its own durable exploration feedback and uses the
    /// larger value, avoiding double charging when the same sample feeds both.
    pub divergence_exposure: f64,
}

/// Durable permission to run exactly one candidate counterfactual.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExplorationLease {
    pub key: PlanProfileKey,
    pub sequence: u64,
    pub started_at_us: i64,
    pub expires_at_us: i64,
    pub divergence_threshold: f64,
}

/// Why a decision returned only the reference or leased one counterfactual.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExplorationReason {
    CandidateReserved,
    NoEligibleCandidate,
    CallCapExhausted,
    ConcurrencyCapReached,
    InvalidClock,
    PersistenceFailure,
}

/// Exploration is deliberately an adjunct to the reference result. There is
/// no field through which an unadmitted candidate can become user-visible.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExplorationDecision {
    pub return_reference: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub counterfactual: Option<ExplorationLease>,
    pub reason: ExplorationReason,
}

impl ExplorationDecision {
    fn reference_only(reason: ExplorationReason) -> Self {
        Self {
            return_reference: true,
            counterfactual: None,
            reason,
        }
    }
}

/// Task labels are optional and evaluation-only. `ObservationOnly` records the
/// production signal without implying that divergence is task quality.
#[derive(Debug, Clone, PartialEq)]
pub enum CounterfactualLabel {
    ObservationOnly,
    TaskQuality {
        reference_quality: f64,
        candidate_quality: f64,
    },
}

/// Feedback for one completed candidate call.
#[derive(Debug, Clone, PartialEq)]
pub struct CounterfactualFeedback {
    pub divergence: f64,
    pub cost_usd: f64,
    pub latency_ms: f64,
    pub label: CounterfactualLabel,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExplorationCompletion {
    Recorded,
    AlreadyRecorded,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExplorationError {
    InvalidPolicy(ExplorationPolicyError),
    InvalidClock,
    InvalidDivergence,
    InvalidCost,
    InvalidLatency,
    InvalidTaskQuality,
    LeaseNotFound,
    LeaseMismatch,
    LeaseNotActive,
    ConflictingFeedback,
    Persistence(String),
}

impl fmt::Display for ExplorationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidPolicy(error) => write!(formatter, "invalid exploration policy: {error}"),
            Self::InvalidClock => formatter.write_str("exploration timestamp is invalid"),
            Self::InvalidDivergence => {
                formatter.write_str("counterfactual divergence must be finite and in [0, 1]")
            }
            Self::InvalidCost => {
                formatter.write_str("counterfactual cost must be finite and non-negative")
            }
            Self::InvalidLatency => {
                formatter.write_str("counterfactual latency must be finite and non-negative")
            }
            Self::InvalidTaskQuality => {
                formatter.write_str("task-quality labels must be finite and in [0, 1]")
            }
            Self::LeaseNotFound => formatter.write_str("exploration lease does not exist"),
            Self::LeaseMismatch => {
                formatter.write_str("exploration lease does not match durable state")
            }
            Self::LeaseNotActive => formatter.write_str("exploration lease is no longer active"),
            Self::ConflictingFeedback => {
                formatter.write_str("exploration lease already has different feedback")
            }
            Self::Persistence(message) => {
                write!(formatter, "exploration persistence failed: {message}")
            }
        }
    }
}

impl Error for ExplorationError {}

/// Rolling summary for diagnostics and experiment manifests.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExplorationSiteSnapshot {
    pub calls_in_window: u32,
    pub active_leases: u32,
    pub completed_calls: u32,
    pub failed_calls: u32,
    pub abandoned_calls: u32,
    pub observed_counterfactuals: u32,
    pub task_labeled_counterfactuals: u32,
    pub counterfactual_cost_usd: f64,
    pub divergence_exposure: f64,
    pub task_damage: f64,
}

/// Stateless policy object. SQLite is the control plane so independently
/// constructed controller instances observe the same spend and concurrency
/// state after restart.
#[derive(Debug, Clone)]
pub struct ExplorationController {
    policy: ExplorationPolicy,
}

impl Default for ExplorationController {
    fn default() -> Self {
        Self::new()
    }
}

impl ExplorationController {
    pub fn new() -> Self {
        Self::with_policy(ExplorationPolicy::default())
            .expect("default exploration policy is valid")
    }

    pub fn with_policy(policy: ExplorationPolicy) -> Result<Self, ExplorationPolicyError> {
        policy.validate()?;
        Ok(Self { policy })
    }

    pub fn policy(&self) -> &ExplorationPolicy {
        &self.policy
    }

    /// Return the reference result and, when all limits permit, durably lease
    /// one under-observed candidate. Any SQLite failure fails closed to the
    /// reference without exposing an untracked provider call.
    pub fn decide_and_reserve(
        &self,
        conn: &mut Connection,
        call_site_version: &CallSiteVersion,
        candidates: &[ExplorationCandidate],
        now_us: i64,
    ) -> ExplorationDecision {
        if now_us < 0 {
            return ExplorationDecision::reference_only(ExplorationReason::InvalidClock);
        }
        match self.try_decide_and_reserve(conn, call_site_version, candidates, now_us) {
            Ok(decision) => decision,
            Err(_) => {
                ExplorationDecision::reference_only(ExplorationReason::PersistenceFailure)
            }
        }
    }

    fn try_decide_and_reserve(
        &self,
        conn: &mut Connection,
        call_site_version: &CallSiteVersion,
        candidates: &[ExplorationCandidate],
        now_us: i64,
    ) -> Result<ExplorationDecision, ExplorationError> {
        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(persistence)?;
        let cutoff = now_us.saturating_sub(self.policy.window_us);

        expire_and_prune(&transaction, call_site_version, cutoff, now_us)?;

        let calls_in_window = count_calls(&transaction, call_site_version, cutoff)?;
        if calls_in_window >= u64::from(self.policy.max_calls_per_site) {
            transaction.commit().map_err(persistence)?;
            return Ok(ExplorationDecision::reference_only(
                ExplorationReason::CallCapExhausted,
            ));
        }

        let active = count_active(&transaction, call_site_version, now_us)?;
        if active >= u64::from(self.policy.max_concurrent_per_site) {
            transaction.commit().map_err(persistence)?;
            return Ok(ExplorationDecision::reference_only(
                ExplorationReason::ConcurrencyCapReached,
            ));
        }

        let mut eligible = Vec::new();
        for candidate in candidates {
            if candidate.key.call_site_version != *call_site_version
                || candidate.paired_observations >= self.policy.evidence_target
                || !candidate.request_compatible
                || candidate.forbidden
                || candidate.disabled
                || !is_unit_fraction(candidate.divergence_threshold)
                || !is_nonnegative_finite(candidate.divergence_exposure)
            {
                continue;
            }

            let (attempts, persisted_exposure, task_damage) = plan_window_totals(
                &transaction,
                &candidate.key,
                cutoff,
            )?;
            let effective_exposure = candidate.divergence_exposure.max(persisted_exposure);
            if effective_exposure >= self.policy.divergence_exposure_budget {
                continue;
            }
            if self
                .policy
                .task_damage_budget
                .is_some_and(|budget| task_damage >= budget)
            {
                continue;
            }

            eligible.push((
                candidate,
                attempts,
                deterministic_priority(
                    self.policy.seed,
                    call_site_version,
                    &candidate.key,
                    calls_in_window.saturating_add(1),
                ),
            ));
        }

        eligible.sort_by(|left, right| {
            left.0
                .paired_observations
                .cmp(&right.0.paired_observations)
                .then_with(|| left.1.cmp(&right.1))
                .then_with(|| left.2.cmp(&right.2))
                .then_with(|| left.0.key.execution_plan_id.cmp(&right.0.key.execution_plan_id))
        });
        let Some((selected, _, _)) = eligible.first() else {
            transaction.commit().map_err(persistence)?;
            return Ok(ExplorationDecision::reference_only(
                ExplorationReason::NoEligibleCandidate,
            ));
        };

        let sequence = allocate_sequence(&transaction, call_site_version)?;
        let expires_at_us = now_us.saturating_add(self.policy.lease_duration_us);
        transaction
            .execute(
                "INSERT INTO execution_plan_exploration (\
                    call_site_version, exploration_sequence, execution_plan_id, status, \
                    divergence_threshold, divergence, divergence_exposure, feedback_kind, \
                    reference_quality, candidate_quality, task_damage, cost_usd, latency_ms, \
                    started_at, lease_expires_at, completed_at\
                 ) VALUES (?1, ?2, ?3, 'reserved', ?4, NULL, NULL, 'none', \
                           NULL, NULL, NULL, NULL, NULL, ?5, ?6, NULL)",
                params![
                    call_site_version.as_str(),
                    to_sqlite_u64(sequence)?,
                    selected.key.execution_plan_id.as_str(),
                    selected.divergence_threshold,
                    now_us,
                    expires_at_us,
                ],
            )
            .map_err(persistence)?;
        transaction.commit().map_err(persistence)?;

        Ok(ExplorationDecision {
            return_reference: true,
            counterfactual: Some(ExplorationLease {
                key: selected.key.clone(),
                sequence,
                started_at_us: now_us,
                expires_at_us,
                divergence_threshold: selected.divergence_threshold,
            }),
            reason: ExplorationReason::CandidateReserved,
        })
    }

    /// Commit counterfactual feedback and release its concurrency slot.
    /// Duplicate identical feedback is idempotent; a conflicting replay fails.
    pub fn complete(
        &self,
        conn: &mut Connection,
        lease: &ExplorationLease,
        feedback: &CounterfactualFeedback,
        completed_at_us: i64,
    ) -> Result<ExplorationCompletion, ExplorationError> {
        validate_feedback(feedback)?;
        if completed_at_us < lease.started_at_us || completed_at_us < 0 {
            return Err(ExplorationError::InvalidClock);
        }
        if !is_unit_fraction(lease.divergence_threshold) {
            return Err(ExplorationError::LeaseMismatch);
        }

        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(persistence)?;
        let Some(row) = load_attempt(&transaction, lease)? else {
            return Err(ExplorationError::LeaseNotFound);
        };
        if !row.matches_lease(lease) {
            return Err(ExplorationError::LeaseMismatch);
        }

        let normalized = NormalizedFeedback::from_feedback(feedback, lease.divergence_threshold);
        match row.status.as_str() {
            "completed" if row.matches_feedback(&normalized) => {
                transaction.commit().map_err(persistence)?;
                return Ok(ExplorationCompletion::AlreadyRecorded);
            }
            "completed" => return Err(ExplorationError::ConflictingFeedback),
            "reserved" => {}
            _ => return Err(ExplorationError::LeaseNotActive),
        }

        transaction
            .execute(
                "UPDATE execution_plan_exploration SET \
                    status = 'completed', divergence = ?3, divergence_exposure = ?4, \
                    feedback_kind = ?5, reference_quality = ?6, candidate_quality = ?7, \
                    task_damage = ?8, cost_usd = ?9, latency_ms = ?10, completed_at = ?11 \
                 WHERE call_site_version = ?1 AND exploration_sequence = ?2",
                params![
                    lease.key.call_site_version.as_str(),
                    to_sqlite_u64(lease.sequence)?,
                    normalized.divergence,
                    normalized.divergence_exposure,
                    normalized.kind,
                    normalized.reference_quality,
                    normalized.candidate_quality,
                    normalized.task_damage,
                    normalized.cost_usd,
                    normalized.latency_ms,
                    completed_at_us,
                ],
            )
            .map_err(persistence)?;
        transaction.commit().map_err(persistence)?;
        Ok(ExplorationCompletion::Recorded)
    }

    /// Mark a leased call failed. It still counts against the rolling call cap
    /// because provider work may already have started.
    pub fn fail(
        &self,
        conn: &mut Connection,
        lease: &ExplorationLease,
        completed_at_us: i64,
    ) -> Result<ExplorationCompletion, ExplorationError> {
        if completed_at_us < lease.started_at_us || completed_at_us < 0 {
            return Err(ExplorationError::InvalidClock);
        }
        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(persistence)?;
        let Some(row) = load_attempt(&transaction, lease)? else {
            return Err(ExplorationError::LeaseNotFound);
        };
        if !row.matches_lease(lease) {
            return Err(ExplorationError::LeaseMismatch);
        }
        match row.status.as_str() {
            "failed" => {
                transaction.commit().map_err(persistence)?;
                return Ok(ExplorationCompletion::AlreadyRecorded);
            }
            "reserved" => {}
            _ => return Err(ExplorationError::LeaseNotActive),
        }
        transaction
            .execute(
                "UPDATE execution_plan_exploration \
                 SET status = 'failed', completed_at = ?3 \
                 WHERE call_site_version = ?1 AND exploration_sequence = ?2",
                params![
                    lease.key.call_site_version.as_str(),
                    to_sqlite_u64(lease.sequence)?,
                    completed_at_us,
                ],
            )
            .map_err(persistence)?;
        transaction.commit().map_err(persistence)?;
        Ok(ExplorationCompletion::Recorded)
    }

    pub fn snapshot(
        &self,
        conn: &Connection,
        call_site_version: &CallSiteVersion,
        now_us: i64,
    ) -> Result<ExplorationSiteSnapshot, ExplorationError> {
        if now_us < 0 {
            return Err(ExplorationError::InvalidClock);
        }
        let cutoff = now_us.saturating_sub(self.policy.window_us);
        conn.query_row(
            "SELECT \
                COUNT(*), \
                COALESCE(SUM(CASE WHEN status = 'reserved' AND lease_expires_at > ?3 THEN 1 ELSE 0 END), 0), \
                COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0), \
                COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0), \
                COALESCE(SUM(CASE WHEN status = 'abandoned' OR \
                                      (status = 'reserved' AND lease_expires_at <= ?3) \
                                  THEN 1 ELSE 0 END), 0), \
                COALESCE(SUM(CASE WHEN feedback_kind = 'observation_only' THEN 1 ELSE 0 END), 0), \
                COALESCE(SUM(CASE WHEN feedback_kind = 'task_quality' THEN 1 ELSE 0 END), 0), \
                COALESCE(SUM(cost_usd), 0.0), \
                COALESCE(SUM(divergence_exposure), 0.0), \
                COALESCE(SUM(task_damage), 0.0) \
             FROM execution_plan_exploration \
             WHERE call_site_version = ?1 AND started_at > ?2",
            params![call_site_version.as_str(), cutoff, now_us],
            |row| {
                Ok(ExplorationSiteSnapshot {
                    calls_in_window: to_u32(row.get::<_, i64>(0)?)?,
                    active_leases: to_u32(row.get::<_, i64>(1)?)?,
                    completed_calls: to_u32(row.get::<_, i64>(2)?)?,
                    failed_calls: to_u32(row.get::<_, i64>(3)?)?,
                    abandoned_calls: to_u32(row.get::<_, i64>(4)?)?,
                    observed_counterfactuals: to_u32(row.get::<_, i64>(5)?)?,
                    task_labeled_counterfactuals: to_u32(row.get::<_, i64>(6)?)?,
                    counterfactual_cost_usd: row.get(7)?,
                    divergence_exposure: row.get(8)?,
                    task_damage: row.get(9)?,
                })
            },
        )
        .map_err(persistence)
    }
}

fn expire_and_prune(
    transaction: &Transaction<'_>,
    call_site_version: &CallSiteVersion,
    cutoff: i64,
    now_us: i64,
) -> Result<(), ExplorationError> {
    transaction
        .execute(
            "UPDATE execution_plan_exploration \
             SET status = 'abandoned', completed_at = ?2 \
             WHERE call_site_version = ?1 AND status = 'reserved' AND lease_expires_at <= ?2",
            params![call_site_version.as_str(), now_us],
        )
        .map_err(persistence)?;
    transaction
        .execute(
            "DELETE FROM execution_plan_exploration \
             WHERE call_site_version = ?1 AND started_at <= ?2",
            params![call_site_version.as_str(), cutoff],
        )
        .map_err(persistence)?;
    Ok(())
}

fn count_calls(
    transaction: &Transaction<'_>,
    call_site_version: &CallSiteVersion,
    cutoff: i64,
) -> Result<u64, ExplorationError> {
    let count = transaction
        .query_row(
            "SELECT COUNT(*) FROM execution_plan_exploration \
             WHERE call_site_version = ?1 AND started_at > ?2",
            params![call_site_version.as_str(), cutoff],
            |row| row.get::<_, i64>(0),
        )
        .map_err(persistence)?;
    u64::try_from(count).map_err(|error| persistence(error.to_string()))
}

fn count_active(
    transaction: &Transaction<'_>,
    call_site_version: &CallSiteVersion,
    now_us: i64,
) -> Result<u64, ExplorationError> {
    let count = transaction
        .query_row(
            "SELECT COUNT(*) FROM execution_plan_exploration \
             WHERE call_site_version = ?1 AND status = 'reserved' AND lease_expires_at > ?2",
            params![call_site_version.as_str(), now_us],
            |row| row.get::<_, i64>(0),
        )
        .map_err(persistence)?;
    u64::try_from(count).map_err(|error| persistence(error.to_string()))
}

fn plan_window_totals(
    transaction: &Transaction<'_>,
    key: &PlanProfileKey,
    cutoff: i64,
) -> Result<(u64, f64, f64), ExplorationError> {
    let (attempts, exposure, damage) = transaction
        .query_row(
            "SELECT COUNT(*), COALESCE(SUM(divergence_exposure), 0.0), \
                    COALESCE(SUM(task_damage), 0.0) \
             FROM execution_plan_exploration \
             WHERE call_site_version = ?1 AND execution_plan_id = ?2 AND started_at > ?3",
            params![
                key.call_site_version.as_str(),
                key.execution_plan_id.as_str(),
                cutoff,
            ],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, f64>(1)?,
                    row.get::<_, f64>(2)?,
                ))
            },
        )
        .map_err(persistence)?;
    if !is_nonnegative_finite(exposure) || !is_nonnegative_finite(damage) {
        return Err(persistence("invalid persisted exploration totals"));
    }
    Ok((
        u64::try_from(attempts).map_err(|error| persistence(error.to_string()))?,
        exposure,
        damage,
    ))
}

fn allocate_sequence(
    transaction: &Transaction<'_>,
    call_site_version: &CallSiteVersion,
) -> Result<u64, ExplorationError> {
    transaction
        .execute(
            "INSERT INTO execution_plan_exploration_site (call_site_version, next_sequence) \
             VALUES (?1, 1) ON CONFLICT(call_site_version) DO NOTHING",
            params![call_site_version.as_str()],
        )
        .map_err(persistence)?;
    let sequence = transaction
        .query_row(
            "SELECT next_sequence FROM execution_plan_exploration_site \
             WHERE call_site_version = ?1",
            params![call_site_version.as_str()],
            |row| row.get::<_, i64>(0),
        )
        .map_err(persistence)?;
    let sequence = u64::try_from(sequence).map_err(|error| persistence(error.to_string()))?;
    let next = sequence
        .checked_add(1)
        .ok_or_else(|| persistence("exploration sequence overflow"))?;
    transaction
        .execute(
            "UPDATE execution_plan_exploration_site SET next_sequence = ?2 \
             WHERE call_site_version = ?1",
            params![call_site_version.as_str(), to_sqlite_u64(next)?],
        )
        .map_err(persistence)?;
    Ok(sequence)
}

fn deterministic_priority(
    seed: u64,
    call_site_version: &CallSiteVersion,
    key: &PlanProfileKey,
    decision_sequence: u64,
) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(seed.to_be_bytes());
    hasher.update(call_site_version.as_str().as_bytes());
    hasher.update(key.execution_plan_id.as_str().as_bytes());
    hasher.update(decision_sequence.to_be_bytes());
    hasher.finalize().into()
}

#[derive(Debug)]
struct StoredAttempt {
    execution_plan_id: String,
    status: String,
    divergence_threshold: f64,
    divergence: Option<f64>,
    divergence_exposure: Option<f64>,
    feedback_kind: String,
    reference_quality: Option<f64>,
    candidate_quality: Option<f64>,
    task_damage: Option<f64>,
    cost_usd: Option<f64>,
    latency_ms: Option<f64>,
    started_at_us: i64,
    expires_at_us: i64,
}

impl StoredAttempt {
    fn matches_lease(&self, lease: &ExplorationLease) -> bool {
        self.execution_plan_id == lease.key.execution_plan_id.as_str()
            && self.divergence_threshold.to_bits() == lease.divergence_threshold.to_bits()
            && self.started_at_us == lease.started_at_us
            && self.expires_at_us == lease.expires_at_us
    }

    fn matches_feedback(&self, feedback: &NormalizedFeedback) -> bool {
        self.feedback_kind == feedback.kind
            && same_optional_f64(self.divergence, Some(feedback.divergence))
            && same_optional_f64(
                self.divergence_exposure,
                Some(feedback.divergence_exposure),
            )
            && same_optional_f64(self.reference_quality, feedback.reference_quality)
            && same_optional_f64(self.candidate_quality, feedback.candidate_quality)
            && same_optional_f64(self.task_damage, feedback.task_damage)
            && same_optional_f64(self.cost_usd, Some(feedback.cost_usd))
            && same_optional_f64(self.latency_ms, Some(feedback.latency_ms))
    }
}

fn load_attempt(
    transaction: &Transaction<'_>,
    lease: &ExplorationLease,
) -> Result<Option<StoredAttempt>, ExplorationError> {
    transaction
        .query_row(
            "SELECT execution_plan_id, status, divergence_threshold, divergence, \
                    divergence_exposure, feedback_kind, reference_quality, candidate_quality, \
                    task_damage, cost_usd, latency_ms, started_at, lease_expires_at \
             FROM execution_plan_exploration \
             WHERE call_site_version = ?1 AND exploration_sequence = ?2",
            params![
                lease.key.call_site_version.as_str(),
                to_sqlite_u64(lease.sequence)?,
            ],
            |row| {
                Ok(StoredAttempt {
                    execution_plan_id: row.get(0)?,
                    status: row.get(1)?,
                    divergence_threshold: row.get(2)?,
                    divergence: row.get(3)?,
                    divergence_exposure: row.get(4)?,
                    feedback_kind: row.get(5)?,
                    reference_quality: row.get(6)?,
                    candidate_quality: row.get(7)?,
                    task_damage: row.get(8)?,
                    cost_usd: row.get(9)?,
                    latency_ms: row.get(10)?,
                    started_at_us: row.get(11)?,
                    expires_at_us: row.get(12)?,
                })
            },
        )
        .optional()
        .map_err(persistence)
}

struct NormalizedFeedback {
    divergence: f64,
    divergence_exposure: f64,
    kind: &'static str,
    reference_quality: Option<f64>,
    candidate_quality: Option<f64>,
    task_damage: Option<f64>,
    cost_usd: f64,
    latency_ms: f64,
}

impl NormalizedFeedback {
    fn from_feedback(feedback: &CounterfactualFeedback, threshold: f64) -> Self {
        let divergence_exposure = (feedback.divergence - threshold).max(0.0);
        match feedback.label {
            CounterfactualLabel::ObservationOnly => Self {
                divergence: feedback.divergence,
                divergence_exposure,
                kind: "observation_only",
                reference_quality: None,
                candidate_quality: None,
                task_damage: None,
                cost_usd: feedback.cost_usd,
                latency_ms: feedback.latency_ms,
            },
            CounterfactualLabel::TaskQuality {
                reference_quality,
                candidate_quality,
            } => Self {
                divergence: feedback.divergence,
                divergence_exposure,
                kind: "task_quality",
                reference_quality: Some(reference_quality),
                candidate_quality: Some(candidate_quality),
                task_damage: Some((reference_quality - candidate_quality).max(0.0)),
                cost_usd: feedback.cost_usd,
                latency_ms: feedback.latency_ms,
            },
        }
    }
}

fn validate_feedback(feedback: &CounterfactualFeedback) -> Result<(), ExplorationError> {
    if !is_unit_fraction(feedback.divergence) {
        return Err(ExplorationError::InvalidDivergence);
    }
    if !is_nonnegative_finite(feedback.cost_usd) {
        return Err(ExplorationError::InvalidCost);
    }
    if !is_nonnegative_finite(feedback.latency_ms) {
        return Err(ExplorationError::InvalidLatency);
    }
    if let CounterfactualLabel::TaskQuality {
        reference_quality,
        candidate_quality,
    } = feedback.label
    {
        if !is_unit_fraction(reference_quality) || !is_unit_fraction(candidate_quality) {
            return Err(ExplorationError::InvalidTaskQuality);
        }
    }
    Ok(())
}

fn same_optional_f64(left: Option<f64>, right: Option<f64>) -> bool {
    match (left, right) {
        (Some(left), Some(right)) => left.to_bits() == right.to_bits(),
        (None, None) => true,
        _ => false,
    }
}

fn is_unit_fraction(value: f64) -> bool {
    value.is_finite() && (0.0..=1.0).contains(&value)
}

fn is_nonnegative_finite(value: f64) -> bool {
    value.is_finite() && value >= 0.0
}

fn to_sqlite_u64(value: u64) -> Result<i64, ExplorationError> {
    i64::try_from(value).map_err(|error| persistence(error.to_string()))
}

fn to_u32(value: i64) -> rusqlite::Result<u32> {
    u32::try_from(value).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(
            0,
            rusqlite::types::Type::Integer,
            Box::new(error),
        )
    })
}

fn persistence(error: impl ToString) -> ExplorationError {
    ExplorationError::Persistence(error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::execution_plan::ExecutionPlanId;
    use crate::schema::ensure_cost_model_schema;

    const NOW_US: i64 = 100_000;

    fn site(number: u8) -> CallSiteVersion {
        CallSiteVersion::parse(format!("{number:02x}{}", "0".repeat(62))).unwrap()
    }

    fn candidate(site: &CallSiteVersion, number: u8) -> ExplorationCandidate {
        ExplorationCandidate {
            key: PlanProfileKey {
                call_site_version: site.clone(),
                execution_plan_id: ExecutionPlanId::parse(format!(
                    "{number:02x}{}",
                    "a".repeat(62)
                ))
                .unwrap(),
            },
            paired_observations: 0,
            request_compatible: true,
            forbidden: false,
            disabled: false,
            divergence_threshold: 0.1,
            divergence_exposure: 0.0,
        }
    }

    fn connection() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&conn).unwrap();
        conn
    }

    fn policy() -> ExplorationPolicy {
        ExplorationPolicy {
            seed: 7,
            max_calls_per_site: 3,
            max_concurrent_per_site: 1,
            evidence_target: 2,
            window_us: 1_000,
            lease_duration_us: 100,
            divergence_exposure_budget: 0.5,
            task_damage_budget: Some(0.5),
        }
    }

    fn observation_feedback(divergence: f64) -> CounterfactualFeedback {
        CounterfactualFeedback {
            divergence,
            cost_usd: 0.01,
            latency_ms: 20.0,
            label: CounterfactualLabel::ObservationOnly,
        }
    }

    #[test]
    fn defaults_match_frozen_contract() {
        let policy = ExplorationPolicy::default();
        assert_eq!(policy.max_calls_per_site, 20);
        assert_eq!(policy.max_concurrent_per_site, 1);
        assert_eq!(policy.evidence_target, 20);
        assert_eq!(policy.window_us, 24 * 60 * 60 * 1_000_000);
        assert_eq!(policy.divergence_exposure_budget, 1.0);
        assert_eq!(policy.task_damage_budget, None);
        policy.validate().unwrap();
    }

    #[test]
    fn selection_is_seeded_and_candidate_order_independent() {
        let call_site = site(1);
        let candidates = vec![
            candidate(&call_site, 1),
            candidate(&call_site, 2),
            candidate(&call_site, 3),
        ];
        let controller = ExplorationController::with_policy(policy()).unwrap();

        let mut first_db = connection();
        let first = controller.decide_and_reserve(
            &mut first_db,
            &call_site,
            &candidates,
            NOW_US,
        );

        let mut reversed = candidates.clone();
        reversed.reverse();
        let mut second_db = connection();
        let second = controller.decide_and_reserve(
            &mut second_db,
            &call_site,
            &reversed,
            NOW_US,
        );

        assert!(first.return_reference);
        assert_eq!(first.reason, ExplorationReason::CandidateReserved);
        assert_eq!(
            first.counterfactual.unwrap().key,
            second.counterfactual.unwrap().key
        );
    }

    #[test]
    fn forbidden_incompatible_disabled_and_warm_candidates_never_run() {
        let call_site = site(2);
        let mut forbidden = candidate(&call_site, 1);
        forbidden.forbidden = true;
        let mut incompatible = candidate(&call_site, 2);
        incompatible.request_compatible = false;
        let mut disabled = candidate(&call_site, 3);
        disabled.disabled = true;
        let mut warm = candidate(&call_site, 4);
        warm.paired_observations = policy().evidence_target;
        let candidates = vec![forbidden, incompatible, disabled, warm];

        let mut conn = connection();
        let decision = ExplorationController::with_policy(policy())
            .unwrap()
            .decide_and_reserve(&mut conn, &call_site, &candidates, NOW_US);
        assert!(decision.return_reference);
        assert!(decision.counterfactual.is_none());
        assert_eq!(decision.reason, ExplorationReason::NoEligibleCandidate);
        assert_eq!(
            conn.query_row(
                "SELECT COUNT(*) FROM execution_plan_exploration",
                [],
                |row| row.get::<_, i64>(0)
            )
            .unwrap(),
            0
        );
    }

    #[test]
    fn one_live_counterfactual_is_enforced_and_failure_releases_it() {
        let call_site = site(3);
        let candidates = vec![candidate(&call_site, 1)];
        let controller = ExplorationController::with_policy(policy()).unwrap();
        let mut conn = connection();

        let first = controller.decide_and_reserve(
            &mut conn,
            &call_site,
            &candidates,
            NOW_US,
        );
        let blocked = controller.decide_and_reserve(
            &mut conn,
            &call_site,
            &candidates,
            NOW_US + 1,
        );
        assert_eq!(blocked.reason, ExplorationReason::ConcurrencyCapReached);

        controller
            .fail(
                &mut conn,
                first.counterfactual.as_ref().unwrap(),
                NOW_US + 2,
            )
            .unwrap();
        let next = controller.decide_and_reserve(
            &mut conn,
            &call_site,
            &candidates,
            NOW_US + 3,
        );
        assert_eq!(next.reason, ExplorationReason::CandidateReserved);
    }

    #[test]
    fn rolling_call_cap_survives_new_controller_instance() {
        let call_site = site(4);
        let candidates = vec![candidate(&call_site, 1)];
        let controller = ExplorationController::with_policy(policy()).unwrap();
        let mut conn = connection();

        for offset in 0..3 {
            let decision = controller.decide_and_reserve(
                &mut conn,
                &call_site,
                &candidates,
                NOW_US + offset * 10,
            );
            let lease = decision.counterfactual.unwrap();
            controller
                .complete(
                    &mut conn,
                    &lease,
                    &observation_feedback(0.0),
                    NOW_US + offset * 10 + 1,
                )
                .unwrap();
        }

        let restarted = ExplorationController::with_policy(policy()).unwrap();
        let blocked = restarted.decide_and_reserve(
            &mut conn,
            &call_site,
            &candidates,
            NOW_US + 40,
        );
        assert_eq!(blocked.reason, ExplorationReason::CallCapExhausted);
        assert!(blocked.counterfactual.is_none());
    }

    #[test]
    fn rolling_state_survives_database_close_and_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("cost_model.db");
        let call_site = site(10);
        let candidates = vec![candidate(&call_site, 1)];
        let controller = ExplorationController::with_policy(policy()).unwrap();
        {
            let mut conn = Connection::open(&path).unwrap();
            ensure_cost_model_schema(&conn).unwrap();
            let lease = controller
                .decide_and_reserve(&mut conn, &call_site, &candidates, NOW_US)
                .counterfactual
                .unwrap();
            controller
                .complete(
                    &mut conn,
                    &lease,
                    &observation_feedback(0.2),
                    NOW_US + 1,
                )
                .unwrap();
        }

        let conn = Connection::open(&path).unwrap();
        let restarted = ExplorationController::with_policy(policy()).unwrap();
        let snapshot = restarted.snapshot(&conn, &call_site, NOW_US + 2).unwrap();
        assert_eq!(snapshot.calls_in_window, 1);
        assert_eq!(snapshot.completed_calls, 1);
        assert_eq!(snapshot.observed_counterfactuals, 1);
        assert!((snapshot.divergence_exposure - 0.1).abs() < 1e-12);
    }

    #[test]
    fn independent_database_connections_share_concurrency_limit() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("cost_model.db");
        let call_site = site(11);
        let candidates = vec![candidate(&call_site, 1)];
        let controller = ExplorationController::with_policy(policy()).unwrap();
        let mut first = Connection::open(&path).unwrap();
        ensure_cost_model_schema(&first).unwrap();
        let mut second = Connection::open(&path).unwrap();

        let reserved = controller.decide_and_reserve(
            &mut first,
            &call_site,
            &candidates,
            NOW_US,
        );
        assert_eq!(reserved.reason, ExplorationReason::CandidateReserved);
        let blocked = controller.decide_and_reserve(
            &mut second,
            &call_site,
            &candidates,
            NOW_US + 1,
        );
        assert_eq!(blocked.reason, ExplorationReason::ConcurrencyCapReached);
        assert!(blocked.counterfactual.is_none());
    }

    #[test]
    fn expired_lease_releases_concurrency_but_still_spends_a_call() {
        let call_site = site(5);
        let candidates = vec![candidate(&call_site, 1)];
        let controller = ExplorationController::with_policy(policy()).unwrap();
        let mut conn = connection();
        controller.decide_and_reserve(&mut conn, &call_site, &candidates, NOW_US);

        let next = controller.decide_and_reserve(
            &mut conn,
            &call_site,
            &candidates,
            NOW_US + 101,
        );
        assert_eq!(next.reason, ExplorationReason::CandidateReserved);
        let snapshot = controller.snapshot(&conn, &call_site, NOW_US + 101).unwrap();
        assert_eq!(snapshot.calls_in_window, 2);
        assert_eq!(snapshot.active_leases, 1);
        assert_eq!(snapshot.abandoned_calls, 1);
    }

    #[test]
    fn observation_and_task_quality_feedback_remain_distinct() {
        let call_site = site(6);
        let candidates = vec![candidate(&call_site, 1)];
        let controller = ExplorationController::with_policy(policy()).unwrap();
        let mut conn = connection();

        let observation = controller
            .decide_and_reserve(&mut conn, &call_site, &candidates, NOW_US)
            .counterfactual
            .unwrap();
        controller
            .complete(
                &mut conn,
                &observation,
                &observation_feedback(0.2),
                NOW_US + 1,
            )
            .unwrap();

        let labeled = controller
            .decide_and_reserve(&mut conn, &call_site, &candidates, NOW_US + 2)
            .counterfactual
            .unwrap();
        controller
            .complete(
                &mut conn,
                &labeled,
                &CounterfactualFeedback {
                    divergence: 0.3,
                    cost_usd: 0.02,
                    latency_ms: 30.0,
                    label: CounterfactualLabel::TaskQuality {
                        reference_quality: 0.9,
                        candidate_quality: 0.6,
                    },
                },
                NOW_US + 3,
            )
            .unwrap();

        let snapshot = controller.snapshot(&conn, &call_site, NOW_US + 3).unwrap();
        assert_eq!(snapshot.observed_counterfactuals, 1);
        assert_eq!(snapshot.task_labeled_counterfactuals, 1);
        assert!((snapshot.counterfactual_cost_usd - 0.03).abs() < 1e-12);
        assert!((snapshot.divergence_exposure - 0.3).abs() < 1e-12);
        assert!((snapshot.task_damage - 0.3).abs() < 1e-12);
    }

    #[test]
    fn divergence_and_task_damage_budgets_stop_only_the_exposed_plan() {
        let call_site = site(7);
        let risky = candidate(&call_site, 1);
        let safe = candidate(&call_site, 2);
        let controller = ExplorationController::with_policy(policy()).unwrap();
        let mut conn = connection();

        // Seed a completed risky-plan row directly through the public API.
        let first = controller
            .decide_and_reserve(
                &mut conn,
                &call_site,
                std::slice::from_ref(&risky),
                NOW_US,
            )
            .counterfactual
            .unwrap();
        controller
            .complete(
                &mut conn,
                &first,
                &CounterfactualFeedback {
                    divergence: 0.7,
                    cost_usd: 0.01,
                    latency_ms: 1.0,
                    label: CounterfactualLabel::TaskQuality {
                        reference_quality: 1.0,
                        candidate_quality: 0.4,
                    },
                },
                NOW_US + 1,
            )
            .unwrap();

        let decision = controller.decide_and_reserve(
            &mut conn,
            &call_site,
            &[risky.clone(), safe.clone()],
            NOW_US + 2,
        );
        assert_eq!(decision.counterfactual.unwrap().key, safe.key);
    }

    #[test]
    fn completion_is_idempotent_and_conflicting_replay_is_rejected() {
        let call_site = site(8);
        let candidate = candidate(&call_site, 1);
        let controller = ExplorationController::with_policy(policy()).unwrap();
        let mut conn = connection();
        let lease = controller
            .decide_and_reserve(
                &mut conn,
                &call_site,
                std::slice::from_ref(&candidate),
                NOW_US,
            )
            .counterfactual
            .unwrap();
        let feedback = observation_feedback(0.2);

        assert_eq!(
            controller
                .complete(&mut conn, &lease, &feedback, NOW_US + 1)
                .unwrap(),
            ExplorationCompletion::Recorded
        );
        assert_eq!(
            controller
                .complete(&mut conn, &lease, &feedback, NOW_US + 2)
                .unwrap(),
            ExplorationCompletion::AlreadyRecorded
        );
        assert_eq!(
            controller
                .complete(
                    &mut conn,
                    &lease,
                    &observation_feedback(0.3),
                    NOW_US + 3,
                )
                .unwrap_err(),
            ExplorationError::ConflictingFeedback
        );
    }

    #[test]
    fn invalid_feedback_does_not_mutate_or_release_the_lease() {
        let call_site = site(9);
        let candidate = candidate(&call_site, 1);
        let controller = ExplorationController::with_policy(policy()).unwrap();
        let mut conn = connection();
        let lease = controller
            .decide_and_reserve(
                &mut conn,
                &call_site,
                std::slice::from_ref(&candidate),
                NOW_US,
            )
            .counterfactual
            .unwrap();
        let error = controller
            .complete(
                &mut conn,
                &lease,
                &observation_feedback(f64::NAN),
                NOW_US + 1,
            )
            .unwrap_err();
        assert_eq!(error, ExplorationError::InvalidDivergence);
        assert_eq!(
            controller.snapshot(&conn, &call_site, NOW_US + 1).unwrap().active_leases,
            1
        );
    }

    #[test]
    fn invalid_policy_is_rejected() {
        let mut invalid = policy();
        invalid.max_calls_per_site = 0;
        assert_eq!(
            ExplorationController::with_policy(invalid).unwrap_err(),
            ExplorationPolicyError::ZeroCallCap
        );
    }

    #[test]
    fn missing_persistence_schema_fails_to_reference_without_a_lease() {
        let call_site = site(12);
        let mut conn = Connection::open_in_memory().unwrap();
        let decision = ExplorationController::with_policy(policy())
            .unwrap()
            .decide_and_reserve(
                &mut conn,
                &call_site,
                &[candidate(&call_site, 1)],
                NOW_US,
            );
        assert!(decision.return_reference);
        assert!(decision.counterfactual.is_none());
        assert_eq!(decision.reason, ExplorationReason::PersistenceFailure);
    }
}
