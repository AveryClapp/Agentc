//! One-to-one comparisons of already completed ordered maps.
//!
//! SQLite owns immutable execution bindings and the paid acquisition journal.
//! Profiles/guards are derived feedback and can be repaired idempotently after
//! a process stops between journal commit and profile flush. A comparison does
//! not establish callback independence or authorize application replay.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::multi_call::{MultiCallPlanSpec, EXACT_MAP_COMPARISON_VERSION};
use crate::plan_guard::PlanGuard;
use crate::plan_profile::{PlanObservationToken, PlanProfiles};

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS multi_call_comparison_execution (
    token TEXT PRIMARY KEY NOT NULL,
    call_site_version TEXT NOT NULL,
    execution_plan_id TEXT NOT NULL,
    observation_sequence INTEGER NOT NULL CHECK(observation_sequence > 0),
    binding_json TEXT NOT NULL,
    used_as_candidate TEXT,
    used_as_reference TEXT,
    UNIQUE(call_site_version, execution_plan_id, observation_sequence)
) STRICT;
CREATE TABLE IF NOT EXISTS multi_call_comparison_pair (
    pair_id TEXT PRIMARY KEY NOT NULL,
    reference_token TEXT NOT NULL UNIQUE,
    candidate_token TEXT NOT NULL UNIQUE,
    call_site_version TEXT NOT NULL,
    execution_plan_id TEXT NOT NULL,
    candidate_runtime_json TEXT NOT NULL,
    candidate_observation_json TEXT NOT NULL,
    divergence REAL NOT NULL CHECK(divergence BETWEEN 0 AND 1),
    acquisition_cost_usd REAL NOT NULL CHECK(acquisition_cost_usd >= 0),
    acquisition_latency_ms REAL NOT NULL CHECK(acquisition_latency_ms >= 0),
    observed_at INTEGER NOT NULL CHECK(observed_at >= 0)
) STRICT;
CREATE TABLE IF NOT EXISTS multi_call_comparison_acquisition (
    call_site_version TEXT NOT NULL,
    execution_plan_id TEXT NOT NULL,
    completed_pairs INTEGER NOT NULL CHECK(completed_pairs > 0),
    total_cost_usd REAL NOT NULL CHECK(total_cost_usd >= 0),
    total_latency_ms REAL NOT NULL CHECK(total_latency_ms >= 0),
    PRIMARY KEY(call_site_version, execution_plan_id)
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS multi_call_comparison_spend (
    call_site_version TEXT NOT NULL,
    execution_plan_id TEXT NOT NULL,
    last_observation_sequence INTEGER NOT NULL CHECK(last_observation_sequence > 0),
    completed_executions INTEGER NOT NULL CHECK(completed_executions > 0),
    total_cost_usd REAL NOT NULL CHECK(total_cost_usd >= 0),
    total_latency_ms REAL NOT NULL CHECK(total_latency_ms >= 0),
    PRIMARY KEY(call_site_version, execution_plan_id)
) STRICT, WITHOUT ROWID;
"#;

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(SCHEMA)
        .context("create map comparison journal")
}

/// Charge every completely billed comparison-requested execution, including
/// unsupported result projections and runs that never form a valid pair.
/// This lifetime total survives window retirement and runtime drift. Failed
/// or unbilled dispatches remain unknown work in the owning provider ledger.
pub fn record_acquisition(
    conn: &mut Connection,
    profiles: &PlanProfiles,
    observation: &PlanObservationToken,
) -> Result<()> {
    ensure_schema(conn)?;
    let transaction = conn.transaction()?;
    record_acquisition_in_transaction(&transaction, profiles, observation)?;
    transaction.commit()?;
    Ok(())
}

