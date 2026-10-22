//! Whole-result quality, risk and cold readmission for complete provider graphs.
//! Immutable pairs own events; billing and constituent feedback cannot issue
//! evidence. Both baseline and candidate runtime/retirement boundaries apply.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::plan_profile::{PlanProfileKey, PlanRuntimeVersion};
use crate::provider_graph_comparison::{self as comparison, Pair};

/// Storage and custody differ; the exposure/readmission algorithm is shared.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Domain {
    Primary,
    Roles,
}

struct ExecutionEvidence {
    execution_id: String,
    started_at_us: Option<i64>,
}

impl Domain {
    fn sql(self, query: &str) -> String {
        if matches!(self, Self::Primary) {
            return query.into();
        }
        let mut sql = query.to_owned();
        for table in [
            "quality_state",
            "quality_event",
            "quality_window",
            "quality_exposure",
            "execution_state",
            "execution",
            "pair",
        ] {
            sql = sql.replace(
                &format!("provider_graph_{table}"),
                &format!("provider_graph_role_{table}"),
            );
        }
        for alias in ["cs", "rs"] {
            let execution = if alias == "cs" { "c" } else { "r" };
            sql=sql.replace(&format!("{alias}.execution_id={execution}.execution_id AND {alias}.lease_id=json_extract({execution}.execution_json,'$.preparation_id')"),
                &format!("{alias}.lease_id=json_extract({execution}.execution_json,'$.lease_id')"));
        }
        sql.replace(
            "$.comparison_context.generation_id",
            "$.role_comparison_context.generation_id",
        )
    }
    fn ensure_pairs(self, conn: &Connection) -> Result<()> {
        match self {
            Self::Primary => comparison::ensure_schema(conn),
            Self::Roles => crate::provider_graph_role_comparison::ensure_schema(conn),
        }
    }
    fn event_version(self) -> &'static str {
        match self {
            Self::Primary => "provider-graph-quality-event-v1",
            Self::Roles => "provider-role-graph-quality-event-v1",
        }
    }
}

