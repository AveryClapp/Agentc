//! One-to-one whole-result comparisons for complete physical role graphs.
//! Monetary projections never replace original execution or quality custody.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};

use crate::provider_graph_comparison::{Outcome, Pair};
use crate::provider_graph_quality::{Domain, Policy, Request};
use crate::provider_graph_roles::{self as roles, Execution};
use crate::provider_receipts::Role;

pub const VERSION: &str = "provider-role-graph-pair-v1";

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    crate::provider_graph_preparation::ensure_schema(conn)?;
    roles::ensure_schema(conn)?;
    conn.execute_batch("CREATE TABLE IF NOT EXISTS provider_graph_role_pair (
        pair_id TEXT PRIMARY KEY,reference_execution_id TEXT NOT NULL UNIQUE,
        candidate_execution_id TEXT NOT NULL UNIQUE,pair_sha256 TEXT NOT NULL,pair_json TEXT NOT NULL,
        FOREIGN KEY(reference_execution_id) REFERENCES provider_graph_role_execution(execution_id),
        FOREIGN KEY(candidate_execution_id) REFERENCES provider_graph_role_execution(execution_id));")?;
    Ok(())
}

fn canonical(value: &impl serde::Serialize) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}

pub(crate) fn compatible(
    r: &Execution,
    c: &Execution,
    rp: &roles::Prepared,
    cp: &roles::Prepared,
) -> Result<()> {
    let rs = &rp.program.graph;
    let cs = &cp.program.graph;
    let nodes = |p: &roles::Prepared| {
        let mut nodes = p
            .program
            .graph
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
    let edges = |s: &crate::provider_graph::Spec| {
        let mut edges = s.dependencies.clone();
        edges.sort();
        edges
    };
    if r.execution_id == c.execution_id
        || r.key == c.key
        || rs.calls.max_workers != 1
        || rs.calls.application_version != cs.calls.application_version
        || rs.calls.callback_version != cs.calls.callback_version
        || rs.calls.item_count != cs.calls.item_count
        || rs.results != cs.results
        || nodes(rp) != nodes(cp)
        || edges(rs) != edges(cs)
        || r.comparison.version != crate::provider_graph::COMPARISON_VERSION
        || r.comparison.version != c.comparison.version
        || r.comparison.input_sha256 != c.comparison.input_sha256
        || rp.source_contracts.is_empty()
        || rp.source_contracts != cp.source_contracts
        || rp.program.dispatches.iter().any(|d| {
            d.role != Role::Primary
                || d.plan.requested_model_id != d.plan.target_model_id
                || !d.plan.rewrites.is_empty()
                || d.plan.cache_policy.stable_name != "provider-call"
        })
    {
        bail!(
            "role graph inputs, controls, dependencies or untouched serial reference do not match"
        );
    }
    Ok(())
}

pub(crate) fn expected(r: &Execution, c: &Execution, at: i64) -> Result<Pair> {
    let cost = if r.acquisition.cost_complete && c.acquisition.cost_complete {
        Some(r.acquisition.known_cost_usd + c.acquisition.known_cost_usd)
    } else {
        None
    };
    let latency = r.acquisition.foreground_latency_ms + c.acquisition.foreground_latency_ms;
    if cost.is_some_and(|v| !v.is_finite())
        || !latency.is_finite()
        || at < r.observed_at_us.max(c.observed_at_us)
    {
        bail!("role graph comparison metrics or clock are invalid");
    }
    Ok(Pair {
        version: VERSION.into(),
        pair_id: content_hash(&canonical(
            &serde_json::json!({"version":VERSION,"reference":r.execution_id,"candidate":c.execution_id}),
        )?),
        reference_execution_id: r.execution_id.clone(),
        candidate_execution_id: c.execution_id.clone(),
        reference_key: r.key.clone(),
        key: c.key.clone(),
        reference_runtime: r.runtime.clone(),
        runtime_version: c.runtime.clone(),
        divergence: f64::from(r.comparison.output_sha256 != c.comparison.output_sha256),
        acquisition_cost_usd: cost,
        acquisition_latency_ms: latency,
        observed_at_us: at,
    })
}

pub fn load(conn: &Connection, id: &str) -> Result<Pair> {
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let pair = crate::provider_graph_archive::Archive::new(&tx)?.pair(id)?;
        tx.commit()?;
        return Ok(pair);
    }
    crate::provider_graph_archive::Archive::new(conn)?.pair(id)
}

