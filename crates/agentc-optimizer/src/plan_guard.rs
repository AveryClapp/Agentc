//! Persistent divergence-exposure guard for complete execution plans.
//!
//! Unlike the compatibility guard in [`crate::budget`], this controller is
//! keyed by the complete [`PlanProfileKey`]. A composed observation therefore
//! consumes only the budget of the exact model-and-rewrite plan that ran. It
//! never fabricates causal evidence for the plan's constituent rules.

use std::collections::{HashMap, VecDeque};
use std::error::Error;
use std::fmt;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result as AnyResult};
use dashmap::DashMap;
use parking_lot::RwLock;
use rusqlite::{params, Connection, OptionalExtension};

use crate::execution_plan::PlanAdmission;
use crate::plan_profile::{PlanObservationToken, PlanProfileKey, PlanRuntimeVersion};

/// Rolling horizon used by the production divergence-exposure contract.
pub const PLAN_EXPOSURE_WINDOW_US: i64 = 24 * 60 * 60 * 1_000_000;
/// Exposure at or above this value disables the exact complete plan.
pub const DEFAULT_PLAN_EXPOSURE_BUDGET: f64 = 1.0;
/// A disabled plan remains blocked for this long before cold re-admission.
pub const PLAN_DISABLE_COOLDOWN_US: i64 = 24 * 60 * 60 * 1_000_000;

/// One above-threshold comparison retained in the rolling exposure window.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanExposureSample {
    pub plan_observation_sequence: u64,
    pub divergence: f64,
    pub excess: f64,
    pub observed_at_us: i64,
}

/// Durable disable metadata for one exact plan.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanDisabledEntry {
    pub exposure: f64,
    pub disabled_at_us: i64,
    pub reenable_at_us: i64,
}

/// Current state for one `(call-site version, execution-plan ID)` key.
#[derive(Debug, Clone)]
pub struct PlanGuardEntry {
    pub key: PlanProfileKey,
    pub runtime_version: PlanRuntimeVersion,
    pub divergence_threshold: f64,
    pub divergence_exposure: f64,
    pub updated_at_us: i64,
    pub disabled: Option<PlanDisabledEntry>,
    samples: Arc<VecDeque<PlanExposureSample>>,
    generation: u64,
}

impl PlanGuardEntry {
    fn new(
        key: PlanProfileKey,
        runtime_version: PlanRuntimeVersion,
        divergence_threshold: f64,
    ) -> Self {
        Self {
            key,
            runtime_version,
            divergence_threshold,
            divergence_exposure: 0.0,
            updated_at_us: 0,
            disabled: None,
            samples: Arc::new(VecDeque::new()),
            generation: 0,
        }
    }

    pub fn retained_samples(&self) -> &VecDeque<PlanExposureSample> {
        &self.samples
    }
}

/// Result of folding one correlated comparison into a plan guard.
#[derive(Debug, Clone, PartialEq)]
pub enum PlanGuardOutcome {
    Tracked { exposure: f64 },
    AlreadyRecorded { exposure: f64 },
    Disabled(PlanDisabledEntry),
}

/// Admission-facing state. Cooldown expiry deliberately does not make a plan
/// user-visible; the selector must explicitly finish cold re-admission first.
#[derive(Debug, Clone, PartialEq)]
pub enum PlanGuardDecision {
    Allow,
    Disabled(PlanDisabledEntry),
    ColdReadmissionRequired(PlanDisabledEntry),
}

impl PlanGuardDecision {
    pub fn blocks_user_visible(&self) -> bool {
        !matches!(self, Self::Allow)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PlanGuardError {
    InvalidExposureBudget,
    InvalidExposureWindow,
    InvalidCooldown,
    InvalidDivergence,
    InvalidThreshold,
    NegativeObservationTime,
    InvalidObservationSequence,
    EmptyRuntimeField(&'static str),
    ThresholdMismatch,
    ConflictingDivergence,
    ReadmissionCooldownActive,
    ExposureBudgetStillExceeded,
}

impl fmt::Display for PlanGuardError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidExposureBudget => {
                formatter.write_str("plan exposure budget must be finite and non-negative")
            }
            Self::InvalidExposureWindow => {
                formatter.write_str("plan exposure window must be positive")
            }
            Self::InvalidCooldown => formatter.write_str("plan disable cooldown must be positive"),
            Self::InvalidDivergence => {
                formatter.write_str("plan divergence must be finite and in [0, 1]")
            }
            Self::InvalidThreshold => {
                formatter.write_str("plan divergence threshold must be finite and in [0, 1]")
            }
            Self::NegativeObservationTime => {
                formatter.write_str("plan guard timestamp must be non-negative")
            }
            Self::InvalidObservationSequence => {
                formatter.write_str("plan guard observation sequence must be positive")
            }
            Self::EmptyRuntimeField(field) => {
                write!(formatter, "plan guard runtime field {field} is empty")
            }
            Self::ThresholdMismatch => formatter
                .write_str("plan guard threshold changed without a new execution-plan identity"),
            Self::ConflictingDivergence => formatter
                .write_str("plan guard observation already has a different divergence value"),
            Self::ReadmissionCooldownActive => {
                formatter.write_str("plan guard cooldown has not elapsed")
            }
            Self::ExposureBudgetStillExceeded => formatter
                .write_str("plan exposure budget is still exhausted after cooldown"),
        }
    }
}

