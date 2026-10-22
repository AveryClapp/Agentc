//! One-to-one whole-result comparisons of immutable, completed provider graphs.
//! Costs are optional projections; neither pairing nor retries execute callbacks
//! or contribute constituent-call quality or scheduling authorization.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::plan_profile::{PlanProfileKey, PlanRuntimeVersion};
use crate::provider_graph::{self, Execution};

pub const VERSION: &str = "provider-graph-pair-v1";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Pair {
    pub version: String,
    pub pair_id: String,
    pub reference_execution_id: String,
    pub candidate_execution_id: String,
    pub reference_key: PlanProfileKey,
    pub key: PlanProfileKey,
    pub reference_runtime: PlanRuntimeVersion,
    pub runtime_version: PlanRuntimeVersion,
    pub divergence: f64,
    pub acquisition_cost_usd: Option<f64>,
    pub acquisition_latency_ms: f64,
    pub observed_at_us: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Outcome {
    pub status: &'static str,
    #[serde(flatten)]
    pub pair: Pair,
    pub quality: Option<crate::provider_graph_quality::Window>,
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    provider_graph::ensure_schema(conn)?;
    conn.execute_batch("CREATE TABLE IF NOT EXISTS provider_graph_pair (
        pair_id TEXT PRIMARY KEY,reference_execution_id TEXT NOT NULL UNIQUE,
        candidate_execution_id TEXT NOT NULL UNIQUE,pair_sha256 TEXT NOT NULL,pair_json TEXT NOT NULL,
        FOREIGN KEY(reference_execution_id) REFERENCES provider_graph_execution(execution_id),
        FOREIGN KEY(candidate_execution_id) REFERENCES provider_graph_execution(execution_id));")?;
    Ok(())
}

fn canonical<T: Serialize>(value: &T) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}

fn compatible(reference: &Execution, candidate: &Execution) -> Result<()> {
    let r = reference
        .comparison
        .as_ref()
        .context("graph reference results are unavailable")?;
    let c = candidate
        .comparison
        .as_ref()
        .context("graph candidate results are unavailable")?;
    let rs = &reference.spec;
    let cs = &candidate.spec;
    let contract = |execution: &Execution| {
        let mut nodes = execution
            .spec
            .calls
            .branches
            .iter()
            .map(|b| {
                (
                    b.position,
                    b.plan.provider_protocol.clone(),
                    b.plan.requested_model_id.clone(),
                )
            })
            .collect::<Vec<_>>();
        nodes.sort();
        nodes
    };
    if reference.execution_id == candidate.execution_id
        || rs.calls.max_workers != 1
        || rs.calls.application_version != cs.calls.application_version
        || rs.calls.callback_version != cs.calls.callback_version
        || rs.calls.item_count != cs.calls.item_count
        || rs.results != cs.results
        || contract(reference) != contract(candidate)
        || r.version != provider_graph::COMPARISON_VERSION
        || r.version != c.version
        || r.input_sha256 != c.input_sha256
        || reference.source_contracts != candidate.source_contracts
        || rs.calls.branches.iter().any(|b| {
            b.plan.requested_model_id != b.plan.target_model_id
                || !b.plan.rewrites.is_empty()
                || b.plan.cache_policy.stable_name != "provider-call"
        })
    {
        bail!(
            "whole graph inputs, callback, logical nodes or original serial reference do not match"
        );
    }
    Ok(())
}

