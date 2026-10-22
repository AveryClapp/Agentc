//! Retained original-result quality and persistent exposure, without money.
//!
//! Every event comes from one immutable original pair. Economic projections
//! neither issue events nor advance their timestamps. Window retirement keeps
//! lifetime evidence and the disable until explicit fresh cold readmission.

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::plan_profile::{PlanProfileKey, PlanRuntimeVersion};
use crate::provider_execution::Pair;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Disabled {
    pub disabled_at_us: i64,
    pub reenable_at_us: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Window {
    pub key: PlanProfileKey,
    pub runtime_version: PlanRuntimeVersion,
    pub lifetime_pairs: u64,
    pub pairs: Vec<Pair>,
    pub divergence_upper_p95: Option<f64>,
    pub last_paired_at_us: Option<i64>,
    pub last_execution_at_us: Option<i64>,
    pub divergence_exposure: f64,
    pub disabled: Option<Disabled>,
    pub evidence_boundary_us: i64,
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_original_quality_state (
           call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,
           runtime_json TEXT NOT NULL,source_sequence INTEGER NOT NULL CHECK(source_sequence>0),
           runtime_floor INTEGER NOT NULL CHECK(runtime_floor>0),lifetime_pairs INTEGER NOT NULL DEFAULT 0 CHECK(typeof(lifetime_pairs)='integer' AND lifetime_pairs>=0),
           disabled_at INTEGER CHECK(disabled_at>=0),reenable_at INTEGER CHECK(reenable_at>=disabled_at),evidence_boundary INTEGER NOT NULL DEFAULT 0 CHECK(evidence_boundary>=0),
           recovery_complete INTEGER NOT NULL DEFAULT 0,
           PRIMARY KEY(call_site_version,execution_plan_id));
         CREATE TABLE IF NOT EXISTS provider_original_quality_event (
           pair_id TEXT PRIMARY KEY,call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,
           runtime_json TEXT NOT NULL,candidate_source_sequence INTEGER NOT NULL CHECK(candidate_source_sequence>0),
           observed_at INTEGER NOT NULL CHECK(observed_at>=0),divergence REAL NOT NULL CHECK(divergence IN (0,1)),
           pair_sha256 TEXT NOT NULL,
           FOREIGN KEY(pair_id) REFERENCES provider_original_execution_pair(pair_id));
         CREATE INDEX IF NOT EXISTS provider_original_quality_window
           ON provider_original_quality_event(call_site_version,execution_plan_id,candidate_source_sequence DESC);
         CREATE INDEX IF NOT EXISTS provider_original_quality_exposure
           ON provider_original_quality_event(call_site_version,execution_plan_id,observed_at);",
    )?;
    Ok(())
}

fn canonical<T: Serialize>(value: &T) -> Result<String> {
    Ok(agentc_core::storage::canonical_json(&serde_json::to_value(
        value,
    )?))
}

struct Cohort {
    runtime: String,
    floor: u64,
    size: usize,
}

fn cohort(conn: &Connection, key: &PlanProfileKey) -> Result<Option<Cohort>> {
    let original: Option<(String,u64,u64,usize)> = conn.query_row(
        "SELECT runtime_json,source_sequence,observation_floor,window_size FROM provider_original_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],
        |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)),
    ).optional()?;
    let Some((runtime, sequence, floor, size)) = original else {
        return Ok(None);
    };
    let stored: Option<String>=conn.query_row(
        "SELECT runtime_json FROM provider_original_quality_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()], |r|r.get(0),
    ).optional()?;
    if stored.as_ref() != Some(&runtime) {
        // Bootstrap old stores once. All original checkpoints remain custody;
        // a returning runtime starts after its last different predecessor.
        let predecessor: Option<u64>=conn.query_row(
            "SELECT MAX(source_sequence) FROM provider_original_execution WHERE call_site_version=?1 AND execution_plan_id=?2 AND source_sequence<=?3 AND json_extract(execution_json,'$.runtime_version')<>?4",
            params![key.call_site_version.as_str(),key.execution_plan_id.as_str(),sequence,runtime], |r|r.get(0),
        )?;
        let runtime_floor = predecessor.map_or(1, |s| s.saturating_add(1));
        conn.execute(
            "INSERT INTO provider_original_quality_state(call_site_version,execution_plan_id,runtime_json,source_sequence,runtime_floor,lifetime_pairs,disabled_at,reenable_at,evidence_boundary) VALUES(?1,?2,?3,?4,?5,0,NULL,NULL,0)
             ON CONFLICT(call_site_version,execution_plan_id) DO UPDATE SET runtime_json=excluded.runtime_json,source_sequence=excluded.source_sequence,runtime_floor=excluded.runtime_floor,disabled_at=NULL,reenable_at=NULL,evidence_boundary=0",
            params![key.call_site_version.as_str(),key.execution_plan_id.as_str(),runtime,sequence,runtime_floor],
        )?;
    } else {
        conn.execute("UPDATE provider_original_quality_state SET source_sequence=MAX(source_sequence,?3) WHERE call_site_version=?1 AND execution_plan_id=?2",
            params![key.call_site_version.as_str(),key.execution_plan_id.as_str(),sequence])?;
    }
    Ok(Some(Cohort {
        runtime,
        floor,
        size,
    }))
}