impl Error for PlanGuardError {}

/// Concurrent plan-level exposure state with exact rolling-window persistence.
pub struct PlanGuard {
    entries: Arc<DashMap<PlanProfileKey, PlanGuardEntry>>,
    dirty: Arc<RwLock<HashMap<PlanProfileKey, u64>>>,
    exposure_budget: f64,
    exposure_window_us: i64,
    cooldown_us: i64,
}

impl Default for PlanGuard {
    fn default() -> Self {
        Self::new()
    }
}

impl PlanGuard {
    pub fn new() -> Self {
        Self::with_limits(
            DEFAULT_PLAN_EXPOSURE_BUDGET,
            PLAN_EXPOSURE_WINDOW_US,
            PLAN_DISABLE_COOLDOWN_US,
        )
        .expect("default plan-guard limits are valid")
    }

    pub fn with_limits(
        exposure_budget: f64,
        exposure_window_us: i64,
        cooldown_us: i64,
    ) -> Result<Self, PlanGuardError> {
        if !exposure_budget.is_finite() || exposure_budget < 0.0 {
            return Err(PlanGuardError::InvalidExposureBudget);
        }
        if exposure_window_us <= 0 {
            return Err(PlanGuardError::InvalidExposureWindow);
        }
        if cooldown_us <= 0 {
            return Err(PlanGuardError::InvalidCooldown);
        }
        Ok(Self {
            entries: Arc::new(DashMap::new()),
            dirty: Arc::new(RwLock::new(HashMap::new())),
            exposure_budget,
            exposure_window_us,
            cooldown_us,
        })
    }

    pub fn exposure_budget(&self) -> f64 {
        self.exposure_budget
    }

    /// Record one paired comparison against the exact plan in `observation`.
    /// Only positive excess contributes to the rolling exposure total.
    pub fn record_sample(
        &self,
        observation: &PlanObservationToken,
        divergence: f64,
        divergence_threshold: f64,
        observed_at_us: Option<i64>,
    ) -> Result<PlanGuardOutcome, PlanGuardError> {
        validate_fraction(divergence).map_err(|_| PlanGuardError::InvalidDivergence)?;
        validate_fraction(divergence_threshold)
            .map_err(|_| PlanGuardError::InvalidThreshold)?;
        if observation.sequence() == 0 {
            return Err(PlanGuardError::InvalidObservationSequence);
        }
        validate_runtime(observation.runtime_version())?;
        let observed_at_us = observed_at_us.unwrap_or_else(now_micros);
        if observed_at_us < 0 {
            return Err(PlanGuardError::NegativeObservationTime);
        }

        let key = observation.key().clone();
        let (outcome, generation) = {
            let mut entry = self.entries.entry(key.clone()).or_insert_with(|| {
                PlanGuardEntry::new(
                    key.clone(),
                    observation.runtime_version().clone(),
                    divergence_threshold,
                )
            });

            if entry.runtime_version != *observation.runtime_version() {
                let generation = entry.generation;
                *entry = PlanGuardEntry::new(
                    key.clone(),
                    observation.runtime_version().clone(),
                    divergence_threshold,
                );
                // Generations are monotonic across runtime cold-starts. A
                // flush may have snapshotted the previous runtime, so reusing
                // its generation could let that flush clear this newer state.
                entry.generation = generation;
            } else if entry.divergence_threshold.to_bits() != divergence_threshold.to_bits() {
                return Err(PlanGuardError::ThresholdMismatch);
            }

            let pruned = prune_expired(&mut entry, observed_at_us, self.exposure_window_us);
            if let Some(existing_divergence) = entry
                .samples
                .iter()
                .find(|sample| sample.plan_observation_sequence == observation.sequence())
                .map(|sample| sample.divergence)
            {
                if existing_divergence.to_bits() == divergence.to_bits() {
                    if pruned {
                        entry.updated_at_us = entry.updated_at_us.max(observed_at_us);
                        entry.generation = entry.generation.saturating_add(1);
                        let generation = entry.generation;
                        let exposure = entry.divergence_exposure;
                        drop(entry);
                        self.dirty.write().insert(key, generation);
                        return Ok(PlanGuardOutcome::AlreadyRecorded { exposure });
                    }
                    return Ok(PlanGuardOutcome::AlreadyRecorded {
                        exposure: entry.divergence_exposure,
                    });
                }
                return Err(PlanGuardError::ConflictingDivergence);
            }

            let excess = (divergence - divergence_threshold).max(0.0);
            if excess > 0.0 {
                Arc::make_mut(&mut entry.samples).push_back(PlanExposureSample {
                    plan_observation_sequence: observation.sequence(),
                    divergence,
                    excess,
                    observed_at_us,
                });
                entry.divergence_exposure = exposure_sum(&entry.samples);
            }
            entry.updated_at_us = entry.updated_at_us.max(observed_at_us);
            entry.generation = entry.generation.saturating_add(1);

            let should_disable = entry.divergence_exposure >= self.exposure_budget
                && entry
                    .disabled
                    .as_ref()
                    .is_none_or(|disabled| observed_at_us >= disabled.reenable_at_us);
            let outcome = if should_disable {
                let disabled = PlanDisabledEntry {
                    exposure: entry.divergence_exposure,
                    disabled_at_us: observed_at_us,
                    reenable_at_us: observed_at_us.saturating_add(self.cooldown_us),
                };
                entry.disabled = Some(disabled.clone());
                PlanGuardOutcome::Disabled(disabled)
            } else {
                PlanGuardOutcome::Tracked {
                    exposure: entry.divergence_exposure,
                }
            };
            (outcome, entry.generation)
        };
        self.dirty.write().insert(key, generation);
        Ok(outcome)
    }

