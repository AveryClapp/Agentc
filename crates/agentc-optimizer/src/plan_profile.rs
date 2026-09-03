//! Bounded empirical profiles for complete execution plans.
//!
//! A profile belongs to exactly one [`CallSiteVersion`] and
//! [`ExecutionPlanId`]. Routing-only, rewrite-only, and composed observations
//! therefore cannot leak evidence into one another. Every decision statistic
//! is recomputed from the exact retained window; lifetime counts are reporting
//! metadata only.

use std::collections::{HashMap, VecDeque};
use std::error::Error;
use std::fmt;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{Context, Result as AnyResult};
use dashmap::DashMap;
use parking_lot::RwLock;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{de::Error as _, Deserialize, Deserializer, Serialize};

use crate::cost_model::WelfordStats;
use crate::execution_plan::ExecutionPlanId;

pub const DEFAULT_PLAN_PROFILE_WINDOW: u32 = 50;
pub const CALL_SITE_VERSION_SCHEMA_VERSION: u16 = 1;

/// Content-free identity for one semantic call site under one request shape.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct CallSiteVersion(String);

impl CallSiteVersion {
    /// Hash the stable call-site and version signatures into one durable key.
    ///
    /// The supplied signatures must describe shapes or versions, never raw
    /// prompt content, tool arguments, credentials, or user data.
    pub fn from_spec(spec: &CallSiteVersionSpec) -> Result<Self, CallSiteVersionError> {
        spec.validate()?;
        #[derive(Serialize)]
        struct Identity<'a> {
            schema_version: u16,
            call_site_id: &'a str,
            prompt_shape_version: &'a str,
            provider_protocol: &'a str,
            tool_schema_version: Option<&'a str>,
            application_config_version: Option<&'a str>,
        }

        let identity = Identity {
            schema_version: CALL_SITE_VERSION_SCHEMA_VERSION,
            call_site_id: &spec.call_site_id,
            prompt_shape_version: &spec.prompt_shape_version,
            provider_protocol: &spec.provider_protocol,
            tool_schema_version: spec.tool_schema_version.as_deref(),
            application_config_version: spec.application_config_version.as_deref(),
        };
        let value = serde_json::to_value(identity)
            .map_err(|error| CallSiteVersionError::Serialization(error.to_string()))?;
        Ok(Self(content_hash(&canonical_json(&value))))
    }

    /// Parse a canonical lowercase SHA-256 digest from durable storage.
    pub fn parse(value: impl Into<String>) -> Result<Self, CallSiteVersionError> {
        let value = value.into();
        if !is_canonical_digest(&value) {
            return Err(CallSiteVersionError::InvalidDigest);
        }
        Ok(Self(value))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for CallSiteVersion {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl<'de> Deserialize<'de> for CallSiteVersion {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        Self::parse(value).map_err(D::Error::custom)
    }
}

fn is_canonical_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Version-bearing, content-free inputs used to identify a call site.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CallSiteVersionSpec {
    pub call_site_id: String,
    pub prompt_shape_version: String,
    pub provider_protocol: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_schema_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub application_config_version: Option<String>,
}

impl CallSiteVersionSpec {
    fn validate(&self) -> Result<(), CallSiteVersionError> {
        require_nonempty("call_site_id", &self.call_site_id)?;
        require_nonempty("prompt_shape_version", &self.prompt_shape_version)?;
        require_nonempty("provider_protocol", &self.provider_protocol)?;
        if let Some(value) = &self.tool_schema_version {
            require_nonempty("tool_schema_version", value)?;
        }
        if let Some(value) = &self.application_config_version {
            require_nonempty("application_config_version", value)?;
        }
        Ok(())
    }
}

fn require_nonempty(field: &'static str, value: &str) -> Result<(), CallSiteVersionError> {
    if value.trim().is_empty() {
        Err(CallSiteVersionError::EmptyField(field))
    } else {
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CallSiteVersionError {
    EmptyField(&'static str),
    InvalidDigest,
    Serialization(String),
}

impl fmt::Display for CallSiteVersionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyField(field) => {
                write!(formatter, "call-site version field {field} is empty")
            }
            Self::InvalidDigest => formatter
                .write_str("call-site version must be a 64-character lowercase SHA-256 digest"),
            Self::Serialization(message) => {
                write!(
                    formatter,
                    "call-site version serialization failed: {message}"
                )
            }
        }
    }
}

impl Error for CallSiteVersionError {}

/// Exact decision key for a plan profile.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct PlanProfileKey {
    pub call_site_version: CallSiteVersion,
    pub execution_plan_id: ExecutionPlanId,
}

/// Versions that determine whether retained observations are still current.
///
/// A change resets the retained window before the next observation. Price is
/// included because old billed-cost samples are not silently repriced.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanRuntimeVersion {
    pub provider_protocol: String,
    pub target_model_id: String,
    pub target_model_version: String,
    pub price_table_version: String,
}

impl PlanRuntimeVersion {
    fn validate(&self) -> Result<(), PlanProfileUpdateError> {
        for (field, value) in [
            ("provider_protocol", self.provider_protocol.as_str()),
            ("target_model_id", self.target_model_id.as_str()),
            ("target_model_version", self.target_model_version.as_str()),
            ("price_table_version", self.price_table_version.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(PlanProfileUpdateError::EmptyField(field));
            }
        }
        Ok(())
    }
}

/// One completed execution of exactly one plan.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanProfileUpdate {
    pub key: PlanProfileKey,
    pub runtime_version: PlanRuntimeVersion,
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub latency_ms: f64,
    pub cost_usd: f64,
    pub output_is_structured: bool,
    pub output_is_short: bool,
    /// Present only when this execution was paired with the reference plan.
    pub divergence: Option<f64>,
    pub dispatch_fallback: bool,
    /// `None` uses the current system clock; tests and replay should pin it.
    pub observed_at_us: Option<i64>,
}

/// Correlation token for a completed execution that may later receive a
/// reference-plan divergence measurement.
///
/// Provider adapters should treat the serialized form as opaque. The token
/// binds delayed feedback to the exact profile key, runtime version, and
/// lifetime execution sequence that produced the candidate output.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanObservationToken {
    key: PlanProfileKey,
    runtime_version: PlanRuntimeVersion,
    sequence: u64,
}

impl PlanObservationToken {
    pub fn key(&self) -> &PlanProfileKey {
        &self.key
    }

    pub fn runtime_version(&self) -> &PlanRuntimeVersion {
        &self.runtime_version
    }

    pub fn sequence(&self) -> u64 {
        self.sequence
    }
}

impl PlanProfileUpdate {
    fn validate(&self, observed_at_us: i64) -> Result<(), PlanProfileUpdateError> {
        self.runtime_version.validate()?;
        if !is_nonnegative_finite(self.latency_ms) {
            return Err(PlanProfileUpdateError::InvalidNumber("latency_ms"));
        }
        if !is_nonnegative_finite(self.cost_usd) {
            return Err(PlanProfileUpdateError::InvalidNumber("cost_usd"));
        }
        if self
            .divergence
            .is_some_and(|value| !value.is_finite() || !(0.0..=1.0).contains(&value))
        {
            return Err(PlanProfileUpdateError::InvalidDivergence);
        }
        if observed_at_us < 0 {
            return Err(PlanProfileUpdateError::NegativeObservationTime);
        }
        Ok(())
    }
}