pub(crate) fn record_acquisition_in_transaction(
    conn: &Connection,
    profiles: &PlanProfiles,
    observation: &PlanObservationToken,
) -> Result<()> {
    let profile = profiles
        .get(observation.key(), observation.runtime_version())
        .context("map acquisition runtime is no longer current")?;
    let sample = profile
        .retained_samples()
        .iter()
        .find(|sample| sample.sequence == observation.sequence())
        .context("map acquisition execution is no longer retained")?;
    let old: Option<(u64,f64,f64)> = conn.query_row(
        "SELECT last_observation_sequence,total_cost_usd,total_latency_ms FROM multi_call_comparison_spend WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![observation.key().call_site_version.as_str(),observation.key().execution_plan_id.as_str()],
        |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?)),
    ).optional()?;
    if let Some((sequence, cost, latency)) = old {
        if sequence == observation.sequence() {
            return Ok(());
        }
        if sequence > observation.sequence()
            || !(cost + sample.cost_usd).is_finite()
            || !(latency + sample.latency_ms).is_finite()
        {
            bail!("map acquisition sequence or lifetime metrics are invalid");
        }
    }
    conn.execute(
        "INSERT INTO multi_call_comparison_spend VALUES(?1,?2,?3,1,?4,?5) ON CONFLICT(call_site_version,execution_plan_id) DO UPDATE SET last_observation_sequence=excluded.last_observation_sequence,completed_executions=completed_executions+1,total_cost_usd=total_cost_usd+excluded.total_cost_usd,total_latency_ms=total_latency_ms+excluded.total_latency_ms",
        params![observation.key().call_site_version.as_str(),observation.key().execution_plan_id.as_str(),observation.sequence(),sample.cost_usd,sample.latency_ms],
    )?;
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ComparisonBinding {
    pub observation: PlanObservationToken,
    pub reference_plan_id: crate::execution_plan::ExecutionPlanId,
    pub max_workers: u32,
    pub input_fingerprint: String,
    pub output_fingerprint: String,
    pub cost_usd: f64,
    pub latency_ms: f64,
    pub observed_at_us: i64,
    pub comparison_version: String,
}

impl ComparisonBinding {
    fn token(&self) -> Result<String> {
        Ok(content_hash(&canonical_json(&serde_json::to_value(self)?)))
    }
}

fn validate_digest(value: &str) -> Result<()> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        bail!("comparison fingerprint must be a canonical SHA-256 digest");
    }
    Ok(())
}

fn current_execution(profiles: &PlanProfiles, binding: &ComparisonBinding) -> Result<()> {
    let profile = profiles
        .get(
            binding.observation.key(),
            binding.observation.runtime_version(),
        )
        .context("map comparison runtime is no longer current")?;
    let sample = profile
        .retained_samples()
        .iter()
        .find(|sample| sample.sequence == binding.observation.sequence())
        .context("map execution is no longer retained")?;
    if sample.cost_usd != binding.cost_usd
        || sample.latency_ms != binding.latency_ms
        || sample.observed_at_us != binding.observed_at_us
    {
        bail!("map execution metrics do not match their native observation");
    }
    Ok(())
}

/// Register only an exact native observation, after its metrics have been
/// flushed. Tokens are lookup keys for immutable bindings, not client-editable
/// serialized claims about inputs or results.
pub fn register_execution(
    conn: &mut Connection,
    profiles: &PlanProfiles,
    spec: &MultiCallPlanSpec,
    observation: &PlanObservationToken,
    input_fingerprint: String,
    output_fingerprint: String,
) -> Result<String> {
    ensure_schema(conn)?;
    validate_digest(&input_fingerprint)?;
    validate_digest(&output_fingerprint)?;
    if spec.comparison_version.as_deref() != Some(EXACT_MAP_COMPARISON_VERSION)
        || spec.profile_key().map_err(anyhow::Error::msg)? != *observation.key()
    {
        bail!("map comparison is not bound to its versioned execution plan");
    }
    // This first comparison contract covers scheduling of original requests.
    // Model/rewrite changes need a richer branch-comparison contract.
    if spec.branches.iter().any(|branch| {
        branch.plan.target_model_id != branch.plan.requested_model_id
            || !branch.plan.rewrites.is_empty()
            || branch.plan.cache_policy.stable_name != "provider-call"
    }) {
        bail!("map comparison reference must use original provider requests");
    }
    let mut reference = spec.clone();
    reference.max_workers = 1;
    let profile = profiles
        .get(observation.key(), observation.runtime_version())
        .context("map execution profile is unavailable")?;
    let sample = profile
        .retained_samples()
        .iter()
        .find(|sample| sample.sequence == observation.sequence())
        .context("map execution is no longer retained")?;
    let binding = ComparisonBinding {
        observation: observation.clone(),
        reference_plan_id: reference
            .profile_key()
            .map_err(anyhow::Error::msg)?
            .execution_plan_id,
        max_workers: spec.max_workers,
        input_fingerprint,
        output_fingerprint,
        cost_usd: sample.cost_usd,
        latency_ms: sample.latency_ms,
        observed_at_us: sample.observed_at_us,
        comparison_version: EXACT_MAP_COMPARISON_VERSION.into(),
    };
    let transaction = conn.transaction()?;
    let token = register_binding_in_transaction(&transaction, profiles, &binding)?;
    transaction.commit()?;
    Ok(token)
}