    /// Return whether an exact plan may be user-visible at `now_us`.
    ///
    /// A cooldown-expired row remains blocked until the constrained selector
    /// has collected fresh evidence and calls [`Self::mark_readmitted`].
    pub fn decision(&self, key: &PlanProfileKey, now_us: i64) -> PlanGuardDecision {
        let Some(entry) = self.entries.get(key) else {
            return PlanGuardDecision::Allow;
        };
        let Some(disabled) = entry.disabled.clone() else {
            return PlanGuardDecision::Allow;
        };
        if now_us < disabled.reenable_at_us {
            PlanGuardDecision::Disabled(disabled)
        } else {
            PlanGuardDecision::ColdReadmissionRequired(disabled)
        }
    }

    /// Build the guard portion of a selector candidate without exposing the
    /// controller's storage layout to candidate generation.
    pub fn admission(
        &self,
        key: &PlanProfileKey,
        request_compatible: bool,
        divergence_threshold: f64,
        now_us: i64,
    ) -> Result<PlanAdmission, PlanGuardError> {
        validate_fraction(divergence_threshold)
            .map_err(|_| PlanGuardError::InvalidThreshold)?;
        if now_us < 0 {
            return Err(PlanGuardError::NegativeObservationTime);
        }
        let Some(entry) = self.entries.get(key) else {
            return Ok(PlanAdmission {
                request_compatible,
                disabled: false,
                divergence_threshold,
                divergence_exposure: 0.0,
            });
        };
        if entry.divergence_threshold.to_bits() != divergence_threshold.to_bits() {
            return Err(PlanGuardError::ThresholdMismatch);
        }
        let cutoff = now_us.saturating_sub(self.exposure_window_us);
        let divergence_exposure = entry
            .samples
            .iter()
            .filter(|sample| sample.observed_at_us > cutoff)
            .map(|sample| sample.excess)
            .sum();
        Ok(PlanAdmission {
            request_compatible,
            disabled: entry.disabled.is_some(),
            divergence_threshold,
            divergence_exposure,
        })
    }