fn is_nonnegative_finite(value: f64) -> bool {
    value.is_finite() && value >= 0.0
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PlanProfileUpdateError {
    EmptyField(&'static str),
    InvalidNumber(&'static str),
    InvalidDivergence,
    NegativeObservationTime,
    ObservationCountOverflow,
    ProfileNotFound,
    RuntimeVersionMismatch,
    InvalidObservationSequence,
    ConflictingDivergence,
}

impl fmt::Display for PlanProfileUpdateError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyField(field) => write!(formatter, "plan observation field {field} is empty"),
            Self::InvalidNumber(field) => {
                write!(
                    formatter,
                    "plan observation field {field} must be finite and non-negative"
                )
            }
            Self::InvalidDivergence => {
                formatter.write_str("plan observation divergence must be finite and in [0, 1]")
            }
            Self::NegativeObservationTime => {
                formatter.write_str("plan observation timestamp must be non-negative")
            }
            Self::ObservationCountOverflow => {
                formatter.write_str("plan observation count overflowed")
            }
            Self::ProfileNotFound => formatter.write_str("plan profile does not exist"),
            Self::RuntimeVersionMismatch => {
                formatter.write_str("plan observation runtime version is no longer current")
            }
            Self::InvalidObservationSequence => {
                formatter.write_str("plan observation sequence does not identify an execution")
            }
            Self::ConflictingDivergence => formatter
                .write_str("plan observation already has a different paired divergence value"),
        }
    }
}

impl Error for PlanProfileUpdateError {}

/// Exact retained sample. It contains metrics and version metadata, never raw
/// prompts or model outputs.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanProfileSample {
    pub sequence: u64,
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub latency_ms: f64,
    pub cost_usd: f64,
    pub output_is_structured: bool,
    pub output_is_short: bool,
    pub dispatch_fallback: bool,
    pub runtime_version: PlanRuntimeVersion,
    pub observed_at_us: i64,
}

/// One reference-paired comparison. Its window advances only when a paired
/// comparison completes, independently of the execution-outcome window.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanDivergenceSample {
    pub sequence: u64,
    pub plan_observation_sequence: u64,
    pub divergence: f64,
    pub runtime_version: PlanRuntimeVersion,
    pub observed_at_us: i64,
}

/// Bounded statistics for one exact call-site-version/plan pair.
#[derive(Debug, Clone)]
pub struct PlanProfile {
    pub key: PlanProfileKey,
    /// Lifetime count for reporting; never used as an admission evidence count.
    pub n_observations: u64,
    /// Lifetime paired count for reporting; admission uses `paired_observations`.
    pub n_paired_observations: u64,
    pub window_observations: u32,
    pub paired_observations: u32,
    pub input_tokens: WelfordStats,
    pub output_tokens: WelfordStats,
    pub latency_ms: WelfordStats,
    pub cost_usd: WelfordStats,
    pub output_token_p95: f64,
    pub output_token_p99: f64,
    pub output_is_structured: f64,
    pub output_is_short: f64,
    pub divergence_upper_p95: Option<f64>,
    pub dispatch_fallback_rate: f64,
    pub runtime_version: PlanRuntimeVersion,
    pub updated_at_us: i64,
    pub last_paired_at_us: Option<i64>,
    samples: Arc<VecDeque<PlanProfileSample>>,
    divergence_samples: Arc<VecDeque<PlanDivergenceSample>>,
    generation: u64,
}

impl PlanProfile {
    fn new(key: PlanProfileKey, runtime_version: PlanRuntimeVersion) -> Self {
        Self {
            key,
            n_observations: 0,
            n_paired_observations: 0,
            window_observations: 0,
            paired_observations: 0,
            input_tokens: WelfordStats::default(),
            output_tokens: WelfordStats::default(),
            latency_ms: WelfordStats::default(),
            cost_usd: WelfordStats::default(),
            output_token_p95: 0.0,
            output_token_p99: 0.0,
            output_is_structured: 0.0,
            output_is_short: 0.0,
            divergence_upper_p95: None,
            dispatch_fallback_rate: 0.0,
            runtime_version,
            updated_at_us: 0,
            last_paired_at_us: None,
            samples: Arc::new(VecDeque::new()),
            divergence_samples: Arc::new(VecDeque::new()),
            generation: 0,
        }
    }

    pub fn retained_samples(&self) -> &VecDeque<PlanProfileSample> {
        &self.samples
    }

    pub fn retained_divergence_samples(&self) -> &VecDeque<PlanDivergenceSample> {
        &self.divergence_samples
    }

    pub fn is_current(&self, runtime_version: &PlanRuntimeVersion) -> bool {
        self.runtime_version == *runtime_version
    }
}

/// Concurrent in-memory profile store with exact-window SQLite persistence.
pub struct PlanProfiles {
    map: Arc<DashMap<PlanProfileKey, PlanProfile>>,
    dirty: Arc<RwLock<HashMap<PlanProfileKey, u64>>>,
    window_size: usize,
}

impl Default for PlanProfiles {
    fn default() -> Self {
        Self::new()
    }
}

impl PlanProfiles {
    pub fn new() -> Self {
        Self::with_window(DEFAULT_PLAN_PROFILE_WINDOW)
    }

    /// Retain the newest `window_size` execution outcomes and, independently,
    /// the newest `window_size` paired divergence observations for each key.
    /// Zero is coerced to one so bad configuration cannot erase all evidence.
    pub fn with_window(window_size: u32) -> Self {
        Self {
            map: Arc::new(DashMap::new()),
            dirty: Arc::new(RwLock::new(HashMap::new())),
            window_size: window_size.max(1) as usize,
        }
    }

    /// Return a decision-safe snapshot only when all runtime versions match.
    pub fn get(
        &self,
        key: &PlanProfileKey,
        runtime_version: &PlanRuntimeVersion,
    ) -> Option<PlanProfile> {
        self.map
            .get(key)
            .and_then(|profile| profile.is_current(runtime_version).then(|| profile.clone()))
    }

    /// Return a snapshot without a version check for reporting and diagnostics.
    pub fn get_for_reporting(&self, key: &PlanProfileKey) -> Option<PlanProfile> {
        self.map.get(key).map(|profile| profile.clone())
    }

    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    pub fn dirty_len(&self) -> usize {
        self.dirty.read().len()
    }

    /// Record one completed plan execution and return its correlation token.
    pub fn observe(
        &self,
        update: PlanProfileUpdate,
    ) -> Result<PlanObservationToken, PlanProfileUpdateError> {
        let observed_at_us = update.observed_at_us.unwrap_or_else(now_micros);
        update.validate(observed_at_us)?;
        let key = update.key.clone();
        let (generation, sequence) = {
            let mut profile = self
                .map
                .entry(key.clone())
                .or_insert_with(|| PlanProfile::new(key.clone(), update.runtime_version.clone()));
            if profile.runtime_version != update.runtime_version {
                Arc::make_mut(&mut profile.samples).clear();
                Arc::make_mut(&mut profile.divergence_samples).clear();
                profile.runtime_version = update.runtime_version.clone();
                recompute_window(&mut profile);
            }
            let sequence = apply_update(&mut profile, &update, observed_at_us, self.window_size)?;
            (profile.generation, sequence)
        };
        self.dirty.write().insert(key, generation);
        Ok(PlanObservationToken {
            key: update.key,
            runtime_version: update.runtime_version,
            sequence,
        })
    }