fn current(conn: &Connection, e: &Execution, size: usize) -> Result<bool> {
    let state:Option<(String,i64,i64,usize)>=conn.query_row("SELECT runtime_json,source_sequence,observation_floor,window_size FROM provider_graph_role_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![e.key.call_site_version.as_str(),e.key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let Some((runtime, sequence, floor, window)) = state else {
        return Ok(false);
    };
    if runtime != canonical(&e.runtime)? || window != size || floor <= 0 || sequence < floor {
        return Ok(false);
    }
    Ok(conn.query_row("SELECT EXISTS(SELECT 1 FROM (SELECT execution_id FROM provider_graph_role_execution
        WHERE json_extract(execution_json,'$.key.call_site_version')=?1 AND json_extract(execution_json,'$.key.execution_plan_id')=?2
        AND rowid>=?3 AND rowid<=?4 ORDER BY rowid DESC LIMIT ?5) WHERE execution_id=?6)",
        params![e.key.call_site_version.as_str(),e.key.execution_plan_id.as_str(),floor,sequence,size,e.execution_id],|r|r.get(0))?)
}

pub fn compare_with_policy(
    conn: &mut Connection,
    reference: &str,
    candidate: &str,
    at: i64,
    size: usize,
    policy: Policy,
) -> Result<Outcome> {
    ensure_schema(conn)?;
    policy.validate()?;
    if size == 0 || at < 0 {
        bail!("role graph comparison window or clock is invalid");
    }
    let tx = conn.transaction()?;
    let mut archive = crate::provider_graph_archive::Archive::new(&tx)?;
    let r = archive.execution(reference)?;
    let c = archive.execution(candidate)?;
    let (_, rp) = archive.preparation(&r.lease_id)?;
    let (_, cp) = archive.preparation(&c.lease_id)?;
    compatible(&r, &c, &rp, &cp)?;
    let pair = expected(&r, &c, at)?;
    let used:Option<String>=tx.query_row("SELECT pair_id FROM provider_graph_role_pair WHERE reference_execution_id IN (?1,?2) OR candidate_execution_id IN (?1,?2)",params![reference,candidate],|r|r.get(0)).optional()?;
    if let Some(id) = used {
        let original = archive.pair(&id)?;
        let mut wanted = pair;
        wanted.observed_at_us = original.observed_at_us;
        if original != wanted || original.observed_at_us > at {
            bail!("role execution already belongs to another pair");
        }
        let quality = crate::provider_graph_quality::window_for(
            Domain::Roles,
            &tx,
            Request::from_pair(&original, size, at, policy),
        )?;
        tx.commit()?;
        return Ok(Outcome {
            status: "already_recorded",
            pair: original,
            quality,
        });
    }
    if !current(&tx, &r, size)? || !current(&tx, &c, size)? {
        bail!("role graph runtime or execution window has retired");
    }
    let raw = canonical(&pair)?;
    tx.execute(
        "INSERT INTO provider_graph_role_pair VALUES(?1,?2,?3,?4,?5)",
        params![pair.pair_id, reference, candidate, content_hash(&raw), raw],
    )?;
    crate::provider_graph_quality::record_for(Domain::Roles, &tx, &pair, size, policy)?;
    let quality = crate::provider_graph_quality::window_for(
        Domain::Roles,
        &tx,
        Request::from_pair(&pair, size, at, policy),
    )?;
    tx.commit()?;
    Ok(Outcome {
        status: "recorded",
        pair,
        quality,
    })
}

pub(crate) fn prepared_matches(
    archive: &mut crate::provider_graph_archive::Archive<'_>,
    pair: &Pair,
    generation: &crate::provider_graph_quality::Generation,
) -> Result<bool> {
    let mut ids = vec![];
    for (id, role, key, runtime) in [
        (
            &pair.reference_execution_id,
            crate::provider_graph_preparation::ComparisonRole::Reference,
            &pair.reference_key,
            &pair.reference_runtime,
        ),
        (
            &pair.candidate_execution_id,
            crate::provider_graph_preparation::ComparisonRole::Candidate,
            &pair.key,
            &pair.runtime_version,
        ),
    ] {
        let e = archive.execution(id)?;
        let (lease, _) = archive.preparation(&e.lease_id)?;
        let Some(context) = lease.role_comparison_context else {
            return Ok(false);
        };
        if context.role != role
            || context.generation != *generation
            || e.key != *key
            || e.runtime != *runtime
            || lease.started_at_us < generation.evidence_boundary_us
        {
            return Ok(false);
        }
        ids.push(context.generation_id);
    }
    Ok(ids[0] == ids[1] && ids[0] == generation.identity()?)
}