    /// Clear a cooldown-expired disable after the selector has independently
    /// established the complete plan's cold-admission evidence.
    pub fn mark_readmitted(
        &self,
        key: &PlanProfileKey,
        now_us: i64,
    ) -> Result<bool, PlanGuardError> {
        if now_us < 0 {
            return Err(PlanGuardError::NegativeObservationTime);
        }
        let Some(mut entry) = self.entries.get_mut(key) else {
            return Ok(false);
        };
        let Some(disabled) = entry.disabled.as_ref() else {
            return Ok(false);
        };
        if now_us < disabled.reenable_at_us {
            return Err(PlanGuardError::ReadmissionCooldownActive);
        }
        let pruned = prune_expired(&mut entry, now_us, self.exposure_window_us);
        if entry.divergence_exposure >= self.exposure_budget {
            if pruned {
                entry.updated_at_us = entry.updated_at_us.max(now_us);
                entry.generation = entry.generation.saturating_add(1);
                let generation = entry.generation;
                drop(entry);
                self.dirty.write().insert(key.clone(), generation);
            }
            return Err(PlanGuardError::ExposureBudgetStillExceeded);
        }
        entry.disabled = None;
        entry.updated_at_us = entry.updated_at_us.max(now_us);
        entry.generation = entry.generation.saturating_add(1);
        let generation = entry.generation;
        drop(entry);
        self.dirty.write().insert(key.clone(), generation);
        Ok(true)
    }

    pub fn get(&self, key: &PlanProfileKey) -> Option<PlanGuardEntry> {
        self.entries.get(key).map(|entry| entry.clone())
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn dirty_len(&self) -> usize {
        self.dirty.read().len()
    }

    /// Hydrate guard summaries, rolling exposure events, and disable rows.
    pub fn warm_from_db(&self, conn: &Connection) -> AnyResult<usize> {
        self.warm_from_db_at(conn, now_micros())
    }

    fn warm_from_db_at(&self, conn: &Connection, now_us: i64) -> AnyResult<usize> {
        let mut statement = conn
            .prepare(
                "SELECT call_site_version, execution_plan_id, divergence_threshold, \
                        divergence_exposure, window_samples, provider_protocol, \
                        target_model_id, target_model_version, price_table_version, updated_at \
                 FROM execution_plan_guard",
            )
            .context("prepare plan-guard warmup")?;
        let rows = statement
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, f64>(2)?,
                    row.get::<_, f64>(3)?,
                    row.get::<_, i64>(4)?,
                    PlanRuntimeVersion {
                        provider_protocol: row.get(5)?,
                        target_model_id: row.get(6)?,
                        target_model_version: row.get(7)?,
                        price_table_version: row.get(8)?,
                    },
                    row.get::<_, i64>(9)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        drop(statement);

        let mut count = 0usize;
        for (
            call_site_version,
            execution_plan_id,
            threshold,
            persisted_exposure,
            persisted_window,
            runtime_version,
            updated_at_us,
        ) in rows
        {
            if validate_fraction(threshold).is_err() {
                anyhow::bail!("persisted plan-guard threshold is invalid");
            }
            validate_runtime(&runtime_version).context("validate persisted plan-guard runtime")?;
            if !persisted_exposure.is_finite()
                || persisted_exposure < 0.0
                || persisted_window < 0
                || updated_at_us < 0
            {
                anyhow::bail!("persisted plan-guard summary is invalid");
            }
            let key = PlanProfileKey {
                call_site_version: crate::plan_profile::CallSiteVersion::parse(call_site_version)
                    .context("decode plan-guard call-site version")?,
                execution_plan_id: crate::execution_plan::ExecutionPlanId::parse(
                    execution_plan_id,
                )
                .context("decode plan-guard execution-plan ID")?,
            };
            let mut samples = load_samples(conn, &key, threshold, &runtime_version)?;
            let original_samples = samples.len();
            let cutoff = now_us.saturating_sub(self.exposure_window_us);
            samples.retain(|sample| sample.observed_at_us > cutoff);
            let exposure = exposure_sum(&samples);
            let disabled = load_disabled(conn, &key)?;
            let normalized = samples.len() != original_samples
                || persisted_window != samples.len() as i64
                || persisted_exposure.to_bits() != exposure.to_bits();
            let generation = u64::from(normalized);
            let entry = PlanGuardEntry {
                key: key.clone(),
                runtime_version,
                divergence_threshold: threshold,
                divergence_exposure: exposure,
                updated_at_us,
                disabled,
                samples: Arc::new(samples),
                generation,
            };
            if normalized {
                self.dirty.write().insert(key.clone(), generation);
            }
            self.entries.insert(key, entry);
            count += 1;
        }
        Ok(count)
    }

    /// Persist every dirty guard summary, exact rolling event, and disable row.
    pub fn flush_dirty(&self, conn: &mut Connection) -> AnyResult<usize> {
        self.flush_dirty_with_hook(conn, || {})
    }

    fn flush_dirty_with_hook<F>(&self, conn: &mut Connection, before_clear: F) -> AnyResult<usize>
    where
        F: FnOnce(),
    {
        let dirty_generations: Vec<(PlanProfileKey, u64)> = self
            .dirty
            .read()
            .iter()
            .map(|(key, generation)| (key.clone(), *generation))
            .collect();
        if dirty_generations.is_empty() {
            return Ok(0);
        }
        let snapshots: Vec<(PlanProfileKey, u64, PlanGuardEntry)> = dirty_generations
            .into_iter()
            .filter_map(|(key, generation)| {
                self.entries
                    .get(&key)
                    .map(|entry| (key, generation, entry.clone()))
            })
            .collect();
        let transaction = conn.transaction().context("begin plan-guard flush")?;
        for (key, _, entry) in &snapshots {
            persist_entry(&transaction, entry).with_context(|| {
                format!(
                    "persist plan guard {}/{}",
                    key.call_site_version, key.execution_plan_id
                )
            })?;
        }
        transaction.commit().context("commit plan-guard flush")?;

        before_clear();
        let mut dirty = self.dirty.write();
        for (key, generation, _) in &snapshots {
            if dirty.get(key).copied() == Some(*generation) {
                dirty.remove(key);
            }
        }
        Ok(snapshots.len())
    }
}

fn validate_fraction(value: f64) -> Result<(), ()> {
    if value.is_finite() && (0.0..=1.0).contains(&value) {
        Ok(())
    } else {
        Err(())
    }
}

fn validate_runtime(runtime: &PlanRuntimeVersion) -> Result<(), PlanGuardError> {
    for (field, value) in [
        ("provider_protocol", runtime.provider_protocol.as_str()),
        ("target_model_id", runtime.target_model_id.as_str()),
        ("target_model_version", runtime.target_model_version.as_str()),
        ("price_table_version", runtime.price_table_version.as_str()),
    ] {
        if value.trim().is_empty() {
            return Err(PlanGuardError::EmptyRuntimeField(field));
        }
    }
    Ok(())
}

fn prune_expired(
    entry: &mut PlanGuardEntry,
    now_us: i64,
    exposure_window_us: i64,
) -> bool {
    let cutoff = now_us.saturating_sub(exposure_window_us);
    let before = entry.samples.len();
    Arc::make_mut(&mut entry.samples).retain(|sample| sample.observed_at_us > cutoff);
    entry.divergence_exposure = exposure_sum(&entry.samples);
    entry.samples.len() != before
}

fn exposure_sum(samples: &VecDeque<PlanExposureSample>) -> f64 {
    samples.iter().map(|sample| sample.excess).sum()
}

fn now_micros() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_micros() as i64)
        .unwrap_or(0)
}