    /// Attach an asynchronously completed reference comparison to one plan
    /// execution without double-counting its cost or latency observation.
    pub fn record_divergence(
        &self,
        observation: &PlanObservationToken,
        divergence: f64,
        observed_at_us: Option<i64>,
    ) -> Result<(), PlanProfileUpdateError> {
        if !divergence.is_finite() || !(0.0..=1.0).contains(&divergence) {
            return Err(PlanProfileUpdateError::InvalidDivergence);
        }
        let observed_at_us = observed_at_us.unwrap_or_else(now_micros);
        if observed_at_us < 0 {
            return Err(PlanProfileUpdateError::NegativeObservationTime);
        }
        observation.runtime_version.validate()?;

        let generation = {
            let Some(mut profile) = self.map.get_mut(&observation.key) else {
                return Err(PlanProfileUpdateError::ProfileNotFound);
            };
            if profile.runtime_version != observation.runtime_version {
                return Err(PlanProfileUpdateError::RuntimeVersionMismatch);
            }
            if observation.sequence == 0 || observation.sequence > profile.n_observations {
                return Err(PlanProfileUpdateError::InvalidObservationSequence);
            }
            if let Some(existing) = profile
                .divergence_samples
                .iter()
                .find(|sample| sample.plan_observation_sequence == observation.sequence)
            {
                if existing.divergence == divergence {
                    return Ok(());
                }
                return Err(PlanProfileUpdateError::ConflictingDivergence);
            }
            append_divergence(
                &mut profile,
                observation.sequence,
                divergence,
                observed_at_us,
                self.window_size,
            )?;
            profile.generation = profile.generation.saturating_add(1);
            profile.generation
        };
        self.dirty
            .write()
            .insert(observation.key.clone(), generation);
        Ok(())
    }

    /// Hydrate all plan profiles from their exact retained observations.
    pub fn warm_from_db(&self, conn: &Connection) -> AnyResult<usize> {
        let persisted = load_persisted_summaries(conn)?;
        let mut count = 0usize;

        for summary in persisted {
            let key = PlanProfileKey {
                call_site_version: CallSiteVersion::parse(summary.call_site_version.clone())
                    .with_context(|| {
                        format!("decode call-site version {}", summary.call_site_version)
                    })?,
                execution_plan_id: ExecutionPlanId::parse(summary.execution_plan_id.clone())
                    .with_context(|| {
                        format!("decode execution plan ID {}", summary.execution_plan_id)
                    })?,
            };
            let persisted_runtime = summary.runtime_version()?;
            let mut samples = load_samples(conn, &key)?;
            let mut divergence_samples = load_divergence_samples(conn, &key)?;
            let mut normalized = false;

            let outcome_count = samples.len();
            samples.retain(|sample| sample.runtime_version == persisted_runtime);
            normalized |= samples.len() != outcome_count;
            let divergence_count = divergence_samples.len();
            divergence_samples.retain(|sample| sample.runtime_version == persisted_runtime);
            normalized |= divergence_samples.len() != divergence_count;
            while samples.len() > self.window_size {
                samples.pop_front();
                normalized = true;
            }
            while divergence_samples.len() > self.window_size {
                divergence_samples.pop_front();
                normalized = true;
            }

            let max_sequence = samples.back().map(|sample| sample.sequence).unwrap_or(0);
            let n_observations = summary.n_observations.max(max_sequence);
            if n_observations != summary.n_observations {
                normalized = true;
            }
            let max_paired_sequence = divergence_samples
                .back()
                .map(|sample| sample.sequence)
                .unwrap_or(0);
            let n_paired_observations = summary.n_paired_observations.max(max_paired_sequence);
            if n_paired_observations != summary.n_paired_observations {
                normalized = true;
            }

            let mut profile = PlanProfile::new(key.clone(), persisted_runtime);
            profile.n_observations = n_observations;
            profile.n_paired_observations = n_paired_observations;
            profile.samples = Arc::new(samples);
            profile.divergence_samples = Arc::new(divergence_samples);
            recompute_window(&mut profile);

            let summary_mismatch = profile.window_observations != summary.window_observations
                || profile.paired_observations != summary.paired_observations
                || profile.updated_at_us != summary.updated_at_us
                || profile.last_paired_at_us != summary.last_paired_at_us;
            if normalized || summary_mismatch {
                profile.generation = 1;
                self.dirty.write().insert(key.clone(), profile.generation);
            }
            self.map.insert(key, profile);
            count += 1;
        }
        Ok(count)
    }

    /// Atomically persist every dirty summary with its exact retained window.
    pub fn flush_dirty(&self, conn: &mut Connection) -> AnyResult<usize> {
        self.flush_dirty_with_hook(conn, || {})
    }

    fn flush_dirty_with_hook<F>(&self, conn: &mut Connection, before_clear: F) -> AnyResult<usize>
    where
        F: FnOnce(),
    {
        let dirty_generations: Vec<(PlanProfileKey, u64)> = {
            let guard = self.dirty.read();
            guard
                .iter()
                .map(|(key, generation)| (key.clone(), *generation))
                .collect()
        };
        if dirty_generations.is_empty() {
            return Ok(0);
        }
        let snapshots: Vec<(PlanProfileKey, u64, PlanProfile)> = dirty_generations
            .into_iter()
            .filter_map(|(key, generation)| {
                self.map
                    .get(&key)
                    .map(|profile| (key, generation, profile.clone()))
            })
            .collect();

        let transaction = conn.transaction().context("begin plan-profile flush")?;
        for (key, _, profile) in &snapshots {
            persist_profile(&transaction, profile).with_context(|| {
                format!(
                    "persist plan profile {}/{}",
                    key.call_site_version, key.execution_plan_id
                )
            })?;
        }
        transaction.commit().context("commit plan-profile flush")?;

        before_clear();
        let mut guard = self.dirty.write();
        for (key, generation, _) in &snapshots {
            if guard.get(key).copied() == Some(*generation) {
                guard.remove(key);
            }
        }
        Ok(snapshots.len())
    }
}

fn now_micros() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_micros() as i64)
        .unwrap_or(0)
}

fn apply_update(
    profile: &mut PlanProfile,
    update: &PlanProfileUpdate,
    observed_at_us: i64,
    window_size: usize,
) -> Result<u64, PlanProfileUpdateError> {
    profile.n_observations = profile
        .n_observations
        .checked_add(1)
        .ok_or(PlanProfileUpdateError::ObservationCountOverflow)?;
    let sequence = profile.n_observations;
    Arc::make_mut(&mut profile.samples).push_back(PlanProfileSample {
        sequence,
        input_tokens: update.input_tokens,
        output_tokens: update.output_tokens,
        latency_ms: update.latency_ms,
        cost_usd: update.cost_usd,
        output_is_structured: update.output_is_structured,
        output_is_short: update.output_is_short,
        dispatch_fallback: update.dispatch_fallback,
        runtime_version: update.runtime_version.clone(),
        observed_at_us,
    });
    while profile.samples.len() > window_size {
        Arc::make_mut(&mut profile.samples).pop_front();
    }
    if let Some(divergence) = update.divergence {
        append_divergence(profile, sequence, divergence, observed_at_us, window_size)?;
    }
    recompute_window(profile);
    profile.generation = profile.generation.saturating_add(1);
    Ok(sequence)
}

fn append_divergence(
    profile: &mut PlanProfile,
    plan_observation_sequence: u64,
    divergence: f64,
    observed_at_us: i64,
    window_size: usize,
) -> Result<(), PlanProfileUpdateError> {
    profile.n_paired_observations = profile
        .n_paired_observations
        .checked_add(1)
        .ok_or(PlanProfileUpdateError::ObservationCountOverflow)?;
    Arc::make_mut(&mut profile.divergence_samples).push_back(PlanDivergenceSample {
        sequence: profile.n_paired_observations,
        plan_observation_sequence,
        divergence,
        runtime_version: profile.runtime_version.clone(),
        observed_at_us,
    });
    while profile.divergence_samples.len() > window_size {
        Arc::make_mut(&mut profile.divergence_samples).pop_front();
    }
    recompute_divergence(profile);
    Ok(())
}