pub(crate) fn register_binding_in_transaction(
    conn: &Connection,
    profiles: &PlanProfiles,
    binding: &ComparisonBinding,
) -> Result<String> {
    validate_digest(&binding.input_fingerprint)?;
    validate_digest(&binding.output_fingerprint)?;
    current_execution(profiles, binding)?;
    let observation = &binding.observation;
    let token = binding.token()?;
    let binding_json = serde_json::to_string(&binding)?;
    conn.execute(
        "INSERT INTO multi_call_comparison_execution(token, call_site_version, execution_plan_id, observation_sequence, binding_json) VALUES(?1,?2,?3,?4,?5) ON CONFLICT DO NOTHING",
        params![token,observation.key().call_site_version.as_str(),observation.key().execution_plan_id.as_str(),observation.sequence(),binding_json],
    )?;
    let stored: String = conn.query_row(
        "SELECT binding_json FROM multi_call_comparison_execution WHERE token=?1",
        [&token],
        |row| row.get(0),
    )?;
    if stored != binding_json {
        bail!("map execution already has a different comparison binding");
    }
    // Receipt storage follows the exact retained execution window. Used flags
    // stay on retained receipts even after old pair rows leave their window.
    conn.execute(
        "DELETE FROM multi_call_comparison_execution WHERE call_site_version=?1 AND execution_plan_id=?2 AND observation_sequence NOT IN (SELECT sample_sequence FROM execution_plan_observation WHERE call_site_version=?1 AND execution_plan_id=?2)",
        params![observation.key().call_site_version.as_str(),observation.key().execution_plan_id.as_str()],
    )?;
    Ok(token)
}

fn load_binding(
    conn: &Connection,
    token: &str,
) -> Result<(ComparisonBinding, Option<String>, Option<String>)> {
    validate_digest(token)?;
    let (json, candidate, reference): (String, Option<String>, Option<String>) = conn.query_row(
        "SELECT binding_json, used_as_candidate, used_as_reference FROM multi_call_comparison_execution WHERE token=?1",
        [token], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?)),
    ).context("map comparison receipt is unavailable")?;
    let binding: ComparisonBinding = serde_json::from_str(&json)?;
    if binding.token()? != token || binding.comparison_version != EXACT_MAP_COMPARISON_VERSION {
        bail!("map comparison receipt binding is invalid");
    }
    validate_digest(&binding.input_fingerprint)?;
    validate_digest(&binding.output_fingerprint)?;
    Ok((binding, candidate, reference))
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ComparisonResult {
    pub status: String,
    pub pair_id: String,
    pub divergence: f64,
    pub acquisition_cost_usd: f64,
    pub acquisition_latency_ms: f64,
}