fn load_samples(
    conn: &Connection,
    key: &PlanProfileKey,
    threshold: f64,
    runtime: &PlanRuntimeVersion,
) -> AnyResult<VecDeque<PlanExposureSample>> {
    let mut statement = conn
        .prepare(
            "SELECT plan_observation_sequence, divergence, excess, provider_protocol, \
                    target_model_id, target_model_version, price_table_version, observed_at \
             FROM execution_plan_guard_observation \
             WHERE call_site_version = ?1 AND execution_plan_id = ?2 \
             ORDER BY observed_at, plan_observation_sequence",
        )
        .context("prepare plan-guard observation load")?;
    let rows = statement
        .query_map(
            params![
                key.call_site_version.as_str(),
                key.execution_plan_id.as_str()
            ],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, f64>(1)?,
                    row.get::<_, f64>(2)?,
                    PlanRuntimeVersion {
                        provider_protocol: row.get(3)?,
                        target_model_id: row.get(4)?,
                        target_model_version: row.get(5)?,
                        price_table_version: row.get(6)?,
                    },
                    row.get::<_, i64>(7)?,
                ))
            },
        )?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    rows.into_iter()
        .map(|(sequence, divergence, excess, sample_runtime, observed_at_us)| {
            if sequence <= 0
                || validate_fraction(divergence).is_err()
                || !excess.is_finite()
                || excess <= 0.0
                || observed_at_us < 0
                || sample_runtime != *runtime
            {
                anyhow::bail!("persisted plan-guard observation is invalid");
            }
            let expected_excess = (divergence - threshold).max(0.0);
            if excess.to_bits() != expected_excess.to_bits() {
                anyhow::bail!("persisted plan-guard excess does not match its threshold");
            }
            Ok(PlanExposureSample {
                plan_observation_sequence: u64::try_from(sequence)
                    .context("negative plan-guard observation sequence")?,
                divergence,
                excess,
                observed_at_us,
            })
        })
        .collect()
}