fn recompute_window(profile: &mut PlanProfile) {
    profile.window_observations = profile.samples.len() as u32;
    profile.input_tokens = WelfordStats::default();
    profile.output_tokens = WelfordStats::default();
    profile.latency_ms = WelfordStats::default();
    profile.cost_usd = WelfordStats::default();

    let mut output_tokens = Vec::with_capacity(profile.samples.len());
    let mut structured = 0usize;
    let mut short = 0usize;
    let mut fallbacks = 0usize;
    let mut updated_at_us = 0i64;
    for sample in profile.samples.iter() {
        profile.input_tokens.update(sample.input_tokens as f64);
        profile.output_tokens.update(sample.output_tokens as f64);
        profile.latency_ms.update(sample.latency_ms);
        profile.cost_usd.update(sample.cost_usd);
        output_tokens.push(sample.output_tokens);
        structured += usize::from(sample.output_is_structured);
        short += usize::from(sample.output_is_short);
        fallbacks += usize::from(sample.dispatch_fallback);
        updated_at_us = updated_at_us.max(sample.observed_at_us);
    }

    profile.updated_at_us = updated_at_us;
    if profile.samples.is_empty() {
        profile.output_token_p95 = 0.0;
        profile.output_token_p99 = 0.0;
        profile.output_is_structured = 0.0;
        profile.output_is_short = 0.0;
        profile.dispatch_fallback_rate = 0.0;
        recompute_divergence(profile);
        return;
    }

    output_tokens.sort_unstable();
    profile.output_token_p95 = nearest_rank(&output_tokens, 95) as f64;
    profile.output_token_p99 = nearest_rank(&output_tokens, 99) as f64;
    let denominator = profile.samples.len() as f64;
    profile.output_is_structured = structured as f64 / denominator;
    profile.output_is_short = short as f64 / denominator;
    profile.dispatch_fallback_rate = fallbacks as f64 / denominator;
    recompute_divergence(profile);
}

fn recompute_divergence(profile: &mut PlanProfile) {
    profile.paired_observations = profile.divergence_samples.len() as u32;
    let mut divergences: Vec<f64> = profile
        .divergence_samples
        .iter()
        .map(|sample| sample.divergence)
        .collect();
    profile.divergence_upper_p95 = conformal_upper_quantile(&mut divergences, 0.95);
    profile.last_paired_at_us = profile
        .divergence_samples
        .iter()
        .map(|sample| sample.observed_at_us)
        .max();
}

fn nearest_rank(sorted: &[u32], percentile: usize) -> u32 {
    debug_assert!(!sorted.is_empty());
    let rank = (percentile * sorted.len()).div_ceil(100);
    sorted[rank.saturating_sub(1).min(sorted.len() - 1)]
}

fn conformal_upper_quantile(values: &mut [f64], coverage: f64) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    values.sort_by(f64::total_cmp);
    let rank = (coverage * (values.len() + 1) as f64).ceil() as usize;
    Some(values[rank.clamp(1, values.len()) - 1])
}

#[derive(Debug)]
struct PersistedSummary {
    call_site_version: String,
    execution_plan_id: String,
    n_observations: u64,
    n_paired_observations: u64,
    window_observations: u32,
    paired_observations: u32,
    provider_protocol: String,
    target_model_id: String,
    target_model_version: String,
    price_table_version: String,
    updated_at_us: i64,
    last_paired_at_us: Option<i64>,
}

impl PersistedSummary {
    fn runtime_version(&self) -> AnyResult<PlanRuntimeVersion> {
        let version = PlanRuntimeVersion {
            provider_protocol: self.provider_protocol.clone(),
            target_model_id: self.target_model_id.clone(),
            target_model_version: self.target_model_version.clone(),
            price_table_version: self.price_table_version.clone(),
        };
        version
            .validate()
            .context("validate persisted plan runtime version")?;
        Ok(version)
    }
}

fn load_persisted_summaries(conn: &Connection) -> AnyResult<Vec<PersistedSummary>> {
    let mut statement = conn
        .prepare(
            "SELECT call_site_version, execution_plan_id, n_observations, \
                    n_paired_observations, window_observations, paired_observations, \
                    provider_protocol, target_model_id, target_model_version, \
                    price_table_version, updated_at, last_paired_at \
             FROM execution_plan_profile",
        )
        .context("prepare plan-profile warmup")?;
    let raw = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, i64>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, String>(7)?,
                row.get::<_, String>(8)?,
                row.get::<_, String>(9)?,
                row.get::<_, i64>(10)?,
                row.get::<_, Option<i64>>(11)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    raw.into_iter()
        .map(
            |(
                call_site_version,
                execution_plan_id,
                n_observations,
                n_paired_observations,
                window_observations,
                paired_observations,
                provider_protocol,
                target_model_id,
                target_model_version,
                price_table_version,
                updated_at_us,
                last_paired_at_us,
            )| {
                Ok(PersistedSummary {
                    call_site_version,
                    execution_plan_id,
                    n_observations: u64::try_from(n_observations)
                        .context("negative persisted plan observation count")?,
                    n_paired_observations: u64::try_from(n_paired_observations)
                        .context("negative persisted paired observation count")?,
                    window_observations: u32::try_from(window_observations)
                        .context("invalid persisted plan window count")?,
                    paired_observations: u32::try_from(paired_observations)
                        .context("invalid persisted paired observation count")?,
                    provider_protocol,
                    target_model_id,
                    target_model_version,
                    price_table_version,
                    updated_at_us,
                    last_paired_at_us,
                })
            },
        )
        .collect()
}

#[derive(Debug)]
struct RawSample {
    sequence: i64,
    input_tokens: i64,
    output_tokens: i64,
    latency_ms: f64,
    cost_usd: f64,
    output_is_structured: i64,
    output_is_short: i64,
    dispatch_fallback: i64,
    provider_protocol: String,
    target_model_id: String,
    target_model_version: String,
    price_table_version: String,
    observed_at_us: i64,
}

fn load_samples(conn: &Connection, key: &PlanProfileKey) -> AnyResult<VecDeque<PlanProfileSample>> {
    let mut statement = conn
        .prepare(
            "SELECT sample_sequence, input_tokens, output_tokens, latency_ms, cost_usd, \
                    output_is_structured, output_is_short, dispatch_fallback, \
                    provider_protocol, target_model_id, target_model_version, \
                    price_table_version, observed_at \
             FROM execution_plan_observation \
             WHERE call_site_version = ?1 AND execution_plan_id = ?2 \
             ORDER BY sample_sequence",
        )
        .context("prepare retained plan-observation load")?;
    let raw = statement
        .query_map(
            params![
                key.call_site_version.as_str(),
                key.execution_plan_id.as_str()
            ],
            |row| {
                Ok(RawSample {
                    sequence: row.get(0)?,
                    input_tokens: row.get(1)?,
                    output_tokens: row.get(2)?,
                    latency_ms: row.get(3)?,
                    cost_usd: row.get(4)?,
                    output_is_structured: row.get(5)?,
                    output_is_short: row.get(6)?,
                    dispatch_fallback: row.get(7)?,
                    provider_protocol: row.get(8)?,
                    target_model_id: row.get(9)?,
                    target_model_version: row.get(10)?,
                    price_table_version: row.get(11)?,
                    observed_at_us: row.get(12)?,
                })
            },
        )?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    raw.into_iter()
        .map(|sample| {
            let runtime_version = PlanRuntimeVersion {
                provider_protocol: sample.provider_protocol,
                target_model_id: sample.target_model_id,
                target_model_version: sample.target_model_version,
                price_table_version: sample.price_table_version,
            };
            runtime_version
                .validate()
                .context("validate retained plan-observation version")?;
            let converted = PlanProfileSample {
                sequence: u64::try_from(sample.sequence)
                    .context("negative retained plan-observation sequence")?,
                input_tokens: u32::try_from(sample.input_tokens)
                    .context("invalid retained input token count")?,
                output_tokens: u32::try_from(sample.output_tokens)
                    .context("invalid retained output token count")?,
                latency_ms: sample.latency_ms,
                cost_usd: sample.cost_usd,
                output_is_structured: sample.output_is_structured != 0,
                output_is_short: sample.output_is_short != 0,
                dispatch_fallback: sample.dispatch_fallback != 0,
                runtime_version,
                observed_at_us: sample.observed_at_us,
            };
            validate_loaded_sample(&converted)?;
            Ok(converted)
        })
        .collect()
}