/// Commit the journal before publishing derived feedback. Retrying an exact
/// pair repairs incomplete profile/guard flushes without charging it twice.
pub fn compare_executions(
    conn: &mut Connection,
    profiles: &PlanProfiles,
    guard: &PlanGuard,
    reference_token: &str,
    candidate_token: &str,
    observed_at_us: i64,
) -> Result<ComparisonResult> {
    ensure_schema(conn)?;
    crate::provider_execution::ensure_schema(conn)?;
    replay_pairs(conn, profiles, guard)?;
    if observed_at_us < 0 || reference_token == candidate_token {
        bail!("map comparison needs two distinct completed executions");
    }
    let (reference, ref_candidate, ref_reference) = load_binding(conn, reference_token)?;
    let (candidate, cand_candidate, cand_reference) = load_binding(conn, candidate_token)?;
    current_execution(profiles, &reference)?;
    current_execution(profiles, &candidate)?;
    if reference.max_workers != 1
        || candidate.max_workers <= 1
        || reference.observation.key().call_site_version
            != candidate.observation.key().call_site_version
        || reference.observation.key().execution_plan_id != candidate.reference_plan_id
        || reference.observation.runtime_version() != candidate.observation.runtime_version()
        || reference.input_fingerprint != candidate.input_fingerprint
        || reference.observed_at_us > observed_at_us
        || candidate.observed_at_us > observed_at_us
    {
        bail!("map comparison reference, inputs, shape, schedule or runtime do not match");
    }
    let pair_id = content_hash(&canonical_json(&serde_json::json!({
        "version":EXACT_MAP_COMPARISON_VERSION,"reference":reference_token,"candidate":candidate_token,
    })));
    if ref_candidate.is_some()
        || cand_reference.is_some()
        || ref_reference.as_ref().is_some_and(|used| used != &pair_id)
        || cand_candidate.as_ref().is_some_and(|used| used != &pair_id)
    {
        bail!("map comparison receipt was already used by another pair");
    }
    let divergence = f64::from(reference.output_fingerprint != candidate.output_fingerprint);
    guard.admission(candidate.observation.key(), true, 0.0, observed_at_us)?;
    if profiles
        .get(
            candidate.observation.key(),
            candidate.observation.runtime_version(),
        )
        .is_some_and(|profile| {
            profile.retained_divergence_samples().iter().any(|sample| {
                sample.plan_observation_sequence == candidate.observation.sequence()
                    && sample.divergence != divergence
            })
        })
    {
        bail!("map execution already has a conflicting comparison");
    }
    let cost = reference.cost_usd + candidate.cost_usd;
    let latency = reference.latency_ms + candidate.latency_ms;
    if !cost.is_finite() || !latency.is_finite() {
        bail!("map acquisition metric sum overflow");
    }
    let transaction = conn.transaction()?;
    let original_pair = crate::provider_execution::compare_usd_in_transaction(
        &transaction,
        reference_token,
        candidate_token,
        observed_at_us,
    )?;
    let existing: Option<(f64,f64,f64,i64)> = transaction.query_row(
        "SELECT divergence, acquisition_cost_usd, acquisition_latency_ms, observed_at FROM multi_call_comparison_pair WHERE pair_id=?1",
        [&pair_id], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?)),
    ).optional()?;
    let effective_time;
    if let Some((old_divergence, old_cost, old_latency, time)) = existing {
        if old_divergence != divergence || old_cost != cost || old_latency != latency {
            bail!("map pair journal conflicts with its execution receipts");
        }
        effective_time = time;
    } else {
        if ref_reference.is_some() || cand_candidate.is_some() {
            bail!("map pair has retired from its comparison window");
        }
        effective_time = original_pair
            .as_ref()
            .map_or(observed_at_us, |pair| pair.observed_at_us);
        transaction.execute(
            "INSERT INTO multi_call_comparison_pair VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
            params![
                pair_id,
                reference_token,
                candidate_token,
                candidate.observation.key().call_site_version.as_str(),
                candidate.observation.key().execution_plan_id.as_str(),
                serde_json::to_string(candidate.observation.runtime_version())?,
                serde_json::to_string(&candidate.observation)?,
                divergence,
                cost,
                latency,
                effective_time
            ],
        )?;
        transaction.execute(
            "UPDATE multi_call_comparison_execution SET used_as_reference=?1 WHERE token=?2",
            params![pair_id, reference_token],
        )?;
        transaction.execute(
            "UPDATE multi_call_comparison_execution SET used_as_candidate=?1 WHERE token=?2",
            params![pair_id, candidate_token],
        )?;
        transaction.execute(
            "INSERT INTO multi_call_comparison_acquisition VALUES(?1,?2,1,?3,?4) ON CONFLICT(call_site_version,execution_plan_id) DO UPDATE SET completed_pairs=completed_pairs+1,total_cost_usd=total_cost_usd+excluded.total_cost_usd,total_latency_ms=total_latency_ms+excluded.total_latency_ms",
            params![candidate.observation.key().call_site_version.as_str(),candidate.observation.key().execution_plan_id.as_str(),cost,latency],
        )?;
    }
    // Retire journal rows before derived-state publication. An interrupted
    // flush cannot cause recovery to replay an already retired comparison and
    // count it again after the profile's paired window advances.
    transaction.execute(
        "DELETE FROM multi_call_comparison_pair WHERE call_site_version=?1 AND execution_plan_id=?2 AND rowid NOT IN (SELECT rowid FROM multi_call_comparison_pair WHERE call_site_version=?1 AND execution_plan_id=?2 AND candidate_runtime_json=?3 ORDER BY rowid DESC LIMIT ?4)",
        params![candidate.observation.key().call_site_version.as_str(),candidate.observation.key().execution_plan_id.as_str(),serde_json::to_string(candidate.observation.runtime_version())?,profiles.retained_window_size() as u64],
    )?;
    transaction.commit()?;
    profiles.record_divergence_once(&candidate.observation, divergence, Some(effective_time))?;
    guard.record_sample(
        &candidate.observation,
        divergence,
        0.0,
        Some(effective_time),
    )?;
    profiles.flush_dirty(conn)?;
    guard.flush_dirty(conn)?;
    Ok(ComparisonResult {
        status: if existing.is_some() {
            "already_recorded"
        } else {
            "recorded"
        }
        .into(),
        pair_id,
        divergence,
        acquisition_cost_usd: cost,
        acquisition_latency_ms: latency,
    })
}