fn load_disabled(
    conn: &Connection,
    key: &PlanProfileKey,
) -> AnyResult<Option<PlanDisabledEntry>> {
    let disabled = conn
        .query_row(
            "SELECT exposure, disabled_at, reenable_at FROM execution_plan_disabled \
             WHERE call_site_version = ?1 AND execution_plan_id = ?2",
            params![
                key.call_site_version.as_str(),
                key.execution_plan_id.as_str()
            ],
            |row| {
                Ok(PlanDisabledEntry {
                    exposure: row.get(0)?,
                    disabled_at_us: row.get(1)?,
                    reenable_at_us: row.get(2)?,
                })
            },
        )
        .optional()?;
    if let Some(disabled) = disabled.as_ref() {
        if !disabled.exposure.is_finite()
            || disabled.exposure < 0.0
            || disabled.disabled_at_us < 0
            || disabled.reenable_at_us < disabled.disabled_at_us
        {
            anyhow::bail!("persisted plan disable is invalid");
        }
    }
    Ok(disabled)
}

fn persist_entry(conn: &Connection, entry: &PlanGuardEntry) -> AnyResult<()> {
    let key = &entry.key;
    conn.execute(
        "DELETE FROM execution_plan_guard_observation \
         WHERE call_site_version = ?1 AND execution_plan_id = ?2",
        params![
            key.call_site_version.as_str(),
            key.execution_plan_id.as_str()
        ],
    )?;
    {
        let mut statement = conn.prepare(
            "INSERT INTO execution_plan_guard_observation (\
                call_site_version, execution_plan_id, plan_observation_sequence, \
                divergence, excess, provider_protocol, target_model_id, \
                target_model_version, price_table_version, observed_at\
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        )?;
        for sample in entry.samples.iter() {
            statement.execute(params![
                key.call_site_version.as_str(),
                key.execution_plan_id.as_str(),
                i64::try_from(sample.plan_observation_sequence)
                    .context("plan-guard observation sequence exceeds SQLite")?,
                sample.divergence,
                sample.excess,
                entry.runtime_version.provider_protocol,
                entry.runtime_version.target_model_id,
                entry.runtime_version.target_model_version,
                entry.runtime_version.price_table_version,
                sample.observed_at_us,
            ])?;
        }
    }
    conn.execute(
        "INSERT INTO execution_plan_guard (\
            call_site_version, execution_plan_id, divergence_threshold, \
            divergence_exposure, window_samples, provider_protocol, target_model_id, \
            target_model_version, price_table_version, updated_at\
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10) \
         ON CONFLICT(call_site_version, execution_plan_id) DO UPDATE SET \
            divergence_threshold = excluded.divergence_threshold, \
            divergence_exposure = excluded.divergence_exposure, \
            window_samples = excluded.window_samples, \
            provider_protocol = excluded.provider_protocol, \
            target_model_id = excluded.target_model_id, \
            target_model_version = excluded.target_model_version, \
            price_table_version = excluded.price_table_version, \
            updated_at = excluded.updated_at",
        params![
            key.call_site_version.as_str(),
            key.execution_plan_id.as_str(),
            entry.divergence_threshold,
            entry.divergence_exposure,
            i64::try_from(entry.samples.len()).context("plan-guard sample count exceeds SQLite")?,
            entry.runtime_version.provider_protocol,
            entry.runtime_version.target_model_id,
            entry.runtime_version.target_model_version,
            entry.runtime_version.price_table_version,
            entry.updated_at_us,
        ],
    )?;
    conn.execute(
        "DELETE FROM execution_plan_disabled \
         WHERE call_site_version = ?1 AND execution_plan_id = ?2",
        params![
            key.call_site_version.as_str(),
            key.execution_plan_id.as_str()
        ],
    )?;
    if let Some(disabled) = entry.disabled.as_ref() {
        conn.execute(
            "INSERT INTO execution_plan_disabled (\
                call_site_version, execution_plan_id, reason, exposure, disabled_at, reenable_at\
             ) VALUES (?1, ?2, 'divergence_exposure', ?3, ?4, ?5)",
            params![
                key.call_site_version.as_str(),
                key.execution_plan_id.as_str(),
                disabled.exposure,
                disabled.disabled_at_us,
                disabled.reenable_at_us,
            ],
        )?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::execution_plan::ExecutionPlanId;
    use crate::plan_profile::{
        CallSiteVersion, CallSiteVersionSpec, PlanProfileUpdate, PlanProfiles,
    };
    use crate::schema::ensure_cost_model_schema;

    fn key(plan: u8) -> PlanProfileKey {
        PlanProfileKey {
            call_site_version: CallSiteVersion::from_spec(&CallSiteVersionSpec {
                call_site_id: "app.agent:step".to_string(),
                prompt_shape_version: "shape-v1".to_string(),
                provider_protocol: "openai-chat".to_string(),
                tool_schema_version: None,
                application_config_version: None,
            })
            .unwrap(),
            execution_plan_id: ExecutionPlanId::parse(format!("{plan:064x}")).unwrap(),
        }
    }

    fn runtime(version: &str) -> PlanRuntimeVersion {
        PlanRuntimeVersion {
            provider_protocol: "openai-chat".to_string(),
            target_model_id: "cheap".to_string(),
            target_model_version: version.to_string(),
            price_table_version: "prices-v1".to_string(),
        }
    }

    fn observation(key: PlanProfileKey, version: &str, sequence: u64) -> PlanObservationToken {
        let profiles = PlanProfiles::new();
        let mut token = None;
        for current in 1..=sequence {
            token = Some(
                profiles
                    .observe(PlanProfileUpdate {
                        key: key.clone(),
                        runtime_version: runtime(version),
                        input_tokens: 1,
                        output_tokens: 1,
                        latency_ms: 1.0,
                        cost_usd: 0.001,
                        output_is_structured: false,
                        output_is_short: true,
                        divergence: None,
                        dispatch_fallback: false,
                        observed_at_us: Some(current as i64),
                    })
                    .unwrap(),
            );
        }
        token.unwrap()
    }

    fn connection() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        connection
    }

    #[test]
    fn composed_sample_has_single_causal_identity() {
        let guard = PlanGuard::with_limits(0.25, 100, 100).unwrap();
        let joint_key = key(3);
        let route_only_key = key(1);
        let rewrite_only_key = key(2);

        let outcome = guard
            .record_sample(
                &observation(joint_key.clone(), "v1", 1),
                0.375,
                0.125,
                Some(1),
            )
            .unwrap();
        assert!(matches!(outcome, PlanGuardOutcome::Disabled(_)));
        assert!(guard.decision(&joint_key, 1).blocks_user_visible());
        let joint_admission = guard.admission(&joint_key, true, 0.125, 1).unwrap();
        assert!(joint_admission.disabled);
        assert_eq!(joint_admission.divergence_exposure, 0.25);
        assert_eq!(guard.decision(&route_only_key, 1), PlanGuardDecision::Allow);
        assert!(!guard
            .admission(&route_only_key, true, 0.125, 1)
            .unwrap()
            .disabled);
        assert_eq!(
            guard.decision(&rewrite_only_key, 1),
            PlanGuardDecision::Allow
        );
    }

    #[test]
    fn exposure_is_rolling_and_replay_is_idempotent() {
        let guard = PlanGuard::with_limits(1.0, 10, 10).unwrap();
        let plan_key = key(1);
        let first = observation(plan_key.clone(), "v1", 1);
        assert_eq!(
            guard.record_sample(&first, 0.375, 0.125, Some(1)).unwrap(),
            PlanGuardOutcome::Tracked { exposure: 0.25 }
        );
        assert_eq!(
            guard.record_sample(&first, 0.375, 0.125, Some(2)).unwrap(),
            PlanGuardOutcome::AlreadyRecorded { exposure: 0.25 }
        );
        let second = observation(plan_key.clone(), "v1", 2);
        assert_eq!(
            guard.record_sample(&second, 0.25, 0.125, Some(11)).unwrap(),
            PlanGuardOutcome::Tracked { exposure: 0.125 }
        );
        let entry = guard.get(&plan_key).unwrap();
        assert_eq!(entry.retained_samples().len(), 1);
        assert_eq!(entry.divergence_exposure, 0.125);
    }

    #[test]
    fn threshold_and_runtime_changes_do_not_mix_state() {
        let guard = PlanGuard::new();
        let plan_key = key(1);
        guard
            .record_sample(
                &observation(plan_key.clone(), "v1", 1),
                0.20,
                0.10,
                Some(1),
            )
            .unwrap();
        assert_eq!(
            guard.record_sample(
                &observation(plan_key.clone(), "v1", 2),
                0.20,
                0.20,
                Some(2),
            ),
            Err(PlanGuardError::ThresholdMismatch)
        );

        guard
            .record_sample(
                &observation(plan_key.clone(), "v2", 1),
                0.20,
                0.20,
                Some(3),
            )
            .unwrap();
        let entry = guard.get(&plan_key).unwrap();
        assert_eq!(entry.runtime_version, runtime("v2"));
        assert_eq!(entry.divergence_threshold, 0.20);
        assert_eq!(entry.divergence_exposure, 0.0);
    }

    #[test]
    fn cooldown_requires_explicit_cold_readmission() {
        let guard = PlanGuard::with_limits(0.10, 10, 10).unwrap();
        let plan_key = key(1);
        guard
            .record_sample(
                &observation(plan_key.clone(), "v1", 1),
                0.20,
                0.10,
                Some(5),
            )
            .unwrap();
        assert!(matches!(
            guard.decision(&plan_key, 14),
            PlanGuardDecision::Disabled(_)
        ));
        assert!(matches!(
            guard.decision(&plan_key, 15),
            PlanGuardDecision::ColdReadmissionRequired(_)
        ));
        assert_eq!(
            guard.mark_readmitted(&plan_key, 14),
            Err(PlanGuardError::ReadmissionCooldownActive)
        );
        assert!(guard.mark_readmitted(&plan_key, 15).unwrap());
        assert_eq!(guard.decision(&plan_key, 15), PlanGuardDecision::Allow);
    }

    #[test]
    fn exposure_and_disable_survive_restart() {
        let mut connection = connection();
        let first = PlanGuard::with_limits(0.20, 100, 10).unwrap();
        let plan_key = key(1);
        first
            .record_sample(
                &observation(plan_key.clone(), "v1", 1),
                0.20,
                0.10,
                Some(1),
            )
            .unwrap();
        first
            .record_sample(
                &observation(plan_key.clone(), "v1", 2),
                0.20,
                0.10,
                Some(2),
            )
            .unwrap();
        assert!(first.decision(&plan_key, 2).blocks_user_visible());
        assert_eq!(first.flush_dirty(&mut connection).unwrap(), 1);

        let restarted = PlanGuard::with_limits(0.20, 100, 10).unwrap();
        assert_eq!(restarted.warm_from_db_at(&connection, 3).unwrap(), 1);
        let entry = restarted.get(&plan_key).unwrap();
        assert_eq!(entry.retained_samples().len(), 2);
        assert!((entry.divergence_exposure - 0.20).abs() < 1e-12);
        assert!(matches!(
            restarted.decision(&plan_key, 3),
            PlanGuardDecision::Disabled(_)
        ));
    }

    #[test]
    fn invalid_inputs_do_not_create_state() {
        let guard = PlanGuard::new();
        let token = observation(key(1), "v1", 1);
        for divergence in [f64::NAN, f64::INFINITY, -0.1, 1.1] {
            assert_eq!(
                guard.record_sample(&token, divergence, 0.1, Some(1)),
                Err(PlanGuardError::InvalidDivergence)
            );
        }
        for threshold in [f64::NAN, f64::INFINITY, -0.1, 1.1] {
            assert_eq!(
                guard.record_sample(&token, 0.1, threshold, Some(1)),
                Err(PlanGuardError::InvalidThreshold)
            );
        }
        assert!(guard.is_empty());
        assert_eq!(guard.dirty_len(), 0);
    }

    #[test]
    fn concurrent_post_snapshot_update_remains_dirty() {
        let mut connection = connection();
        let guard = PlanGuard::new();
        let plan_key = key(1);
        guard
            .record_sample(
                &observation(plan_key.clone(), "v1", 1),
                0.20,
                0.10,
                Some(1),
            )
            .unwrap();
        guard
            .flush_dirty_with_hook(&mut connection, || {
                guard
                    .record_sample(
                        &observation(plan_key.clone(), "v1", 2),
                        0.30,
                        0.10,
                        Some(2),
                    )
                    .unwrap();
            })
            .unwrap();
        assert_eq!(guard.dirty_len(), 1);
    }

    #[test]
    fn concurrent_runtime_cold_start_does_not_reuse_flushed_generation() {
        let mut connection = connection();
        let guard = PlanGuard::new();
        let plan_key = key(1);
        guard
            .record_sample(
                &observation(plan_key.clone(), "v1", 1),
                0.20,
                0.10,
                Some(1),
            )
            .unwrap();
        guard
            .flush_dirty_with_hook(&mut connection, || {
                guard
                    .record_sample(
                        &observation(plan_key.clone(), "v2", 1),
                        0.30,
                        0.10,
                        Some(2),
                    )
                    .unwrap();
            })
            .unwrap();
        assert_eq!(guard.dirty_len(), 1);
        assert_eq!(guard.flush_dirty(&mut connection).unwrap(), 1);

        let restarted = PlanGuard::new();
        assert_eq!(restarted.warm_from_db_at(&connection, 2).unwrap(), 1);
        let entry = restarted.get(&plan_key).unwrap();
        assert_eq!(entry.runtime_version, runtime("v2"));
        assert_eq!(entry.retained_samples().len(), 1);
        assert!((entry.divergence_exposure - 0.20).abs() < 1e-12);
    }
}