#[derive(Debug)]
struct RawDivergenceSample {
    sequence: i64,
    plan_observation_sequence: i64,
    divergence: f64,
    provider_protocol: String,
    target_model_id: String,
    target_model_version: String,
    price_table_version: String,
    observed_at_us: i64,
}

fn load_divergence_samples(
    conn: &Connection,
    key: &PlanProfileKey,
) -> AnyResult<VecDeque<PlanDivergenceSample>> {
    let mut statement = conn
        .prepare(
            "SELECT sample_sequence, plan_observation_sequence, divergence, \
                    provider_protocol, target_model_id, target_model_version, \
                    price_table_version, observed_at \
             FROM execution_plan_divergence_observation \
             WHERE call_site_version = ?1 AND execution_plan_id = ?2 \
             ORDER BY sample_sequence",
        )
        .context("prepare retained plan-divergence load")?;
    let raw = statement
        .query_map(
            params![
                key.call_site_version.as_str(),
                key.execution_plan_id.as_str()
            ],
            |row| {
                Ok(RawDivergenceSample {
                    sequence: row.get(0)?,
                    plan_observation_sequence: row.get(1)?,
                    divergence: row.get(2)?,
                    provider_protocol: row.get(3)?,
                    target_model_id: row.get(4)?,
                    target_model_version: row.get(5)?,
                    price_table_version: row.get(6)?,
                    observed_at_us: row.get(7)?,
                })
            },
        )?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    raw.into_iter()
        .map(|sample| {
            let runtime_version = PlanRuntimeVersion {
                provider_protocol: sample.provider_protocol,
                target_model_id: sample.target_model_id,
                target_model_version: sample.target_model_version,
                price_table_version: sample.price_table_version,
            };
            runtime_version
                .validate()
                .context("validate retained plan-divergence version")?;
            if sample.sequence <= 0
                || sample.plan_observation_sequence <= 0
                || !sample.divergence.is_finite()
                || !(0.0..=1.0).contains(&sample.divergence)
                || sample.observed_at_us < 0
            {
                anyhow::bail!("retained plan-divergence sample is invalid");
            }
            Ok(PlanDivergenceSample {
                sequence: u64::try_from(sample.sequence)
                    .context("negative retained plan-divergence sequence")?,
                plan_observation_sequence: u64::try_from(sample.plan_observation_sequence)
                    .context("negative paired plan-observation sequence")?,
                divergence: sample.divergence,
                runtime_version,
                observed_at_us: sample.observed_at_us,
            })
        })
        .collect()
}

fn validate_loaded_sample(sample: &PlanProfileSample) -> AnyResult<()> {
    if sample.sequence == 0 {
        anyhow::bail!("retained plan-observation sequence must be positive");
    }
    if !is_nonnegative_finite(sample.latency_ms) || !is_nonnegative_finite(sample.cost_usd) {
        anyhow::bail!("retained plan-observation metrics must be finite and non-negative");
    }
    if sample.observed_at_us < 0 {
        anyhow::bail!("retained plan-observation timestamp is negative");
    }
    Ok(())
}