/// Restore committed comparisons after an interrupted derived-state flush.
/// Only the current runtime/cohort can receive evidence; old runtime journal
/// rows remain accounting history, never a source of fresh quality samples.
pub fn replay_pairs(
    conn: &mut Connection,
    profiles: &PlanProfiles,
    guard: &PlanGuard,
) -> Result<usize> {
    ensure_schema(conn)?;
    let records: Vec<(String, String, f64, f64, f64, i64)> = {
        let mut statement = conn.prepare("SELECT candidate_observation_json, candidate_runtime_json, divergence, acquisition_cost_usd, acquisition_latency_ms, observed_at FROM multi_call_comparison_pair ORDER BY rowid")?;
        let values = statement.query_map([], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
            ))
        })?;
        values.collect::<std::result::Result<_, _>>()?
    };
    let mut repaired = 0;
    for (json, runtime_json, divergence, cost, latency, time) in records {
        let observation: PlanObservationToken = serde_json::from_str(&json)?;
        let runtime: crate::plan_profile::PlanRuntimeVersion = serde_json::from_str(&runtime_json)?;
        if runtime != *observation.runtime_version()
            || !(divergence == 0.0 || divergence == 1.0)
            || !cost.is_finite()
            || cost < 0.0
            || !latency.is_finite()
            || latency < 0.0
            || time < 0
        {
            bail!("map pair journal contains invalid observation metadata");
        }
        if profiles.get(observation.key(), &runtime).is_none() {
            continue;
        }
        let recorded = match profiles.record_divergence_once(&observation, divergence, Some(time)) {
            Ok(recorded) => recorded,
            Err(crate::plan_profile::PlanProfileUpdateError::InvalidObservationSequence) => {
                continue
            }
            Err(error) => return Err(error.into()),
        };
        guard.record_sample(&observation, divergence, 0.0, Some(time))?;
        repaired += usize::from(recorded);
    }
    profiles.flush_dirty(conn)?;
    guard.flush_dirty(conn)?;
    Ok(repaired)
}