/// Validate the original journal and both result bindings, even after execution
/// window eviction. Archival proofs can support retained paired evidence.
fn load_pair(conn: &Connection, id: &str) -> Result<(Pair, u64, i64, i64)> {
    let (hash,raw,sequence):(String,String,u64)=conn.query_row(
        "SELECT pair_sha256,pair_json,candidate_source_sequence FROM provider_original_execution_pair WHERE pair_id=?1",
        [id], |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)),
    )?;
    if agentc_core::storage::content_hash(&raw) != hash {
        bail!("original quality pair fingerprint is invalid");
    }
    let pair: Pair = serde_json::from_str(&raw)?;
    let reference = crate::provider_execution::load(conn, &pair.reference_execution_id)?;
    let candidate = crate::provider_execution::load(conn, &pair.candidate_execution_id)?;
    let r = reference
        .original_comparison
        .as_ref()
        .context("original reference quality is missing")?;
    let c = candidate
        .original_comparison
        .as_ref()
        .context("original candidate quality is missing")?;
    let identity = agentc_core::storage::content_hash(&canonical(
        &serde_json::json!({"version":"provider-original-pair-v1","reference":reference.execution_id,"candidate":candidate.execution_id}),
    )?);
    if identity != id
        || pair.pair_id != id
        || candidate.source_sequence != sequence
        || pair.key != candidate.key
        || pair.runtime_version != candidate.runtime_version
        || reference.runtime_version != candidate.runtime_version
        || reference.key.execution_plan_id != c.reference_plan_id
        || reference.key.call_site_version != candidate.key.call_site_version
        || r.max_workers != 1
        || c.max_workers <= 1
        || r.input_fingerprint != c.input_fingerprint
        || pair.divergence != f64::from(r.output_fingerprint != c.output_fingerprint)
        || pair.observed_at_us < reference.observed_at_us
        || pair.observed_at_us < candidate.observed_at_us
    {
        bail!("original quality differs from its original execution bindings");
    }
    Ok((
        pair,
        sequence,
        reference.observed_at_us,
        candidate.observed_at_us,
    ))
}