fn persist_profile(conn: &Connection, profile: &PlanProfile) -> AnyResult<()> {
    conn.execute(
        "DELETE FROM execution_plan_observation \
         WHERE call_site_version = ?1 AND execution_plan_id = ?2",
        params![
            profile.key.call_site_version.as_str(),
            profile.key.execution_plan_id.as_str()
        ],
    )?;
    conn.execute(
        "DELETE FROM execution_plan_divergence_observation \
         WHERE call_site_version = ?1 AND execution_plan_id = ?2",
        params![
            profile.key.call_site_version.as_str(),
            profile.key.execution_plan_id.as_str()
        ],
    )?;
    {
        let mut statement = conn.prepare(
            "INSERT INTO execution_plan_observation (\
                call_site_version, execution_plan_id, sample_sequence, input_tokens, \
                output_tokens, latency_ms, cost_usd, output_is_structured, output_is_short, \
                dispatch_fallback, provider_protocol, target_model_id, \
                target_model_version, price_table_version, observed_at\
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
        )?;
        for sample in profile.samples.iter() {
            statement.execute(params![
                profile.key.call_site_version.as_str(),
                profile.key.execution_plan_id.as_str(),
                i64::try_from(sample.sequence).context("plan sample sequence exceeds SQLite")?,
                i64::from(sample.input_tokens),
                i64::from(sample.output_tokens),
                sample.latency_ms,
                sample.cost_usd,
                i64::from(sample.output_is_structured),
                i64::from(sample.output_is_short),
                i64::from(sample.dispatch_fallback),
                sample.runtime_version.provider_protocol,
                sample.runtime_version.target_model_id,
                sample.runtime_version.target_model_version,
                sample.runtime_version.price_table_version,
                sample.observed_at_us,
            ])?;
        }
    }
    {
        let mut statement = conn.prepare(
            "INSERT INTO execution_plan_divergence_observation (\
                call_site_version, execution_plan_id, sample_sequence, \
                plan_observation_sequence, divergence, provider_protocol, target_model_id, \
                target_model_version, price_table_version, observed_at\
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        )?;
        for sample in profile.divergence_samples.iter() {
            statement.execute(params![
                profile.key.call_site_version.as_str(),
                profile.key.execution_plan_id.as_str(),
                i64::try_from(sample.sequence)
                    .context("plan divergence sequence exceeds SQLite")?,
                i64::try_from(sample.plan_observation_sequence)
                    .context("paired plan-observation sequence exceeds SQLite")?,
                sample.divergence,
                sample.runtime_version.provider_protocol,
                sample.runtime_version.target_model_id,
                sample.runtime_version.target_model_version,
                sample.runtime_version.price_table_version,
                sample.observed_at_us,
            ])?;
        }
    }

    conn.execute(
        "INSERT INTO execution_plan_profile (\
            call_site_version, execution_plan_id, n_observations, n_paired_observations, \
            window_observations, paired_observations, input_tokens_mean, input_tokens_var, \
            output_tokens_mean, output_tokens_var, latency_ms_mean, latency_ms_var, \
            cost_usd_mean, cost_usd_var, output_token_p95, output_token_p99, \
            output_is_structured, output_is_short, divergence_upper_p95, \
            dispatch_fallback_rate, provider_protocol, target_model_id, target_model_version, \
            price_table_version, updated_at, last_paired_at\
         ) VALUES (\
            ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, \
            ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23, ?24, \
            ?25, ?26\
         ) ON CONFLICT(call_site_version, execution_plan_id) DO UPDATE SET \
            n_observations = excluded.n_observations, \
            n_paired_observations = excluded.n_paired_observations, \
            window_observations = excluded.window_observations, \
            paired_observations = excluded.paired_observations, \
            input_tokens_mean = excluded.input_tokens_mean, \
            input_tokens_var = excluded.input_tokens_var, \
            output_tokens_mean = excluded.output_tokens_mean, \
            output_tokens_var = excluded.output_tokens_var, \
            latency_ms_mean = excluded.latency_ms_mean, \
            latency_ms_var = excluded.latency_ms_var, \
            cost_usd_mean = excluded.cost_usd_mean, \
            cost_usd_var = excluded.cost_usd_var, \
            output_token_p95 = excluded.output_token_p95, \
            output_token_p99 = excluded.output_token_p99, \
            output_is_structured = excluded.output_is_structured, \
            output_is_short = excluded.output_is_short, \
            divergence_upper_p95 = excluded.divergence_upper_p95, \
            dispatch_fallback_rate = excluded.dispatch_fallback_rate, \
            provider_protocol = excluded.provider_protocol, \
            target_model_id = excluded.target_model_id, \
            target_model_version = excluded.target_model_version, \
            price_table_version = excluded.price_table_version, \
            updated_at = excluded.updated_at, \
            last_paired_at = excluded.last_paired_at",
        params![
            profile.key.call_site_version.as_str(),
            profile.key.execution_plan_id.as_str(),
            i64::try_from(profile.n_observations)
                .context("plan observation count exceeds SQLite")?,
            i64::try_from(profile.n_paired_observations)
                .context("paired plan observation count exceeds SQLite")?,
            i64::from(profile.window_observations),
            i64::from(profile.paired_observations),
            profile.input_tokens.mean,
            profile.input_tokens.variance(),
            profile.output_tokens.mean,
            profile.output_tokens.variance(),
            profile.latency_ms.mean,
            profile.latency_ms.variance(),
            profile.cost_usd.mean,
            profile.cost_usd.variance(),
            profile.output_token_p95,
            profile.output_token_p99,
            profile.output_is_structured,
            profile.output_is_short,
            profile.divergence_upper_p95,
            profile.dispatch_fallback_rate,
            profile.runtime_version.provider_protocol,
            profile.runtime_version.target_model_id,
            profile.runtime_version.target_model_version,
            profile.runtime_version.price_table_version,
            profile.updated_at_us,
            profile.last_paired_at_us,
        ],
    )?;
    Ok(())
}

/// Load one persisted profile directly for diagnostic consumers.
pub fn load_plan_profile(
    conn: &Connection,
    key: &PlanProfileKey,
) -> AnyResult<Option<PlanProfile>> {
    let summary = conn
        .query_row(
            "SELECT call_site_version, execution_plan_id, n_observations, \
                    n_paired_observations, window_observations, paired_observations, \
                    provider_protocol, target_model_id, target_model_version, \
                    price_table_version, updated_at, last_paired_at \
             FROM execution_plan_profile \
             WHERE call_site_version = ?1 AND execution_plan_id = ?2",
            params![
                key.call_site_version.as_str(),
                key.execution_plan_id.as_str()
            ],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, i64>(4)?,
                    row.get::<_, i64>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, String>(7)?,
                    row.get::<_, String>(8)?,
                    row.get::<_, String>(9)?,
                    row.get::<_, i64>(10)?,
                    row.get::<_, Option<i64>>(11)?,
                ))
            },
        )
        .optional()?;
    let Some((
        site,
        plan,
        n,
        n_paired,
        window,
        paired,
        provider,
        target,
        model_version,
        price,
        updated,
        last_paired,
    )) = summary
    else {
        return Ok(None);
    };
    let persisted = PersistedSummary {
        call_site_version: site,
        execution_plan_id: plan,
        n_observations: u64::try_from(n).context("negative plan observation count")?,
        n_paired_observations: u64::try_from(n_paired)
            .context("negative paired plan observation count")?,
        window_observations: u32::try_from(window).context("invalid plan window count")?,
        paired_observations: u32::try_from(paired).context("invalid paired count")?,
        provider_protocol: provider,
        target_model_id: target,
        target_model_version: model_version,
        price_table_version: price,
        updated_at_us: updated,
        last_paired_at_us: last_paired,
    };
    let mut profile = PlanProfile::new(key.clone(), persisted.runtime_version()?);
    profile.n_observations = persisted.n_observations;
    profile.n_paired_observations = persisted.n_paired_observations;
    profile.samples = Arc::new(load_samples(conn, key)?);
    profile.divergence_samples = Arc::new(load_divergence_samples(conn, key)?);
    recompute_window(&mut profile);
    Ok(Some(profile))
}

#[cfg(test)]
mod tests {
    use std::thread;

    use super::*;
    use crate::schema::ensure_cost_model_schema;