/// Verify archival pairs without requiring execution-window membership.
/// Retained paired evidence has its own window at sparse sampling rates.
pub fn load(conn: &Connection, id: &str) -> Result<Pair> {
    let (hash, raw): (String, String) = conn.query_row(
        "SELECT pair_sha256,pair_json FROM provider_graph_pair WHERE pair_id=?1",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let pair: Pair = serde_json::from_str(&raw)?;
    let r = provider_graph::load(conn, &pair.reference_execution_id)?;
    let c = provider_graph::load(conn, &pair.candidate_execution_id)?;
    compatible(&r, &c)?;
    let identity = content_hash(&canonical(
        &serde_json::json!({"version":VERSION,"reference":r.execution_id,"candidate":c.execution_id}),
    )?);
    let cost = if r.cost_source.is_measured() && c.cost_source.is_measured() {
        Some(r.totals.cost_usd + c.totals.cost_usd)
    } else {
        None
    };
    if content_hash(&raw) != hash
        || pair.version != VERSION
        || pair.pair_id != id
        || identity != id
        || pair.reference_key != r.key
        || pair.key != c.key
        || pair.reference_runtime != r.runtime
        || pair.runtime_version != c.runtime
        || pair.divergence
            != f64::from(
                r.comparison.as_ref().unwrap().output_sha256
                    != c.comparison.as_ref().unwrap().output_sha256,
            )
        || pair.acquisition_cost_usd != cost
        || pair.acquisition_latency_ms != r.totals.wall_latency_ms + c.totals.wall_latency_ms
        || pair.observed_at_us < r.observed_at_us.max(c.observed_at_us)
    {
        bail!("graph quality pair differs from its immutable executions");
    }
    Ok(pair)
}

fn current(conn: &Connection, execution: &Execution, size: usize) -> Result<bool> {
    let state: Option<(String,i64,i64,usize)> = conn.query_row(
        "SELECT runtime_json,source_sequence,observation_floor,window_size FROM provider_graph_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![execution.key.call_site_version.as_str(),execution.key.execution_plan_id.as_str()],
        |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let Some((runtime, sequence, floor, window)) = state else {
        return Ok(false);
    };
    if runtime != canonical(&execution.runtime)? || window != size || floor <= 0 || sequence < floor
    {
        return Ok(false);
    }
    let retained: bool = conn.query_row("SELECT EXISTS(SELECT 1 FROM
        (SELECT execution_id FROM provider_graph_execution WHERE json_extract(execution_json,'$.key.call_site_version')=?1
        AND json_extract(execution_json,'$.key.execution_plan_id')=?2 AND rowid>=?3 AND rowid<=?4 ORDER BY rowid DESC LIMIT ?5)
        WHERE execution_id=?6)",params![execution.key.call_site_version.as_str(),execution.key.execution_plan_id.as_str(),floor,sequence,size,execution.execution_id],|r|r.get(0))?;
    Ok(retained)
}

/// Compare saved ordered results once. Both runtime vectors remain separate:
/// different selected models cannot pretend to share one original-map runtime.
pub fn compare(
    conn: &mut Connection,
    reference: &str,
    candidate: &str,
    at: i64,
    window: usize,
) -> Result<Outcome> {
    compare_with_policy(
        conn,
        reference,
        candidate,
        at,
        window,
        crate::provider_graph_quality::Policy::default(),
    )
}

pub fn compare_with_policy(
    conn: &mut Connection,
    reference: &str,
    candidate: &str,
    at: i64,
    window: usize,
    policy: crate::provider_graph_quality::Policy,
) -> Result<Outcome> {
    ensure_schema(conn)?;
    if window == 0 || at < 0 {
        bail!("graph comparison window or clock is invalid");
    }
    let tx = conn.transaction()?;
    let r = provider_graph::load(&tx, reference)?;
    let c = provider_graph::load(&tx, candidate)?;
    compatible(&r, &c)?;
    if at < r.observed_at_us || at < c.observed_at_us {
        bail!("graph comparison predates its execution");
    }
    let pair_id = content_hash(&canonical(
        &serde_json::json!({"version":VERSION,"reference":reference,"candidate":candidate}),
    )?);
    let cost = if r.cost_source.is_measured() && c.cost_source.is_measured() {
        Some(r.totals.cost_usd + c.totals.cost_usd)
    } else {
        None
    };
    let latency = r.totals.wall_latency_ms + c.totals.wall_latency_ms;
    if cost.is_some_and(|v| !v.is_finite()) || !latency.is_finite() {
        bail!("graph comparison acquisition metrics overflow");
    }
    let expected = Pair {
        version: VERSION.into(),
        pair_id: pair_id.clone(),
        reference_execution_id: reference.into(),
        candidate_execution_id: candidate.into(),
        reference_key: r.key.clone(),
        key: c.key.clone(),
        reference_runtime: r.runtime.clone(),
        runtime_version: c.runtime.clone(),
        divergence: f64::from(
            r.comparison.as_ref().unwrap().output_sha256
                != c.comparison.as_ref().unwrap().output_sha256,
        ),
        acquisition_cost_usd: cost,
        acquisition_latency_ms: latency,
        observed_at_us: at,
    };
    let used: Option<(String, String, String)> = tx
        .query_row(
            "SELECT pair_id,pair_sha256,pair_json FROM provider_graph_pair
        WHERE reference_execution_id IN (?1,?2) OR candidate_execution_id IN (?1,?2)",
            params![reference, candidate],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    if let Some((id, hash, raw)) = used {
        let saved: Pair = serde_json::from_str(&raw)?;
        let mut original = expected;
        original.observed_at_us = saved.observed_at_us;
        if id != pair_id
            || content_hash(&raw) != hash
            || saved != original
            || saved.observed_at_us > at
            || saved.observed_at_us < r.observed_at_us.max(c.observed_at_us)
        {
            bail!("graph execution already belongs to another pair or its pair is invalid");
        }
        let quality = crate::provider_graph_quality::window(
            &tx,
            crate::provider_graph_quality::Request::from_pair(&saved, window, at, policy),
        )?;
        tx.commit()?;
        return Ok(Outcome {
            status: "already_recorded",
            pair: saved,
            quality,
        });
    }
    if !current(&tx, &r, window)? || !current(&tx, &c, window)? {
        bail!("graph comparison runtime or execution window has retired");
    }
    let raw = canonical(&expected)?;
    tx.execute(
        "INSERT INTO provider_graph_pair VALUES(?1,?2,?3,?4,?5)",
        params![pair_id, reference, candidate, content_hash(&raw), raw],
    )?;
    crate::provider_graph_quality::record(&tx, &expected, window, policy)?;
    let quality = crate::provider_graph_quality::window(
        &tx,
        crate::provider_graph_quality::Request::from_pair(&expected, window, at, policy),
    )?;
    tx.commit()?;
    Ok(Outcome {
        status: "recorded",
        pair: expected,
        quality,
    })
}