/// Called in the original pair's transaction. No invoice or interval is an
/// input, and an exact retry cannot consume exposure or lifetime evidence again.
pub(crate) fn record(conn: &Connection, pair: &Pair) -> Result<()> {
    ensure_schema(conn)?;
    let Some(current) = cohort(conn, &pair.key)? else {
        bail!("original quality cohort is unavailable");
    };
    let (original, sequence, _, _) = load_pair(conn, &pair.pair_id)?;
    if original != *pair {
        bail!("original quality pair conflicts");
    }
    let hash = agentc_core::storage::content_hash(&canonical(pair)?);
    let inserted=conn.execute(
        "INSERT INTO provider_original_quality_event VALUES(?1,?2,?3,?4,?5,?6,?7,?8) ON CONFLICT(pair_id) DO NOTHING",
        params![pair.pair_id,pair.key.call_site_version.as_str(),pair.key.execution_plan_id.as_str(),canonical(&pair.runtime_version)?,sequence,pair.observed_at_us,pair.divergence,hash],
    )?;
    let stored: String = conn.query_row(
        "SELECT pair_sha256 FROM provider_original_quality_event WHERE pair_id=?1",
        [&pair.pair_id],
        |r| r.get(0),
    )?;
    if stored != hash {
        bail!("original quality event conflicts");
    }
    if inserted == 0 {
        return Ok(());
    }
    conn.execute("UPDATE provider_original_quality_state SET lifetime_pairs=lifetime_pairs+1 WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![pair.key.call_site_version.as_str(),pair.key.execution_plan_id.as_str()])?;
    let (floor,reenable,boundary):(u64,Option<i64>,i64)=conn.query_row(
        "SELECT runtime_floor,reenable_at,evidence_boundary FROM provider_original_quality_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![pair.key.call_site_version.as_str(),pair.key.execution_plan_id.as_str()], |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)),
    )?;
    // EXACT_MAP_COMPARISON_VERSION admits only divergence zero. One original
    // changed result consumes the production exposure budget of one.
    if current.runtime == canonical(&pair.runtime_version)?
        && sequence >= floor
        && pair.divergence == 1.0
        && pair.observed_at_us >= boundary
        && reenable.is_none_or(|time| pair.observed_at_us >= time)
    {
        conn.execute("UPDATE provider_original_quality_state SET disabled_at=?3,reenable_at=?4 WHERE call_site_version=?1 AND execution_plan_id=?2",
            params![pair.key.call_site_version.as_str(),pair.key.execution_plan_id.as_str(),pair.observed_at_us,pair.observed_at_us.saturating_add(crate::plan_guard::PLAN_DISABLE_COOLDOWN_US)])?;
    }
    Ok(())
}

fn recover(conn: &Connection, key: &PlanProfileKey) -> Result<()> {
    let complete: bool=conn.query_row("SELECT recovery_complete FROM provider_original_quality_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()], |r|r.get(0))?;
    if complete {
        return Ok(());
    }
    let ids = {
        let mut query=conn.prepare("SELECT p.pair_id FROM provider_original_execution_pair p LEFT JOIN provider_original_quality_event e USING(pair_id) WHERE p.call_site_version=?1 AND p.execution_plan_id=?2 AND e.pair_id IS NULL ORDER BY json_extract(p.pair_json,'$.observed_at_us'),p.candidate_source_sequence")?;
        let rows = query
            .query_map(
                params![
                    key.call_site_version.as_str(),
                    key.execution_plan_id.as_str()
                ],
                |r| r.get::<_, String>(0),
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    for id in ids {
        let (pair, _, _, _) = load_pair(conn, &id)?;
        record(conn, &pair)?;
    }
    conn.execute("UPDATE provider_original_quality_state SET recovery_complete=1 WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()])?;
    Ok(())
}

fn window_in_transaction(
    conn: &Connection,
    key: &PlanProfileKey,
    runtime: &PlanRuntimeVersion,
    size: usize,
    now: i64,
    minimum_time: i64,
) -> Result<Option<Window>> {
    if now < 0 || size == 0 {
        bail!("original quality window clock or size is invalid");
    }
    let Some(current) = cohort(conn, key)? else {
        return Ok(None);
    };
    if current.runtime != canonical(runtime)? || current.size != size {
        return Ok(None);
    }
    recover(conn, key)?;
    let (lifetime,disabled,reenable,boundary,runtime_floor):(u64,Option<i64>,Option<i64>,i64,u64)=conn.query_row(
        "SELECT lifetime_pairs,disabled_at,reenable_at,evidence_boundary,runtime_floor FROM provider_original_quality_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()], |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)),
    )?;
    let boundary = boundary.max(reenable.unwrap_or(0)).max(minimum_time);
    let ids = {
        let mut query=conn.prepare("SELECT pair_id FROM provider_original_quality_event WHERE call_site_version=?1 AND execution_plan_id=?2 AND runtime_json=?3 AND candidate_source_sequence>=?4 AND observed_at>=?5 ORDER BY candidate_source_sequence DESC LIMIT ?6")?;
        let rows = query
            .query_map(
                params![
                    key.call_site_version.as_str(),
                    key.execution_plan_id.as_str(),
                    current.runtime,
                    current.floor,
                    boundary,
                    size
                ],
                |r| r.get::<_, String>(0),
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let mut pairs = Vec::new();
    let mut execution_times = Vec::new();
    for id in ids {
        let (pair, _, reference_at, candidate_at) = load_pair(conn, &id)?;
        if reference_at >= boundary && candidate_at >= boundary {
            execution_times.push(reference_at.min(candidate_at));
            pairs.push(pair);
        }
    }
    let exposure: f64=conn.query_row(
        "SELECT COALESCE(SUM(divergence),0) FROM provider_original_quality_event WHERE call_site_version=?1 AND execution_plan_id=?2 AND runtime_json=?3 AND candidate_source_sequence>=?4 AND observed_at>?5",
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str(),current.runtime,runtime_floor,now.saturating_sub(crate::plan_guard::PLAN_EXPOSURE_WINDOW_US)], |r|r.get(0),
    )?;
    let mut values: Vec<_> = pairs.iter().map(|p| p.divergence).collect();
    let upper = crate::plan_profile::conformal_upper_quantile(&mut values, 0.95);
    Ok(Some(Window {
        key: key.clone(),
        runtime_version: runtime.clone(),
        lifetime_pairs: lifetime,
        last_paired_at_us: pairs.iter().map(|p| p.observed_at_us).max(),
        last_execution_at_us: execution_times.into_iter().max(),
        pairs,
        divergence_upper_p95: upper,
        divergence_exposure: exposure,
        disabled: disabled
            .zip(reenable)
            .map(|(disabled_at_us, reenable_at_us)| Disabled {
                disabled_at_us,
                reenable_at_us,
            }),
        evidence_boundary_us: boundary,
    }))
}

pub fn window(
    conn: &Connection,
    key: &PlanProfileKey,
    runtime: &PlanRuntimeVersion,
    size: usize,
    now: i64,
) -> Result<Option<Window>> {
    window_after(conn, key, runtime, size, now, 0)
}

pub(crate) fn window_after(
    conn: &Connection,
    key: &PlanProfileKey,
    runtime: &PlanRuntimeVersion,
    size: usize,
    now: i64,
    minimum_time: i64,
) -> Result<Option<Window>> {
    crate::provider_execution::ensure_schema(conn)?;
    ensure_schema(conn)?;
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let result = window_in_transaction(&tx, key, runtime, size, now, minimum_time)?;
        tx.commit()?;
        return Ok(result);
    }
    window_in_transaction(conn, key, runtime, size, now, minimum_time)
}

/// The selector calls this only after the entire fresh candidate passes its
/// quality, pricing, benefit and resource limits. Cooldown expiry alone holds.
pub(crate) fn retain_readmission_boundary(
    conn: &Connection,
    window: &Window,
    boundary: i64,
) -> Result<()> {
    let changed = conn.execute(
        "UPDATE provider_original_quality_state SET evidence_boundary=MAX(evidence_boundary,?4)
         WHERE call_site_version=?1 AND execution_plan_id=?2 AND runtime_json=?3",
        params![
            window.key.call_site_version.as_str(),
            window.key.execution_plan_id.as_str(),
            canonical(&window.runtime_version)?,
            boundary
        ],
    )?;
    if changed != 1 {
        bail!("original readmission runtime is no longer current");
    }
    Ok(())
}

pub(crate) fn mark_readmitted(conn: &Connection, window: &Window, now: i64) -> Result<()> {
    let disabled = window
        .disabled
        .as_ref()
        .context("original quality is not disabled")?;
    if now < disabled.reenable_at_us || window.divergence_exposure >= 1.0 {
        bail!("original quality cooldown or exposure still blocks readmission");
    }
    conn.execute("UPDATE provider_original_quality_state SET disabled_at=NULL,reenable_at=NULL,evidence_boundary=?3 WHERE call_site_version=?1 AND execution_plan_id=?2 AND reenable_at=?3",
        params![window.key.call_site_version.as_str(),window.key.execution_plan_id.as_str(),disabled.reenable_at_us])?;
    Ok(())
}