pub(crate) fn prepare_context(
    conn: &Connection,
    pair_id: &str,
    prepared: &roles::Prepared,
    policy: Policy,
    size: usize,
    now: i64,
) -> Result<crate::provider_graph_preparation::ComparisonContext> {
    use crate::provider_graph_preparation::{ComparisonContext, ComparisonRole};
    let mut archive = crate::provider_graph_archive::Archive::new(conn)?;
    let pair = archive.pair(pair_id)?;
    if pair.observed_at_us > now {
        bail!("role preparation predates its source pair");
    }
    let key = prepared.program.key()?;
    let runtime = prepared.runtime()?;
    let role = if key == pair.reference_key && runtime == pair.reference_runtime {
        ComparisonRole::Reference
    } else if key == pair.key && runtime == pair.runtime_version {
        ComparisonRole::Candidate
    } else {
        bail!("prepared role program is not a member of its source comparison")
    };
    let id = match role {
        ComparisonRole::Reference => &pair.reference_execution_id,
        ComparisonRole::Candidate => &pair.candidate_execution_id,
    };
    let execution = archive.execution(id)?;
    let (_, source) = archive.preparation(&execution.lease_id)?;
    if prepared.source_contracts != source.source_contracts {
        bail!("prepared role controls differ from their source comparison");
    }
    crate::provider_graph_quality::window_for(
        Domain::Roles,
        conn,
        Request::from_pair(&pair, size, now, policy),
    )?
    .context("role comparison runtime or quality window has retired")?;
    let generation =
        crate::provider_graph_quality::current_generation_for(Domain::Roles, conn, &pair.key)?;
    Ok(ComparisonContext {
        source_pair_id: pair_id.into(),
        source_pair_sha256: content_hash(&canonical(&pair)?),
        role,
        generation_id: generation.identity()?,
        generation,
        candidate_head_sha256: roles::head(conn, &pair.key)?,
        reference_head_sha256: roles::head(conn, &pair.reference_key)?,
    })
}

pub(crate) fn verify_context_custody(
    conn: &Connection,
    lease: &crate::provider_graph_preparation::Lease,
) -> Result<()> {
    let Some(context) = &lease.role_comparison_context else {
        return Ok(());
    };
    let prepared = roles::original(conn, lease)?
        .context("role comparison has no prepared physical program")?;
    let pair = load(conn, &context.source_pair_id)?;
    validate_context_custody(lease, &prepared, &pair)
}

pub(crate) fn validate_context_custody(
    lease: &crate::provider_graph_preparation::Lease,
    prepared: &roles::Prepared,
    pair: &Pair,
) -> Result<()> {
    let context = lease
        .role_comparison_context
        .as_ref()
        .context("role comparison context is missing")?;
    if lease.comparison_context.is_some() || lease.template.is_some() {
        bail!("role comparison has another graph contract");
    }
    let generation = &context.generation;
    let (key, runtime) = match context.role {
        crate::provider_graph_preparation::ComparisonRole::Reference => {
            (&pair.reference_key, &pair.reference_runtime)
        }
        crate::provider_graph_preparation::ComparisonRole::Candidate => {
            (&pair.key, &pair.runtime_version)
        }
    };
    if content_hash(&canonical(&pair)?) != context.source_pair_sha256
        || generation.identity()? != context.generation_id
        || generation.key != pair.key
        || generation.reference_key != pair.reference_key
        || generation.runtime != pair.runtime_version
        || generation.reference_runtime != pair.reference_runtime
        || generation.policy != lease.policy
        || generation.window_size != lease.window_size
        || prepared.program.key()? != *key
        || prepared.runtime()? != *runtime
    {
        bail!("role comparison source or generation custody is invalid");
    }
    Ok(())
}

pub(crate) fn verify_context_current(
    conn: &Connection,
    lease: &crate::provider_graph_preparation::Lease,
) -> Result<()> {
    let Some(context) = &lease.role_comparison_context else {
        return Ok(());
    };
    let current = crate::provider_graph_quality::current_generation_for(
        Domain::Roles,
        conn,
        &context.generation.key,
    )?;
    if current != context.generation
        || roles::head(conn, &current.key)? != context.candidate_head_sha256
        || roles::head(conn, &current.reference_key)? != context.reference_head_sha256
    {
        bail!("prepared role comparison generation has retired");
    }
    Ok(())
}

pub fn verify_dispatch_policy(
    conn: &Connection,
    batch: &str,
    policy: Policy,
    size: usize,
) -> Result<()> {
    let exists:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_graph_preparation')",[],|r|r.get(0))?;
    if !exists {
        return Ok(());
    }
    let raw: Option<String> = conn
        .query_row(
            "SELECT lease_json FROM provider_graph_preparation WHERE batch_id=?1",
            [batch],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(raw) = raw {
        let lease: crate::provider_graph_preparation::Lease = serde_json::from_str(&raw)?;
        if lease.role_program_id.is_some() && (lease.policy != policy || lease.window_size != size)
        {
            bail!("role dispatch configuration differs from its frozen preparation");
        }
    }
    Ok(())
}

pub fn window(
    conn: &Connection,
    request: Request<'_>,
) -> Result<Option<crate::provider_graph_quality::Window>> {
    crate::provider_graph_quality::window_for(Domain::Roles, conn, request)
}

/// Quality-only commit for a trusted admission caller. Financial/resource
/// limits and dispatch authorization remain separate mandatory gates.
pub fn finish_readmission_after_admission(
    conn: &Connection,
    window: &crate::provider_graph_quality::Window,
    now: i64,
) -> Result<()> {
    crate::provider_graph_quality::finish_readmission_after_admission_for(
        Domain::Roles,
        conn,
        window,
        now,
    )
}