/// One quality operation shares immutable custody without caching its mutable
/// controller, event policy, execution heads or admission generation.
struct Evidence<'a> {
    conn: &'a Connection,
    roles: Option<crate::provider_graph_archive::Archive<'a>>,
}
impl<'a> Evidence<'a> {
    fn new(domain: Domain, conn: &'a Connection) -> Result<Self> {
        Ok(Self {
            conn,
            roles: match domain {
                Domain::Primary => None,
                Domain::Roles => Some(crate::provider_graph_archive::Archive::new(conn)?),
            },
        })
    }
    fn pair(&mut self, id: &str) -> Result<Pair> {
        match &mut self.roles {
            None => comparison::load(self.conn, id),
            Some(archive) => archive.pair(id),
        }
    }
    fn execution(&mut self, id: &str) -> Result<ExecutionEvidence> {
        Ok(match &mut self.roles {
            None => {
                let e = crate::provider_graph::load(self.conn, id)?;
                ExecutionEvidence {
                    started_at_us: crate::provider_graph_preparation::original_start(
                        self.conn, &e,
                    )?,
                    execution_id: e.execution_id,
                }
            }
            Some(archive) => {
                let e = archive.execution(id)?;
                ExecutionEvidence {
                    started_at_us: Some(e.started_at_us),
                    execution_id: e.execution_id,
                }
            }
        })
    }
    fn prepared_matches(&mut self, pair: &Pair, generation: &Generation) -> Result<bool> {
        match &mut self.roles {
            None => crate::provider_graph_preparation::pair_generation_matches(
                self.conn, pair, generation,
            ),
            Some(archive) => {
                crate::provider_graph_role_comparison::prepared_matches(archive, pair, generation)
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Policy {
    pub divergence_threshold: f64,
    pub exposure_budget: f64,
    pub min_pairs: usize,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            divergence_threshold: 0.,
            exposure_budget: 1.,
            min_pairs: 20,
        }
    }
}
impl Policy {
    pub(crate) fn validate(self) -> Result<()> {
        if !self.divergence_threshold.is_finite()
            || !(0.0..=1.0).contains(&self.divergence_threshold)
            || !self.exposure_budget.is_finite()
            || self.exposure_budget < 0.
            || self.min_pairs < 20
        {
            bail!("graph quality policy is invalid");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Disabled {
    pub disabled_at_us: i64,
    pub reenable_at_us: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Window {
    pub key: PlanProfileKey,
    pub reference_key: PlanProfileKey,
    pub runtime_version: PlanRuntimeVersion,
    pub reference_runtime: PlanRuntimeVersion,
    pub policy: Policy,
    pub lifetime_pairs: u64,
    pub pairs: Vec<Pair>,
    pub prepared_pairs: Vec<Pair>,
    pub prepared_divergence_upper_p95: Option<f64>,
    pub divergence_upper_p95: Option<f64>,
    pub divergence_exposure: f64,
    pub disabled: Option<Disabled>,
    pub evidence_boundary_us: i64,
    pub candidate_floor: i64,
    pub reference_floor: i64,
    pub window_size: usize,
    pub last_execution_at_us: Option<i64>,
}

#[derive(Clone, Copy)]
pub struct Request<'a> {
    pub key: &'a PlanProfileKey,
    pub reference_key: &'a PlanProfileKey,
    pub runtime: &'a PlanRuntimeVersion,
    pub reference_runtime: &'a PlanRuntimeVersion,
    pub size: usize,
    pub now: i64,
    pub policy: Policy,
}
impl<'a> Request<'a> {
    pub fn from_pair(pair: &'a Pair, size: usize, now: i64, policy: Policy) -> Self {
        Self {
            key: &pair.key,
            reference_key: &pair.reference_key,
            runtime: &pair.runtime_version,
            reference_runtime: &pair.reference_runtime,
            size,
            now,
            policy,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct Cohort {
    reference_key: PlanProfileKey,
    runtime: PlanRuntimeVersion,
    reference_runtime: PlanRuntimeVersion,
    candidate_floor: i64,
    reference_floor: i64,
    window_size: usize,
    policy: Policy,
}

/// Shared evidence epoch, independent of ordinary publications and a completed
/// readmission. Cooldown's effective boundary survives clearing the disable.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Generation {
    pub key: PlanProfileKey,
    pub reference_key: PlanProfileKey,
    pub runtime: PlanRuntimeVersion,
    pub reference_runtime: PlanRuntimeVersion,
    pub candidate_floor: i64,
    pub reference_floor: i64,
    pub candidate_boundary: i64,
    pub evidence_boundary_us: i64,
    pub window_size: usize,
    pub policy: Policy,
}

impl Generation {
    pub fn identity(&self) -> Result<String> {
        Ok(content_hash(&canonical(self)?))
    }
}

pub(crate) fn current_generation_for(
    domain: Domain,
    conn: &Connection,
    key: &PlanProfileKey,
) -> Result<Generation> {
    let (raw, boundary, evidence, reenable): (String, i64, i64, Option<i64>) = conn.query_row(
        &domain.sql("SELECT cohort_json,candidate_boundary,evidence_boundary,reenable_at FROM provider_graph_quality_state WHERE call_site_version=?1 AND execution_plan_id=?2"),
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],
        |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)),
    )?;
    let c: Cohort = serde_json::from_str(&raw)?;
    c.policy.validate()?;
    if c.candidate_floor <= 0
        || c.reference_floor <= 0
        || boundary < c.candidate_floor
        || c.window_size == 0
        || evidence < 0
        || reenable.is_some_and(|t| t < 0)
    {
        bail!("graph evidence generation is invalid");
    }
    Ok(Generation {
        key: key.clone(),
        reference_key: c.reference_key,
        runtime: c.runtime,
        reference_runtime: c.reference_runtime,
        candidate_floor: c.candidate_floor,
        reference_floor: c.reference_floor,
        candidate_boundary: boundary,
        evidence_boundary_us: evidence.max(reenable.unwrap_or(0)),
        window_size: c.window_size,
        policy: c.policy,
    })
}

pub(crate) fn ensure_schema_for(domain: Domain, conn: &Connection) -> Result<()> {
    crate::provider_graph_preparation::ensure_schema(conn)?;
    domain.ensure_pairs(conn)?;
    conn.execute_batch(&domain.sql("CREATE TABLE IF NOT EXISTS provider_graph_quality_state (
        call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,cohort_json TEXT NOT NULL,
        candidate_boundary INTEGER NOT NULL CHECK(candidate_boundary>0),
        lifetime_pairs INTEGER NOT NULL DEFAULT 0 CHECK(lifetime_pairs>=0),
        disabled_at INTEGER CHECK(disabled_at>=0),reenable_at INTEGER CHECK(reenable_at>=disabled_at),
        evidence_boundary INTEGER NOT NULL DEFAULT 0 CHECK(evidence_boundary>=0),
        recovery_complete INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(call_site_version,execution_plan_id));
        CREATE TABLE IF NOT EXISTS provider_graph_quality_event (
        pair_id TEXT PRIMARY KEY,call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,
        candidate_sequence INTEGER NOT NULL CHECK(candidate_sequence>0),reference_sequence INTEGER NOT NULL CHECK(reference_sequence>0),
        observed_at INTEGER NOT NULL CHECK(observed_at>=0),divergence REAL NOT NULL CHECK(divergence IN (0,1)),pair_sha256 TEXT NOT NULL,
        policy_json TEXT NOT NULL,excess REAL NOT NULL CHECK(excess BETWEEN 0 AND 1),event_sha256 TEXT NOT NULL,
        FOREIGN KEY(pair_id) REFERENCES provider_graph_pair(pair_id));
        CREATE INDEX IF NOT EXISTS provider_graph_quality_window ON provider_graph_quality_event(call_site_version,execution_plan_id,candidate_sequence DESC);
        CREATE INDEX IF NOT EXISTS provider_graph_quality_exposure ON provider_graph_quality_event(call_site_version,execution_plan_id,observed_at);"))?;
    Ok(())
}
fn canonical<T: Serialize>(value: &T) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}

fn event_hash(domain: Domain, pair: &Pair, policy: Policy, excess: f64) -> Result<String> {
    Ok(content_hash(&canonical(
        &serde_json::json!({"version":domain.event_version(),"pair_id":pair.pair_id,"pair_sha256":content_hash(&canonical(pair)?),"policy":policy,"excess":excess}),
    )?))
}

fn issued_excess(domain: Domain, conn: &Connection, pair: &Pair) -> Result<Option<f64>> {
    let row:Option<(String,f64,String)>=conn.query_row(&domain.sql("SELECT policy_json,excess,event_sha256 FROM provider_graph_quality_event WHERE pair_id=?1"),[&pair.pair_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    row.map(|(raw, excess, hash)| {
        let policy: Policy = serde_json::from_str(&raw)?;
        policy.validate()?;
        if excess != (pair.divergence - policy.divergence_threshold).max(0.)
            || event_hash(domain, pair, policy, excess)? != hash
        {
            bail!("graph quality policy fingerprint is invalid");
        }
        Ok(excess)
    })
    .transpose()
}

fn execution_head(
    domain: Domain,
    conn: &Connection,
    key: &PlanProfileKey,
) -> Result<Option<(PlanRuntimeVersion, i64, i64, usize)>> {
    let row:Option<(String,i64,i64,usize)>=conn.query_row(&domain.sql("SELECT runtime_json,source_sequence,observation_floor,window_size FROM provider_graph_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2"),
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    row.map(|(raw, sequence, floor, size)| {
        if floor <= 0 || sequence < floor || size == 0 {
            bail!("graph quality execution boundary is invalid");
        }
        Ok((serde_json::from_str(&raw)?, sequence, floor, size))
    })
    .transpose()
}

fn sync(
    domain: Domain,
    conn: &Connection,
    request: Request<'_>,
    incoming: Option<i64>,
) -> Result<Option<Cohort>> {
    request.policy.validate()?;
    if request.size == 0 || request.now < 0 {
        bail!("graph quality clock or window is invalid");
    }
    let Some((runtime, sequence, candidate_floor, size)) =
        execution_head(domain, conn, request.key)?
    else {
        return Ok(None);
    };
    let Some((reference_runtime, _, reference_floor, reference_size)) =
        execution_head(domain, conn, request.reference_key)?
    else {
        return Ok(None);
    };
    if runtime != *request.runtime
        || reference_runtime != *request.reference_runtime
        || size != request.size
        || reference_size != request.size
    {
        return Ok(None);
    }
    let context = Cohort {
        reference_key: request.reference_key.clone(),
        runtime,
        reference_runtime,
        candidate_floor,
        reference_floor,
        window_size: size,
        policy: request.policy,
    };
    let saved:Option<(String,i64)>=conn.query_row(&domain.sql("SELECT cohort_json,candidate_boundary FROM provider_graph_quality_state WHERE call_site_version=?1 AND execution_plan_id=?2"),
        params![request.key.call_site_version.as_str(),request.key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    let boundary = match saved {
        None => {
            let existing:bool=conn.query_row(&domain.sql("SELECT EXISTS(SELECT 1 FROM provider_graph_quality_event WHERE call_site_version=?1 AND execution_plan_id=?2)"),params![request.key.call_site_version.as_str(),request.key.execution_plan_id.as_str()],|r|r.get(0))?;
            if existing {
                bail!("graph quality controller is missing for retained events");
            }
            candidate_floor
        }
        Some((raw, boundary)) if serde_json::from_str::<Cohort>(&raw)? == context => {
            boundary.max(candidate_floor)
        }
        Some((_, boundary)) => boundary.max(candidate_floor).max(
            incoming
                .filter(|s| *s == sequence)
                .unwrap_or(sequence.saturating_add(1)),
        ),
    };
    conn.execute(&domain.sql("INSERT INTO provider_graph_quality_state(call_site_version,execution_plan_id,cohort_json,candidate_boundary) VALUES(?1,?2,?3,?4)
        ON CONFLICT(call_site_version,execution_plan_id) DO UPDATE SET cohort_json=excluded.cohort_json,candidate_boundary=excluded.candidate_boundary"),
        params![request.key.call_site_version.as_str(),request.key.execution_plan_id.as_str(),canonical(&context)?,boundary])?;
    Ok(Some(context))
}

fn load_event(
    domain: Domain,
    conn: &Connection,
    evidence: &mut Evidence<'_>,
    id: &str,
) -> Result<(Pair, i64, i64, i64, i64)> {
    let pair = evidence.pair(id)?;
    let r = evidence.execution(&pair.reference_execution_id)?;
    let c = evidence.execution(&pair.candidate_execution_id)?;
    let rseq = conn.query_row(
        &domain.sql("SELECT rowid FROM provider_graph_execution WHERE execution_id=?1"),
        [&r.execution_id],
        |r| r.get(0),
    )?;
    let cseq = conn.query_row(
        &domain.sql("SELECT rowid FROM provider_graph_execution WHERE execution_id=?1"),
        [&c.execution_id],
        |r| r.get(0),
    )?;
    let event:Option<(String,String,i64,i64,i64,f64,String)>=conn.query_row(
        &domain.sql("SELECT call_site_version,execution_plan_id,candidate_sequence,reference_sequence,observed_at,divergence,pair_sha256 FROM provider_graph_quality_event WHERE pair_id=?1"),[id],
        |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;
    let expected = (
        pair.key.call_site_version.as_str().into(),
        pair.key.execution_plan_id.as_str().into(),
        cseq,
        rseq,
        pair.observed_at_us,
        pair.divergence,
        content_hash(&canonical(&pair)?),
    );
    if event.is_some_and(|e| e != expected) {
        bail!("graph quality event differs from its original pair");
    }
    issued_excess(domain, conn, &pair)?;
    let rstart = r.started_at_us.unwrap_or(-1);
    let cstart = c.started_at_us.unwrap_or(-1);
    Ok((pair, cseq, rseq, rstart, cstart))
}

fn exposure(
    domain: Domain,
    conn: &Connection,
    evidence: &mut Evidence<'_>,
    key: &PlanProfileKey,
    now: i64,
    policy: Policy,
) -> Result<f64> {
    let values = {
        let mut query=conn.prepare(&domain.sql("SELECT p.pair_id,e.pair_sha256 FROM provider_graph_pair p JOIN provider_graph_quality_event e USING(pair_id)
            WHERE json_extract(p.pair_json,'$.key.call_site_version')=?1 AND json_extract(p.pair_json,'$.key.execution_plan_id')=?2
            AND json_extract(p.pair_json,'$.observed_at_us')>?3 AND json_extract(p.pair_json,'$.observed_at_us')<=?4 ORDER BY json_extract(p.pair_json,'$.observed_at_us')"))?;
        let rows = query
            .query_map(
                params![
                    key.call_site_version.as_str(),
                    key.execution_plan_id.as_str(),
                    now.saturating_sub(crate::plan_guard::PLAN_EXPOSURE_WINDOW_US),
                    now
                ],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let mut total = 0.;
    for (id, hash) in values {
        let (pair, _, _, _, _) = load_event(domain, conn, evidence, &id)?;
        if pair.key != *key || content_hash(&canonical(&pair)?) != hash {
            bail!("graph exposure event conflicts with its original pair");
        }
        let excess =
            issued_excess(domain, conn, &pair)?.context("graph exposure event is missing")?;
        total += excess.max((pair.divergence - policy.divergence_threshold).max(0.));
    }
    if !total.is_finite() {
        bail!("graph exposure overflow");
    }
    Ok(total)
}

fn insert_event(
    domain: Domain,
    conn: &Connection,
    evidence: &mut Evidence<'_>,
    pair: &Pair,
    policy: Policy,
) -> Result<()> {
    let (original, cseq, rseq, _, _) = load_event(domain, conn, evidence, &pair.pair_id)?;
    if original != *pair {
        bail!("graph quality pair conflicts");
    }
    let hash = content_hash(&canonical(pair)?);
    let excess = (pair.divergence - policy.divergence_threshold).max(0.);
    let inserted=conn.execute(&domain.sql("INSERT INTO provider_graph_quality_event VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11) ON CONFLICT(pair_id) DO NOTHING"),
        params![pair.pair_id,pair.key.call_site_version.as_str(),pair.key.execution_plan_id.as_str(),cseq,rseq,pair.observed_at_us,pair.divergence,hash,canonical(&policy)?,excess,event_hash(domain,pair,policy,excess)?])?;
    let stored:(String,i64,i64,i64,f64)=conn.query_row(&domain.sql("SELECT pair_sha256,candidate_sequence,reference_sequence,observed_at,divergence FROM provider_graph_quality_event WHERE pair_id=?1"),[&pair.pair_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
    if stored != (hash, cseq, rseq, pair.observed_at_us, pair.divergence) {
        bail!("graph quality event conflicts");
    }
    if inserted == 0 {
        return Ok(());
    }
    conn.execute(&domain.sql("UPDATE provider_graph_quality_state SET lifetime_pairs=lifetime_pairs+1 WHERE call_site_version=?1 AND execution_plan_id=?2"),params![pair.key.call_site_version.as_str(),pair.key.execution_plan_id.as_str()])?;
    let risk = exposure(
        domain,
        conn,
        evidence,
        &pair.key,
        pair.observed_at_us,
        policy,
    )?;
    if risk > 0. && risk >= policy.exposure_budget {
        conn.execute(&domain.sql("UPDATE provider_graph_quality_state SET disabled_at=?3,reenable_at=?4 WHERE call_site_version=?1 AND execution_plan_id=?2 AND (reenable_at IS NULL OR reenable_at<=?3)"),
            params![pair.key.call_site_version.as_str(),pair.key.execution_plan_id.as_str(),pair.observed_at_us,pair.observed_at_us.saturating_add(crate::plan_guard::PLAN_DISABLE_COOLDOWN_US)])?;
    }
    Ok(())
}

fn recover(
    domain: Domain,
    conn: &Connection,
    evidence: &mut Evidence<'_>,
    key: &PlanProfileKey,
    policy: Policy,
) -> Result<()> {
    let complete:bool=conn.query_row(&domain.sql("SELECT recovery_complete FROM provider_graph_quality_state WHERE call_site_version=?1 AND execution_plan_id=?2"),params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],|r|r.get(0))?;
    if complete {
        return Ok(());
    }
    let ids = {
        let mut query=conn.prepare(&domain.sql("SELECT p.pair_id FROM provider_graph_pair p LEFT JOIN provider_graph_quality_event e USING(pair_id)
        WHERE json_extract(p.pair_json,'$.key.call_site_version')=?1 AND json_extract(p.pair_json,'$.key.execution_plan_id')=?2 AND e.pair_id IS NULL ORDER BY json_extract(p.pair_json,'$.observed_at_us')"))?;
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
        let pair = evidence.pair(&id)?;
        insert_event(domain, conn, evidence, &pair, policy)?;
    }
    conn.execute(&domain.sql("UPDATE provider_graph_quality_state SET recovery_complete=1 WHERE call_site_version=?1 AND execution_plan_id=?2"),params![key.call_site_version.as_str(),key.execution_plan_id.as_str()])?;
    Ok(())
}

/// Runs inside the immutable pair transaction; failures roll both back.
pub(crate) fn record_for(
    domain: Domain,
    conn: &Connection,
    pair: &Pair,
    size: usize,
    policy: Policy,
) -> Result<()> {
    ensure_schema_for(domain, conn)?;
    let mut evidence = Evidence::new(domain, conn)?;
    let (_, sequence, _, _, _) = load_event(domain, conn, &mut evidence, &pair.pair_id)?;
    let request = Request::from_pair(pair, size, pair.observed_at_us, policy);
    sync(domain, conn, request, Some(sequence))?.context("graph quality cohort has retired")?;
    recover(domain, conn, &mut evidence, &pair.key, policy)?;
    insert_event(domain, conn, &mut evidence, pair, policy)
}

fn window_inner(
    domain: Domain,
    conn: &Connection,
    evidence: &mut Evidence<'_>,
    request: Request<'_>,
) -> Result<Option<Window>> {
    let Some(context) = sync(domain, conn, request, None)? else {
        return Ok(None);
    };
    recover(domain, conn, evidence, request.key, request.policy)?;
    let (all_pairs,all_events):(u64,u64)=conn.query_row(&domain.sql("SELECT COUNT(*),COUNT(e.pair_id) FROM provider_graph_pair p LEFT JOIN provider_graph_quality_event e USING(pair_id)
        WHERE json_extract(p.pair_json,'$.key.call_site_version')=?1 AND json_extract(p.pair_json,'$.key.execution_plan_id')=?2"),params![request.key.call_site_version.as_str(),request.key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?)))?;
    let (lifetime,candidate_floor,disabled,reenable,boundary):(u64,i64,Option<i64>,Option<i64>,i64)=conn.query_row(&domain.sql("SELECT lifetime_pairs,candidate_boundary,disabled_at,reenable_at,evidence_boundary FROM provider_graph_quality_state WHERE call_site_version=?1 AND execution_plan_id=?2"),
        params![request.key.call_site_version.as_str(),request.key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
    if all_pairs != all_events || lifetime != all_events {
        bail!("graph quality event or lifetime coverage is incomplete");
    }
    let boundary = boundary.max(reenable.unwrap_or(0));
    let generation = current_generation_for(domain, conn, request.key)?;
    let generation_id = generation.identity()?;
    let ids = |prepared: bool| -> Result<Vec<String>> {
        let mut query=conn.prepare(&domain.sql("SELECT e.pair_id FROM provider_graph_quality_event e JOIN provider_graph_pair p USING(pair_id)
        JOIN provider_graph_execution c ON c.execution_id=json_extract(p.pair_json,'$.candidate_execution_id')
        JOIN provider_graph_execution r ON r.execution_id=json_extract(p.pair_json,'$.reference_execution_id')
        JOIN provider_graph_preparation cs ON cs.execution_id=c.execution_id AND cs.lease_id=json_extract(c.execution_json,'$.preparation_id')
        JOIN provider_graph_preparation rs ON rs.execution_id=r.execution_id AND rs.lease_id=json_extract(r.execution_json,'$.preparation_id')
        WHERE json_extract(p.pair_json,'$.key.call_site_version')=?1 AND json_extract(p.pair_json,'$.key.execution_plan_id')=?2 AND c.rowid>=?3 AND r.rowid>=?4
        AND json_extract(p.pair_json,'$.observed_at_us')>=?5 AND json_extract(p.pair_json,'$.observed_at_us')<=?6 AND json_extract(p.pair_json,'$.runtime_version')=?7
        AND json_extract(p.pair_json,'$.reference_runtime')=?8 AND json_extract(p.pair_json,'$.reference_key')=?9
        AND json_extract(cs.lease_json,'$.started_at_us')>=?5 AND json_extract(rs.lease_json,'$.started_at_us')>=?5
        AND json_extract(cs.lease_json,'$.window_size')=?10 AND json_extract(rs.lease_json,'$.window_size')=?10
        AND json_extract(cs.lease_json,'$.policy')=?11 AND json_extract(rs.lease_json,'$.policy')=?11
        AND (?12=0 OR (json_extract(cs.lease_json,'$.comparison_context.generation_id')=?13
            AND json_extract(rs.lease_json,'$.comparison_context.generation_id')=?13))
        ORDER BY c.rowid DESC LIMIT ?10"))?;
        let rows = query
            .query_map(
                params![
                    request.key.call_site_version.as_str(),
                    request.key.execution_plan_id.as_str(),
                    candidate_floor,
                    context.reference_floor,
                    boundary,
                    request.now,
                    canonical(request.runtime)?,
                    canonical(request.reference_runtime)?,
                    canonical(request.reference_key)?,
                    request.size,
                    canonical(&request.policy)?,
                    prepared,
                    generation_id
                ],
                |r| r.get::<_, String>(0),
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    };
    let mut pairs = vec![];
    let mut times = vec![];
    for id in ids(false)? {
        let (pair, cseq, rseq, rt, ct) = load_event(domain, conn, evidence, &id)?;
        if pair.key != *request.key
            || cseq < candidate_floor
            || rseq < context.reference_floor
            || pair.observed_at_us > request.now
        {
            bail!("graph retained quality projection conflicts");
        }
        if rt >= boundary && ct >= boundary {
            times.push(rt.min(ct));
            pairs.push(pair);
        }
    }
    let mut divergences = pairs.iter().map(|p| p.divergence).collect::<Vec<_>>();
    let upper = crate::plan_profile::conformal_upper_quantile(&mut divergences, 0.95);
    let mut prepared_pairs = vec![];
    for id in ids(true)? {
        let (pair, _, _, rt, ct) = load_event(domain, conn, evidence, &id)?;
        if rt < boundary || ct < boundary || !evidence.prepared_matches(&pair, &generation)? {
            bail!("prepared graph quality projection conflicts with its generation");
        }
        prepared_pairs.push(pair);
    }
    let mut prepared_divergences = prepared_pairs
        .iter()
        .map(|p| p.divergence)
        .collect::<Vec<_>>();
    let prepared_upper =
        crate::plan_profile::conformal_upper_quantile(&mut prepared_divergences, 0.95);
    let risk = exposure(
        domain,
        conn,
        evidence,
        request.key,
        request.now,
        request.policy,
    )?;
    if risk > 0. && risk >= request.policy.exposure_budget && disabled.is_none() {
        conn.execute(&domain.sql("UPDATE provider_graph_quality_state SET disabled_at=?3,reenable_at=?4 WHERE call_site_version=?1 AND execution_plan_id=?2"),params![request.key.call_site_version.as_str(),request.key.execution_plan_id.as_str(),request.now,request.now.saturating_add(crate::plan_guard::PLAN_DISABLE_COOLDOWN_US)])?;
        return window_inner(domain, conn, evidence, request);
    }
    Ok(Some(Window {
        key: request.key.clone(),
        reference_key: request.reference_key.clone(),
        runtime_version: request.runtime.clone(),
        reference_runtime: request.reference_runtime.clone(),
        policy: request.policy,
        lifetime_pairs: lifetime,
        pairs,
        prepared_pairs,
        prepared_divergence_upper_p95: prepared_upper,
        divergence_upper_p95: upper,
        divergence_exposure: risk,
        disabled: disabled
            .zip(reenable)
            .map(|(disabled_at_us, reenable_at_us)| Disabled {
                disabled_at_us,
                reenable_at_us,
            }),
        evidence_boundary_us: boundary,
        candidate_floor,
        reference_floor: context.reference_floor,
        window_size: request.size,
        last_execution_at_us: times.into_iter().max(),
    }))
}

pub(crate) fn window_for(
    domain: Domain,
    conn: &Connection,
    request: Request<'_>,
) -> Result<Option<Window>> {
    domain.ensure_pairs(conn)?;
    ensure_schema_for(domain, conn)?;
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let mut evidence = Evidence::new(domain, &tx)?;
        let result = window_inner(domain, &tx, &mut evidence, request)?;
        tx.commit()?;
        Ok(result)
    } else {
        window_inner(domain, conn, &mut Evidence::new(domain, conn)?, request)
    }
}

/// Quality-only commit for a trusted admission caller that has independently
/// passed billing, acquisition and resource limits. This supplies no dispatch
/// authorization. A stale snapshot, cooldown expiry or old executions cannot
/// clear the disable; all retained comparisons must be fresh and qualified.
pub(crate) fn finish_readmission_after_admission_for(
    domain: Domain,
    conn: &Connection,
    window: &Window,
    now: i64,
) -> Result<()> {
    if !conn.is_autocommit() {
        return finish_readmission_inner(domain, conn, window, now);
    }
    let tx = conn.unchecked_transaction()?;
    finish_readmission_inner(domain, &tx, window, now)?;
    tx.commit()?;
    Ok(())
}
fn finish_readmission_inner(
    domain: Domain,
    conn: &Connection,
    snapshot: &Window,
    now: i64,
) -> Result<()> {
    let request = Request {
        key: &snapshot.key,
        reference_key: &snapshot.reference_key,
        runtime: &snapshot.runtime_version,
        reference_runtime: &snapshot.reference_runtime,
        size: snapshot.window_size,
        now,
        policy: snapshot.policy,
    };
    let fresh = window_inner(domain, conn, &mut Evidence::new(domain, conn)?, request)?
        .context("graph readmission runtime or cohort has retired")?;
    let disabled = fresh
        .disabled
        .as_ref()
        .context("graph quality is not disabled")?;
    if fresh != *snapshot
        || now < disabled.reenable_at_us
        || fresh.prepared_pairs.len() < fresh.policy.min_pairs
        || fresh
            .prepared_divergence_upper_p95
            .is_none_or(|d| d > fresh.policy.divergence_threshold)
        || fresh.divergence_exposure > 0.
            && fresh.divergence_exposure >= fresh.policy.exposure_budget
    {
        bail!("graph readmission needs the current complete fresh quality window");
    }
    let changed=conn.execute(&domain.sql("UPDATE provider_graph_quality_state SET disabled_at=NULL,reenable_at=NULL,evidence_boundary=MAX(evidence_boundary,?3)
        WHERE call_site_version=?1 AND execution_plan_id=?2 AND reenable_at=?3"),params![fresh.key.call_site_version.as_str(),fresh.key.execution_plan_id.as_str(),disabled.reenable_at_us])?;
    if changed != 1 {
        bail!("graph readmission disable changed");
    }
    Ok(())
}

pub(crate) fn current_generation(conn: &Connection, key: &PlanProfileKey) -> Result<Generation> {
    current_generation_for(Domain::Primary, conn, key)
}
pub fn ensure_schema(conn: &Connection) -> Result<()> {
    ensure_schema_for(Domain::Primary, conn)
}
pub(crate) fn record(conn: &Connection, pair: &Pair, size: usize, policy: Policy) -> Result<()> {
    record_for(Domain::Primary, conn, pair, size, policy)
}
pub fn window(conn: &Connection, request: Request<'_>) -> Result<Option<Window>> {
    window_for(Domain::Primary, conn, request)
}

/// Preserve original risk and cooldown while a sampler rediscovers a retired
/// template. This validates archival events without requiring its old runtime.
pub(crate) fn refresh_cooldown(
    conn: &Connection,
    key: &PlanProfileKey,
    now: i64,
    policy: Policy,
) -> Result<Option<Disabled>> {
    policy.validate()?;
    if now < 0 {
        bail!("refresh clock is invalid");
    }
    let domain = Domain::Primary;
    let mut evidence = Evidence::new(domain, conn)?;
    recover(domain, conn, &mut evidence, key, policy)?;
    current_generation(conn, key)?;
    let ids = {
        let mut q = conn.prepare("SELECT pair_id FROM provider_graph_pair WHERE json_extract(pair_json,'$.key.call_site_version')=?1 AND json_extract(pair_json,'$.key.execution_plan_id')=?2")?;
        let rows = q
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
    for id in &ids {
        load_event(domain, conn, &mut evidence, id)?;
    }
    let (lifetime,events,disabled,reenable):(u64,u64,Option<i64>,Option<i64>)=conn.query_row("SELECT lifetime_pairs,(SELECT COUNT(*) FROM provider_graph_quality_event WHERE call_site_version=?1 AND execution_plan_id=?2),disabled_at,reenable_at FROM provider_graph_quality_state WHERE call_site_version=?1 AND execution_plan_id=?2",params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
    if lifetime != ids.len() as u64
        || events != lifetime
        || disabled.is_some() != reenable.is_some()
        || disabled.is_some_and(|d| d < 0 || reenable.unwrap() < d)
    {
        bail!("refresh quality event or cooldown custody is incomplete");
    }
    let risk = exposure(domain, conn, &mut evidence, key, now, policy)?;
    if disabled.is_none() && risk > 0. && risk >= policy.exposure_budget {
        let done = Disabled {
            disabled_at_us: now,
            reenable_at_us: now.saturating_add(crate::plan_guard::PLAN_DISABLE_COOLDOWN_US),
        };
        conn.execute("UPDATE provider_graph_quality_state SET disabled_at=?3,reenable_at=?4 WHERE call_site_version=?1 AND execution_plan_id=?2",params![key.call_site_version.as_str(),key.execution_plan_id.as_str(),done.disabled_at_us,done.reenable_at_us])?;
        return Ok(Some(done));
    }
    Ok(disabled
        .zip(reenable)
        .map(|(disabled_at_us, reenable_at_us)| Disabled {
            disabled_at_us,
            reenable_at_us,
        }))
}
pub fn finish_readmission_after_admission(
    conn: &Connection,
    window: &Window,
    now: i64,
) -> Result<()> {
    finish_readmission_after_admission_for(Domain::Primary, conn, window, now)
}
