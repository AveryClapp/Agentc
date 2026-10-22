//! Complete provider graphs, with dependencies and verified realized work.
//!
//! Observation describes what actually ran. It supplies no callback replay or
//! scheduling authorization and no quality for a constituent node or rewrite.

use std::collections::{BTreeMap, BTreeSet};

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::execution_plan::{ExecutionPlanId, PlanIdentityError, PlanSemantics};
use crate::multi_call::{
    CallPosition, MultiCallOutcome, MultiCallPlanSpec, MultiCallRuntimeBranch, MultiCallTotals,
};
use crate::plan_profile::{
    CallSiteVersion, CallSiteVersionSpec, PlanProfileKey, PlanProfileUpdate, PlanProfiles,
    PlanRuntimeVersion,
};
use crate::provider_receipts::{Completion, Invocation, Role, Status};

pub const VERSION: &str = "provider-call-graph-v1";
pub const RUNTIME_VERSION: &str = "provider-call-graph-runtime-v1";
pub const COMPARISON_VERSION: &str = "exact-provider-graph-results-v1";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Comparison {
    pub version: String,
    pub input_sha256: String,
    pub output_sha256: String,
}

impl Comparison {
    pub(crate) fn validate(&self) -> Result<()> {
        if self.version != COMPARISON_VERSION {
            bail!("provider graph result contract is unsupported");
        }
        ExecutionPlanId::parse(&self.input_sha256)?;
        ExecutionPlanId::parse(&self.output_sha256)?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceContract {
    pub position: CallPosition,
    pub sha256: String,
}

fn validate_sources(spec: &Spec, sources: &[SourceContract]) -> Result<()> {
    let positions: BTreeSet<_> = sources.iter().map(|s| s.position).collect();
    if positions.len() != sources.len()
        || positions != spec.calls.branches.iter().map(|b| b.position).collect()
    {
        bail!("graph comparison source controls do not cover every logical node");
    }
    for source in sources {
        ExecutionPlanId::parse(&source.sha256)?;
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Dependency {
    pub producer: CallPosition,
    pub consumer: CallPosition,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Results {
    Nodes {
        positions: Vec<CallPosition>,
    },
    /// Aggregation is opaque and versioned by the callback. A callback need
    /// not return the last SDK response, so no node producer is invented.
    CallbackItems {
        items: Vec<u32>,
    },
}

/// The graph has its own identity namespace. Existing map identities and
/// original-request comparison contracts cannot stand in for this contract.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Spec {
    pub version: String,
    pub calls: MultiCallPlanSpec,
    pub dependencies: Vec<Dependency>,
    pub results: Results,
}

impl Spec {
    pub fn validate(&self) -> Result<()> {
        if self.version != VERSION
            || self.calls.execution_contract.is_some()
            || self.calls.comparison_version.is_some()
        {
            bail!("provider graph uses an unsupported or original-map contract");
        }
        self.calls.profile_key().map_err(anyhow::Error::msg)?;
        let nodes: BTreeSet<_> = self.calls.branches.iter().map(|b| b.position).collect();
        match &self.results {
            Results::Nodes { positions }
                if !positions.is_empty()
                    && positions.iter().all(|p| nodes.contains(p))
                    && positions.iter().copied().collect::<BTreeSet<_>>().len()
                        == positions.len() => {}
            Results::CallbackItems { items }
                if items.len() == self.calls.item_count as usize
                    && items.iter().copied().collect::<BTreeSet<_>>()
                        == (0..self.calls.item_count).collect() => {}
            _ => bail!("provider graph results are missing, duplicated or unknown"),
        }
        let mut remaining: BTreeMap<_, usize> = nodes.iter().map(|p| (*p, 0)).collect();
        let mut successors: BTreeMap<_, Vec<_>> = BTreeMap::new();
        let mut edges = BTreeSet::new();
        for edge in &self.dependencies {
            if edge.producer == edge.consumer
                || !nodes.contains(&edge.producer)
                || !nodes.contains(&edge.consumer)
                || !edges.insert(*edge)
            {
                bail!("provider graph dependency is self-referential, duplicated or unknown");
            }
            *remaining.get_mut(&edge.consumer).unwrap() += 1;
            successors
                .entry(edge.producer)
                .or_default()
                .push(edge.consumer);
        }
        // Program order inside one map item is a real dependency. An explicit
        // graph cannot erase it while still declaring an ordered callback.
        for node in &nodes {
            if node.call > 0
                && !edges.contains(&Dependency {
                    producer: CallPosition {
                        item: node.item,
                        call: node.call - 1,
                    },
                    consumer: *node,
                })
            {
                bail!("provider graph omits an item's program-order dependency");
            }
        }
        let mut ready: BTreeSet<_> = remaining
            .iter()
            .filter_map(|(p, n)| (*n == 0).then_some(*p))
            .collect();
        let mut visited = 0;
        while let Some(node) = ready.pop_first() {
            visited += 1;
            for successor in successors.get(&node).into_iter().flatten() {
                let count = remaining.get_mut(successor).unwrap();
                *count -= 1;
                if *count == 0 {
                    ready.insert(*successor);
                }
            }
        }
        if visited != nodes.len() {
            bail!("provider graph dependencies contain a cycle");
        }
        Ok(())
    }

    pub fn profile_key(&self) -> Result<PlanProfileKey> {
        self.validate()?;
        let mut dependencies = self.dependencies.clone();
        dependencies.sort();
        let mut branches = self.calls.branches.iter().collect::<Vec<_>>();
        branches.sort_by_key(|branch| branch.position);
        let shape = serde_json::json!({
            "version": VERSION, "callback_version": self.calls.callback_version,
            "item_count": self.calls.item_count, "dependencies": dependencies, "results": self.results,
            "nodes": branches.iter().map(|b| serde_json::json!({"position": b.position,"call_site_version": b.call_site_version})).collect::<Vec<_>>(),
        });
        let plan = serde_json::json!({
            "version": VERSION, "application_version": self.calls.application_version,
            "shape": shape, "max_workers": self.calls.max_workers,
            "nodes": branches.iter().map(|b| Ok(serde_json::json!({"position": b.position,"plan": b.plan.execution_plan_id()?}))).collect::<std::result::Result<Vec<_>, PlanIdentityError>>()?,
        });
        Ok(PlanProfileKey {
            call_site_version: CallSiteVersion::from_spec(&CallSiteVersionSpec {
                call_site_id: VERSION.into(),
                prompt_shape_version: content_hash(&canonical_json(&shape)),
                provider_protocol: RUNTIME_VERSION.into(),
                tool_schema_version: None,
                application_config_version: Some(self.calls.application_version.clone()),
            })?,
            execution_plan_id: ExecutionPlanId::parse(content_hash(&canonical_json(&plan)))?,
        })
    }

    pub fn runtime(&self, versions: &[MultiCallRuntimeBranch]) -> Result<PlanRuntimeVersion> {
        self.validate()?;
        let mut runtime = crate::multi_call::profile_runtime(&self.calls, versions)
            .map_err(anyhow::Error::msg)?;
        runtime.provider_protocol = RUNTIME_VERSION.into();
        Ok(runtime)
    }
}

impl PlanSemantics for Spec {
    fn execution_plan_id(&self) -> std::result::Result<ExecutionPlanId, PlanIdentityError> {
        self.profile_key()
            .map(|key| key.execution_plan_id)
            .map_err(|e| PlanIdentityError::InvalidMultiCallPlan(e.to_string()))
    }
    fn mutation_count(&self) -> usize {
        usize::from(self.calls.max_workers > 1)
            + self
                .calls
                .branches
                .iter()
                .map(|b| {
                    usize::from(b.plan.target_model_id != b.plan.requested_model_id)
                        + b.plan.rewrites.len()
                })
                .sum::<usize>()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Execution {
    pub version: String,
    pub execution_id: String,
    pub batch_id: String,
    pub key: PlanProfileKey,
    pub spec: Spec,
    pub runtime: PlanRuntimeVersion,
    pub input_sha256: String,
    pub journal_sha256: String,
    pub observed_at_us: i64,
    pub totals: MultiCallTotals,
    pub cost_source: crate::cost_evidence::CostSource,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub runtime_branches: Vec<MultiCallRuntimeBranch>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comparison: Option<Comparison>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub source_contracts: Vec<SourceContract>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preparation_id: Option<String>,
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    crate::provider_receipts::ensure_schema(conn)?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_graph_execution (
        execution_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE,
        execution_sha256 TEXT NOT NULL, execution_json TEXT NOT NULL,
        FOREIGN KEY(batch_id) REFERENCES provider_receipt_batch(batch_id));
        CREATE INDEX IF NOT EXISTS provider_graph_execution_window ON provider_graph_execution(
        json_extract(execution_json,'$.key.call_site_version'),
        json_extract(execution_json,'$.key.execution_plan_id'));
        CREATE TABLE IF NOT EXISTS provider_graph_execution_state (
        call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,
        runtime_json TEXT NOT NULL,source_sequence INTEGER NOT NULL,
        observation_floor INTEGER NOT NULL,window_size INTEGER NOT NULL,
        lifetime_executions INTEGER NOT NULL,
        PRIMARY KEY(call_site_version,execution_plan_id));",
    )?;
    Ok(())
}

/// Verify every primary against the issued complete plan and the physical SDK
/// journal. Additional calls stay accounted in that journal but require the
/// later explicit graph-role contract before they can form a complete profile.
fn verify(
    conn: &Connection,
    batch_id: &str,
    spec: &Spec,
    versions: &[MultiCallRuntimeBranch],
    outcomes: &[MultiCallOutcome],
    requests: &BTreeMap<CallPosition, String>,
) -> Result<Vec<crate::cost_evidence::PhysicalInvocation>> {
    spec.validate()?;
    let summary = crate::provider_receipts::summarize(conn, batch_id)?;
    if !summary.sealed
        || summary.pending_invocations != 0
        || summary.failed_invocations != 0
        || summary.cancelled_invocations != 0
        || summary.unverified_transport_invocations != 0
        || summary.unknown_http_attempt_invocations != 0
        || summary.known_http_attempts != spec.calls.branches.len() as u64
        || summary.sdk_provider_invocations != spec.calls.branches.len() as u64
    {
        bail!("provider graph journal is incomplete or contains unbound auxiliary work");
    }
    let (metadata, seal): (String, String) = conn.query_row(
        "SELECT metadata_json,expected_calls_json FROM provider_receipt_batch WHERE batch_id=?1",
        [batch_id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let metadata: crate::provider_receipts::Batch = serde_json::from_str(&metadata)?;
    let seal: BTreeSet<CallPosition> = serde_json::from_str::<Vec<CallPosition>>(&seal)?
        .into_iter()
        .collect();
    let nodes: BTreeSet<_> = spec.calls.branches.iter().map(|b| b.position).collect();
    if metadata.application_version.as_ref() != Some(&spec.calls.application_version)
        || metadata.callback_version != spec.calls.callback_version
        || metadata.item_count != spec.calls.item_count
        || seal != nodes
        || requests.keys().copied().collect::<BTreeSet<_>>() != nodes
    {
        bail!("provider graph belongs to another driver or has incomplete request coverage");
    }
    let mut query = conn.prepare("SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal")?;
    let rows = query
        .query_map([batch_id], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut branches = spec.calls.branches.iter().collect::<Vec<_>>();
    branches.sort_by_key(|b| b.position);
    let outcomes: BTreeMap<_, _> = outcomes.iter().map(|o| (o.position, o)).collect();
    let versions: BTreeMap<_, _> = versions.iter().map(|v| (v.position, &v.runtime)).collect();
    let mut physical = Vec::new();
    for (branch, (invocation, completion)) in branches.into_iter().zip(rows) {
        let invocation: Invocation = serde_json::from_str(&invocation)?;
        let completion: Completion = serde_json::from_str(&completion)?;
        let measured = outcomes
            .get(&branch.position)
            .context("provider graph outcome missing")?;
        let runtime = versions
            .get(&branch.position)
            .context("provider graph runtime missing")?;
        let key = PlanProfileKey {
            call_site_version: branch.call_site_version.clone(),
            execution_plan_id: branch.plan.execution_plan_id()?,
        };
        let (protocol, model) = if branch.plan.provider_protocol == "litellm.completion.v1" {
            (
                "openai.chat.completions.v1",
                branch
                    .plan
                    .target_model_id
                    .strip_prefix("openai/")
                    .context("provider graph LiteLLM backend is unsupported")?,
            )
        } else {
            (
                branch.plan.provider_protocol.as_str(),
                branch.plan.target_model_id.as_str(),
            )
        };
        let identity = crate::cost_evidence::PhysicalInvocation {
            batch_id: batch_id.into(),
            invocation_id: invocation.invocation_id.clone(),
        };
        if invocation.position != branch.position
            || invocation.ordinal != 0
            || invocation.role != Role::Primary
            || invocation.logical_plan_key.as_ref() != Some(&key)
            || invocation.dispatch_plan_key.as_ref() != Some(&key)
            || invocation.dispatch_runtime.as_ref().is_none_or(|planned| {
                planned.provider_protocol != runtime.provider_protocol
                    || planned.target_model_id != runtime.target_model_id
                    || planned.price_table_version != runtime.price_table_version
            })
            || invocation.provider_protocol != protocol
            || invocation.requested_model_id != model
            || invocation.logical_request_sha256.as_ref() != requests.get(&branch.position)
            || invocation.request_sha256.is_none()
            || invocation.transport_sha256.is_none()
            || completion.status != Status::Completed
            || completion.http_attempts != Some(1)
            || !crate::model_catalog::reported_revision_matches(
                runtime,
                completion.model_version.as_deref(),
            )
            || completion.input_tokens != Some(measured.outcome.input_tokens)
            || completion.output_tokens != Some(measured.outcome.output_tokens)
            || completion.latency_ms > measured.outcome.latency_ms + 1.0
            || completion
                .cost_usd
                .is_some_and(|cost| cost != measured.outcome.cost_usd)
            || completion.cost_source != measured.cost_source
            || measured.outcome.physical_invocations != vec![identity.clone()]
        {
            bail!("provider graph outcome differs from its issued plan or physical SDK receipt");
        }
        physical.push(identity);
    }
    Ok(physical)
}

/// Publish one verified realized graph atomically with its economic profile.
/// Exact retry returns the first identity/time without adding an observation.
/// No paired quality or deployment authorization is produced by this method.
pub struct PublishRequest<'a> {
    pub batch_id: &'a str,
    pub spec: &'a Spec,
    pub versions: &'a [MultiCallRuntimeBranch],
    pub outcomes: &'a [MultiCallOutcome],
    pub requests: &'a BTreeMap<CallPosition, String>,
    pub wall_latency_ms: f64,
    pub input_sha256: &'a str,
    pub observed_at_us: i64,
    pub comparison: Option<Comparison>,
    pub source_contracts: &'a [SourceContract],
    pub preparation_id: Option<&'a str>,
}

fn identity(execution: &Execution) -> Result<String> {
    let mut value = serde_json::json!({"version":VERSION,"batch_id":execution.batch_id,
        "input_sha256":execution.input_sha256,"journal_sha256":execution.journal_sha256,
        "key":execution.key,"runtime":execution.runtime});
    if let Some(comparison) = &execution.comparison {
        value["comparison"] = serde_json::to_value(comparison)?;
        value["source_contracts"] = serde_json::to_value(&execution.source_contracts)?;
    }
    if let Some(preparation) = &execution.preparation_id {
        value["preparation_id"] = serde_json::json!(preparation);
    }
    Ok(content_hash(&canonical_json(&value)))
}

fn insert_execution(
    conn: &Connection,
    execution: &Execution,
    raw: &str,
    size: usize,
) -> Result<()> {
    crate::provider_graph_preparation::bind_execution(conn, execution)?;
    conn.execute(
        "INSERT INTO provider_graph_execution VALUES(?1,?2,?3,?4)",
        params![
            execution.execution_id,
            execution.batch_id,
            content_hash(raw),
            raw
        ],
    )?;
    let sequence = conn.last_insert_rowid();
    conn.execute("INSERT INTO provider_graph_execution_state VALUES(?1,?2,?3,?4,?4,?5,1)
        ON CONFLICT(call_site_version,execution_plan_id) DO UPDATE SET
        observation_floor=CASE WHEN excluded.runtime_json<>runtime_json OR excluded.window_size<>window_size THEN excluded.observation_floor ELSE observation_floor END,
        runtime_json=excluded.runtime_json,source_sequence=excluded.source_sequence,
        window_size=excluded.window_size,lifetime_executions=lifetime_executions+1",
        params![execution.key.call_site_version.as_str(),execution.key.execution_plan_id.as_str(),
            canonical_json(&serde_json::to_value(&execution.runtime)?),sequence,size])?;
    Ok(())
}

/// Load original graph custody and verify its identity, full runtime vector and
/// physical journal. Later billing projections cannot replace these bindings.
pub fn load(conn: &Connection, id: &str) -> Result<Execution> {
    ExecutionPlanId::parse(id)?;
    let (hash,raw): (String,String) = conn.query_row(
        "SELECT execution_sha256,execution_json FROM provider_graph_execution WHERE execution_id=?1",
        [id],|r|Ok((r.get(0)?,r.get(1)?)))?;
    if content_hash(&raw) != hash {
        bail!("provider graph execution fingerprint is invalid");
    }
    let mut execution: Execution = serde_json::from_str(&raw)?;
    // Earlier observation-only rows already stored the complete vector in the
    // aggregate runtime JSON. Reading it does not alter their original custody
    // or manufacture a comparison contract.
    if execution.runtime_branches.is_empty() && execution.comparison.is_none() {
        execution.runtime_branches = serde_json::from_str(&execution.runtime.target_model_version)?;
    }
    if execution.version != VERSION
        || execution.execution_id != id
        || identity(&execution)? != id
        || execution.spec.profile_key()? != execution.key
        || execution.spec.runtime(&execution.runtime_branches)? != execution.runtime
        || execution.observed_at_us < 0
        || crate::provider_publication::journal_fingerprint(conn, &execution.batch_id)?
            != execution.journal_sha256
    {
        bail!("provider graph custody, identity or runtime is invalid");
    }
    if let Some(comparison) = &execution.comparison {
        comparison.validate()?;
        validate_sources(&execution.spec, &execution.source_contracts)?;
    }
    crate::provider_graph_preparation::original_start(conn, &execution)?;
    Ok(execution)
}

pub fn publish(
    conn: &mut Connection,
    profiles: &PlanProfiles,
    request: PublishRequest<'_>,
) -> Result<Execution> {
    let PublishRequest {
        batch_id,
        spec,
        versions,
        outcomes,
        requests,
        wall_latency_ms,
        input_sha256,
        observed_at_us,
        comparison,
        source_contracts,
        preparation_id,
    } = request;
    ensure_schema(conn)?;
    let key = spec.profile_key()?;
    let runtime = spec.runtime(versions)?;
    ExecutionPlanId::parse(input_sha256)?;
    if let Some(comparison) = &comparison {
        comparison.validate()?;
        validate_sources(spec, source_contracts)?;
    } else if !source_contracts.is_empty() {
        bail!("source control comparison bindings require a graph result contract");
    }
    let mut source_contracts = source_contracts.to_vec();
    source_contracts.sort_by_key(|s| s.position);
    if observed_at_us < 0 {
        bail!("provider graph clock is invalid");
    }
    let physical = verify(conn, batch_id, spec, versions, outcomes, requests)?;
    let totals =
        crate::multi_call::aggregate_original_evidence(&spec.calls, outcomes, wall_latency_ms)
            .map_err(anyhow::Error::msg)?;
    let cost_source = outcomes.iter().fold(
        crate::cost_evidence::CostSource::NoInference,
        |source, outcome| source.combine((&outcome.cost_source).into()),
    );
    let journal = crate::provider_publication::journal_fingerprint(conn, batch_id)?;
    let saved: Option<(String,String)> = conn.query_row("SELECT execution_sha256,execution_json FROM provider_graph_execution WHERE batch_id=?1", [batch_id], |r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if let Some((hash, raw)) = saved {
        if content_hash(&raw) != hash {
            bail!("provider graph execution fingerprint is invalid");
        }
        let saved: Execution = serde_json::from_str(&raw)?;
        if saved.version != VERSION
            || saved.batch_id != batch_id
            || saved.execution_id != identity(&saved)?
            || saved.spec.profile_key()? != saved.key
            || saved.key != key
            || saved.runtime != runtime
            || saved.input_sha256 != input_sha256
            || saved.journal_sha256 != journal
            || saved.totals != totals
            || saved.cost_source != cost_source
            || saved.comparison != comparison
            || saved.source_contracts != source_contracts
            || saved.preparation_id.as_deref() != preparation_id
            || !saved.runtime_branches.is_empty()
                && saved.spec.runtime(&saved.runtime_branches)? != runtime
            || saved.comparison.is_some() && saved.runtime_branches.is_empty()
        {
            bail!("provider graph retry differs from its original execution");
        }
        crate::provider_graph_preparation::original_start(conn, &saved)?;
        return Ok(saved);
    }
    let mut runtime_branches = versions.to_vec();
    runtime_branches.sort_by_key(|v| v.position);
    let mut execution = Execution {
        version: VERSION.into(),
        execution_id: String::new(),
        batch_id: batch_id.into(),
        key: key.clone(),
        spec: spec.clone(),
        runtime: runtime.clone(),
        input_sha256: input_sha256.into(),
        journal_sha256: journal,
        observed_at_us,
        totals: totals.clone(),
        cost_source,
        runtime_branches,
        comparison,
        source_contracts,
        preparation_id: preparation_id.map(str::to_owned),
    };
    execution.execution_id = identity(&execution)?;
    let raw = canonical_json(&serde_json::to_value(&execution)?);
    if !cost_source.is_measured() {
        let tx = conn.transaction()?;
        insert_execution(&tx, &execution, &raw, profiles.retained_window_size())?;
        tx.commit()?;
        return Ok(execution);
    }
    let boundary = profiles.publication_boundary(&key);
    profiles.publish_atomic(
        conn,
        &boundary,
        PlanProfileUpdate {
            key,
            runtime_version: runtime,
            input_tokens: totals.input_tokens,
            output_tokens: totals.output_tokens,
            cost_usd: totals.cost_usd,
            latency_ms: totals.wall_latency_ms,
            cost_evidence: crate::cost_evidence::CostEvidence {
                source: crate::cost_evidence::CostSource::ProviderReported,
                physical_invocations: physical,
            },
            output_is_structured: outcomes.iter().all(|o| o.outcome.output_is_structured),
            output_is_short: totals.output_tokens <= 128,
            divergence: None,
            dispatch_fallback: false,
            observed_at_us: Some(observed_at_us),
        },
        |tx, _, _| {
            insert_execution(tx, &execution, &raw, profiles.retained_window_size())?;
            Ok(())
        },
    )?;
    Ok(execution)
}