    fn fresh_conn() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        connection
    }

    fn site(prompt_shape: &str) -> CallSiteVersion {
        CallSiteVersion::from_spec(&CallSiteVersionSpec {
            call_site_id: "app.agent:step".to_string(),
            prompt_shape_version: prompt_shape.to_string(),
            provider_protocol: "openai.responses.v1".to_string(),
            tool_schema_version: Some("tools-v1".to_string()),
            application_config_version: Some("config-v1".to_string()),
        })
        .unwrap()
    }

    fn plan(number: u8) -> ExecutionPlanId {
        ExecutionPlanId::parse(format!("{number:064x}")).unwrap()
    }

    fn key(prompt_shape: &str, plan_number: u8) -> PlanProfileKey {
        PlanProfileKey {
            call_site_version: site(prompt_shape),
            execution_plan_id: plan(plan_number),
        }
    }

    fn runtime(model_version: &str) -> PlanRuntimeVersion {
        PlanRuntimeVersion {
            provider_protocol: "openai.responses.v1".to_string(),
            target_model_id: "gpt-cheap".to_string(),
            target_model_version: model_version.to_string(),
            price_table_version: "prices-2026-09".to_string(),
        }
    }

    fn update(
        key: PlanProfileKey,
        output_tokens: u32,
        divergence: Option<f64>,
        observed_at_us: i64,
    ) -> PlanProfileUpdate {
        PlanProfileUpdate {
            key,
            runtime_version: runtime("model-v1"),
            input_tokens: output_tokens * 2,
            output_tokens,
            latency_ms: output_tokens as f64 * 10.0,
            cost_usd: output_tokens as f64 / 10_000.0,
            output_is_structured: output_tokens.is_multiple_of(2),
            output_is_short: output_tokens <= 128,
            divergence,
            dispatch_fallback: false,
            observed_at_us: Some(observed_at_us),
        }
    }

    #[test]
    fn call_site_version_is_stable_and_all_signatures_are_identity_bearing() {
        let base = CallSiteVersionSpec {
            call_site_id: "site".to_string(),
            prompt_shape_version: "prompt-v1".to_string(),
            provider_protocol: "openai.responses.v1".to_string(),
            tool_schema_version: None,
            application_config_version: None,
        };
        assert_eq!(
            CallSiteVersion::from_spec(&base).unwrap(),
            CallSiteVersion::from_spec(&base).unwrap()
        );
        for changed in [
            CallSiteVersionSpec {
                prompt_shape_version: "prompt-v2".to_string(),
                ..base.clone()
            },
            CallSiteVersionSpec {
                provider_protocol: "anthropic.messages.v1".to_string(),
                ..base.clone()
            },
            CallSiteVersionSpec {
                tool_schema_version: Some("tools-v1".to_string()),
                ..base.clone()
            },
            CallSiteVersionSpec {
                application_config_version: Some("config-v1".to_string()),
                ..base.clone()
            },
        ] {
            assert_ne!(
                CallSiteVersion::from_spec(&base).unwrap(),
                CallSiteVersion::from_spec(&changed).unwrap()
            );
        }
    }

    #[test]
    fn call_site_version_rejects_empty_fields_and_noncanonical_storage_keys() {
        let mut invalid = CallSiteVersionSpec {
            call_site_id: "site".to_string(),
            prompt_shape_version: "shape".to_string(),
            provider_protocol: " ".to_string(),
            tool_schema_version: None,
            application_config_version: None,
        };
        assert_eq!(
            CallSiteVersion::from_spec(&invalid),
            Err(CallSiteVersionError::EmptyField("provider_protocol"))
        );
        invalid.provider_protocol = "openai".to_string();
        assert!(CallSiteVersion::from_spec(&invalid).is_ok());
        assert_eq!(
            CallSiteVersion::parse("A".repeat(64)),
            Err(CallSiteVersionError::InvalidDigest)
        );
        assert!(serde_json::from_str::<CallSiteVersion>("\"short\"").is_err());
    }

    #[test]
    fn complete_plan_evidence_is_not_synthesized() {
        let profiles = PlanProfiles::new();
        // Reference, routing-only, rewrite-only, and joint plans.
        for (plan_number, observations) in [(1, 2), (2, 3), (3, 4), (4, 1)] {
            for observed in 0..observations {
                profiles
                    .observe(update(
                        key("shape-v1", plan_number),
                        10 + observed,
                        Some(0.01),
                        i64::from(observed),
                    ))
                    .unwrap();
            }
        }
        assert_eq!(profiles.len(), 4);
        assert_eq!(
            profiles
                .get(&key("shape-v1", 4), &runtime("model-v1"))
                .unwrap()
                .paired_observations,
            1
        );
        assert!(profiles
            .get(&key("shape-v1", 5), &runtime("model-v1"))
            .is_none());
    }

    #[test]
    fn every_statistic_uses_only_the_bounded_window() {
        let profiles = PlanProfiles::with_window(3);
        let profile_key = key("shape-v1", 1);
        profiles
            .observe(update(profile_key.clone(), 1_000, Some(0.9), 1))
            .unwrap();
        for (tokens, divergence, now) in [(10, None, 2), (20, Some(0.1), 3), (30, Some(0.2), 4)] {
            profiles
                .observe(update(profile_key.clone(), tokens, divergence, now))
                .unwrap();
        }
        let profile = profiles.get(&profile_key, &runtime("model-v1")).unwrap();
        assert_eq!(profile.n_observations, 4);
        assert_eq!(profile.window_observations, 3);
        assert_eq!(profile.paired_observations, 3);
        assert_eq!(profile.input_tokens.mean, 40.0);
        assert_eq!(profile.output_tokens.mean, 20.0);
        assert_eq!(profile.latency_ms.mean, 200.0);
        assert!((profile.cost_usd.mean - 0.002).abs() < 1e-12);
        assert_eq!(profile.output_token_p95, 30.0);
        assert_eq!(profile.output_token_p99, 30.0);
        assert_eq!(profile.divergence_upper_p95, Some(0.9));
        assert_eq!(profile.updated_at_us, 4);
    }

    #[test]
    fn paired_window_remains_usable_at_two_percent_sampling() {
        let profiles = PlanProfiles::with_window(50);
        let profile_key = key("shape-v1", 1);
        for observation in 1..=1_000 {
            let observation_token = profiles
                .observe(update(
                    profile_key.clone(),
                    100,
                    None,
                    i64::from(observation),
                ))
                .unwrap();
            if observation % 50 == 0 {
                profiles
                    .record_divergence(&observation_token, 0.01, Some(i64::from(observation)))
                    .unwrap();
            }
        }

        let profile = profiles.get(&profile_key, &runtime("model-v1")).unwrap();
        assert_eq!(profile.window_observations, 50);
        assert_eq!(profile.n_paired_observations, 20);
        assert_eq!(profile.paired_observations, 20);
        assert_eq!(
            profile
                .retained_divergence_samples()
                .front()
                .unwrap()
                .plan_observation_sequence,
            50
        );
        assert_eq!(profile.last_paired_at_us, Some(1_000));
    }

    #[test]
    fn asynchronous_pairing_is_idempotent_and_does_not_duplicate_cost() {
        let profiles = PlanProfiles::new();
        let profile_key = key("shape-v1", 1);
        let observation_token = profiles
            .observe(update(profile_key.clone(), 100, None, 1))
            .unwrap();
        profiles
            .record_divergence(&observation_token, 0.2, Some(2))
            .unwrap();
        profiles
            .record_divergence(&observation_token, 0.2, Some(3))
            .unwrap();
        assert_eq!(
            profiles.record_divergence(&observation_token, 0.3, Some(3)),
            Err(PlanProfileUpdateError::ConflictingDivergence)
        );

        let profile = profiles.get(&profile_key, &runtime("model-v1")).unwrap();
        assert_eq!(profile.n_observations, 1);
        assert_eq!(profile.window_observations, 1);
        assert_eq!(profile.n_paired_observations, 1);
        assert_eq!(profile.paired_observations, 1);
        assert_eq!(profile.divergence_upper_p95, Some(0.2));
    }

    #[test]
    fn observation_token_round_trips_and_rejects_wrong_binding() {
        let profiles = PlanProfiles::new();
        let profile_key = key("shape-v1", 1);
        let token = profiles
            .observe(update(profile_key.clone(), 100, None, 1))
            .unwrap();
        let token: PlanObservationToken =
            serde_json::from_str(&serde_json::to_string(&token).unwrap()).unwrap();
        assert_eq!(token.key(), &profile_key);
        assert_eq!(token.runtime_version(), &runtime("model-v1"));
        assert_eq!(token.sequence(), 1);

        let mut wrong_runtime = token.clone();
        wrong_runtime.runtime_version = runtime("model-v2");
        assert_eq!(
            profiles.record_divergence(&wrong_runtime, 0.1, Some(2)),
            Err(PlanProfileUpdateError::RuntimeVersionMismatch)
        );

        let mut invalid_sequence = token;
        invalid_sequence.sequence = 0;
        assert_eq!(
            profiles.record_divergence(&invalid_sequence, 0.1, Some(2)),
            Err(PlanProfileUpdateError::InvalidObservationSequence)
        );
        assert_eq!(
            profiles
                .get(&profile_key, &runtime("model-v1"))
                .unwrap()
                .paired_observations,
            0
        );
    }

    #[test]
    fn exact_window_survives_restart_and_continues_eviction() {
        let mut connection = fresh_conn();
        let profile_key = key("shape-v1", 1);
        let initial = PlanProfiles::with_window(3);
        for output in [10, 20, 30, 40] {
            initial
                .observe(update(
                    profile_key.clone(),
                    output,
                    Some(output as f64 / 100.0),
                    i64::from(output),
                ))
                .unwrap();
        }
        assert_eq!(initial.flush_dirty(&mut connection).unwrap(), 1);

        let restarted = PlanProfiles::with_window(3);
        assert_eq!(restarted.warm_from_db(&connection).unwrap(), 1);
        let warm = restarted.get(&profile_key, &runtime("model-v1")).unwrap();
        assert_eq!(warm.n_observations, 4);
        assert_eq!(warm.window_observations, 3);
        assert_eq!(warm.paired_observations, 3);
        assert_eq!(warm.output_tokens.mean, 30.0);
        assert_eq!(warm.retained_samples().front().unwrap().sequence, 2);
        assert_eq!(
            warm.retained_divergence_samples().front().unwrap().sequence,
            2
        );

        restarted
            .observe(update(profile_key.clone(), 50, Some(0.5), 50))
            .unwrap();
        restarted.flush_dirty(&mut connection).unwrap();
        let second_restart = PlanProfiles::with_window(3);
        second_restart.warm_from_db(&connection).unwrap();
        let warm = second_restart
            .get(&profile_key, &runtime("model-v1"))
            .unwrap();
        assert_eq!(warm.n_observations, 5);
        assert_eq!(warm.n_paired_observations, 5);
        assert_eq!(warm.output_tokens.mean, 40.0);
        assert_eq!(warm.retained_samples().front().unwrap().sequence, 3);
        assert_eq!(
            warm.retained_divergence_samples().front().unwrap().sequence,
            3
        );
    }

    #[test]
    fn smaller_window_is_marked_dirty_and_persisted() {
        let mut connection = fresh_conn();
        let profile_key = key("shape-v1", 1);
        let initial = PlanProfiles::with_window(5);
        for output in 1..=5 {
            initial
                .observe(update(
                    profile_key.clone(),
                    output,
                    Some(0.01),
                    output as i64,
                ))
                .unwrap();
        }
        initial.flush_dirty(&mut connection).unwrap();

        let resized = PlanProfiles::with_window(3);
        resized.warm_from_db(&connection).unwrap();
        assert_eq!(resized.dirty_len(), 1);
        assert_eq!(
            resized
                .get(&profile_key, &runtime("model-v1"))
                .unwrap()
                .output_tokens
                .mean,
            4.0
        );
        resized.flush_dirty(&mut connection).unwrap();
        let retained: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM execution_plan_observation",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(retained, 3);
        let retained_divergence: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM execution_plan_divergence_observation",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(retained_divergence, 3);
    }

    #[test]
    fn changed_model_version_cold_starts_before_mixing_samples() {
        let profiles = PlanProfiles::with_window(50);
        let profile_key = key("shape-v1", 1);
        for now in 1..=20 {
            profiles
                .observe(update(profile_key.clone(), 100, Some(0.01), now))
                .unwrap();
        }
        assert_eq!(
            profiles
                .get(&profile_key, &runtime("model-v1"))
                .unwrap()
                .paired_observations,
            20
        );
        assert!(profiles.get(&profile_key, &runtime("model-v2")).is_none());

        let mut changed = update(profile_key.clone(), 200, Some(0.02), 21);
        changed.runtime_version = runtime("model-v2");
        profiles.observe(changed).unwrap();
        assert!(profiles.get(&profile_key, &runtime("model-v1")).is_none());
        let cold = profiles.get(&profile_key, &runtime("model-v2")).unwrap();
        assert_eq!(cold.n_observations, 21);
        assert_eq!(cold.window_observations, 1);
        assert_eq!(cold.paired_observations, 1);
        assert_eq!(cold.output_tokens.mean, 200.0);
    }

    #[test]
    fn changed_prompt_shape_uses_a_cold_key() {
        let profiles = PlanProfiles::new();
        profiles
            .observe(update(key("shape-v1", 1), 10, Some(0.01), 1))
            .unwrap();
        assert!(profiles
            .get(&key("shape-v2", 1), &runtime("model-v1"))
            .is_none());
    }

    #[test]
    fn output_shape_fallback_and_versions_survive_restart() {
        let mut connection = fresh_conn();
        let profiles = PlanProfiles::new();
        let profile_key = key("shape-v1", 1);
        let mut first = update(profile_key.clone(), 64, None, 10);
        first.output_is_structured = true;
        first.dispatch_fallback = true;
        profiles.observe(first).unwrap();
        let mut second = update(profile_key.clone(), 256, Some(0.2), 20);
        second.output_is_structured = false;
        second.output_is_short = false;
        profiles.observe(second).unwrap();
        profiles.flush_dirty(&mut connection).unwrap();

        let loaded = load_plan_profile(&connection, &profile_key)
            .unwrap()
            .unwrap();
        assert_eq!(loaded.window_observations, 2);
        assert_eq!(loaded.paired_observations, 1);
        assert_eq!(loaded.output_token_p95, 256.0);
        assert_eq!(loaded.output_is_structured, 0.5);
        assert_eq!(loaded.output_is_short, 0.5);
        assert_eq!(loaded.dispatch_fallback_rate, 0.5);
        assert_eq!(loaded.runtime_version, runtime("model-v1"));
    }

    #[test]
    fn invalid_observations_fail_without_mutating_state() {
        let profiles = PlanProfiles::new();
        let mut invalid = update(key("shape-v1", 1), 10, Some(f64::NAN), 1);
        assert_eq!(
            profiles.observe(invalid.clone()),
            Err(PlanProfileUpdateError::InvalidDivergence)
        );
        invalid.divergence = None;
        invalid.cost_usd = -1.0;
        assert_eq!(
            profiles.observe(invalid),
            Err(PlanProfileUpdateError::InvalidNumber("cost_usd"))
        );
        assert!(profiles.is_empty());
        assert_eq!(profiles.dirty_len(), 0);
    }

    #[test]
    fn concurrent_observations_do_not_lose_lifetime_or_window_counts() {
        let profiles = Arc::new(PlanProfiles::with_window(50));
        let profile_key = key("shape-v1", 1);
        let workers: Vec<_> = (0..8)
            .map(|worker| {
                let profiles = Arc::clone(&profiles);
                let profile_key = profile_key.clone();
                thread::spawn(move || {
                    for observation in 0..100 {
                        profiles
                            .observe(update(
                                profile_key.clone(),
                                (worker * 100 + observation) as u32,
                                Some(0.01),
                                i64::from(worker * 100 + observation),
                            ))
                            .unwrap();
                    }
                })
            })
            .collect();
        for worker in workers {
            worker.join().unwrap();
        }
        let profile = profiles.get(&profile_key, &runtime("model-v1")).unwrap();
        assert_eq!(profile.n_observations, 800);
        assert_eq!(profile.window_observations, 50);
        assert_eq!(profile.paired_observations, 50);
    }

    #[test]
    fn concurrent_post_snapshot_observation_remains_dirty() {
        let profiles = PlanProfiles::with_window(2);
        let profile_key = key("shape-v1", 1);
        profiles
            .observe(update(profile_key.clone(), 10, Some(0.01), 1))
            .unwrap();
        let mut connection = fresh_conn();
        profiles
            .flush_dirty_with_hook(&mut connection, || {
                profiles
                    .observe(update(profile_key.clone(), 30, Some(0.02), 2))
                    .unwrap();
            })
            .unwrap();
        assert_eq!(profiles.dirty_len(), 1);
        profiles.flush_dirty(&mut connection).unwrap();

        let restarted = PlanProfiles::with_window(2);
        restarted.warm_from_db(&connection).unwrap();
        let profile = restarted.get(&profile_key, &runtime("model-v1")).unwrap();
        assert_eq!(profile.n_observations, 2);
        assert_eq!(profile.output_tokens.mean, 20.0);
    }

    #[test]
    fn conformal_upper_quantile_is_conservative_at_small_n() {
        let mut values = vec![0.1, 0.2, 0.3, 0.4, 0.5];
        assert_eq!(conformal_upper_quantile(&mut values, 0.95), Some(0.5));
        let mut fifty: Vec<f64> = (1..=50).map(|value| value as f64).collect();
        assert_eq!(conformal_upper_quantile(&mut fifty, 0.95), Some(49.0));
    }
}
